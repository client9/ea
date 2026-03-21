"""
Tests for handle_external_reply() — slot_taken and counter paths.

The confirmed (accept) path is covered by test_state_machine.py.
These tests focus on the two paths that weren't covered:
  - slot_taken: chosen slot is no longer available → send alternatives
  - counter: recipient proposes a different time → send new suggestions
"""

from ea.calendar import CalendarClient
from ea.responder import handle_external_reply
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

MY_EMAIL = "me@example.com"
SARAH = "sarah@example.com"
THREAD_ID = "t1"

CONFIG = {
    "user": {"email": MY_EMAIL},
    "schedule": {"timezone": "America/New_York"},
}

ORIGINAL_SLOT = {
    "start": "2026-03-19T18:00:00Z",
    "end": "2026-03-19T18:30:00Z",
    "slot_type": "preferred",
}

NEW_SLOTS = [
    {
        "start": "2026-03-20T15:00:00Z",
        "end": "2026-03-20T15:30:00Z",
        "slot_type": "preferred",
    },
    {
        "start": "2026-03-20T16:00:00Z",
        "end": "2026-03-20T16:30:00Z",
        "slot_type": "preferred",
    },
]


def make_entry():
    return {
        "recipient": SARAH,
        "subject": "Re: Team meeting",
        "suggested_slots": [ORIGINAL_SLOT],
        "attendees": [SARAH],
        "topic": "Team meeting",
    }


def make_gmail():
    gmail = FakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread(
        THREAD_ID,
        [FakeMsg(MY_EMAIL, SARAH, "Re: Team meeting", "Here are some times...")],
    )
    return gmail


def free_calendar():
    return CalendarClient(
        fixture_data={"calendars": {MY_EMAIL: {"busy": []}, SARAH: {"busy": []}}}
    )


# ---------------------------------------------------------------------------
# slot_taken — chosen slot is now busy
# ---------------------------------------------------------------------------


def test_slot_taken_returns_correct_action():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(THREAD_ID, make_entry())

    def find_slots_fn(reply, entry):
        return ("slot_taken", NEW_SLOTS)

    action = handle_external_reply(
        "I'll take Thursday at 2pm",
        THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        find_slots_fn=find_slots_fn,
    )

    assert action == "slot-taken-new-options"


def test_slot_taken_sends_alternatives_to_recipient():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(THREAD_ID, make_entry())

    def find_slots_fn(reply, entry):
        return ("slot_taken", NEW_SLOTS)

    handle_external_reply(
        "Thursday 2pm works",
        THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        find_slots_fn=find_slots_fn,
    )

    sent = gmail.sent_to(SARAH)
    assert "just taken" in sent[-1].body


def test_slot_taken_updates_state_with_new_slots():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(THREAD_ID, make_entry())

    def find_slots_fn(reply, entry):
        return ("slot_taken", NEW_SLOTS)

    handle_external_reply(
        "Thursday 2pm",
        THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        find_slots_fn=find_slots_fn,
    )

    entry = state.get(THREAD_ID)
    assert entry is not None
    assert entry["suggested_slots"] == NEW_SLOTS


# ---------------------------------------------------------------------------
# counter — recipient proposes a different time
# ---------------------------------------------------------------------------


def test_counter_returns_correct_action():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(THREAD_ID, make_entry())

    def find_slots_fn(reply, entry):
        return ("counter", NEW_SLOTS)

    action = handle_external_reply(
        "Actually could we do Friday instead?",
        THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        find_slots_fn=find_slots_fn,
    )

    assert action == "counter-new-options"


def test_counter_sends_new_slots_to_recipient():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(THREAD_ID, make_entry())

    def find_slots_fn(reply, entry):
        return ("counter", NEW_SLOTS)

    handle_external_reply(
        "Can we do Friday?",
        THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        find_slots_fn=find_slots_fn,
    )

    assert len(gmail.sent_to(SARAH)) >= 1


def test_counter_updates_state_with_new_slots():
    gmail = make_gmail()
    state = StateStore(path=None)
    state.set(THREAD_ID, make_entry())

    def find_slots_fn(reply, entry):
        return ("counter", NEW_SLOTS)

    handle_external_reply(
        "Friday?",
        THREAD_ID,
        make_entry(),
        gmail,
        free_calendar(),
        state,
        CONFIG,
        find_slots_fn=find_slots_fn,
    )

    entry = state.get(THREAD_ID)
    assert entry is not None
    assert entry["suggested_slots"] == NEW_SLOTS
