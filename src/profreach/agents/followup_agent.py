import logging
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from profreach.agents.writer_agent import WriterAgent, WriterError
from profreach.config import Settings, get_settings
from profreach.db.models import DraftStatus, SendAction, UserProfile
from profreach.db.outreach_store import OutreachStore, as_utc
from profreach.gmail.client import GmailClient, GmailError

logger = logging.getLogger(__name__)


class FollowUpReport(BaseModel):
    replied: list[int] = Field(default_factory=list)
    followups_drafted: list[int] = Field(default_factory=list)
    errors: dict[int, str] = Field(default_factory=dict)


class FollowUpAgent:
    """Detects replies in sent threads and drafts one follow-up for unanswered emails."""

    def __init__(
        self,
        store: OutreachStore,
        client: GmailClient,
        writer: WriterAgent,
        settings: Settings | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.store = store
        self.client = client
        self.writer = writer
        self.settings = settings or get_settings()
        self.clock = clock

    def run(self, profile: UserProfile | None) -> FollowUpReport:
        logger.info("Starting reply check and follow-up drafting")
        report = FollowUpReport()
        now = self.clock()
        try:
            me = self.client.my_address()
        except GmailError as error:
            logger.warning("Could not read the Gmail profile: %s", error)
            report.errors[0] = str(error)
            return report

        for draft, professor in self.store.list_drafts(statuses={DraftStatus.SENT}):
            if not draft.gmail_thread_id:
                continue
            try:
                messages = self.client.thread_messages(draft.gmail_thread_id)
            except GmailError as error:
                report.errors[draft.id] = str(error)
                continue
            sent_at = as_utc(draft.sent_at) or now
            sent_ms = int(sent_at.timestamp() * 1000)
            reply = next(
                (
                    message
                    for message in messages
                    if message.sender
                    and message.sender != me
                    and message.internal_date_ms >= sent_ms
                ),
                None,
            )
            if reply:
                self._record_reply(draft.id, draft.parent_draft_id, reply.id, now)
                report.replied.append(draft.id)
                continue

            if draft.is_followup or self.store.followup_of(draft.id) is not None:
                continue
            days = (now - sent_at).days
            if days < self.settings.followup_after_days:
                continue
            if profile is None:
                report.errors[draft.id] = "Save a profile before drafting follow-ups."
                continue
            try:
                subject, body = self.writer.write_followup(
                    profile, professor, draft.subject, draft.body, days
                )
            except WriterError as error:
                report.errors[draft.id] = str(error)
                continue
            followup = self.store.create_draft(
                professor.id,
                subject=subject,
                body=body,
                match_note=f"Follow-up to the email sent {days} days ago with no reply.",
                start_term=draft.start_term,
                is_followup=True,
                parent_draft_id=draft.id,
                gmail_thread_id=draft.gmail_thread_id,
            )
            self.store.add_log(
                followup.id,
                SendAction.FOLLOWUP_DRAFTED,
                gmail_thread_id=draft.gmail_thread_id,
                recipient=professor.email,
            )
            report.followups_drafted.append(followup.id)
        logger.info(
            "Completed reply check: %s replies, %s follow-ups drafted, %s errors",
            len(report.replied),
            len(report.followups_drafted),
            len(report.errors),
        )
        return report

    def _record_reply(
        self,
        draft_id: int,
        parent_id: int | None,
        reply_message_id: str,
        now: datetime,
    ) -> None:
        original_id = parent_id or draft_id
        for related_id in {draft_id, original_id}:
            self.store.update_draft(related_id, status=DraftStatus.REPLIED, replied_at=now)
        self.store.add_log(
            draft_id,
            SendAction.REPLY_DETECTED,
            gmail_message_id=reply_message_id,
        )
        # A reply makes any pending follow-up unnecessary.
        pending = self.store.followup_of(original_id)
        if pending and pending.status in {DraftStatus.DRAFT, DraftStatus.APPROVED}:
            if pending.gmail_draft_id:
                try:
                    self.client.delete_draft(pending.gmail_draft_id)
                except GmailError as error:
                    logger.info("Could not delete withdrawn Gmail draft: %s", error)
            self.store.update_draft(
                pending.id,
                status=DraftStatus.REJECTED,
                gmail_draft_id=None,
                last_error="Reply received; follow-up withdrawn.",
            )
