"""
fake_gmail.py

In-memory GmailClient for state machine tests. Supports:
  - Adding pre-seeded threads (inbound mail)
  - Sending emails (appends to thread or creates a new one)
  - Applying labels to threads
  - Querying sent mail and labels for assertions
"""

import uuid
from dataclasses import dataclass

from ea.gmail import GmailMessage, GmailThread


class FakeGmailClient:
    """
    In-memory Gmail backend. All threads and messages are stored in
    plain Python dicts — no network, no OAuth.

    Usage in tests:
        gmail = FakeGmailClient(my_email="me@example.com")
        thread = gmail.seed_thread(
            thread_id="t1",
            messages=[
                FakeMsg("sarah@example.com", "me@example.com", "Meeting?",
                        "Can we meet Thursday at 2pm?"),
                FakeMsg("me@example.com", "sarah@example.com", "Re: Meeting?",
                        "EA: please schedule"),
            ],
        )
    """

    def __init__(self, my_email: str = "me@example.com"):
        self.my_email = my_email
        self._threads: dict[str, GmailThread] = {}

    # ------------------------------------------------------------------
    # Test helpers (seeding data)
    # ------------------------------------------------------------------

    def seed_thread(
        self,
        thread_id: str,
        messages: list,  # list of GmailMessage or FakeMsg helper
        label_ids: list[str] = None,
    ) -> GmailThread:
        """Seed an existing thread (e.g. inbound email from someone else)."""
        msgs = []
        for i, m in enumerate(messages):
            if isinstance(m, GmailMessage):
                msgs.append(m)
            else:
                # FakeMsg namedtuple or similar
                msgs.append(
                    GmailMessage(
                        id=f"{thread_id}-msg-{i}",
                        thread_id=thread_id,
                        from_addr=m.from_addr,
                        to_addr=m.to_addr,
                        subject=m.subject,
                        date=m.date,
                        body=m.body,
                    )
                )
        thread = GmailThread(
            id=thread_id,
            messages=msgs,
            label_ids=list(label_ids or []),
        )
        self._threads[thread_id] = thread
        return thread

    def add_reply(
        self,
        thread_id: str,
        from_addr: str,
        body: str,
        date: str = "2026-03-19T20:00:00Z",
        extra_headers: dict = None,
    ) -> GmailMessage:
        """Simulate a reply arriving on an existing thread."""
        thread = self._threads[thread_id]
        first = thread.messages[0]
        msg = GmailMessage(
            id=f"{thread_id}-msg-{len(thread.messages)}",
            thread_id=thread_id,
            from_addr=from_addr,
            to_addr=first.to_addr if from_addr != self.my_email else first.from_addr,
            subject=f"Re: {first.subject}",
            date=date,
            body=body,
            extra_headers=extra_headers or {},
        )
        thread.messages.append(msg)
        return msg

    # ------------------------------------------------------------------
    # GmailClient interface (used by poll loop / responder)
    # ------------------------------------------------------------------

    def list_threads(self, exclude_label_ids: set | list = None) -> list[GmailThread]:
        """Return threads that do not carry any of the excluded labels."""
        excluded = set(exclude_label_ids or [])
        return [
            t for t in self._threads.values() if not excluded.intersection(t.label_ids)
        ]

    def get_thread(self, thread_id: str) -> GmailThread | None:
        return self._threads.get(thread_id)

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str = None,
        extra_headers: dict = None,
    ) -> GmailMessage:
        """
        Simulate sending an email. If thread_id is given, appends to that thread;
        otherwise creates a new thread. Returns the sent GmailMessage.
        """
        if thread_id and thread_id in self._threads:
            thread = self._threads[thread_id]
            msg_id = f"{thread_id}-msg-{len(thread.messages)}"
            msg = GmailMessage(
                id=msg_id,
                thread_id=thread_id,
                from_addr=self.my_email,
                to_addr=to,
                subject=subject,
                date="2026-03-19T20:00:00Z",
                body=body,
                extra_headers=extra_headers or {},
            )
            thread.messages.append(msg)
            return msg
        else:
            new_thread_id = thread_id or f"ea-thread-{uuid.uuid4().hex[:8]}"
            msg = GmailMessage(
                id=f"{new_thread_id}-msg-0",
                thread_id=new_thread_id,
                from_addr=self.my_email,
                to_addr=to,
                subject=subject,
                date="2026-03-19T20:00:00Z",
                body=body,
                extra_headers=extra_headers or {},
            )
            self._threads[new_thread_id] = GmailThread(
                id=new_thread_id,
                messages=[msg],
            )
            return msg

    def apply_label(self, thread_id: str, label: str) -> None:
        thread = self._threads.get(thread_id)
        if thread and label not in thread.label_ids:
            thread.label_ids.append(label)

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def has_label(self, thread_id: str, label: str) -> bool:
        thread = self._threads.get(thread_id)
        return thread is not None and label in thread.label_ids

    def sent_to(self, address: str) -> list[GmailMessage]:
        """Return all messages sent to a given address (across all threads)."""
        result = []
        for thread in self._threads.values():
            for msg in thread.messages:
                if msg.from_addr == self.my_email and msg.to_addr == address:
                    result.append(msg)
        return result

    def thread_message_count(self, thread_id: str) -> int:
        thread = self._threads.get(thread_id)
        return len(thread.messages) if thread else 0


class NewThreadFakeGmailClient(FakeGmailClient):
    """FakeGmailClient variant where send_email always creates a new thread.

    Simulates Gmail's behaviour when it ignores the threadId hint because the
    outgoing subject does not match the original thread's subject — exactly what
    happens when EA sends "EA: confirm slot — <original subject>" as a reply to
    the original scheduling thread.
    """

    def send_email(self, to, subject, body, thread_id=None, extra_headers=None):
        return super().send_email(
            to=to,
            subject=subject,
            body=body,
            thread_id=None,  # force new thread regardless of caller's intent
            extra_headers=extra_headers,
        )


@dataclass
class FakeMsg:
    """Convenience builder for seeding messages in FakeGmailClient.seed_thread()."""

    from_addr: str
    to_addr: str
    subject: str
    body: str
    date: str = "2026-03-19T14:00:00Z"
