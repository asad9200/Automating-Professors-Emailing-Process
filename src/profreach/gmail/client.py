import base64
import logging
import mimetypes
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class GmailError(RuntimeError):
    """Raised when a Gmail API call fails."""


class GmailNotFound(GmailError):
    """Raised when a referenced draft or message no longer exists."""


class Attachment(BaseModel):
    filename: str
    content: bytes


class ThreadMessage(BaseModel):
    id: str
    sender: str
    internal_date_ms: int


def build_message(
    to: str,
    subject: str,
    body: str,
    attachment: Attachment | None = None,
    in_reply_to: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body)
    if attachment is not None:
        mime_type, _ = mimetypes.guess_type(attachment.filename)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    return message


def encode_message(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailClient:
    """Thin wrapper over the Gmail v1 service so agents can be tested with fakes."""

    def __init__(self, service: Any):
        self.service = service
        self._address: str | None = None

    def _execute(self, request: Any) -> dict:
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status == 404:
                raise GmailNotFound("The Gmail item was not found.") from error
            raise GmailError(f"Gmail API request failed with status {status}.") from error
        except OSError as error:
            raise GmailError("Could not reach Gmail.") from error

    def my_address(self) -> str:
        if self._address is None:
            profile = self._execute(self.service.users().getProfile(userId="me"))
            self._address = profile["emailAddress"].lower()
        return self._address

    def create_draft(self, message: EmailMessage, thread_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"raw": encode_message(message)}
        if thread_id:
            payload["threadId"] = thread_id
        return self._execute(
            self.service.users().drafts().create(userId="me", body={"message": payload})
        )

    def update_draft(
        self,
        draft_id: str,
        message: EmailMessage,
        thread_id: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"raw": encode_message(message)}
        if thread_id:
            payload["threadId"] = thread_id
        return self._execute(
            self.service.users()
            .drafts()
            .update(userId="me", id=draft_id, body={"id": draft_id, "message": payload})
        )

    def delete_draft(self, draft_id: str) -> None:
        self._execute(self.service.users().drafts().delete(userId="me", id=draft_id))

    def send_draft(self, draft_id: str) -> dict:
        return self._execute(self.service.users().drafts().send(userId="me", body={"id": draft_id}))

    def message_id_header(self, message_id: str) -> str | None:
        message = self._execute(
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=["Message-ID"])
        )
        for header in message.get("payload", {}).get("headers", []):
            if header.get("name", "").lower() == "message-id":
                return header.get("value")
        return None

    def thread_messages(self, thread_id: str) -> list[ThreadMessage]:
        thread = self._execute(
            self.service.users()
            .threads()
            .get(userId="me", id=thread_id, format="metadata", metadataHeaders=["From"])
        )
        messages = []
        for item in thread.get("messages", []):
            sender = next(
                (
                    header.get("value", "")
                    for header in item.get("payload", {}).get("headers", [])
                    if header.get("name", "").lower() == "from"
                ),
                "",
            )
            messages.append(
                ThreadMessage(
                    id=item["id"],
                    sender=parseaddr(sender)[1].lower(),
                    internal_date_ms=int(item.get("internalDate", 0)),
                )
            )
        return messages
