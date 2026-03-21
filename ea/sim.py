"""
sim.py

Simulation clients for CLI testing of EA flows without touching real Gmail or
making irreversible calendar changes.

  SimGmailClient      — captures sent emails and labels in-memory; same interface
                         as LiveGmailClient / FakeGmailClient in tests/.
  DryRunCalendarClient — delegates read-only calls (get_freebusy,
                         find_matching_event, list_events) to a real
                         CalendarClient but intercepts writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ea.gmail import GmailMessage, GmailThread


# ---------------------------------------------------------------------------
# SimGmailClient
# ---------------------------------------------------------------------------


class SimGmailClient:
    """In-memory Gmail client that captures sent emails and applied labels.

    Pre-populate with seed_thread() before running run_poll().  After the run,
    inspect .sent and thread label_ids to see what EA would have done.
    """

    def __init__(self, my_email: str):
        self.my_email = my_email
        self._threads: dict[str, GmailThread] = {}
        self.sent: list[GmailMessage] = []

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def seed_thread(self, thread_id: str, messages: list[GmailMessage]) -> None:
        self._threads[thread_id] = GmailThread(id=thread_id, messages=list(messages))

    # ------------------------------------------------------------------
    # Duck-type interface (mirrors LiveGmailClient)
    # ------------------------------------------------------------------

    def list_threads(
        self, exclude_label_ids: list[str] | None = None
    ) -> list[GmailThread]:
        exclude = set(exclude_label_ids or [])
        return [t for t in self._threads.values() if not (exclude & set(t.label_ids))]

    def get_thread(self, thread_id: str) -> GmailThread | None:
        return self._threads.get(thread_id)

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        extra_headers: dict | None = None,
    ) -> GmailMessage:
        now = datetime.now(timezone.utc).isoformat()
        msg_id = str(uuid.uuid4())[:8]
        msg = GmailMessage(
            id=msg_id,
            thread_id=thread_id or msg_id,
            from_addr=self.my_email,
            to_addr=to,
            subject=subject,
            date=now,
            body=body,
            extra_headers=extra_headers or {},
        )
        self.sent.append(msg)
        # Also append to the thread so multi-pass reads see the reply
        if thread_id and thread_id in self._threads:
            self._threads[thread_id].messages.append(msg)
        return msg

    def apply_label(self, thread_id: str, label: str) -> None:
        if thread_id in self._threads:
            t = self._threads[thread_id]
            if label not in t.label_ids:
                t.label_ids.append(label)


# ---------------------------------------------------------------------------
# DryRunCalendarClient
# ---------------------------------------------------------------------------


class DryRunCalendarClient:
    """Wraps a real CalendarClient for reads; captures writes instead of
    executing them.

    Attributes set after run_poll():
      would_create  — list of kwargs passed to create_event()
      would_delete  — list of event_id strings passed to delete_event()
      would_update  — list of (event_id, start, end) tuples
    """

    def __init__(self, live):
        """
        Args:
            live: A CalendarClient instance with real credentials (for reads).
        """
        self._live = live
        self.would_create: list[dict] = []
        self.would_delete: list[str] = []
        self.would_update: list[tuple] = []

    # Read-through delegates
    def get_freebusy(self, *args, **kwargs):
        return self._live.get_freebusy(*args, **kwargs)

    def find_matching_event(self, *args, **kwargs):
        return self._live.find_matching_event(*args, **kwargs)

    def list_events(self, *args, **kwargs):
        return self._live.list_events(*args, **kwargs)

    # Captured writes
    def create_event(self, **kwargs) -> dict:
        self.would_create.append(kwargs)
        return {"id": "dry-run-event", "htmlLink": ""}

    def delete_event(self, event_id: str, send_updates: bool = True) -> None:
        self.would_delete.append(event_id)

    def update_event(self, event_id: str, start: str, end: str) -> dict:
        self.would_update.append((event_id, start, end))
        return {"id": event_id}
