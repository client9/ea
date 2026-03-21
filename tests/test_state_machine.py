"""
tests/test_state_machine.py

Scenario-based tests for the EA state machine. These tests exercise the
full poll loop (Pass 1 / 2 / 3 + expiry) without touching Gmail, Google
Calendar, or the Claude API.

Fixtures injected:
  - FakeGmailClient     — in-memory inbox / sent / labels
  - CalendarClient(fixture_data=...)  — canned freebusy + event tracking
  - StateStore(path=None)   — in-memory, no disk I/O
  - parser= lambda         — returns a hand-crafted parsed dict
  - confirm_eval_fn= lambda — returns a ScheduleResult for modification replies
  - external_reply_fn= lambda — classifies outbound reply + returns new slots

Dates used throughout:
  Thu 2026-03-19 2pm EDT  = 18:00 UTC  (preferred hours → open)
  Thu 2026-03-19 7pm EDT  = 23:00 UTC  (after hours → needs_confirmation)
  Thu 2026-03-19 9am EDT  = 13:00 UTC  (working, not preferred)
"""

from datetime import datetime, timezone, timedelta

from ea.calendar import CalendarClient
from ea.poll import run_poll
from ea.scheduler import ScheduleResult
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg, NewThreadFakeGmailClient

# ---------------------------------------------------------------------------
# Shared config / constants
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

THU_2PM = "2026-03-19T18:00:00+00:00"   # preferred
THU_7PM = "2026-03-19T23:00:00+00:00"   # after hours
THU_2PM_30 = "2026-03-19T18:30:00+00:00"

FREE_CALENDAR = CalendarClient(fixture_data={
    "calendars": {
        MY_EMAIL: {"busy": []},
        SARAH: {"busy": []},
    }
})


def make_inbound_thread(thread_id="t1"):
    """Seed: Sarah emails me asking for a meeting, I reply EA: please schedule."""
    gmail = FakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread(thread_id, [
        FakeMsg(SARAH, MY_EMAIL, "Coffee chat?",
                "Hey, can we grab a coffee this week? Thursday at 2pm works for me."),
        FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?",
                "EA: please schedule"),
    ])
    return gmail


def parsed_open(topic="Coffee chat", proposed_time=THU_2PM):
    return {
        "intent": "meeting_request",
        "topic": topic,
        "attendees": [SARAH],
        "proposed_times": [{"text": "Thursday at 2pm", "datetimes": [proposed_time]}],
        "duration_minutes": 30,
        "ambiguities": [],
        "urgency": "medium",
    }


def parsed_after_hours(topic="Coffee chat"):
    return {
        "intent": "meeting_request",
        "topic": topic,
        "attendees": [SARAH],
        "proposed_times": [{"text": "Thursday at 7pm", "datetimes": [THU_7PM]}],
        "duration_minutes": 30,
        "ambiguities": [],
        "urgency": "medium",
    }


def parsed_explicit_after_hours():
    return {
        "intent": "meeting_request",
        "topic": "Coffee chat",
        "attendees": [SARAH],
        "proposed_times": [{"text": "Thursday at 7pm", "datetimes": [THU_7PM]}],
        "new_proposed_times": [],
        "duration_minutes": 30,
        "ambiguities": [],
        "urgency": "medium",
        "all_day": False,
        "event_type": None,
        "times_explicitly_specified": True,
    }


# ---------------------------------------------------------------------------
# Pass 1 — Inbound: new EA: triggers
# ---------------------------------------------------------------------------

class TestPass1Inbound:

    def test_open_slot_schedules_event(self):
        gmail = make_inbound_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_open())

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t1") is None   # no pending state
        # EA should send me a confirmation email so I know it was booked
        sent = gmail.sent_to(MY_EMAIL)
        assert any("booked" in m.subject.lower() for m in sent)

    def test_open_slot_confirmation_shows_attendee_tz(self):
        """When attendee timezone differs from owner's, both appear in the booked email."""
        gmail = make_inbound_thread()
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        state = StateStore(path=None)

        parsed = parsed_open()
        parsed["timezone"] = "America/Los_Angeles"   # attendee is Pacific; owner is Eastern (CONFIG)

        run_poll(gmail, calendar, state, CONFIG, parser=lambda _: parsed)

        sent = gmail.sent_to(MY_EMAIL)
        booked = [m for m in sent if "booked" in m.subject.lower()]
        assert booked
        body = booked[0].body
        # Owner's tz (EDT/EST) and attendee's tz (PDT/PST) should both appear
        assert "ET" in body or "EST" in body or "EDT" in body
        assert "PT" in body or "PST" in body or "PDT" in body

    def test_suggest_times_shows_attendee_tz(self):
        """Slot suggestions sent to external party show their timezone and owner's."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-out", [
            FakeMsg(SARAH, MY_EMAIL, "Coffee chat?", "When are you free?"),
            FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?", "EA: suggest some times"),
        ])
        state = StateStore(path=None)
        slot = {"start": THU_2PM, "end": THU_2PM_30, "slot_type": "preferred"}

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: {
                     "intent": "suggest_times",
                     "topic": "Coffee chat",
                     "attendees": [SARAH],
                     "proposed_times": [],
                     "duration_minutes": 30,
                     "urgency": "medium",
                     "timezone": "America/Los_Angeles",   # attendee is Pacific; owner is Eastern (CONFIG)
                 },
                 find_slots_fn=lambda parsed, config, cal: [slot])

        thread = gmail.get_thread("t-out")
        last_msg = thread.messages[-1]
        assert last_msg.to_addr == SARAH
        # Attendee's tz (Pacific) should appear first; owner's tz (Eastern) in parens
        assert "PT" in last_msg.body or "PST" in last_msg.body or "PDT" in last_msg.body
        assert "my time" in last_msg.body

    def test_ambiguous_notifies_me(self):
        gmail = make_inbound_thread()
        state = StateStore(path=None)

        ambiguous_parsed = {
            "intent": "meeting_request",
            "topic": "Coffee chat",
            "attendees": [SARAH],
            "proposed_times": [],
            "duration_minutes": 30,
            "ambiguities": ["No specific time mentioned"],
            "urgency": "medium",
        }
        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: ambiguous_parsed)

        assert gmail.has_label("t1", "ea-notified")
        sent = gmail.sent_to(MY_EMAIL)
        assert any("needs more info" in m.subject for m in sent)
        # Body should tell the user how to fix it
        bodies = [m.body for m in sent]
        assert any("Reply to this email" in b for b in bodies)
        assert state.get("t1") is None

    def test_busy_no_alternatives_notifies_me(self):
        """All proposed slots busy AND no alternatives found → conflict notification."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        busy_calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
            SARAH: {"busy": [{"start": THU_2PM, "end": THU_2PM_30}]},
        }})

        run_poll(gmail, busy_calendar, state, CONFIG,
                 parser=lambda _: parsed_open(),
                 find_slots_fn=lambda parsed, config, cal: [])

        assert gmail.has_label("t1", "ea-notified")
        sent = gmail.sent_to(MY_EMAIL)
        assert any("conflict" in m.subject for m in sent)
        assert state.get("t1") is None

    def test_busy_with_alternatives_sends_options(self):
        """All proposed slots busy but alternatives exist → send them to the other party."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        busy_calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
            SARAH: {"busy": [{"start": THU_2PM, "end": THU_2PM_30}]},
        }})
        alt_slot = {"start": THU_2PM_30, "end": "2026-03-19T19:00:00+00:00", "slot_type": "preferred"}

        run_poll(gmail, busy_calendar, state, CONFIG,
                 parser=lambda _: parsed_open(),
                 find_slots_fn=lambda parsed, config, cal: [alt_slot])

        # Should NOT label the thread terminal — it's now pending external reply
        assert not gmail.has_label("t1", "ea-notified")
        assert not gmail.has_label("t1", "ea-scheduled")
        # Alternatives sent to Sarah on the original thread
        thread = gmail.get_thread("t1")
        last_msg = thread.messages[-1]
        assert last_msg.to_addr == SARAH
        assert "alternative" in last_msg.body.lower()
        # State written as pending_external_reply
        entry = state.get("t1")
        assert entry is not None
        assert entry["type"] == "pending_external_reply"
        assert entry["suggested_slots"] == [alt_slot]

    def test_after_hours_creates_pending_state(self):
        gmail = make_inbound_thread()
        state = StateStore(path=None)

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert not gmail.has_label("t1", "ea-scheduled")
        assert not gmail.has_label("t1", "ea-notified")
        entry = state.get("t1")
        assert entry is not None
        assert entry["type"] == "pending_confirmation"
        assert "confirmation_thread_id" in entry
        # EA should have sent me a private email asking to confirm
        sent = gmail.sent_to(MY_EMAIL)
        assert any("confirm slot" in m.subject for m in sent)

    def test_unknown_intent_notifies_me(self):
        """When the parser returns an unrecognised intent, EA sends a parse-error
        notification and labels the thread so it isn't retried."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: {"intent": "none", "topic": None, "ambiguities": []})

        assert gmail.has_label("t1", "ea-notified")
        sent = gmail.sent_to(MY_EMAIL)
        assert any("could not parse" in m.subject for m in sent)
        assert state.get("t1") is None

    def test_thread_already_in_state_is_skipped(self):
        """If a thread is already pending, Pass 1 should not re-process it."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        # Pre-populate state as if Pass 1 already ran
        state.set("t1", {
            "type": "pending_confirmation",
            "confirmation_thread_id": "conf-1",
            "created_at": "2026-03-19T14:00:00+00:00",
            "expires_at": "2026-03-21T14:00:00+00:00",
            "confirmation_messages_seen": 1,
            "schedule_result": {
                "outcome": "needs_confirmation",
                "slot_start": THU_7PM,
                "slot_end": THU_2PM_30,
                "slot_type": "after_hours",
                "topic": "Coffee chat",
                "attendees": [SARAH],
                "duration_minutes": 30,
            },
        })
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        call_count = 0

        def counting_parser(text):
            nonlocal call_count
            call_count += 1
            return parsed_open()

        run_poll(gmail, calendar, state, CONFIG, parser=counting_parser)
        assert call_count == 0, "Parser should not be called for a thread already in state"

    def test_thread_with_ea_label_is_skipped(self):
        """Threads already labeled ea-* should be excluded from Pass 1."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t1", [
            FakeMsg(SARAH, MY_EMAIL, "Coffee chat?", "Can we meet?"),
            FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?", "EA: please schedule"),
        ], label_ids=["ea-scheduled"])
        state = StateStore(path=None)
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        call_count = 0

        def counting_parser(text):
            nonlocal call_count
            call_count += 1
            return parsed_open()

        run_poll(gmail, calendar, state, CONFIG, parser=counting_parser)
        assert call_count == 0

    def test_thread_without_ea_trigger_is_skipped(self):
        """Normal reply from me without EA: should not trigger processing."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t1", [
            FakeMsg(SARAH, MY_EMAIL, "Lunch?", "Want to grab lunch Thursday?"),
            FakeMsg(MY_EMAIL, SARAH, "Re: Lunch?", "Sounds good!"),
        ])
        state = StateStore(path=None)
        call_count = 0

        def counting_parser(text):
            nonlocal call_count
            call_count += 1
            return parsed_open()

        run_poll(gmail, FREE_CALENDAR, state, CONFIG, parser=counting_parser)
        assert call_count == 0


# ---------------------------------------------------------------------------
# Pass 2 — Pending confirmations
# ---------------------------------------------------------------------------

class TestPass2PendingConfirmation:

    def _setup(self, ea_body="EA: please schedule", after_hours=True):
        """Seed a thread and run Pass 1 to get into pending_confirmation state."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        def parser(_):
            return parsed_after_hours() if after_hours else parsed_open()
        run_poll(gmail, FREE_CALENDAR, state, CONFIG, parser=parser)
        return gmail, state

    def test_yes_reply_schedules_event(self):
        gmail, state = self._setup()
        entry = state.get("t1")
        conf_thread_id = entry["confirmation_thread_id"]

        # Simulate me replying "yes" on the confirmation thread
        gmail.add_reply(conf_thread_id, MY_EMAIL, "yes")
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t1") is None

    def test_no_reply_cancels(self):
        gmail, state = self._setup()
        entry = state.get("t1")
        conf_thread_id = entry["confirmation_thread_id"]

        gmail.add_reply(conf_thread_id, MY_EMAIL, "no")

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert gmail.has_label("t1", "ea-cancelled")
        assert state.get("t1") is None
        # Should send a "noted" message on the confirmation thread
        conf_thread = gmail.get_thread(conf_thread_id)
        bodies = [m.body for m in conf_thread.messages]
        assert any("Noted" in b for b in bodies)

    def test_cancel_keyword_also_cancels(self):
        gmail, state = self._setup()
        conf_thread_id = state.get("t1")["confirmation_thread_id"]
        gmail.add_reply(conf_thread_id, MY_EMAIL, "cancel this please")

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert gmail.has_label("t1", "ea-cancelled")
        assert state.get("t1") is None

    def test_modification_reply_reschedules(self):
        """'yes but try 2pm instead' — modification that finds an open slot."""
        gmail, state = self._setup()
        conf_thread_id = state.get("t1")["confirmation_thread_id"]
        gmail.add_reply(conf_thread_id, MY_EMAIL, "yes but try Thursday at 2pm instead")
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})

        # Inject an eval function that returns an open result at 2pm
        open_result = ScheduleResult(
            outcome="open",
            slot_start=datetime.fromisoformat(THU_2PM),
            slot_end=datetime.fromisoformat(THU_2PM) + timedelta(minutes=30),
            slot_type="preferred",
            topic="Coffee chat",
            attendees=[MY_EMAIL, SARAH],
            duration_minutes=30,
        )

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours(),
                 confirm_eval_fn=lambda text, entry: open_result)

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t1") is None

    def test_modification_reply_still_busy(self):
        """Modification that hits a busy slot — stays in pending_confirmation."""
        gmail, state = self._setup()
        conf_thread_id = state.get("t1")["confirmation_thread_id"]
        gmail.add_reply(conf_thread_id, MY_EMAIL, "try Friday at 10am")

        busy_result = ScheduleResult(
            outcome="busy",
            busy_attendees=[SARAH],
            topic="Coffee chat",
            attendees=[MY_EMAIL, SARAH],
            duration_minutes=30,
        )

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours(),
                 confirm_eval_fn=lambda text, entry: busy_result)

        # Should remain in pending_confirmation
        assert not gmail.has_label("t1", "ea-scheduled")
        assert not gmail.has_label("t1", "ea-cancelled")
        entry = state.get("t1")
        assert entry is not None
        assert entry["type"] == "pending_confirmation"
        # Should have sent a "still busy" message
        conf_thread = gmail.get_thread(conf_thread_id)
        bodies = [m.body for m in conf_thread.messages]
        assert any("Still busy" in b or "still busy" in b.lower() for b in bodies)

    def test_no_new_reply_does_nothing(self):
        """If I haven't replied yet, Pass 2 should do nothing."""
        gmail, state = self._setup()
        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        entry_after = state.get("t1")
        assert entry_after is not None
        assert not gmail.has_label("t1", "ea-scheduled")
        assert not gmail.has_label("t1", "ea-cancelled")


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

class TestExpiry:

    def test_expired_entry_gets_label_and_removed(self):
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        # Manually insert an already-expired state entry
        state.set("t1", {
            "type": "pending_confirmation",
            "confirmation_thread_id": "conf-1",
            "created_at": "2026-03-17T14:00:00+00:00",
            "expires_at": "2026-03-17T16:00:00+00:00",   # in the past
            "confirmation_messages_seen": 1,
            "schedule_result": {
                "outcome": "needs_confirmation",
                "slot_start": THU_7PM,
                "slot_end": THU_2PM_30,
                "slot_type": "after_hours",
                "topic": "Coffee chat",
                "attendees": [SARAH],
                "duration_minutes": 30,
            },
        })
        gmail.seed_thread("conf-1", [
            FakeMsg(MY_EMAIL, MY_EMAIL, "EA: confirm slot — Coffee chat?",
                    "EA found a slot outside working hours..."),
        ])

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert gmail.has_label("t1", "ea-expired")
        assert state.get("t1") is None
        sent = gmail.sent_to(MY_EMAIL)
        assert any("lapsed" in m.subject for m in sent)


# ---------------------------------------------------------------------------
# Pass 3 — Pending external replies (outbound)
# ---------------------------------------------------------------------------

class TestPass3OutboundReplies:
    """
    Simulate the outbound flow: EA sent suggested times, recipient replied.
    For Pass 3 we pre-seed the state (as if Pass 1 / suggest_times ran)
    and add a reply to the original thread.
    """

    FRI_10AM = "2026-03-20T14:00:00+00:00"   # Fri 10am EDT = 14:00 UTC
    FRI_10AM_30 = "2026-03-20T14:30:00+00:00"

    def _setup_outbound_state(self, gmail, thread_id="t-out"):
        """Pre-seed state as if EA already sent time suggestions."""
        state = StateStore(path=None)
        state.set(thread_id, {
            "type": "pending_external_reply",
            "created_at": "2026-03-19T14:00:00+00:00",
            "expires_at": "2026-03-21T14:00:00+00:00",
            "original_messages_seen": 2,    # 2 messages: original + EA's suggestion
            "topic": "Coffee chat",
            "recipient": SARAH,
            "subject": "Re: Coffee chat?",
            "attendees": [MY_EMAIL, SARAH],
            "suggested_slots": [
                {"start": THU_2PM, "end": THU_2PM_30, "slot_type": "preferred"},
                {"start": self.FRI_10AM, "end": self.FRI_10AM_30, "slot_type": "preferred"},
            ],
        })
        return state

    def test_they_confirm_a_slot(self):
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-out", [
            FakeMsg(MY_EMAIL, SARAH, "Coffee chat?",
                    "Hey Sarah — EA is handling scheduling. Here are some times..."),
            FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?",
                    "[EA suggested: Thu 2pm or Fri 10am]"),
        ])
        state = self._setup_outbound_state(gmail)
        # Sarah replies confirming Thu 2pm
        gmail.add_reply("t-out", SARAH, "Thursday at 2pm works great!")

        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})

        confirmed_slot = {"start": THU_2PM, "end": THU_2PM_30, "slot_type": "preferred"}

        def ext_reply_fn(text, entry):
            return "confirmed", confirmed_slot

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_open(),
                 external_reply_fn=ext_reply_fn)

        assert gmail.has_label("t-out", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t-out") is None

    def test_they_counter_propose(self):
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-out", [
            FakeMsg(MY_EMAIL, SARAH, "Coffee chat?", "Here are some times..."),
            FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?", "[EA suggested slots]"),
        ])
        state = self._setup_outbound_state(gmail)
        gmail.add_reply("t-out", SARAH, "Neither works — do you have Friday at 2pm?")

        new_slots = [
            {"start": "2026-03-20T18:00:00+00:00",
             "end": "2026-03-20T18:30:00+00:00",
             "slot_type": "preferred"},
        ]

        def ext_reply_fn(text, entry):
            return "counter", new_slots

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_open(),
                 external_reply_fn=ext_reply_fn)

        # Should not be scheduled yet; state should have updated slots
        assert not gmail.has_label("t-out", "ea-scheduled")
        entry = state.get("t-out")
        assert entry is not None
        assert entry["suggested_slots"] == new_slots
        # EA should have replied on the original thread with new options
        thread = gmail.get_thread("t-out")
        assert len(thread.messages) > 2

    def test_they_confirm_sends_confirmation_email(self):
        """After the external party confirms a slot, EA sends me a confirmation email."""
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-out", [
            FakeMsg(MY_EMAIL, SARAH, "Coffee chat?", "Here are some times..."),
            FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?", "[EA suggested slots]"),
        ])
        state = self._setup_outbound_state(gmail)
        gmail.add_reply("t-out", SARAH, "Thursday at 2pm works!")

        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        confirmed_slot = {"start": THU_2PM, "end": THU_2PM_30, "slot_type": "preferred"}

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: {"intent": "suggest_times", "topic": "Coffee chat",
                                   "attendees": [SARAH], "proposed_times": [],
                                   "duration_minutes": 30, "urgency": "medium"},
                 external_reply_fn=lambda text, entry: ("confirmed", confirmed_slot))

        assert gmail.has_label("t-out", "ea-scheduled")
        assert len(calendar.events_created) == 1
        sent = gmail.sent_to(MY_EMAIL)
        assert any("booked" in m.subject.lower() for m in sent)

    def test_no_reply_yet_does_nothing(self):
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread("t-out", [
            FakeMsg(MY_EMAIL, SARAH, "Coffee chat?", "Here are some times..."),
            FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?", "[EA suggested slots]"),
        ])
        state = self._setup_outbound_state(gmail)
        # No reply added

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_open())

        assert not gmail.has_label("t-out", "ea-scheduled")
        assert state.get("t-out") is not None


# ---------------------------------------------------------------------------
# Full round-trip scenario
# ---------------------------------------------------------------------------

class TestFullRoundTrip:

    def test_inbound_needs_confirmation_then_yes(self):
        """
        Full scenario:
          Poll 1: inbound thread detected → after-hours slot → pending_confirmation
          Poll 2: I reply 'yes' → event created → ea-scheduled
        """
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})

        # --- Poll 1 ---
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert state.get("t1") is not None
        assert state.get("t1")["type"] == "pending_confirmation"
        assert len(calendar.events_created) == 0

        # --- I reply "yes" on the confirmation thread ---
        conf_thread_id = state.get("t1")["confirmation_thread_id"]
        gmail.add_reply(conf_thread_id, MY_EMAIL, "yes go ahead")

        # --- Poll 2 ---
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t1") is None

    def test_inbound_needs_confirmation_then_modify_then_yes(self):
        """
        Poll 1: after-hours → pending_confirmation
        Poll 2: 'try 2pm instead' → open → scheduled
        """
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        conf_thread_id = state.get("t1")["confirmation_thread_id"]
        gmail.add_reply(conf_thread_id, MY_EMAIL, "try Thursday 2pm instead")

        open_result = ScheduleResult(
            outcome="open",
            slot_start=datetime.fromisoformat(THU_2PM),
            slot_end=datetime.fromisoformat(THU_2PM) + timedelta(minutes=30),
            slot_type="preferred",
            topic="Coffee chat",
            attendees=[MY_EMAIL, SARAH],
            duration_minutes=30,
        )

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours(),
                 confirm_eval_fn=lambda text, entry: open_result)

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t1") is None


# ---------------------------------------------------------------------------
# Cancel / Reschedule
# ---------------------------------------------------------------------------

# Shared fixture: a standup event on Thu 2pm UTC (Thu 10am EDT)
STANDUP_EVENT = {
    "id": "ev-standup-1",
    "summary": "Standup",
    "start": {"dateTime": THU_2PM},
    "end":   {"dateTime": THU_2PM_30},
    "attendees": [{"email": MY_EMAIL}, {"email": SARAH}],
}

# Parsed dicts for the two new intents
def parsed_cancel(topic="standup", proposed_time=THU_2PM):
    return {
        "intent": "cancel_event",
        "topic": topic,
        "attendees": [],
        "proposed_times": [{"text": "Thursday at 2pm", "datetimes": [proposed_time]}],
        "new_proposed_times": [],
        "duration_minutes": None,
        "urgency": "medium",
    }

def parsed_reschedule(new_time, topic="standup", proposed_time=THU_2PM, duration=None):
    return {
        "intent": "reschedule",
        "topic": topic,
        "attendees": [],
        "proposed_times": [{"text": "Thursday at 2pm", "datetimes": [proposed_time]}],
        "new_proposed_times": [{"text": "new time", "datetimes": [new_time]}],
        "duration_minutes": duration,
        "urgency": "medium",
    }


def make_self_thread(thread_id="t-self", command="EA: cancel my standup on Thursday"):
    """Seed a self-addressed thread with an EA: command."""
    gmail = FakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread(thread_id, [
        FakeMsg(MY_EMAIL, MY_EMAIL, "EA command", command),
    ])
    return gmail


class TestCancelEvent:

    def test_cancel_found_event(self):
        """Matching event found → deleted, owner notified, thread labelled ea-scheduled."""
        gmail = make_self_thread()
        calendar = CalendarClient(fixture_data={
            "calendars": {MY_EMAIL: {"busy": []}, SARAH: {"busy": []}},
            "events": [STANDUP_EVENT],
        })
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_cancel(),
                 find_event_fn=lambda parsed, cal, tz: STANDUP_EVENT)

        assert gmail.has_label("t-self", "ea-scheduled")
        assert calendar.events_deleted == ["ev-standup-1"]
        sent = gmail.sent_to(MY_EMAIL)
        assert any("cancelled" in m.subject.lower() for m in sent)

    def test_cancel_event_not_found(self):
        """No matching event → notified, thread labelled ea-notified."""
        gmail = make_self_thread()
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}, "events": []})
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_cancel(),
                 find_event_fn=lambda parsed, cal, tz: None)

        assert gmail.has_label("t-self", "ea-notified")
        assert calendar.events_deleted == []
        sent = gmail.sent_to(MY_EMAIL)
        assert any("not found" in m.subject.lower() for m in sent)

    def test_cancel_ambiguous(self):
        """Multiple matching events → owner notified to be more specific."""
        second_event = {**STANDUP_EVENT, "id": "ev-standup-2", "summary": "Standup (backfill)"}
        gmail = make_self_thread()
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}, "events": []})
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_cancel(),
                 find_event_fn=lambda parsed, cal, tz: [STANDUP_EVENT, second_event])

        assert gmail.has_label("t-self", "ea-notified")
        assert calendar.events_deleted == []
        sent = gmail.sent_to(MY_EMAIL)
        assert any("ambiguous" in m.subject.lower() for m in sent)
        # Body should list both candidates
        bodies = " ".join(m.body for m in sent)
        assert "Standup" in bodies


class TestRescheduleEvent:

    FRI_10AM = "2026-03-20T14:00:00+00:00"   # Fri 10am EDT = 14:00 UTC
    FRI_10AM_30 = "2026-03-20T14:30:00+00:00"

    def test_reschedule_found_free(self):
        """Found event, new slot is free → event updated, owner notified."""
        gmail = make_self_thread(command="EA: move my standup to Friday 10am")
        calendar = CalendarClient(fixture_data={
            "calendars": {MY_EMAIL: {"busy": []}, SARAH: {"busy": []}},
            "events": [STANDUP_EVENT],
        })
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_reschedule(new_time=self.FRI_10AM),
                 find_event_fn=lambda parsed, cal, tz: STANDUP_EVENT)

        assert gmail.has_label("t-self", "ea-scheduled")
        assert len(calendar.events_updated) == 1
        assert calendar.events_updated[0]["id"] == "ev-standup-1"
        assert calendar.events_updated[0]["start"] == self.FRI_10AM
        sent = gmail.sent_to(MY_EMAIL)
        assert any("rescheduled" in m.subject.lower() for m in sent)

    def test_reschedule_preserves_existing_duration(self):
        """When duration_minutes is null, the existing event's duration is kept."""
        gmail = make_self_thread()
        # 60-minute event
        long_event = {
            **STANDUP_EVENT,
            "end": {"dateTime": "2026-03-19T19:00:00+00:00"},  # 1h after THU_2PM
        }
        calendar = CalendarClient(fixture_data={
            "calendars": {MY_EMAIL: {"busy": []}, SARAH: {"busy": []}},
            "events": [long_event],
        })
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_reschedule(new_time=self.FRI_10AM, duration=None),
                 find_event_fn=lambda parsed, cal, tz: long_event)

        assert len(calendar.events_updated) == 1
        updated = calendar.events_updated[0]
        # New end should be FRI_10AM + 60 min
        new_start = datetime.fromisoformat(updated["start"])
        new_end   = datetime.fromisoformat(updated["end"])
        assert (new_end - new_start).total_seconds() == 3600

    def test_reschedule_found_busy(self):
        """New slot is busy → owner notified, event unchanged."""
        gmail = make_self_thread()
        calendar = CalendarClient(fixture_data={
            "calendars": {
                MY_EMAIL: {"busy": [{"start": self.FRI_10AM, "end": self.FRI_10AM_30}]},
                SARAH:    {"busy": []},
            },
            "events": [STANDUP_EVENT],
        })
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_reschedule(new_time=self.FRI_10AM),
                 find_event_fn=lambda parsed, cal, tz: STANDUP_EVENT)

        assert gmail.has_label("t-self", "ea-notified")
        assert calendar.events_updated == []
        sent = gmail.sent_to(MY_EMAIL)
        assert any("conflict" in m.subject.lower() for m in sent)

    def test_reschedule_not_found(self):
        """Event not found → owner notified."""
        gmail = make_self_thread()
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}, "events": []})
        state = StateStore(path=None)

        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_reschedule(new_time=self.FRI_10AM),
                 find_event_fn=lambda parsed, cal, tz: None)

        assert gmail.has_label("t-self", "ea-notified")
        assert calendar.events_updated == []
        sent = gmail.sent_to(MY_EMAIL)
        assert any("not found" in m.subject.lower() for m in sent)

    def test_reschedule_no_new_time(self):
        """Parsed dict has no new_proposed_times → owner notified."""
        gmail = make_self_thread()
        calendar = CalendarClient(fixture_data={"calendars": {MY_EMAIL: {"busy": []}}, "events": []})
        state = StateStore(path=None)

        bad_parsed = {
            "intent": "reschedule",
            "topic": "standup",
            "attendees": [],
            "proposed_times": [{"text": "Thursday", "datetimes": [THU_2PM]}],
            "new_proposed_times": [],   # empty — EA can't determine new time
            "duration_minutes": None,
            "urgency": "medium",
        }
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: bad_parsed,
                 find_event_fn=lambda parsed, cal, tz: STANDUP_EVENT)

        assert gmail.has_label("t-self", "ea-notified")
        sent = gmail.sent_to(MY_EMAIL)
        assert any("no new time" in m.subject.lower() for m in sent)


class TestFindMatchingEvent:
    """Unit tests for find_matching_event() independent of the poll loop."""

    from ea.scheduler import find_matching_event as _find

    def _calendar(self, events):
        return CalendarClient(fixture_data={
            "calendars": {MY_EMAIL: {"busy": []}},
            "events": events,
        })

    def test_exact_topic_match(self):
        from ea.scheduler import find_matching_event
        cal = self._calendar([STANDUP_EVENT])
        result = find_matching_event("standup", [THU_2PM], cal, "America/New_York")
        assert isinstance(result, dict)
        assert result["id"] == "ev-standup-1"

    def test_partial_topic_match(self):
        from ea.scheduler import find_matching_event
        event = {**STANDUP_EVENT, "summary": "Weekly Standup Call"}
        cal = self._calendar([event])
        result = find_matching_event("standup", [THU_2PM], cal, "America/New_York")
        assert isinstance(result, dict)
        assert result["summary"] == "Weekly Standup Call"

    def test_no_match_returns_none(self):
        from ea.scheduler import find_matching_event
        cal = self._calendar([STANDUP_EVENT])
        result = find_matching_event("budget review", [THU_2PM], cal, "America/New_York")
        assert result is None

    def test_no_events_in_window_returns_none(self):
        from ea.scheduler import find_matching_event
        # THU_2PM is Thursday; search a Monday datetime — no events there
        monday_dt = "2026-03-16T14:00:00+00:00"
        cal = self._calendar([STANDUP_EVENT])
        result = find_matching_event("standup", [monday_dt], cal, "America/New_York")
        assert result is None

    def test_ambiguous_returns_list(self):
        from ea.scheduler import find_matching_event
        ev2 = {**STANDUP_EVENT, "id": "ev-standup-2", "summary": "Standup (PM)"}
        cal = self._calendar([STANDUP_EVENT, ev2])
        result = find_matching_event("standup", [THU_2PM], cal, "America/New_York")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_no_time_hint_searches_14_days(self):
        """With no search_datetimes, an event within 14 days should be found."""
        from ea.scheduler import find_matching_event
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        future_end = (datetime.now(timezone.utc) + timedelta(days=3, minutes=30)).isoformat()
        ev = {
            "id": "ev-future",
            "summary": "Budget Review",
            "start": {"dateTime": future},
            "end":   {"dateTime": future_end},
            "attendees": [],
        }
        cal = self._calendar([ev])
        result = find_matching_event("budget review", [], cal, "America/New_York")
        assert isinstance(result, dict)
        assert result["id"] == "ev-future"


# ---------------------------------------------------------------------------
# Explicit times — skip after-hours confirmation
# ---------------------------------------------------------------------------

class TestExplicitTimesEndToEnd:

    def test_explicit_after_hours_books_directly(self):
        """Owner names time explicitly → books immediately, no confirmation email."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_explicit_after_hours())

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t1") is None
        sent = gmail.sent_to(MY_EMAIL)
        assert not any("confirm slot" in m.subject for m in sent)
        assert any("booked" in m.subject.lower() for m in sent)

    def test_explicit_after_hours_busy_no_pending_confirmation(self):
        """Explicit time, but slot is busy → notified, NOT pending_confirmation."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        busy_cal = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []},
            SARAH: {"busy": [{"start": THU_7PM, "end": "2026-03-19T23:30:00+00:00"}]},
        }})
        run_poll(gmail, busy_cal, state, CONFIG,
                 parser=lambda _: parsed_explicit_after_hours(),
                 find_slots_fn=lambda *_: [])  # no alternatives found

        assert not gmail.has_label("t1", "ea-scheduled")
        assert state.get("t1") is None
        assert gmail.has_label("t1", "ea-notified")

    def test_non_explicit_after_hours_still_creates_pending(self):
        """Generic EA command + after-hours slot → still asks for confirmation."""
        gmail = make_inbound_thread()
        state = StateStore(path=None)
        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        entry = state.get("t1")
        assert entry is not None
        assert entry["type"] == "pending_confirmation"
        sent = gmail.sent_to(MY_EMAIL)
        assert any("confirm slot" in m.subject for m in sent)


# ---------------------------------------------------------------------------
# Confirmation flow — Gmail creates a new thread for the confirmation email
# ---------------------------------------------------------------------------

def make_inbound_thread_new_thread(thread_id="t1"):
    """Same as make_inbound_thread but uses NewThreadFakeGmailClient so that
    send_email always creates a separate thread, simulating Gmail's behaviour
    when the confirmation subject differs from the original thread subject."""
    gmail = NewThreadFakeGmailClient(my_email=MY_EMAIL)
    gmail.seed_thread(thread_id, [
        FakeMsg(SARAH, MY_EMAIL, "Coffee chat?",
                "Hey, can we grab a coffee this week? Thursday at 7pm works for me."),
        FakeMsg(MY_EMAIL, SARAH, "Re: Coffee chat?",
                "EA: please schedule"),
    ])
    return gmail


class TestConfirmationNewThread:

    def test_confirmation_messages_seen_is_1_for_new_thread(self):
        """When Gmail creates a new thread for the confirmation email,
        confirmation_messages_seen must be 1 (relative to the new thread),
        not msgs_before_send+1 (which was relative to the original thread)."""
        gmail = make_inbound_thread_new_thread()
        state = StateStore(path=None)
        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        entry = state.get("t1")
        assert entry is not None
        assert entry["type"] == "pending_confirmation"
        conf_id = entry["confirmation_thread_id"]
        assert conf_id != "t1"                              # different thread
        assert entry["confirmation_messages_seen"] == 1    # skip only EA's own message

    def test_new_confirmation_thread_gets_notified_label(self):
        """The new confirmation thread must be labeled ea-notified so Pass 1
        does not try to process 'EA: confirm slot — ...' as a fresh command."""
        gmail = make_inbound_thread_new_thread()
        state = StateStore(path=None)
        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        conf_id = state.get("t1")["confirmation_thread_id"]
        assert gmail.has_label(conf_id, "ea-notified")
        # Original thread must NOT have ea-notified (it is still pending)
        assert not gmail.has_label("t1", "ea-notified")

    def test_yes_reply_on_new_confirmation_thread_books_event(self):
        """Full round-trip: confirmation lands in a new thread, owner replies
        'yes' → Pass 2 detects the reply and books the event."""
        gmail = make_inbound_thread_new_thread()
        state = StateStore(path=None)
        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})

        # Pass 1 — creates pending_confirmation with a separate confirmation thread
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        conf_id = state.get("t1")["confirmation_thread_id"]
        assert conf_id != "t1"

        # Owner replies "yes" on the separate confirmation thread
        gmail.add_reply(conf_id, MY_EMAIL, "yes")

        # Pass 2 — must detect "yes" and book the event
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
        assert state.get("t1") is None

    def test_regression_same_thread_confirmation_still_works(self):
        """When the confirmation lands on the same thread (normal FakeGmailClient),
        the existing confirmation_messages_seen = msgs_before_send + 1 logic is
        preserved and a 'yes' reply still books the event."""
        gmail = make_inbound_thread()   # standard client — same thread
        state = StateStore(path=None)

        run_poll(gmail, FREE_CALENDAR, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        entry = state.get("t1")
        conf_id = entry["confirmation_thread_id"]
        assert conf_id == "t1"                              # same thread
        assert entry["confirmation_messages_seen"] == 3    # 2 original + 1 EA confirmation

        calendar = CalendarClient(fixture_data={"calendars": {
            MY_EMAIL: {"busy": []}, SARAH: {"busy": []},
        }})
        gmail.add_reply("t1", MY_EMAIL, "yes")
        run_poll(gmail, calendar, state, CONFIG,
                 parser=lambda _: parsed_after_hours())

        assert gmail.has_label("t1", "ea-scheduled")
        assert len(calendar.events_created) == 1
