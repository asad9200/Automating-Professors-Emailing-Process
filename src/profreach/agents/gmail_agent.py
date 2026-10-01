import hashlib
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from profreach.config import Settings, get_settings
from profreach.db.models import DraftStatus, EmailDraft, Professor, SendAction
from profreach.db.outreach_store import OutreachStore, as_utc
from profreach.gmail.client import Attachment, GmailClient, GmailError, GmailNotFound, build_message

logger = logging.getLogger(__name__)
_UNSENDABLE = {DraftStatus.SENT, DraftStatus.REPLIED, DraftStatus.REJECTED}


class ThrottleStatus(BaseModel):
    can_send: bool
    sent_last_24h: int
    max_per_day: int
    next_allowed_at: datetime | None = None
    reason: str = ""


class SendReport(BaseModel):
    sent: list[int] = Field(default_factory=list)
    failed: dict[int, str] = Field(default_factory=dict)
    deferred: list[int] = Field(default_factory=list)
    stopped_reason: str | None = None


class SyncReport(BaseModel):
    created: list[int] = Field(default_factory=list)
    updated: list[int] = Field(default_factory=list)
    skipped: dict[int, str] = Field(default_factory=dict)


def load_cv_attachment(path: str | None) -> Attachment | None:
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    return Attachment(filename=file.name, content=file.read_bytes())


class GmailAgent:
    def __init__(
        self,
        store: OutreachStore,
        client: GmailClient,
        settings: Settings | None = None,
        attachment: Attachment | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.store = store
        self.client = client
        self.settings = settings or get_settings()
        self.attachment = attachment or load_cv_attachment(self.settings.cv_attachment_path)
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()

    # Drafts

    def sync_drafts(self, drafts: list[tuple[EmailDraft, Professor]]) -> SyncReport:
        """Create or refresh Gmail drafts for local drafts that are still sendable."""
        logger.info("Starting Gmail draft sync for %s drafts", len(drafts))
        report = SyncReport()
        for draft, professor in drafts:
            if draft.status in _UNSENDABLE:
                report.skipped[draft.id] = f"status is {draft.status.value}"
                continue
            try:
                created = self._sync_one(draft, professor)
            except (GmailError, ValueError) as error:
                logger.warning("Gmail draft sync failed for draft %s: %s", draft.id, error)
                self.store.update_draft(draft.id, last_error=str(error))
                report.skipped[draft.id] = str(error)
                continue
            if created is None:
                report.skipped[draft.id] = "already up to date"
            else:
                (report.created if created else report.updated).append(draft.id)
        logger.info(
            "Completed Gmail draft sync: %s created, %s updated, %s skipped",
            len(report.created),
            len(report.updated),
            len(report.skipped),
        )
        return report

    @staticmethod
    def content_hash(draft: EmailDraft, professor: Professor) -> str:
        parts = [professor.email or "", draft.subject, draft.body, str(draft.attach_cv)]
        return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()

    def _sync_one(self, draft: EmailDraft, professor: Professor) -> bool | None:
        """Create or update the Gmail draft; returns None when it is already current."""
        if not professor.email:
            raise ValueError("No published email address for this professor.")
        content_hash = self.content_hash(draft, professor)
        if draft.gmail_draft_id and draft.gmail_synced_hash == content_hash:
            return None
        attachment = None
        if draft.attach_cv:
            if self.attachment is None:
                raise ValueError("CV attachment requested but no CV file is available.")
            attachment = self.attachment
        in_reply_to = None
        if draft.parent_draft_id:
            parent = self.store.get_draft(draft.parent_draft_id)
            in_reply_to = parent.rfc_message_id if parent else None
        message = build_message(
            professor.email,
            draft.subject,
            draft.body,
            attachment=attachment,
            in_reply_to=in_reply_to,
        )
        created = False
        response = None
        if draft.gmail_draft_id:
            try:
                response = self.client.update_draft(
                    draft.gmail_draft_id, message, draft.gmail_thread_id
                )
            except GmailNotFound:
                logger.info("Gmail draft for draft %s was deleted; recreating it", draft.id)
        if response is None:
            response = self.client.create_draft(message, draft.gmail_thread_id)
            created = True
        gmail_message = response.get("message", {})
        self.store.update_draft(
            draft.id,
            gmail_draft_id=response["id"],
            gmail_message_id=gmail_message.get("id"),
            gmail_thread_id=gmail_message.get("threadId") or draft.gmail_thread_id,
            gmail_synced_hash=content_hash,
            last_error=None,
        )
        self.store.add_log(
            draft.id,
            SendAction.GMAIL_DRAFT_CREATED if created else SendAction.GMAIL_DRAFT_UPDATED,
            gmail_message_id=gmail_message.get("id"),
            gmail_thread_id=gmail_message.get("threadId"),
            recipient=professor.email,
        )
        return created

    # Sending

    def throttle_status(self) -> ThrottleStatus:
        now = self.clock()
        sent = self.store.sent_count_since(now - timedelta(hours=24))
        maximum = self.settings.send_max_per_day
        if sent >= maximum:
            return ThrottleStatus(
                can_send=False,
                sent_last_24h=sent,
                max_per_day=maximum,
                reason=f"Daily limit reached ({sent}/{maximum} in the last 24 hours).",
            )
        last = self.store.last_send()
        next_allowed = as_utc(last.next_send_after) if last else None
        if next_allowed and now < next_allowed:
            return ThrottleStatus(
                can_send=False,
                sent_last_24h=sent,
                max_per_day=maximum,
                next_allowed_at=next_allowed,
                reason="Waiting for the randomized gap between sends.",
            )
        return ThrottleStatus(can_send=True, sent_last_24h=sent, max_per_day=maximum)

    def send_approved(
        self,
        professor_ids: list[int] | None = None,
        limit: int | None = None,
        wait: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> SendReport:
        """Send approved drafts only, honoring the daily cap and randomized gaps."""
        logger.info("Starting approved-email sending")
        report = SendReport()
        attempted: set[int] = set()
        while limit is None or len(report.sent) < limit:
            queue = [
                (draft, professor)
                for draft, professor in self.store.list_drafts(
                    statuses={DraftStatus.APPROVED}, professor_ids=professor_ids
                )
                if draft.id not in attempted
            ]
            if not queue:
                break
            status = self.throttle_status()
            if not status.can_send:
                if wait and status.next_allowed_at:
                    delay = (status.next_allowed_at - self.clock()).total_seconds()
                    if progress:
                        progress(f"Waiting {max(0, round(delay))} seconds before the next send")
                    self.sleep(max(0.0, delay))
                    continue
                report.stopped_reason = status.reason
                report.deferred = [draft.id for draft, _ in queue]
                break
            draft, professor = queue[0]
            attempted.add(draft.id)
            error = self._send_one(draft.id, professor)
            if error:
                report.failed[draft.id] = error
            else:
                report.sent.append(draft.id)
                if progress:
                    progress(f"Sent email to {professor.name}")
        logger.info(
            "Completed approved-email sending: %s sent, %s failed, %s deferred",
            len(report.sent),
            len(report.failed),
            len(report.deferred),
        )
        return report

    def _send_one(self, draft_id: int, professor: Professor) -> str | None:
        # Re-read so a last-moment edit or un-approval in the UI is respected.
        draft = self.store.get_draft(draft_id)
        if draft is None or draft.status is not DraftStatus.APPROVED:
            return "Draft is no longer approved."
        try:
            self._sync_one(draft, professor)
            draft = self.store.get_draft(draft_id)
            response = self.client.send_draft(draft.gmail_draft_id)
            message_id = response.get("id")
            thread_id = response.get("threadId")
            rfc_message_id = self.client.message_id_header(message_id) if message_id else None
        except (GmailError, ValueError) as error:
            logger.warning("Sending draft %s failed: %s", draft_id, error)
            self.store.update_draft(draft_id, last_error=str(error))
            self.store.add_log(
                draft_id, SendAction.FAILED, recipient=professor.email, detail=str(error)
            )
            return str(error)
        now = self.clock()
        gap = self.rng.uniform(
            self.settings.send_min_gap_seconds,
            max(self.settings.send_min_gap_seconds, self.settings.send_max_gap_seconds),
        )
        self.store.update_draft(
            draft_id,
            status=DraftStatus.SENT,
            sent_at=now,
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            rfc_message_id=rfc_message_id,
            gmail_draft_id=None,
            gmail_synced_hash=None,
            last_error=None,
        )
        self.store.add_log(
            draft_id,
            SendAction.SENT,
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            recipient=professor.email,
            next_send_after=now + timedelta(seconds=gap),
            created_at=now,
        )
        return None
