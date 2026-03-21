"""
Tests for handle_confirmation_reply() and _apply_modification().

Covers:
  - yes/no/nevermind paths
  - calendar error on yes-confirmation
  - unrecognised reply without evaluate_fn
  - all four _apply_modification() outcome branches
"""

from datetime import datetime, timezone

from ea.calendar import CalendarClient
from ea.responder import handle_confirmation_reply
from ea.scheduler import ScheduleResult
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

MY_EMAIL = "me@example.com"
ORIGINAL_THREAD_ID = "orig-t1"
CONF_THREAD_ID = "conf-t1"

CONFIG = {
    "user": {"email": MY_EMAIL},
    "schedule": {"timezone": "America/New_York"},
}

SLOT_START = "2026-03-19T23:00:00+00:00"  # 7pm EDT — after hours
SLOT_END = "2026-03-19T23:30:00+00:00"


def make_entry():
    return {
        "confirmation_thread_id": CONF_THREAD_ID,
        "schedule_result": {
            "outcome": "needs_confirmation",
            "slot_start": SLOT_START,
            "slot_end": SLOT_END,
            "slot_type": "after_hours",
            "topic": "Standup",
            "attendees": ["sarah@example.com"],
            "duration_minutes": 30,
        },
    }


def make_gmail():
    gmail = FakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread(
        ORIGINAL_THREAD_ID,
        [FakeMsg("sarah@example.com", MY_EMAIL, "Meeting?", "Can we meet?")],
    )
    gmail.seed_thread(
        CONF_THREAD_ID,
        [FakeMsg(MY_EMAIL, MY_EMAIL, "EA: confirm slot", "After hours — ok?")],
    )
    return gmail


def free_calendar():
    return CalendarClient(fixture_data={"calendars": {}})


class ErrorCalendar(CalendarClient):
    """CalendarClient that raises on create_event."""

    def __init__(self):
        super().__init__(fixture_data={"calendars": {}})

    def create_event(self, **kwargs):
        raise RuntimeError("API quota exceeded")


# ---------------------------------------------------------------------------
# Yes path — success
# ---------------------------------------------------------------------------


def test_yes_books_event():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())
    calendar = free_calendar()

    action = handle_confirmation_reply(
        "yes", ORIGINAL_THREAD_ID, make_entry(), gmail, calendar, state, CONFIG
    )

    assert action == "scheduled"
    assert gmail.has_label(ORIGINAL_THREAD_ID, "ea-scheduled")
    assert state.get(ORIGINAL_THREAD_ID) is None  # state cleaned up


def test_yes_variations_all_book():
    for phrase in ("go ahead", "confirm", "ok", "sounds good"):
        gmail = make_gmail()
        state = StateStore(path=None)
        state.set(ORIGINAL_THREAD_ID, make_entry())
        calendar = free_calendar()

        action = handle_confirmation_reply(
            phrase, ORIGINAL_THREAD_ID, make_entry(), gmail, calendar, state, CONFIG
        )
        assert action == "scheduled", f"expected scheduled for '{phrase}'"


# ---------------------------------------------------------------------------
# Yes path — calendar error
# ---------------------------------------------------------------------------


def test_yes_calendar_error_sends_email():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    action = handle_confirmation_reply(
        "yes", ORIGINAL_THREAD_ID, make_entry(), gmail, ErrorCalendar(), state, CONFIG
    )

    assert action == "calendar-error"
    sent = gmail.sent_to(MY_EMAIL)
    last = sent[-1]
    assert "calendar error" in last.subject
    assert "quota exceeded" in last.body


def test_yes_calendar_error_state_not_deleted():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    handle_confirmation_reply(
        "yes", ORIGINAL_THREAD_ID, make_entry(), gmail, ErrorCalendar(), state, CONFIG
    )

    assert state.get(ORIGINAL_THREAD_ID) is not None  # state preserved on error


# ---------------------------------------------------------------------------
# No / cancel path
# ---------------------------------------------------------------------------


def test_no_cancels():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    action = handle_confirmation_reply(
        "no", ORIGINAL_THREAD_ID, make_entry(), gmail, free_calendar(), state, CONFIG
    )

    assert action == "cancelled"
    assert gmail.has_label(ORIGINAL_THREAD_ID, "ea-cancelled")
    assert state.get(ORIGINAL_THREAD_ID) is None


def test_nevermind_cancels():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    action = handle_confirmation_reply(
        "nevermind",
        ORIGINAL_THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
    )

    assert action == "cancelled"


# ---------------------------------------------------------------------------
# Fallback: unrecognised reply, no evaluate_fn
# ---------------------------------------------------------------------------


def test_unrecognised_reply_without_evaluate_fn():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    action = handle_confirmation_reply(
        "maybe tomorrow?",
        ORIGINAL_THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
    )

    assert action == "still-unclear"
    sent = gmail.sent_to(MY_EMAIL)
    last = sent[-1]
    assert (
        "still unclear" in last.subject.lower() or "still unclear" in last.body.lower()
    )


# ---------------------------------------------------------------------------
# _apply_modification() — 4 outcome branches via evaluate_fn
# ---------------------------------------------------------------------------


def _make_slot_result(outcome, **kwargs):
    start = datetime(2026, 3, 20, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 20, 15, 30, 0, tzinfo=timezone.utc)
    return ScheduleResult(
        outcome=outcome,
        slot_start=start,
        slot_end=end,
        slot_type="preferred",
        topic="Standup",
        attendees=["sarah@example.com"],
        duration_minutes=30,
        **kwargs,
    )


def test_modify_open_schedules():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    def evaluate_fn(text, entry):
        return _make_slot_result("open")

    action = handle_confirmation_reply(
        "how about Friday at 10am?",
        ORIGINAL_THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        evaluate_fn=evaluate_fn,
    )

    assert action == "scheduled"
    assert gmail.has_label(ORIGINAL_THREAD_ID, "ea-scheduled")
    assert state.get(ORIGINAL_THREAD_ID) is None


def test_modify_busy_notifies():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    def evaluate_fn(text, entry):
        return _make_slot_result("busy", busy_attendees=["sarah@example.com"])

    action = handle_confirmation_reply(
        "Friday at 10am?",
        ORIGINAL_THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        evaluate_fn=evaluate_fn,
    )

    assert action == "still-busy"
    sent = gmail.sent_to(MY_EMAIL)
    assert any("still busy" in m.subject for m in sent)


def test_modify_ambiguous_notifies():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())

    def evaluate_fn(text, entry):
        return ScheduleResult(outcome="ambiguous", ambiguities=["Which Friday?"])

    action = handle_confirmation_reply(
        "sometime Friday",
        ORIGINAL_THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        evaluate_fn=evaluate_fn,
    )

    assert action == "still-ambiguous"
    sent = gmail.sent_to(MY_EMAIL)
    assert any("Which Friday?" in m.body for m in sent)


def test_modify_needs_confirmation_updates_state():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(ORIGINAL_THREAD_ID, make_entry())
    new_start = datetime(2026, 3, 20, 23, 0, 0, tzinfo=timezone.utc)  # after hours
    new_end = datetime(2026, 3, 20, 23, 30, 0, tzinfo=timezone.utc)

    def evaluate_fn(text, entry):
        return ScheduleResult(
            outcome="needs_confirmation",
            slot_start=new_start,
            slot_end=new_end,
            slot_type="after_hours",
            topic="Standup",
            attendees=["sarah@example.com"],
            duration_minutes=30,
        )

    action = handle_confirmation_reply(
        "Friday evening",
        ORIGINAL_THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        evaluate_fn=evaluate_fn,
    )

    assert action == "needs-confirmation-updated"
    sent = gmail.sent_to(MY_EMAIL)
    assert any("new proposed slot" in m.subject for m in sent)
    # State should be updated with the new slot
    entry = state.get(ORIGINAL_THREAD_ID)
    assert entry is not None
    assert entry["schedule_result"]["slot_start"] == new_start.isoformat()
