"""
Tests for evaluate_parsed() — the mid-level pipeline coordinator.

All tests bypass the Claude API by passing hand-crafted parsed dicts directly.
process_thread() (which calls the live parser) is covered by integration tests.
"""

from datetime import datetime, timezone
import pytest

from ea.calendar import CalendarClient
from ea.scheduler import ScheduleResult, evaluate_parsed

# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

TIMEZONE = "America/New_York"

WORKING_HOURS = {
    "monday":    {"start": "09:00", "end": "17:00"},
    "tuesday":   {"start": "09:00", "end": "17:00"},
    "wednesday": {"start": "09:00", "end": "17:00"},
    "thursday":  {"start": "09:00", "end": "17:00"},
    "friday":    {"start": "09:00", "end": "15:00"},
}

PREFERRED_HOURS = {
    "monday":    {"start": "10:00", "end": "16:00"},
    "tuesday":   {"start": "10:00", "end": "16:00"},
    "wednesday": {"start": "10:00", "end": "16:00"},
    "thursday":  {"start": "10:00", "end": "16:00"},
    "friday":    {"start": "10:00", "end": "14:00"},
}

MY_EMAIL = "me@example.com"

# Thu Mar 19 2026 2pm EDT = 18:00 UTC  (EDT = UTC-4 in March)
THU_2PM_UTC = "2026-03-19T18:00:00Z"
# Thu Mar 19 2026 7pm EDT = 23:00 UTC  (after hours)
THU_7PM_UTC = "2026-03-19T23:00:00Z"
# Thu Mar 19 2026 9am EDT = 13:00 UTC  (working, not preferred)
THU_9AM_UTC = "2026-03-19T13:00:00Z"


def parsed(
    intent="meeting_request",
    proposed_times=None,
    duration_minutes=30,
    attendees=None,
    ambiguities=None,
    topic="Test meeting",
):
    """Build a minimal parsed dict for testing."""
    if proposed_times is None:
        proposed_times = [{"text": "Thursday at 2pm", "datetimes": [THU_2PM_UTC]}]
    return {
        "intent": intent,
        "topic": topic,
        "attendees": attendees or ["sarah@example.com"],
        "proposed_times": proposed_times,
        "duration_minutes": duration_minutes,
        "ambiguities": ambiguities or [],
        "urgency": "medium",
    }


def free_calendar(*emails) -> CalendarClient:
    return CalendarClient(fixture_data={
        "calendars": {e: {"busy": []} for e in emails}
    })


def busy_at(email: str, iso_start: str, iso_end: str) -> CalendarClient:
    return CalendarClient(fixture_data={
        "calendars": {
            email: {"busy": [{"start": iso_start, "end": iso_end}]}
        }
    })


def call(parsed_dict, calendar) -> ScheduleResult:
    return evaluate_parsed(
        parsed=parsed_dict,
        working_hours=WORKING_HOURS,
        preferred_hours=PREFERRED_HOURS,
        timezone=TIMEZONE,
        calendar=calendar,
        my_email=MY_EMAIL,
    )


# ---------------------------------------------------------------------------
# Ambiguous outcomes
# ---------------------------------------------------------------------------

def test_ambiguous_when_intent_is_none():
    result = call(parsed(intent="none"), free_calendar(MY_EMAIL))
    assert result.outcome == "ambiguous"
    assert result.ambiguities


def test_ambiguous_when_parser_returns_ambiguities():
    result = call(
        parsed(ambiguities=["Which Thursday?", "No duration specified"]),
        free_calendar(MY_EMAIL, "sarah@example.com"),
    )
    assert result.outcome == "ambiguous"
    assert "Which Thursday?" in result.ambiguities


def test_ambiguous_when_no_proposed_times():
    result = call(parsed(proposed_times=[]), free_calendar(MY_EMAIL))
    assert result.outcome == "ambiguous"


def test_ambiguous_when_no_duration():
    result = call(parsed(duration_minutes=None), free_calendar(MY_EMAIL))
    assert result.outcome == "ambiguous"


# ---------------------------------------------------------------------------
# Open outcomes
# ---------------------------------------------------------------------------

def test_open_preferred_slot():
    result = call(
        parsed(),
        free_calendar(MY_EMAIL, "sarah@example.com"),
    )
    assert result.outcome == "open"
    assert result.slot_type == "preferred"
    assert result.slot_start is not None


def test_open_working_slot():
    # 9am is in working hours but outside preferred (10am–4pm)
    p = parsed(proposed_times=[{"text": "Thursday at 9am", "datetimes": [THU_9AM_UTC]}])
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.outcome == "open"
    assert result.slot_type == "working"


def test_open_block_time_uses_my_email():
    # block_time has no attendees — my_email should be injected automatically
    p = parsed(
        intent="block_time",
        attendees=[],
        proposed_times=[{"text": "Thursday at 2pm", "datetimes": [THU_2PM_UTC]}],
    )
    result = call(p, free_calendar(MY_EMAIL))
    assert result.outcome == "open"
    assert MY_EMAIL in result.attendees


def test_open_picks_preferred_over_working():
    # Two slots: first is working-hours, second is preferred.
    # Should pick the preferred one even though it's listed second.
    p = parsed(proposed_times=[
        {"text": "Thursday at 9am",  "datetimes": [THU_9AM_UTC]},   # working
        {"text": "Thursday at 2pm",  "datetimes": [THU_2PM_UTC]},   # preferred
    ])
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.outcome == "open"
    assert result.slot_type == "preferred"


# ---------------------------------------------------------------------------
# Needs confirmation
# ---------------------------------------------------------------------------

def test_needs_confirmation_for_after_hours_slot():
    p = parsed(proposed_times=[{"text": "Thursday at 7pm", "datetimes": [THU_7PM_UTC]}])
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.outcome == "needs_confirmation"
    assert result.slot_type == "after_hours"


def test_needs_confirmation_only_when_all_free_slots_are_after_hours():
    # Two slots: the working-hours one is busy, only after-hours is free.
    p = parsed(proposed_times=[
        {"text": "Thursday at 2pm",  "datetimes": [THU_2PM_UTC]},   # preferred, but busy
        {"text": "Thursday at 7pm",  "datetimes": [THU_7PM_UTC]},   # after-hours, free
    ])
    # Make the preferred slot busy for sarah, free for me; she's the bottleneck
    calendar = CalendarClient(fixture_data={"calendars": {
        MY_EMAIL:              {"busy": []},
        "sarah@example.com":   {"busy": [{"start": THU_2PM_UTC,
                                           "end":   "2026-03-19T18:30:00Z"}]},
    }})
    result = call(p, calendar)
    assert result.outcome == "needs_confirmation"
    assert result.slot_type == "after_hours"


# ---------------------------------------------------------------------------
# Explicit times skip needs_confirmation
# ---------------------------------------------------------------------------

def test_explicit_time_after_hours_books_directly():
    p = parsed(proposed_times=[{"text": "Thursday at 7pm", "datetimes": [THU_7PM_UTC]}])
    p["times_explicitly_specified"] = True
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.outcome == "open"
    assert result.slot_type == "after_hours"


def test_explicit_time_after_hours_busy_returns_busy():
    p = parsed(proposed_times=[{"text": "Thursday at 7pm", "datetimes": [THU_7PM_UTC]}])
    p["times_explicitly_specified"] = True
    result = call(p, busy_at("sarah@example.com", THU_7PM_UTC, "2026-03-19T23:30:00Z"))
    assert result.outcome == "busy"
    assert "sarah@example.com" in result.busy_attendees


def test_non_explicit_time_after_hours_still_needs_confirmation():
    p = parsed(proposed_times=[{"text": "Thursday at 7pm", "datetimes": [THU_7PM_UTC]}])
    p["times_explicitly_specified"] = False
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.outcome == "needs_confirmation"
    assert result.slot_type == "after_hours"


def test_explicit_time_preferred_hours_unchanged():
    p = parsed()  # default THU_2PM_UTC (preferred)
    p["times_explicitly_specified"] = True
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.outcome == "open"
    assert result.slot_type == "preferred"


def test_explicit_time_working_hours_unchanged():
    p = parsed(proposed_times=[{"text": "Thursday at 9am", "datetimes": [THU_9AM_UTC]}])
    p["times_explicitly_specified"] = True
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.outcome == "open"
    assert result.slot_type == "working"


# ---------------------------------------------------------------------------
# Busy outcome
# ---------------------------------------------------------------------------

def test_busy_when_all_slots_conflict():
    p = parsed(proposed_times=[{"text": "Thursday at 2pm", "datetimes": [THU_2PM_UTC]}])
    result = call(
        p,
        busy_at("sarah@example.com", THU_2PM_UTC, "2026-03-19T18:30:00Z"),
    )
    assert result.outcome == "busy"
    assert "sarah@example.com" in result.busy_attendees


def test_busy_collects_all_conflicting_attendees():
    p = parsed(
        attendees=["sarah@example.com", "bob@example.com"],
        proposed_times=[{"text": "Thursday at 2pm", "datetimes": [THU_2PM_UTC]}],
    )
    calendar = CalendarClient(fixture_data={"calendars": {
        MY_EMAIL:             {"busy": []},
        "sarah@example.com":  {"busy": [{"start": THU_2PM_UTC, "end": "2026-03-19T18:30:00Z"}]},
        "bob@example.com":    {"busy": [{"start": THU_2PM_UTC, "end": "2026-03-19T18:30:00Z"}]},
    }})
    result = call(p, calendar)
    assert result.outcome == "busy"
    assert set(result.busy_attendees) == {"sarah@example.com", "bob@example.com"}


def test_busy_skips_free_slots_that_have_zero_datetimes():
    # proposed_times entry with empty datetimes list should be skipped gracefully
    p = parsed(proposed_times=[
        {"text": "sometime Thursday", "datetimes": []},
    ])
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    # No datetimes to check → treated as ambiguous (no times to evaluate)
    assert result.outcome == "ambiguous"


# ---------------------------------------------------------------------------
# Parsed dict is always present for debugging
# ---------------------------------------------------------------------------

def test_parsed_dict_always_in_result():
    p = parsed()
    result = call(p, free_calendar(MY_EMAIL, "sarah@example.com"))
    assert result.parsed is p
