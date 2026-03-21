"""
tests/test_allday.py

Tests for all-day and multi-day event creation (feature #6).
Covers: single-day OOO, multi-day vacation, informational/transparent events,
missing-dates error path, exclusive end-date calculation, and email formatting.
"""

import pytest
from ea.calendar import CalendarClient
from ea.responder import handle_allday_block
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

MY_EMAIL = "me@example.com"

CONFIG = {
    "user": {"email": MY_EMAIL, "name": "Nick"},
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
        },
    },
}


def _make_gmail(subject="EA: out of office Monday", body=""):
    gmail = FakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread("t-allday", [
        FakeMsg(MY_EMAIL, MY_EMAIL, subject, body),
    ])
    return gmail


def _make_calendar():
    return CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}})


def _parsed(dates, event_type="ooo", topic=None):
    """Build a minimal parsed dict for an all-day block_time."""
    return {
        "intent": "block_time",
        "all_day": True,
        "event_type": event_type,
        "topic": topic,
        "proposed_times": [{"text": "Monday", "datetimes": dates}],
        "new_proposed_times": [],
        "attendees": [],
        "duration_minutes": None,
    }


# ---------------------------------------------------------------------------
# Single-day all-day event
# ---------------------------------------------------------------------------

class TestSingleDayAllDay:

    def test_ooo_creates_opaque_event(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        parsed   = _parsed(["2026-03-23"])

        action = handle_allday_block(parsed, gmail.get_thread("t-allday"), gmail, calendar, CONFIG)

        assert action == "scheduled"
        assert len(calendar.events_created) == 1
        ev = calendar.events_created[0]
        assert ev["start"] == {"date": "2026-03-23"}
        assert ev["end"]   == {"date": "2026-03-24"}   # exclusive end = +1 day
        assert ev["transparency"] == "opaque"
        assert ev["summary"] == "Out of Office"

    def test_ooo_applies_scheduled_label(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        handle_allday_block(_parsed(["2026-03-23"]), gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        assert gmail.has_label("t-allday", "ea-scheduled")

    def test_ooo_sends_confirmation_email(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        handle_allday_block(_parsed(["2026-03-23"]), gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        sent = gmail.sent_to(MY_EMAIL)
        assert any("blocked" in m.subject.lower() for m in sent)
        body = sent[-1].body
        assert "2026" in body
        assert "blocking" in body   # opaque events show "blocking (busy)"

    def test_vacation_opaque(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        handle_allday_block(_parsed(["2026-03-23"], event_type="vacation"), gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        ev = calendar.events_created[0]
        assert ev["transparency"] == "opaque"
        assert ev["summary"] == "Vacation"


# ---------------------------------------------------------------------------
# Multi-day range
# ---------------------------------------------------------------------------

class TestMultiDayAllDay:

    def test_multiday_start_and_end_dates(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        parsed   = _parsed(["2026-03-23", "2026-03-27"])   # Mon–Fri inclusive

        handle_allday_block(parsed, gmail.get_thread("t-allday"), gmail, calendar, CONFIG)

        ev = calendar.events_created[0]
        assert ev["start"] == {"date": "2026-03-23"}
        assert ev["end"]   == {"date": "2026-03-28"}   # Fri inclusive → Sat exclusive

    def test_multiday_confirmation_email_shows_range(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        handle_allday_block(_parsed(["2026-03-23", "2026-03-27"]), gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        body = gmail.sent_to(MY_EMAIL)[-1].body
        # Both start and end dates should appear in the confirmation
        assert "Mar 23" in body
        assert "Mar 27" in body


# ---------------------------------------------------------------------------
# Transparent (informational) events
# ---------------------------------------------------------------------------

class TestTransparentAllDay:

    def test_conference_transparent(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        parsed   = _parsed(["2026-04-15"], event_type="conference", topic="PyCon")

        handle_allday_block(parsed, gmail.get_thread("t-allday"), gmail, calendar, CONFIG)

        ev = calendar.events_created[0]
        assert ev["transparency"] == "transparent"
        assert ev["summary"] == "PyCon"

    def test_holiday_transparent(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        parsed   = _parsed(["2026-07-04"], event_type="holiday", topic="Independence Day")

        handle_allday_block(parsed, gmail.get_thread("t-allday"), gmail, calendar, CONFIG)

        ev = calendar.events_created[0]
        assert ev["transparency"] == "transparent"

    def test_transparent_confirmation_says_free(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        handle_allday_block(_parsed(["2026-04-15"], event_type="conference", topic="PyCon"),
                            gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        body = gmail.sent_to(MY_EMAIL)[-1].body
        assert "informational" in body   # transparent events show "informational (free)"

    def test_ooo_block_types_are_opaque(self):
        for event_type in ("ooo", "vacation", "block"):
            gmail    = _make_gmail()
            calendar = _make_calendar()
            handle_allday_block(_parsed(["2026-03-23"], event_type=event_type),
                                gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
            ev = calendar.events_created[0]
            assert ev["transparency"] == "opaque", f"Expected opaque for event_type={event_type!r}"


# ---------------------------------------------------------------------------
# Error path: missing dates
# ---------------------------------------------------------------------------

class TestMissingDates:

    def test_no_proposed_times_sends_error(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        parsed = {
            "intent": "block_time",
            "all_day": True,
            "event_type": "ooo",
            "topic": None,
            "proposed_times": [],
            "new_proposed_times": [],
            "attendees": [],
            "duration_minutes": None,
        }
        action = handle_allday_block(parsed, gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        assert action == "notified-ambiguous"
        assert gmail.has_label("t-allday", "ea-notified")
        assert not calendar.events_created

    def test_empty_datetimes_sends_error(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        parsed   = _parsed([])   # empty datetimes list

        action = handle_allday_block(parsed, gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        assert action == "notified-ambiguous"
        assert not calendar.events_created


# ---------------------------------------------------------------------------
# Topic defaults
# ---------------------------------------------------------------------------

class TestTopicDefaults:

    @pytest.mark.parametrize("event_type,expected_topic", [
        ("ooo",      "Out of Office"),
        ("vacation", "Vacation"),
        ("block",    "All Day Block"),
    ])
    def test_default_topics(self, event_type, expected_topic):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        handle_allday_block(_parsed(["2026-03-23"], event_type=event_type, topic=None),
                            gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        assert calendar.events_created[0]["summary"] == expected_topic

    def test_explicit_topic_overrides_default(self):
        gmail    = _make_gmail()
        calendar = _make_calendar()
        handle_allday_block(_parsed(["2026-03-23"], event_type="ooo", topic="At offsite"),
                            gmail.get_thread("t-allday"), gmail, calendar, CONFIG)
        assert calendar.events_created[0]["summary"] == "At offsite"


# ---------------------------------------------------------------------------
# Dispatch from poll loop (integration)
# ---------------------------------------------------------------------------

class TestPollDispatch:

    def test_allday_dispatched_from_poll(self):
        """poll loop routes all_day=True block_time to handle_allday_block, not evaluate_parsed."""
        from ea.poll import run_poll

        gmail    = _make_gmail("EA: out of office Monday")
        calendar = _make_calendar()
        state    = StateStore(path=None)

        parsed_result = {
            "intent": "block_time",
            "all_day": True,
            "event_type": "ooo",
            "topic": "Out of Office",
            "proposed_times": [{"text": "Monday", "datetimes": ["2026-03-23"]}],
            "new_proposed_times": [],
            "attendees": [],
            "duration_minutes": None,
            "ambiguities": [],
            "urgency": "low",
        }

        summary = run_poll(
            gmail=gmail,
            calendar=calendar,
            state=state,
            config=CONFIG,
            parser=lambda _: parsed_result,
        )

        assert len(calendar.events_created) == 1
        ev = calendar.events_created[0]
        assert ev["start"] == {"date": "2026-03-23"}
        assert ev["transparency"] == "opaque"
        assert summary["pass1"][0]["action"] == "scheduled"

    def test_non_allday_block_still_uses_evaluate_parsed(self):
        """Regular block_time (all_day=False) still goes through evaluate_parsed."""
        from ea.poll import run_poll

        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-block", [
            FakeMsg(MY_EMAIL, MY_EMAIL, "Block lunch", "EA: block Thursday 12-1pm for lunch"),
        ])
        calendar = _make_calendar()
        state    = StateStore(path=None)

        parsed_result = {
            "intent": "block_time",
            "all_day": False,
            "event_type": None,
            "topic": "Lunch",
            "proposed_times": [{"text": "Thursday 12-1pm", "datetimes": ["2026-03-19T16:00:00+00:00"]}],
            "new_proposed_times": [],
            "attendees": [],
            "duration_minutes": 60,
            "ambiguities": [],
            "urgency": "low",
        }

        run_poll(
            gmail=gmail,
            calendar=calendar,
            state=state,
            config=CONFIG,
            parser=lambda _: parsed_result,
        )

        assert len(calendar.events_created) == 1
        ev = calendar.events_created[0]
        # Regular block uses dateTime, not date
        assert "dateTime" in ev["start"] or isinstance(ev["start"], str)
