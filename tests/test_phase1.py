"""
tests/test_phase1.py

Tests for Phase 1 additions:
  - block_time separate dispatch (solo event, no invites, after-hours still blocks)
  - suggest_times outbound trigger (find slots → send → pending_external_reply)
  - find_slots() slot-finding logic
"""

from datetime import datetime, timezone, timedelta

import pytest

from ea.calendar import CalendarClient
from ea.poll import run_poll
from ea.scheduler import find_slots, ScheduleResult
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

# ---------------------------------------------------------------------------
# Shared config / constants  (same as test_state_machine.py)
# ---------------------------------------------------------------------------

CONFIG = {
    "user": {"email": "me@example.com", "name": "Nick"},
    "schedule": {
        "timezone": "America/New_York",
        "working_hours": {
            "monday":    {"start": "09:00", "end": "17:00"},
            "tuesday":   {"start": "09:00", "end": "17:00"},
            "wednesday": {"start": "09:00", "end": "17:00"},
            "thursday":  {"start": "09:00", "end": "17:00"},
            "friday":    {"start": "09:00", "end": "15:00"},
        },
        "preferred_hours": {
            "monday":    {"start": "10:00", "end": "16:00"},
            "tuesday":   {"start": "10:00", "end": "16:00"},
            "wednesday": {"start": "10:00", "end": "16:00"},
            "thursday":  {"start": "10:00", "end": "16:00"},
            "friday":    {"start": "10:00", "end": "14:00"},
        },
    },
}

MY_EMAIL = "me@example.com"
SARAH = "sarah@example.com"

THU_2PM = "2026-03-19T18:00:00+00:00"    # Thu 2pm EDT = 18:00 UTC (preferred)
THU_7PM = "2026-03-19T23:00:00+00:00"    # Thu 7pm EDT (after hours)
THU_2PM_END = "2026-03-19T18:30:00+00:00"


# ---------------------------------------------------------------------------
# block_time dispatch
# ---------------------------------------------------------------------------

class TestBlockTime:

    def _seed_block_thread(self, body="EA: block Thursday 12-1pm for lunch"):
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-block", [
            FakeMsg(MY_EMAIL, MY_EMAIL, "Block time", body),
        ])
        return gmail

    def test_block_time_creates_solo_event(self):
        gmail = self._seed_block_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
        }})
        state = StateStore(path=None)

        block_parsed = {
            "intent": "block_time",
            "topic": "Lunch",
            "attendees": [],
            "proposed_times": [{"text": "Thursday 12-1pm", "datetimes": [THU_2PM]}],
            "duration_minutes": 60,
            "ambiguities": [],
            "urgency": "low",
        }

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: block_parsed)

        assert gmail.has_label("t-block", "ea-scheduled")
        assert len(calendar.events_created) == 1
        event = calendar.events_created[0]
        # Solo event — only my email
        assert event["attendees"] == [MY_EMAIL]

    def test_block_time_after_hours_still_blocks_without_confirmation(self):
        """block_time bypasses the needs_confirmation path — always blocks."""
        gmail = self._seed_block_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
        }})
        state = StateStore(path=None)

        after_hours_parsed = {
            "intent": "block_time",
            "topic": "Late work",
            "attendees": [],
            "proposed_times": [{"text": "Thursday at 7pm", "datetimes": [THU_7PM]}],
            "duration_minutes": 60,
            "ambiguities": [],
            "urgency": "low",
        }

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: after_hours_parsed)

        # Should block without asking for confirmation
        assert gmail.has_label("t-block", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t-block") is None   # no pending state

    def test_block_time_busy_notifies_me(self):
        gmail = self._seed_block_thread()
        # Already have something at that time
        busy_calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": [{"start": THU_2PM, "end": THU_2PM_END}]},
        }})
        state = StateStore(path=None)

        block_parsed = {
            "intent": "block_time",
            "topic": "Lunch",
            "attendees": [],
            "proposed_times": [{"text": "Thursday 12-1pm", "datetimes": [THU_2PM]}],
            "duration_minutes": 30,
            "ambiguities": [],
            "urgency": "low",
        }

        run_poll(gmail, busy_calendar, state, CONFIG,
                 parser=lambda _: block_parsed)

        assert gmail.has_label("t-block", "ea-notified")
        assert len(busy_calendar.events_created) == 0
        sent = gmail.sent_to(MY_EMAIL)
        assert any("conflict" in m.subject for m in sent)

    def test_block_time_does_not_invite_attendees(self):
        """Even if the parser returns attendees for block_time, only my email is invited."""
        gmail = self._seed_block_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
        }})
        state = StateStore(path=None)

        # Parser incorrectly returned attendees — should be ignored
        parsed_with_attendees = {
            "intent": "block_time",
            "topic": "Lunch with Sarah",
            "attendees": [SARAH],  # should be stripped for block_time
            "proposed_times": [{"text": "Thursday 12-1pm", "datetimes": [THU_2PM]}],
            "duration_minutes": 60,
            "ambiguities": [],
            "urgency": "low",
        }

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_with_attendees)

        assert gmail.has_label("t-block", "ea-scheduled")
        event = calendar.events_created[0]
        assert event["attendees"] == [MY_EMAIL]


# ---------------------------------------------------------------------------
# suggest_times dispatch
# ---------------------------------------------------------------------------

class TestSuggestTimes:

    def _seed_outbound_thread(self):
        """An outgoing email from me to Sarah with EA: suggest some times."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-out", [
            FakeMsg(MY_EMAIL, SARAH, "Coffee chat?",
                    "Hey Sarah, would love to catch up.\n\nEA: suggest some times to meet"),
        ])
        return gmail

    def _suggest_parsed(self):
        return {
            "intent": "suggest_times",
            "topic": "Coffee chat",
            "attendees": [SARAH],
            "proposed_times": [],
            "duration_minutes": 30,
            "ambiguities": [],
            "urgency": "medium",
        }

    def test_suggest_times_sends_slots_and_creates_state(self):
        gmail = self._seed_outbound_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        state = StateStore(path=None)

        canned_slots = [
            {"start": THU_2PM, "end": THU_2PM_END, "slot_type": "preferred"},
        ]

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: self._suggest_parsed(),
                 find_slots_fn=lambda parsed, cfg, cal: canned_slots)

        # State should exist as pending_external_reply
        entry = state.get("t-out")
        assert entry is not None
        assert entry["type"] == "pending_external_reply"
        assert entry["suggested_slots"] == canned_slots
        assert entry["recipient"] == SARAH

        # EA should have sent Sarah a reply on the original thread
        thread = gmail.get_thread("t-out")
        assert len(thread.messages) == 2   # original + EA reply
        ea_reply = thread.messages[1]
        assert ea_reply.to_addr == SARAH

    def test_suggest_times_no_slots_notifies_me(self):
        gmail = self._seed_outbound_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
        }})
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: self._suggest_parsed(),
                 find_slots_fn=lambda parsed, cfg, cal: [])  # no slots

        assert state.get("t-out") is None
        sent = gmail.sent_to(MY_EMAIL)
        assert any("no availability" in m.subject for m in sent)

    def test_suggest_times_then_they_confirm(self):
        """Full outbound round-trip: suggestions sent → they confirm → event created."""
        gmail = self._seed_outbound_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        state = StateStore(path=None)

        canned_slots = [
            {"start": THU_2PM, "end": THU_2PM_END, "slot_type": "preferred"},
        ]

        # Poll 1: EA sends suggestions
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: self._suggest_parsed(),
                 find_slots_fn=lambda parsed, cfg, cal: canned_slots)

        assert state.get("t-out") is not None

        # Sarah replies confirming
        gmail.add_reply("t-out", SARAH, "Thursday at 2pm works perfectly!")

        confirmed_slot = canned_slots[0]

        # Poll 2: EA sees reply, creates event
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: self._suggest_parsed(),
                 external_reply_fn=lambda text, entry: ("confirmed", confirmed_slot))

        assert gmail.has_label("t-out", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t-out") is None

    def test_suggest_times_self_addressed(self):
        """suggest_times works from a standalone self-addressed email."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-self", [
            FakeMsg(MY_EMAIL, MY_EMAIL, "My availability",
                    "EA: suggest some times on Friday for a 1 hour meeting"),
        ])
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}})
        state = StateStore(path=None)
        canned_slots = [{"start": THU_2PM, "end": THU_2PM_END, "slot_type": "preferred"}]

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: {
                     "intent": "suggest_times",
                     "topic": "Meeting",
                     "attendees": [],
                     "proposed_times": [],
                     "duration_minutes": 60,
                     "urgency": "medium",
                 },
                 find_slots_fn=lambda parsed, cfg, cal: canned_slots)

        entry = state.get("t-self")
        assert entry is not None
        assert entry["type"] == "pending_external_reply"
        assert entry["recipient"] == MY_EMAIL
        # Reply sent on thread back to me
        thread = gmail.get_thread("t-self")
        assert len(thread.messages) == 2
        assert thread.messages[1].to_addr == MY_EMAIL

    def test_suggest_times_no_slots_labels_thread(self):
        """No-availability should label the thread ea-notified to prevent retry."""
        gmail = self._seed_outbound_thread()
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}})
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: self._suggest_parsed(),
                 find_slots_fn=lambda parsed, cfg, cal: [])

        assert gmail.has_label("t-out", "ea-notified")
        assert state.get("t-out") is None

    def test_suggest_times_state_seen_count(self):
        """original_messages_seen should account for the EA message just sent."""
        gmail = self._seed_outbound_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        state = StateStore(path=None)

        canned_slots = [{"start": THU_2PM, "end": THU_2PM_END, "slot_type": "preferred"}]

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: self._suggest_parsed(),
                 find_slots_fn=lambda parsed, cfg, cal: canned_slots)

        entry = state.get("t-out")
        # Thread now has 2 messages (original + EA's suggestion).
        # Pass 3 should look for replies starting at index 2.
        assert entry["original_messages_seen"] == 2


# ---------------------------------------------------------------------------
# find_slots() unit tests
# ---------------------------------------------------------------------------

class TestFindSlots:
    """
    Tests for the scheduler.find_slots() function.
    Uses a fixed 'now' so results are deterministic.
    """

    # now = Thu 2026-03-19 08:00 EDT = 12:00 UTC  (before working hours start)
    NOW = datetime(2026, 3, 19, 12, 0, 0, tzinfo=timezone.utc)

    WORKING = CONFIG["schedule"]["working_hours"]
    PREFERRED = CONFIG["schedule"]["preferred_hours"]
    TZ = CONFIG["schedule"]["timezone"]

    def test_returns_up_to_n_slots(self):
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
        }})
        slots = find_slots(
            attendees=[MY_EMAIL],
            duration_minutes=30,
            working_hours=self.WORKING,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=calendar,
            n=3,
            now=self.NOW,
        )
        assert len(slots) <= 3
        assert len(slots) > 0

    def test_prefers_preferred_hours(self):
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
        }})
        slots = find_slots(
            attendees=[MY_EMAIL],
            duration_minutes=30,
            working_hours=self.WORKING,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=calendar,
            n=3,
            now=self.NOW,
        )
        # All returned slots should be preferred (since there are many open preferred slots)
        assert all(s["slot_type"] == "preferred" for s in slots)

    def test_skips_busy_slots(self):
        # Make the whole first day busy
        busy_calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": [
                {"start": "2026-03-19T09:00:00Z", "end": "2026-03-19T21:00:00Z"},
            ]},
        }})
        slots = find_slots(
            attendees=[MY_EMAIL],
            duration_minutes=30,
            working_hours=self.WORKING,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=busy_calendar,
            n=3,
            now=self.NOW,
        )
        # All slots should be on Friday or later
        for slot in slots:
            start = datetime.fromisoformat(slot["start"])
            assert start.date().strftime("%A") != "Thursday"

    def test_no_slots_returns_empty(self):
        # Block the entire 7-day lookahead window
        busy_calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": [
                {"start": "2026-03-19T00:00:00Z", "end": "2026-03-27T00:00:00Z"},
            ]},
        }})
        slots = find_slots(
            attendees=[MY_EMAIL],
            duration_minutes=30,
            working_hours=self.WORKING,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=busy_calendar,
            n=3,
            now=self.NOW,
        )
        assert slots == []

    def test_slots_are_in_future(self):
        """No slot should be at or before 'now'."""
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
        }})
        slots = find_slots(
            attendees=[MY_EMAIL],
            duration_minutes=30,
            working_hours=self.WORKING,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=calendar,
            n=5,
            now=self.NOW,
        )
        for slot in slots:
            start = datetime.fromisoformat(slot["start"])
            assert start > self.NOW

    def test_restrict_to_date_in_working_hours(self):
        """restrict_to_date limits results to a single day that is in working_hours."""
        from datetime import date
        from zoneinfo import ZoneInfo
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}})
        target = date(2026, 3, 19)  # Thursday — in working_hours
        slots = find_slots(
            attendees=[MY_EMAIL],
            duration_minutes=30,
            working_hours=self.WORKING,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=calendar,
            n=5,
            now=self.NOW,
            restrict_to_date=target,
        )
        assert len(slots) > 0
        tz = ZoneInfo(self.TZ)
        for slot in slots:
            local = datetime.fromisoformat(slot["start"]).astimezone(tz)
            assert local.date() == target

    def test_restrict_to_date_not_in_working_hours(self):
        """restrict_to_date returns slots even for days absent from working_hours."""
        from datetime import date
        from zoneinfo import ZoneInfo
        # Use a config with no Saturday entry
        working_no_sat = {k: v for k, v in self.WORKING.items() if k != "saturday"}
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}})
        saturday = date(2026, 3, 21)  # Saturday — not in working_hours
        slots = find_slots(
            attendees=[MY_EMAIL],
            duration_minutes=30,
            working_hours=working_no_sat,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=calendar,
            n=3,
            now=self.NOW,
            restrict_to_date=saturday,
        )
        assert len(slots) > 0
        tz = ZoneInfo(self.TZ)
        for slot in slots:
            local = datetime.fromisoformat(slot["start"]).astimezone(tz)
            assert local.date() == saturday

    def test_respects_all_attendees_freebusy(self):
        """A slot is only returned if ALL attendees are free."""
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
            SARAH: {"busy": [
                # Sarah is busy all of Thursday working hours
                {"start": "2026-03-19T13:00:00Z", "end": "2026-03-19T21:00:00Z"},
            ]},
        }})
        slots = find_slots(
            attendees=[MY_EMAIL, SARAH],
            duration_minutes=30,
            working_hours=self.WORKING,
            preferred_hours=self.PREFERRED,
            tz_name=self.TZ,
            calendar=calendar,
            n=3,
            now=self.NOW,
        )
        # No Thursday slots should appear for SARAH
        for slot in slots:
            start = datetime.fromisoformat(slot["start"])
            assert start.date().isoformat() != "2026-03-19"
