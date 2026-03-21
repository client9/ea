"""
tests/test_pass1_error.py

Tests for Pass 1 error handling and reply-context EA: commands.

Covers:
  - Parser exception → owner notified, thread labeled ea-notified
  - Parser exception → ea-notified label gates retry on next poll cycle
  - Reply-context command ("EA: book it") resolved via full thread text
  - Reply-context command with ambiguous outcome → "needs more info" email
  - intent="none" from parser → "could not parse" email sent to owner
"""

from ea.calendar import CalendarClient
from ea.poll import run_poll
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

MY_EMAIL = "me@example.com"
BOB      = "bob@example.com"

CONFIG = {
    "user": {"email": MY_EMAIL},
    "schedule": {
        "timezone": "America/New_York",
        "working_hours":  {"thursday": {"start": "09:00", "end": "17:00"}},
        "preferred_hours": {"thursday": {"start": "10:00", "end": "16:00"}},
    },
}

# Thursday 2026-03-19 2pm–3pm EDT = 18:00–19:00 UTC
SLOT_START = "2026-03-19T18:00:00+00:00"
SLOT_END   = "2026-03-19T19:00:00+00:00"

FREE_CAL = CalendarClient(fixture_data={
    "calendars": {
        MY_EMAIL: {"busy": []},
        BOB:      {"busy": []},
    }
})


def _reply_context_thread(thread_id="t1"):
    """Bob asks 'Are you free Thursday 2-3pm?', owner replies 'EA: book it'."""
    gmail = FakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread(thread_id, [
        FakeMsg(BOB,      MY_EMAIL, "Quick meeting?", "Are you free Thursday 2-3pm?"),
        FakeMsg(MY_EMAIL, BOB,      "Re: Quick meeting?", "EA: book it"),
    ])
    return gmail


# ---------------------------------------------------------------------------
# Bug fix: exception during parsing notifies owner and labels thread
# ---------------------------------------------------------------------------

class TestParserException:

    def test_owner_notified_on_parser_exception(self):
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=lambda _: (_ for _ in ()).throw(RuntimeError("Claude API timeout")),
        )

        sent = gmail.sent_to(MY_EMAIL)
        assert any("error processing" in m.subject for m in sent), \
            "Owner should receive an error notification email"

    def test_ea_notified_label_applied_on_exception(self):
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=lambda _: (_ for _ in ()).throw(RuntimeError("timeout")),
        )

        assert gmail.has_label("t1", "ea-notified"), \
            "ea-notified label must be applied so the thread is not retried"

    def test_action_is_notified_error_in_summary(self):
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        summary = run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        actions = [item["action"] for item in summary["pass1"]]
        assert "notified-error" in actions

    def test_exception_gates_retry_on_next_poll(self):
        """After ea-notified is applied, a second poll cycle skips the thread."""
        gmail = _reply_context_thread()
        state = StateStore(path=None)
        call_count = {"n": 0}

        def counting_parser(_):
            call_count["n"] += 1
            raise RuntimeError("always fails")

        run_poll(gmail, FREE_CAL, state, CONFIG, parser=counting_parser)
        assert call_count["n"] == 1

        # Second poll — thread is labeled ea-notified, list_threads excludes it
        run_poll(gmail, FREE_CAL, state, CONFIG, parser=counting_parser)
        assert call_count["n"] == 1, \
            "Parser should not be called again after ea-notified is applied"

    def test_dry_run_suppresses_notification_on_exception(self):
        """In dry-run mode, no email should be sent even when parsing fails."""
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
            dry_run=True,
        )

        sent = gmail.sent_to(MY_EMAIL)
        assert not any("error processing" in m.subject for m in sent)


# ---------------------------------------------------------------------------
# Reply-context commands: "EA: book it" resolved via full thread text
# ---------------------------------------------------------------------------

class TestReplyContextCommand:

    def _booked_parser(self, _text):
        """Simulates parser correctly reading thread context."""
        return {
            "intent": "meeting_request",
            "topic": "Quick meeting",
            "attendees": [BOB],
            "proposed_times": [{"text": "Thursday 2-3pm", "datetimes": [SLOT_START]}],
            "duration_minutes": 60,
            "ambiguities": [],
            "timezone": None,
        }

    def test_reply_context_schedules_meeting(self):
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=self._booked_parser,
        )

        assert gmail.has_label("t1", "ea-scheduled")
        sent = gmail.sent_to(MY_EMAIL)
        assert any("booked" in m.subject.lower() for m in sent)

    def test_reply_context_ambiguous_notifies_owner(self):
        """Parser reads the thread but can't extract a time → ambiguous."""
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        def ambiguous_parser(_):
            return {
                "intent": "meeting_request",
                "topic": "Quick meeting",
                "attendees": [BOB],
                "proposed_times": [],
                "duration_minutes": None,
                "ambiguities": ["No proposed time could be determined from the thread."],
                "timezone": None,
            }

        run_poll(gmail, FREE_CAL, state, CONFIG, parser=ambiguous_parser)

        sent = gmail.sent_to(MY_EMAIL)
        assert any("needs more info" in m.subject.lower() for m in sent), \
            "Ambiguous parse should result in 'needs more info' email to owner"
        assert gmail.has_label("t1", "ea-notified")


# ---------------------------------------------------------------------------
# intent="none" path: existing behaviour locked in
# ---------------------------------------------------------------------------

class TestIntentNone:

    def test_intent_none_sends_could_not_parse_email(self):
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=lambda _: {"intent": "none", "topic": None},
        )

        sent = gmail.sent_to(MY_EMAIL)
        assert any("could not parse" in m.subject.lower() for m in sent)

    def test_intent_none_applies_ea_notified(self):
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=lambda _: {"intent": "none", "topic": None},
        )

        assert gmail.has_label("t1", "ea-notified")

    def test_intent_none_email_contains_parsed_json(self):
        """The parse-error email body should include the raw parsed output."""
        gmail = _reply_context_thread()
        state = StateStore(path=None)

        run_poll(
            gmail, FREE_CAL, state, CONFIG,
            parser=lambda _: {"intent": "none", "topic": None, "debug_field": "present"},
        )

        sent = gmail.sent_to(MY_EMAIL)
        error_emails = [m for m in sent if "could not parse" in m.subject.lower()]
        assert error_emails
        assert "debug_field" in error_emails[0].body, \
            "Parsed JSON should be included in the error email body"
