"""
Tests for small responder helper functions:
  _send_calendar_error()
  _handle_event_not_found()
  _handle_event_ambiguous()
  _local_slot_desc()
  handle_block_time_result() — ambiguous outcome
"""

from datetime import datetime, timezone

from ea.calendar import CalendarClient
from ea.responder import (
    _handle_event_ambiguous,
    _handle_event_not_found,
    _local_slot_desc,
    _send_calendar_error,
    handle_block_time_result,
)
from ea.scheduler import ScheduleResult
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

MY_EMAIL = "me@example.com"
THREAD_ID = "t1"
SUBJECT = "Team standup"

CONFIG = {
    "user": {"email": MY_EMAIL},
    "schedule": {"timezone": "America/New_York"},
}

# Thu Mar 19 2026 2:00–2:30 PM UTC (preferred hours in EDT)
SLOT_START = datetime(2026, 3, 19, 18, 0, 0, tzinfo=timezone.utc)
SLOT_END = datetime(2026, 3, 19, 18, 30, 0, tzinfo=timezone.utc)


def make_gmail():
    gmail = FakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread(
        thread_id=THREAD_ID,
        messages=[FakeMsg(MY_EMAIL, "sarah@example.com", SUBJECT, "EA: block time")],
    )
    return gmail


def free_calendar():
    return CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}})


# ---------------------------------------------------------------------------
# _send_calendar_error
# ---------------------------------------------------------------------------


def test_send_calendar_error_sends_email():
    gmail = make_gmail()
    exc = RuntimeError("quota exceeded")
    result = _send_calendar_error(
        gmail, MY_EMAIL, THREAD_ID, SUBJECT, "book meeting", exc
    )

    assert result == "calendar-error"
    sent = gmail.sent_to(MY_EMAIL)
    assert len(sent) == 1
    assert f"EA: calendar error — {SUBJECT}" == sent[0].subject
    assert "book meeting" in sent[0].body
    assert "quota exceeded" in sent[0].body


def test_send_calendar_error_applies_label():
    gmail = make_gmail()
    _send_calendar_error(
        gmail, MY_EMAIL, THREAD_ID, SUBJECT, "book meeting", Exception("x")
    )
    assert gmail.has_label(THREAD_ID, "ea-notified")


# ---------------------------------------------------------------------------
# _handle_event_not_found
# ---------------------------------------------------------------------------


def test_handle_event_not_found_notifies():
    gmail = make_gmail()
    result = _handle_event_not_found(gmail, MY_EMAIL, THREAD_ID, "Weekly Standup")

    assert result == "notified-not-found"
    sent = gmail.sent_to(MY_EMAIL)
    assert len(sent) == 1
    assert "EA: event not found — Weekly Standup" == sent[0].subject
    assert "Weekly Standup" in sent[0].body
    assert gmail.has_label(THREAD_ID, "ea-notified")


# ---------------------------------------------------------------------------
# _handle_event_ambiguous
# ---------------------------------------------------------------------------


def test_handle_event_ambiguous_lists_events():
    gmail = make_gmail()
    match = [
        {"summary": "Standup", "start": {"dateTime": "2026-03-19T14:00:00Z"}},
        {"summary": "Standup", "start": {"dateTime": "2026-03-20T14:00:00Z"}},
    ]
    result = _handle_event_ambiguous(
        gmail, MY_EMAIL, THREAD_ID, "Standup", match, "cancel"
    )

    assert result == "notified-ambiguous"
    sent = gmail.sent_to(MY_EMAIL)
    assert len(sent) == 1
    assert "EA: ambiguous cancel — Standup" == sent[0].subject
    assert "Standup" in sent[0].body
    assert "2026-03-19" in sent[0].body
    assert gmail.has_label(THREAD_ID, "ea-notified")


# ---------------------------------------------------------------------------
# _local_slot_desc
# ---------------------------------------------------------------------------


def test_local_slot_desc_single_tz():
    desc = _local_slot_desc(SLOT_START, SLOT_END, CONFIG)
    assert "for them" not in desc
    # Should include the date and time in EDT
    assert "Mar" in desc or "2026" in desc or "PM" in desc


def test_local_slot_desc_dual_tz():
    # Owner is in New York (EDT), attendee is in LA (PDT) — different TZs
    desc = _local_slot_desc(
        SLOT_START, SLOT_END, CONFIG, attendee_tz="America/Los_Angeles"
    )
    assert "for them" in desc
    # Both timezone abbreviations should appear
    assert "EDT" in desc or "ET" in desc


def test_local_slot_desc_invalid_attendee_tz():
    # Should not raise; silently omit dual-timezone annotation
    desc = _local_slot_desc(SLOT_START, SLOT_END, CONFIG, attendee_tz="Not/ATimezone")
    assert "for them" not in desc


def test_local_slot_desc_same_tz_no_annotation():
    # When attendee_tz matches owner tz, no dual annotation
    desc = _local_slot_desc(
        SLOT_START, SLOT_END, CONFIG, attendee_tz="America/New_York"
    )
    assert "for them" not in desc


# ---------------------------------------------------------------------------
# handle_block_time_result — ambiguous outcome
# ---------------------------------------------------------------------------


def test_block_time_ambiguous_notifies():
    gmail = make_gmail()
    thread = gmail.get_thread(THREAD_ID)
    state = StateStore(path=None)
    calendar = free_calendar()

    result_sr = ScheduleResult(
        outcome="ambiguous",
        ambiguities=["No time specified", "Duration unclear"],
    )

    action = handle_block_time_result(result_sr, thread, gmail, calendar, state, CONFIG)

    assert action == "notified-ambiguous"
    sent = gmail.sent_to(MY_EMAIL)
    assert len(sent) == 1
    assert "needs more info" in sent[0].subject
    assert "No time specified" in sent[0].body
    assert "Duration unclear" in sent[0].body
    assert gmail.has_label(THREAD_ID, "ea-notified")
