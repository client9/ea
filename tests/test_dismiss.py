"""
tests/test_dismiss.py

Tests for the ignore/dismiss intent:
  - handle_ignore_result() responder function
  - Pass 1 dispatch (including bypass of the state-skip check)
  - _DISMISS_RE regex coverage

Does NOT test the CLI _run_dismiss() since that requires live Google auth.
"""

import pytest

from ea.calendar import CalendarClient
from ea.poll import _DISMISS_RE, run_poll
from ea.responder import handle_ignore_result
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

MY_EMAIL = "me@example.com"
SARAH = "sarah@example.com"
THU_2PM = "2026-03-19T18:00:00+00:00"

CONFIG = {
    "user": {"email": MY_EMAIL, "name": "Nick"},
    "schedule": {
        "timezone": "America/New_York",
        "working_hours": {"thursday": {"start": "09:00", "end": "17:00"}},
        "preferred_hours": {"thursday": {"start": "10:00", "end": "16:00"}},
    },
}


def free_calendar():
    return CalendarClient(
        fixture_data={"calendars": {MY_EMAIL: {"busy": []}, SARAH: {"busy": []}}}
    )


# ---------------------------------------------------------------------------
# _DISMISS_RE regex
# ---------------------------------------------------------------------------


class TestDismissRegex:
    def _match(self, text):
        return bool(_DISMISS_RE.match(text.strip()))

    def test_ignore(self):
        assert self._match("ignore")

    def test_dismiss(self):
        assert self._match("dismiss")

    def test_never_mind(self):
        assert self._match("never mind")

    def test_forget_it(self):
        assert self._match("forget it")

    def test_case_insensitive(self):
        assert self._match("IGNORE")
        assert self._match("Dismiss")
        assert self._match("Never Mind")

    def test_non_dismiss_not_matched(self):
        assert not self._match("schedule")
        assert not self._match("ignore everything")
        assert not self._match("please ignore")


# ---------------------------------------------------------------------------
# handle_ignore_result unit tests
# ---------------------------------------------------------------------------


class TestHandleIgnoreResult:
    def _seed(self, thread_id="t1", body="EA: dismiss"):
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread(
            thread_id,
            [FakeMsg(MY_EMAIL, MY_EMAIL, "Schedule meeting", body)],
        )
        return gmail

    def test_no_state_sends_confirmation_and_labels(self):
        """Thread with no state entry → permanent suppression."""
        gmail = self._seed("t1")
        state = StateStore(path=None)
        parsed = {"intent": "ignore", "topic": "Mystery meeting"}
        thread = gmail.get_thread("t1")

        action = handle_ignore_result(parsed, thread, gmail, state, CONFIG)

        assert action == "dismissed"
        assert gmail.has_label("t1", "ea-cancelled")
        sent = gmail.sent_to(MY_EMAIL)
        assert any("dismissed" in m.subject for m in sent)

    def test_pending_external_reply_dismissed(self):
        """Owner writes dismiss on the original pending_external_reply thread."""
        gmail = self._seed("t-orig")
        state = StateStore(path=None)
        state.set(
            "t-orig",
            {
                "type": "pending_external_reply",
                "topic": "Coffee chat",
                "attendees": [SARAH],
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )
        parsed = {"intent": "ignore", "topic": "Coffee chat"}
        thread = gmail.get_thread("t-orig")

        action = handle_ignore_result(parsed, thread, gmail, state, CONFIG)

        assert action == "dismissed"
        assert state.get("t-orig") is None  # deleted from state
        assert gmail.has_label("t-orig", "ea-cancelled")

    def test_pending_external_reply_note_about_attendees(self):
        """Confirmation email includes note about no cancellation to external party."""
        gmail = self._seed("t-orig")
        state = StateStore(path=None)
        state.set(
            "t-orig",
            {
                "type": "pending_external_reply",
                "topic": "Coffee chat",
                "attendees": [SARAH],
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )
        parsed = {"intent": "ignore", "topic": "Coffee chat"}
        thread = gmail.get_thread("t-orig")

        handle_ignore_result(parsed, thread, gmail, state, CONFIG)

        sent = gmail.sent_to(MY_EMAIL)
        assert any(SARAH in m.body for m in sent), "Note about external party missing"

    def test_pending_external_reply_no_attendees_no_note(self):
        """No attendees stored → no note appended."""
        gmail = self._seed("t-orig")
        state = StateStore(path=None)
        state.set(
            "t-orig",
            {
                "type": "pending_external_reply",
                "topic": "Focus block",
                "attendees": [],
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )
        parsed = {"intent": "ignore", "topic": "Focus block"}
        thread = gmail.get_thread("t-orig")

        handle_ignore_result(parsed, thread, gmail, state, CONFIG)

        sent = gmail.sent_to(MY_EMAIL)
        assert all("no cancellation" not in m.body for m in sent)

    def test_pending_confirmation_dismissed_via_conf_thread(self):
        """Owner replies 'EA: dismiss' on the confirmation thread.
        handle_ignore_result finds the original thread entry via confirmation_thread_id."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        # Original thread (in state as pending_confirmation)
        gmail.seed_thread(
            "t-orig", [FakeMsg(MY_EMAIL, SARAH, "Meeting?", "EA: schedule")]
        )
        # Confirmation thread (what EA sent back to owner)
        gmail.seed_thread(
            "t-conf",
            [
                FakeMsg(MY_EMAIL, MY_EMAIL, "EA: confirm slot", "Please confirm."),
                FakeMsg(MY_EMAIL, MY_EMAIL, "Re: EA: confirm slot", "EA: dismiss"),
            ],
        )
        state = StateStore(path=None)
        state.set(
            "t-orig",
            {
                "type": "pending_confirmation",
                "confirmation_thread_id": "t-conf",
                "schedule_result": {"topic": "Coffee chat", "attendees": [SARAH]},
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )

        parsed = {"intent": "ignore", "topic": None}
        conf_thread = gmail.get_thread("t-conf")

        action = handle_ignore_result(parsed, conf_thread, gmail, state, CONFIG)

        assert action == "dismissed"
        assert state.get("t-orig") is None  # original entry deleted
        assert gmail.has_label("t-orig", "ea-cancelled")  # label on original thread

    def test_topic_sourced_from_state_when_parsed_topic_is_none(self):
        """topic in confirmation email comes from state entry when parsed has no topic."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread(
            "t-orig", [FakeMsg(MY_EMAIL, SARAH, "Meeting?", "EA: schedule")]
        )
        gmail.seed_thread(
            "t-conf",
            [FakeMsg(MY_EMAIL, MY_EMAIL, "EA: confirm slot", "Please confirm.")],
        )
        state = StateStore(path=None)
        state.set(
            "t-orig",
            {
                "type": "pending_confirmation",
                "confirmation_thread_id": "t-conf",
                "schedule_result": {"topic": "Board review", "attendees": []},
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )

        parsed = {"intent": "ignore", "topic": None}
        conf_thread = gmail.get_thread("t-conf")
        handle_ignore_result(parsed, conf_thread, gmail, state, CONFIG)

        sent = gmail.sent_to(MY_EMAIL)
        assert any("Board review" in m.subject for m in sent)


# ---------------------------------------------------------------------------
# Pass 1 integration: bypass state-skip for dismiss
# ---------------------------------------------------------------------------


class TestDismissInPoll:
    def test_dismiss_pending_external_reply_via_poll(self):
        """'EA: dismiss' on a thread in pending_external_reply state is processed."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread(
            "t-orig",
            [
                FakeMsg(SARAH, MY_EMAIL, "When are you free?", "Can we meet?"),
                FakeMsg(MY_EMAIL, SARAH, "Re: When are you free?", "EA: dismiss"),
            ],
        )
        state = StateStore(path=None)
        state.set(
            "t-orig",
            {
                "type": "pending_external_reply",
                "topic": "Coffee chat",
                "attendees": [SARAH],
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )

        parsed_result = {
            "intent": "ignore",
            "topic": "Coffee chat",
            "attendees": [],
            "proposed_times": [],
            "new_proposed_times": [],
            "duration_minutes": None,
            "meeting_type": None,
            "ambiguities": [],
            "urgency": "low",
            "all_day": False,
            "event_type": None,
            "times_explicitly_specified": False,
        }

        summary = run_poll(
            gmail=gmail,
            calendar=free_calendar(),
            state=state,
            config=CONFIG,
            parser=lambda _: parsed_result,
        )

        assert state.get("t-orig") is None
        assert gmail.has_label("t-orig", "ea-cancelled")
        assert any(item["action"] == "dismissed" for item in summary["pass1"])

    def test_non_dismiss_in_state_still_skipped(self):
        """Normal EA: commands on threads in state are still skipped."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread(
            "t-orig",
            [
                FakeMsg(SARAH, MY_EMAIL, "When are you free?", "Can we meet?"),
                FakeMsg(MY_EMAIL, SARAH, "Re: When are you free?", "EA: schedule it"),
            ],
        )
        state = StateStore(path=None)
        state.set(
            "t-orig",
            {
                "type": "pending_external_reply",
                "topic": "Coffee chat",
                "attendees": [SARAH],
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )

        # parser should never be called for a skipped thread
        parser_calls = []

        def counting_parser(text):
            parser_calls.append(text)
            return {
                "intent": "meeting_request",
                "topic": "x",
                "proposed_times": [],
                "attendees": [],
                "duration_minutes": 30,
                "ambiguities": [],
                "urgency": "low",
                "all_day": False,
                "event_type": None,
                "new_proposed_times": [],
                "times_explicitly_specified": False,
                "meeting_type": None,
            }

        run_poll(
            gmail=gmail,
            calendar=free_calendar(),
            state=state,
            config=CONFIG,
            parser=counting_parser,
        )

        assert len(parser_calls) == 0  # thread was skipped

    def test_thread_with_no_state_dismissed_permanently(self):
        """'EA: ignore' on a thread with no state entry permanently suppresses it."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread(
            "t-new",
            [FakeMsg(MY_EMAIL, MY_EMAIL, "Some noise", "EA: ignore")],
        )
        state = StateStore(path=None)

        parsed_result = {
            "intent": "ignore",
            "topic": "Some noise",
            "attendees": [],
            "proposed_times": [],
            "new_proposed_times": [],
            "duration_minutes": None,
            "meeting_type": None,
            "ambiguities": [],
            "urgency": "low",
            "all_day": False,
            "event_type": None,
            "times_explicitly_specified": False,
        }

        run_poll(
            gmail=gmail,
            calendar=free_calendar(),
            state=state,
            config=CONFIG,
            parser=lambda _: parsed_result,
        )

        assert gmail.has_label("t-new", "ea-cancelled")
        sent = gmail.sent_to(MY_EMAIL)
        assert any("dismissed" in m.subject for m in sent)
