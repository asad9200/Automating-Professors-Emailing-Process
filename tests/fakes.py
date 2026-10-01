from profreach.gmail.client import GmailNotFound, ThreadMessage


class FakeGmailClient:
    """In-memory stand-in for GmailClient."""

    def __init__(self, address="me@example.org"):
        self.address = address
        self.drafts: dict[str, dict] = {}
        self.sent: list[str] = []
        self.threads: dict[str, list[ThreadMessage]] = {}
        self.fail_send_for: set[str] = set()
        self.updates = 0
        self.deleted: list[str] = []
        self._counter = 0

    def _next(self, prefix):
        self._counter += 1
        return f"{prefix}{self._counter}"

    def my_address(self):
        return self.address

    def create_draft(self, message, thread_id=None):
        draft_id = self._next("d")
        message_id = self._next("m")
        self.drafts[draft_id] = {
            "message": message,
            "message_id": message_id,
            "thread_id": thread_id or self._next("t"),
        }
        return {
            "id": draft_id,
            "message": {"id": message_id, "threadId": self.drafts[draft_id]["thread_id"]},
        }

    def update_draft(self, draft_id, message, thread_id=None):
        if draft_id not in self.drafts:
            raise GmailNotFound("gone")
        self.updates += 1
        self.drafts[draft_id]["message"] = message
        entry = self.drafts[draft_id]
        return {
            "id": draft_id,
            "message": {"id": entry["message_id"], "threadId": entry["thread_id"]},
        }

    def delete_draft(self, draft_id):
        self.deleted.append(draft_id)
        self.drafts.pop(draft_id, None)

    def send_draft(self, draft_id):
        entry = self.drafts[draft_id]
        if entry["message"]["To"] in self.fail_send_for:
            from profreach.gmail.client import GmailError

            raise GmailError("send failed")
        self.sent.append(entry["message"]["To"])
        sent_id = self._next("sent")
        self.threads.setdefault(entry["thread_id"], []).append(
            ThreadMessage(id=sent_id, sender=self.address, internal_date_ms=0)
        )
        return {"id": sent_id, "threadId": entry["thread_id"]}

    def message_id_header(self, message_id):
        return f"<{message_id}@mail.example.org>"

    def thread_messages(self, thread_id):
        return self.threads.get(thread_id, [])
