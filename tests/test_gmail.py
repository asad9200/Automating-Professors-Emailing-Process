import base64
import random
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from unittest.mock import MagicMock

from fakes import FakeGmailClient

from profreach.agents.gmail_agent import GmailAgent
from profreach.config import Settings
from profreach.db.models import DraftStatus, SendAction
from profreach.db.outreach_store import OutreachStore
from profreach.gmail.client import Attachment, GmailClient, build_message


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def make_agent(engine, client, clock, **settings):
    values = {"send_max_per_day": 20, "send_min_gap_seconds": 120, "send_max_gap_seconds": 300}
    values.update(settings)
    return GmailAgent(
        OutreachStore(engine),
        client,
        settings=Settings(**values),
        attachment=Attachment(filename="cv.pdf", content=b"%PDF-1.4"),
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(1),
    )


def add_draft(store, professor, status=DraftStatus.DRAFT, **fields):
    return store.create_draft(
        professor.id, subject="PhD inquiry", body="Hello", status=status, **fields
    )


def test_build_message_includes_attachment_and_reply_headers():
    message = build_message(
        "prof@example.edu",
        "Re: PhD inquiry",
        "Body text",
        attachment=Attachment(filename="cv.pdf", content=b"%PDF"),
        in_reply_to="<abc@mail>",
    )

    parsed = message_from_bytes(message.as_bytes())
    assert parsed["To"] == "prof@example.edu"
    assert parsed["In-Reply-To"] == "<abc@mail>"
    attachments = [part for part in parsed.walk() if part.get_filename()]
    assert attachments[0].get_filename() == "cv.pdf"
    assert attachments[0].get_content_type() == "application/pdf"


def test_gmail_client_encodes_raw_message_and_thread():
    service = MagicMock()
    service.users().drafts().create().execute.return_value = {"id": "d1", "message": {}}

    GmailClient(service).create_draft(build_message("a@example.edu", "S", "B"), "t1")

    body = service.users().drafts().create.call_args.kwargs["body"]
    assert body["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    assert b"a@example.edu" in raw


def test_only_approved_drafts_are_sent_and_ids_recorded(engine, make_professor):
    store = OutreachStore(engine)
    approved = add_draft(
        store, make_professor("Avery Chen", "avery@example.edu"), DraftStatus.APPROVED
    )
    add_draft(store, make_professor("Blake Diaz", "blake@example.edu"), DraftStatus.DRAFT)
    add_draft(store, make_professor("Casey Park", "casey@example.edu"), DraftStatus.REJECTED)
    client, clock = FakeGmailClient(), Clock()

    report = make_agent(engine, client, clock).send_approved()

    assert report.sent == [approved.id]
    assert client.sent == ["avery@example.edu"]
    sent = store.get_draft(approved.id)
    assert sent.status is DraftStatus.SENT
    assert sent.gmail_message_id and sent.gmail_thread_id
    assert sent.rfc_message_id.startswith("<sent")
    logs = [log.action for log in store.list_logs()]
    assert SendAction.SENT in logs and SendAction.GMAIL_DRAFT_CREATED in logs


def test_gap_and_daily_cap_are_enforced(engine, make_professor):
    store = OutreachStore(engine)
    for index in range(3):
        add_draft(
            store,
            make_professor(f"Prof Number{index}", f"p{index}@example.edu"),
            DraftStatus.APPROVED,
        )
    client, clock = FakeGmailClient(), Clock()
    agent = make_agent(engine, client, clock, send_max_per_day=2)

    first = agent.send_approved()
    assert len(first.sent) == 1
    assert "gap" in first.stopped_reason
    gap = agent.throttle_status().next_allowed_at - clock.now
    assert timedelta(seconds=120) <= gap <= timedelta(seconds=300)

    waited = agent.send_approved(wait=True)
    assert len(waited.sent) == 1
    assert "Daily limit" in waited.stopped_reason
    assert len(waited.deferred) == 1

    clock.now += timedelta(hours=25)
    assert agent.throttle_status().can_send


def test_send_failure_is_logged_and_does_not_stop_other_sends(engine, make_professor):
    store = OutreachStore(engine)
    bad = add_draft(store, make_professor("Avery Chen", "bad@example.edu"), DraftStatus.APPROVED)
    good = add_draft(store, make_professor("Blake Diaz", "good@example.edu"), DraftStatus.APPROVED)
    client, clock = FakeGmailClient(), Clock()
    client.fail_send_for = {"bad@example.edu"}

    report = make_agent(
        engine, client, clock, send_min_gap_seconds=0, send_max_gap_seconds=0
    ).send_approved()

    assert report.failed == {bad.id: "send failed"}
    assert report.sent == [good.id]
    assert store.get_draft(bad.id).status is DraftStatus.APPROVED
    assert store.get_draft(bad.id).last_error == "send failed"


def test_sync_skips_missing_emails_and_unchanged_drafts(engine, make_professor):
    store = OutreachStore(engine)
    professor = make_professor("Avery Chen", "avery@example.edu")
    no_email = make_professor("Blake Diaz", None)
    draft = add_draft(store, professor, attach_cv=True)
    missing = add_draft(store, no_email)
    client, clock = FakeGmailClient(), Clock()
    agent = make_agent(engine, client, clock)

    report = agent.sync_drafts(store.list_drafts())
    assert report.created == [draft.id]
    assert "No published email" in report.skipped[missing.id]
    message = next(iter(client.drafts.values()))["message"]
    assert any(part.get_filename() == "cv.pdf" for part in message.walk())

    again = agent.sync_drafts(store.list_drafts())
    assert again.skipped[draft.id] == "already up to date"
    assert client.updates == 0

    store.update_draft(draft.id, body="Edited body")
    edited = agent.sync_drafts(store.list_drafts())
    assert edited.updated == [draft.id]


def test_sync_requires_a_cv_when_attachment_requested(engine, make_professor):
    store = OutreachStore(engine)
    draft = add_draft(store, make_professor(), attach_cv=True)
    agent = GmailAgent(store, FakeGmailClient(), settings=Settings(cv_attachment_path=None))

    report = agent.sync_drafts(store.list_drafts())

    assert "CV attachment requested" in report.skipped[draft.id]
