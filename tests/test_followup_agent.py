from datetime import UTC, datetime, timedelta

from fakes import FakeGmailClient

from profreach.agents.followup_agent import FollowUpAgent
from profreach.config import Settings
from profreach.db.models import DraftStatus
from profreach.db.outreach_store import OutreachStore
from profreach.gmail.client import ThreadMessage

SENT_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


class FakeWriter:
    def __init__(self):
        self.calls = []

    def write_followup(self, profile, professor, subject, body, days):
        self.calls.append(days)
        return f"Re: {subject}", "Dear Professor Chen, following up."


def sent_draft(store, professor, thread_id="t1"):
    return store.create_draft(
        professor.id,
        subject="PhD inquiry",
        body="Original",
        status=DraftStatus.SENT,
        sent_at=SENT_AT,
        gmail_thread_id=thread_id,
        rfc_message_id="<orig@mail>",
    )


def make_agent(engine, client, writer, now):
    return FollowUpAgent(
        OutreachStore(engine),
        client,
        writer,
        settings=Settings(followup_after_days=10),
        clock=lambda: now,
    )


def test_reply_marks_draft_replied(engine, make_professor, profile):
    store = OutreachStore(engine)
    draft = sent_draft(store, make_professor())
    client = FakeGmailClient()
    reply_ms = int((SENT_AT + timedelta(days=2)).timestamp() * 1000)
    client.threads["t1"] = [
        ThreadMessage(id="m1", sender="me@example.org", internal_date_ms=0),
        ThreadMessage(id="m2", sender="avery@example.edu", internal_date_ms=reply_ms),
    ]

    report = make_agent(engine, client, FakeWriter(), SENT_AT + timedelta(days=3)).run(profile)

    assert report.replied == [draft.id]
    assert store.get_draft(draft.id).status is DraftStatus.REPLIED


def test_drafts_one_followup_after_threshold(engine, make_professor, profile):
    store = OutreachStore(engine)
    draft = sent_draft(store, make_professor())
    writer = FakeWriter()

    early = make_agent(engine, FakeGmailClient(), writer, SENT_AT + timedelta(days=5)).run(profile)
    assert early.followups_drafted == []

    later = SENT_AT + timedelta(days=11)
    report = make_agent(engine, FakeGmailClient(), writer, later).run(profile)
    again = make_agent(engine, FakeGmailClient(), writer, later).run(profile)

    assert len(report.followups_drafted) == 1
    assert again.followups_drafted == []
    followup = store.get_draft(report.followups_drafted[0])
    assert followup.is_followup and followup.parent_draft_id == draft.id
    assert followup.status is DraftStatus.DRAFT
    assert followup.gmail_thread_id == "t1"
    assert followup.subject == "Re: PhD inquiry"
    assert writer.calls == [11]


def test_reply_withdraws_pending_followup(engine, make_professor, profile):
    store = OutreachStore(engine)
    professor = make_professor()
    draft = sent_draft(store, professor)
    pending = store.create_draft(
        professor.id,
        subject="Re: PhD inquiry",
        body="Following up",
        is_followup=True,
        parent_draft_id=draft.id,
        gmail_draft_id="d9",
    )
    client = FakeGmailClient()
    client.threads["t1"] = [
        ThreadMessage(
            id="m2",
            sender="avery@example.edu",
            internal_date_ms=int((SENT_AT + timedelta(days=12)).timestamp() * 1000),
        )
    ]

    make_agent(engine, client, FakeWriter(), SENT_AT + timedelta(days=13)).run(profile)

    assert store.get_draft(pending.id).status is DraftStatus.REJECTED
    assert client.deleted == ["d9"]
