"""
gmail.py

GmailMessage and GmailThread data structures, plus LiveGmailClient which
implements the same duck-type interface as tests/fake_gmail.py:FakeGmailClient.

Interface (both live and fake implement):
  list_threads(exclude_label_ids) -> list[GmailThread]
  get_thread(thread_id)           -> GmailThread | None
  send_email(to, subject, body, thread_id=None, extra_headers=None) -> GmailMessage
  apply_label(thread_id, label)   -> None
"""

import base64
import logging
from dataclasses import dataclass, field
from email.mime.text import MIMEText

from ea.network import call_with_retry

_log = logging.getLogger("ea.gmail")


@dataclass
class GmailMessage:
    id: str
    thread_id: str
    from_addr: str
    to_addr: str
    subject: str
    date: str                    # RFC 2822 or ISO 8601
    body: str
    label_ids: list[str] = field(default_factory=list)
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class GmailThread:
    id: str
    messages: list[GmailMessage]
    label_ids: list[str] = field(default_factory=list)


def thread_to_text(thread: GmailThread) -> str:
    """
    Render a GmailThread as the flat text format expected by
    parse_thread() / parse_meeting_request().
    """
    parts = []
    for msg in thread.messages:
        header_block = "\n".join([
            f"From: {msg.from_addr}",
            f"To: {msg.to_addr}",
            f"Date: {msg.date}",
            f"Subject: {msg.subject}",
        ])
        parts.append(header_block + "\n\n" + msg.body)
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Footer wrapper
# ---------------------------------------------------------------------------

class FooterGmailClient:
    """
    Wraps any gmail client (live or fake) and appends a footer to every
    outgoing email body.  All other methods delegate transparently to the
    inner client via __getattr__, so the wrapper is a drop-in replacement
    for any duck-typed gmail interface.

    Instantiated in runner.py when config["user"]["email_footer"] is set.
    """

    def __init__(self, inner, footer: str):
        self._inner = inner
        self._footer = footer

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str = None,
        extra_headers: dict = None,
    ) -> "GmailMessage":
        if self._footer:
            body = f"{body}\n\n---\n{self._footer}"
        return self._inner.send_email(
            to, subject, body, thread_id=thread_id, extra_headers=extra_headers
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Live Gmail client
# ---------------------------------------------------------------------------

class LiveGmailClient:
    """
    Gmail API client.  Implements the same duck-type interface as
    FakeGmailClient so it can be dropped into run_poll() as-is.

    Args:
        creds: google.oauth2.credentials.Credentials from ea.auth.load_creds()
    """

    def __init__(self, creds):
        from googleapiclient.discovery import build
        service = build("gmail", "v1", credentials=creds)
        self._users = service.users()
        self._label_id_cache: dict[str, str] = {}   # label name → Gmail label ID

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    def list_threads(self, exclude_label_ids: set | list = None) -> list[GmailThread]:
        """
        Return inbox threads that don't carry any of the excluded labels.
        Fetches full thread content for each match (one API call per thread).
        """
        excluded = list(exclude_label_ids or [])
        query_parts = ["in:inbox"]
        for label in excluded:
            query_parts.append(f"-label:{label}")
        query = " ".join(query_parts)

        threads: list[GmailThread] = []
        page_token = None
        while True:
            resp = call_with_retry(
                lambda pt=page_token: self._users.threads().list(
                    userId="me", q=query, pageToken=pt
                ).execute()
            )
            for item in resp.get("threads", []):
                thread = self.get_thread(item["id"])
                if thread:
                    threads.append(thread)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return threads

    def get_thread(self, thread_id: str) -> GmailThread | None:
        """Fetch a single thread with all messages fully decoded."""
        try:
            data = call_with_retry(
                lambda: self._users.threads().get(
                    userId="me", id=thread_id, format="full"
                ).execute()
            )
        except Exception:
            return None

        messages = [
            self._parse_message(m)
            for m in data.get("messages", [])
        ]
        # Collect label IDs from the most recent message
        last_labels = data["messages"][-1].get("labelIds", []) if data.get("messages") else []
        return GmailThread(id=thread_id, messages=messages, label_ids=last_labels)

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str = None,
        extra_headers: dict = None,
    ) -> GmailMessage:
        """
        Send an email.  If thread_id is given, the message is sent as a reply
        on that thread.  Returns the sent message as a GmailMessage.
        """
        _log.debug(
            "send_email to=%r subject=%r thread_id=%r", to, subject, thread_id,
            extra={"to": to, "thread_id": thread_id},
        )
        mime = MIMEText(body)
        mime["To"] = to
        mime["From"] = "me"
        mime["Subject"] = subject
        if extra_headers:
            for k, v in extra_headers.items():
                mime[k] = v

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        send_body: dict = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        result = call_with_retry(
            lambda: self._users.messages().send(userId="me", body=send_body).execute()
        )
        # Fetch the full message so we can return a GmailMessage
        full = call_with_retry(
            lambda: self._users.messages().get(
                userId="me", id=result["id"], format="full"
            ).execute()
        )
        return self._parse_message(full)

    def apply_label(self, thread_id: str, label: str) -> None:
        """Create the label if it doesn't exist, then apply it to every message in the thread."""
        label_id = self._get_or_create_label(label)
        thread = call_with_retry(
            lambda: self._users.threads().get(
                userId="me", id=thread_id, format="minimal"
            ).execute()
        )
        for msg in thread.get("messages", []):
            msg_id = msg["id"]
            call_with_retry(
                lambda mid=msg_id: self._users.messages().modify(
                    userId="me",
                    id=mid,
                    body={"addLabelIds": [label_id]},
                ).execute()
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_create_label(self, name: str) -> str:
        if name in self._label_id_cache:
            return self._label_id_cache[name]

        labels = call_with_retry(
            lambda: self._users.labels().list(userId="me").execute()
        )
        for label in labels.get("labels", []):
            if label["name"] == name:
                self._label_id_cache[name] = label["id"]
                return label["id"]

        # Label doesn't exist — create it
        new_label = call_with_retry(
            lambda: self._users.labels().create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ).execute()
        )
        self._label_id_cache[name] = new_label["id"]
        return new_label["id"]

    def _parse_message(self, m: dict) -> GmailMessage:
        headers_raw = m.get("payload", {}).get("headers", [])
        headers = {h["name"].lower(): h["value"] for h in headers_raw}

        # Collect any X-EA-* custom headers
        extra = {
            h["name"]: h["value"]
            for h in headers_raw
            if h["name"].startswith("X-EA-")
        }

        return GmailMessage(
            id=m["id"],
            thread_id=m["threadId"],
            from_addr=headers.get("from", ""),
            to_addr=headers.get("to", ""),
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
            body=_decode_body(m.get("payload", {})),
            label_ids=m.get("labelIds", []),
            extra_headers=extra,
        )


def _decode_body(payload: dict) -> str:
    """
    Extract plain-text body from a Gmail API message payload.
    Handles both simple messages (payload.body.data) and multipart.
    """
    # Simple message
    data = payload.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart — look for text/plain parts
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            part_data = part.get("body", {}).get("data", "")
            if part_data:
                return base64.urlsafe_b64decode(part_data).decode("utf-8", errors="replace")
        # Recurse into nested multipart
        nested = _decode_body(part)
        if nested:
            return nested

    return ""
