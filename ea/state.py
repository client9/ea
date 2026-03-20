"""
state.py

Thread-level scheduling state. Only threads actively awaiting a reply
are stored here. Completed threads are removed and marked with Gmail labels.

See docs/state-machine.md for the full schema and flow description.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_FILE = "state.json"


class StateStore:
    def __init__(self, path: str | None = DEFAULT_STATE_FILE):
        """
        Args:
            path: Path to the JSON state file. Pass None for in-memory only
                  (no disk I/O — useful for tests).
        """
        self._path = Path(path) if path else None
        self._state: dict = {}
        if self._path and self._path.exists():
            self._state = json.loads(self._path.read_text())

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, thread_id: str) -> dict | None:
        return self._state.get(thread_id)

    def set(self, thread_id: str, entry: dict) -> None:
        self._state[thread_id] = entry
        self._persist()

    def update(self, thread_id: str, updates: dict) -> None:
        entry = self._state.get(thread_id, {})
        entry.update(updates)
        self._state[thread_id] = entry
        self._persist()

    def delete(self, thread_id: str) -> None:
        self._state.pop(thread_id, None)
        self._persist()

    def all(self) -> dict:
        return dict(self._state)

    # ------------------------------------------------------------------
    # Filtered views
    # ------------------------------------------------------------------

    def pending_confirmations(self) -> list[tuple[str, dict]]:
        return [
            (tid, entry)
            for tid, entry in self._state.items()
            if entry.get("type") == "pending_confirmation"
        ]

    def pending_external_replies(self) -> list[tuple[str, dict]]:
        return [
            (tid, entry)
            for tid, entry in self._state.items()
            if entry.get("type") == "pending_external_reply"
        ]

    def expired(self) -> list[tuple[str, dict]]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            (tid, entry)
            for tid, entry in self._state.items()
            if entry.get("expires_at", "9999") < now
        ]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        if self._path:
            self._path.write_text(json.dumps(self._state, indent=2))
