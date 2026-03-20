"""
Tests for ea.scheduler.check_slot().

Each test constructs a CalendarClient with inline fixture_data so there are
no file I/O or API dependencies.
"""

from datetime import datetime, timedelta, timezone
import pytest

from ea.calendar import CalendarClient
from ea.scheduler import SlotResult, check_slot

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TIMEZONE = "America/New_York"  # UTC-5 (standard), UTC-4 (DST — March is DST)
# March 2026 is in DST, so EST offset is actually -4 (EDT).
# Thu Mar 19 2026 14:00 EST = 18:00 UTC (not 19:00).
# All UTC times below are calculated accordingly.

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

def make_slot(date_str: str, hour: int, duration_minutes: int):
    """Return (start, end) as UTC-aware datetimes for a given local EDT hour."""
    # March 2026 is EDT (UTC-4). So local hour N = UTC hour N+4.
    start = datetime(
        int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]),
        hour + 4, 0, 0, tzinfo=timezone.utc
    )
    end = start + timedelta(minutes=duration_minutes)
    return start, end

def free_calendar(*emails) -> CalendarClient:
    """CalendarClient where every attendee has no busy blocks."""
    return CalendarClient(fixture_data={
        "calendars": {email: {"busy": []} for email in emails}
    })

def busy_calendar(email: str, busy_start: datetime, busy_end: datetime) -> CalendarClient:
    """CalendarClient where one attendee has a single busy block."""
    return CalendarClient(fixture_data={
        "calendars": {
            email: {"busy": [
                {
                    "start": busy_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end":   busy_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ]}
        }
    })


# ---------------------------------------------------------------------------
# Slot type classification
# ---------------------------------------------------------------------------

def test_slot_in_preferred_hours():
    # Thu 2pm–2:30pm EDT — within preferred (10am–4pm)
    start, end = make_slot("2026-03-19", 14, 30)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "preferred"
    assert result.free is True


def test_slot_in_working_hours_outside_preferred():
    # Thu 9am–9:30am EDT — within working (9am–5pm) but outside preferred (10am–4pm)
    start, end = make_slot("2026-03-19", 9, 30)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "working"
    assert result.free is True


def test_slot_after_working_hours():
    # Thu 6pm–6:30pm EDT — past end of working hours (5pm)
    start, end = make_slot("2026-03-19", 18, 30)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "after_hours"
    assert result.free is True


def test_slot_on_weekend():
    # Sat Mar 21, 2pm EDT — Saturday not in working_hours
    start, end = make_slot("2026-03-21", 14, 30)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "after_hours"


def test_slot_partially_outside_working_hours():
    # Thu 4:30pm–5:30pm EDT — starts in working hours, ends after 5pm close
    # EDT = UTC-4, so 4:30pm EDT = 20:30 UTC, 5:30pm EDT = 21:30 UTC
    start = datetime(2026, 3, 19, 20, 30, 0, tzinfo=timezone.utc)
    end   = datetime(2026, 3, 19, 21, 30, 0, tzinfo=timezone.utc)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "after_hours"


def test_slot_spans_midnight():
    # Thu 11:30pm–Fri 12:30am EDT — crosses midnight
    # 11:30pm EDT = 03:30 UTC on Mar 20; 12:30am EDT = 04:30 UTC on Mar 20
    start = datetime(2026, 3, 20, 3, 30, 0, tzinfo=timezone.utc)
    end   = datetime(2026, 3, 20, 4, 30, 0, tzinfo=timezone.utc)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "after_hours"


def test_friday_preferred_hours_end_earlier():
    # Fri 1pm–1:30pm EDT — within Friday preferred (10am–2pm)
    start, end = make_slot("2026-03-20", 13, 30)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "preferred"


def test_friday_outside_preferred_but_within_working():
    # Fri 2:30pm–3pm EDT — past preferred (ends 2pm) but within working (ends 3pm)
    start, end = make_slot("2026-03-20", 14, 30)
    result = check_slot(start, end, ["me@example.com"], WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com"), TIMEZONE)
    assert result.slot_type == "working"


# ---------------------------------------------------------------------------
# Free / busy detection
# ---------------------------------------------------------------------------

def test_free_when_no_conflicts():
    start, end = make_slot("2026-03-19", 14, 30)
    result = check_slot(start, end, ["me@example.com", "sarah@example.com"],
                        WORKING_HOURS, PREFERRED_HOURS,
                        free_calendar("me@example.com", "sarah@example.com"), TIMEZONE)
    assert result.free is True
    assert result.busy_attendees == []


def test_busy_when_attendee_conflicts():
    start, end = make_slot("2026-03-19", 14, 30)
    result = check_slot(start, end, ["me@example.com"],
                        WORKING_HOURS, PREFERRED_HOURS,
                        busy_calendar("me@example.com", start, end), TIMEZONE)
    assert result.free is False
    assert "me@example.com" in result.busy_attendees


def test_only_conflicting_attendee_reported():
    # me is free, sarah is busy
    start, end = make_slot("2026-03-19", 14, 30)
    calendar = CalendarClient(fixture_data={
        "calendars": {
            "me@example.com": {"busy": []},
            "sarah@example.com": {"busy": [
                {
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ]},
        }
    })
    result = check_slot(start, end, ["me@example.com", "sarah@example.com"],
                        WORKING_HOURS, PREFERRED_HOURS, calendar, TIMEZONE)
    assert result.free is False
    assert result.busy_attendees == ["sarah@example.com"]


def test_busy_block_partially_overlaps_slot():
    # Busy block starts before slot ends — counts as conflict
    start, end = make_slot("2026-03-19", 14, 30)   # 2:00–2:30pm
    busy_start = end - timedelta(minutes=10)        # 2:20pm — overlaps end of slot
    busy_end = end + timedelta(minutes=30)          # 3:00pm
    result = check_slot(start, end, ["me@example.com"],
                        WORKING_HOURS, PREFERRED_HOURS,
                        busy_calendar("me@example.com", busy_start, busy_end), TIMEZONE)
    assert result.free is False


def test_busy_block_adjacent_but_not_overlapping():
    # Busy block ends exactly when slot starts — not a conflict
    start, end = make_slot("2026-03-19", 14, 30)   # 2:00–2:30pm
    busy_end = start                                # ends at 2:00pm exactly
    busy_start = busy_end - timedelta(hours=1)
    result = check_slot(start, end, ["me@example.com"],
                        WORKING_HOURS, PREFERRED_HOURS,
                        busy_calendar("me@example.com", busy_start, busy_end), TIMEZONE)
    assert result.free is True
