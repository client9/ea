"""
tests/test_duration_defaults.py

Tests for _resolve_duration() and its integration with the poll loop.
Verifies that meeting_type + [schedule.duration_defaults] config fills in
a null duration_minutes before evaluate_parsed() is called, preventing a
spurious "ambiguous" outcome for common meeting types.
"""

from datetime import datetime

from ea.calendar import CalendarClient
from ea.poll import _resolve_duration, run_poll
from ea.state import StateStore
from tests.fake_gmail import FakeGmailClient, FakeMsg

MY_EMAIL = "me@example.com"
SARAH = "sarah@example.com"
THU_2PM = "2026-03-19T18:00:00+00:00"  # Thu 2pm EDT = 18:00 UTC (preferred)

BASE_CONFIG = {
    "user": {"email": MY_EMAIL, "name": "Nick"},
    "schedule": {
        "timezone": "America/New_York",
        "working_hours": {
            "thursday": {"start": "09:00", "end": "17:00"},
        },
        "preferred_hours": {
            "thursday": {"start": "10:00", "end": "16:00"},
        },
    },
}


def config_with_defaults(**overrides):
    import copy

    cfg = copy.deepcopy(BASE_CONFIG)
    cfg["schedule"]["duration_defaults"] = overrides
    return cfg


def free_calendar():
    return CalendarClient(
        fixture_data={"calendars": {MY_EMAIL: {"busy": []}, SARAH: {"busy": []}}}
    )


# ---------------------------------------------------------------------------
# Unit tests for _resolve_duration
# ---------------------------------------------------------------------------


class TestResolveDuration:
    def test_already_set_is_unchanged(self):
        parsed = {"duration_minutes": 45, "meeting_type": "interview"}
        cfg = config_with_defaults(interview=60, default=30)
        result = _resolve_duration(parsed, cfg)
        assert result["duration_minutes"] == 45  # not overwritten

    def test_per_type_default_applied(self):
        parsed = {"duration_minutes": None, "meeting_type": "interview"}
        cfg = config_with_defaults(interview=60, default=30)
        result = _resolve_duration(parsed, cfg)
        assert result["duration_minutes"] == 60

    def test_global_default_when_type_is_none(self):
        parsed = {"duration_minutes": None, "meeting_type": None}
        cfg = config_with_defaults(interview=60, default=30)
        result = _resolve_duration(parsed, cfg)
        assert result["duration_minutes"] == 30

    def test_global_default_when_type_not_in_table(self):
        parsed = {"duration_minutes": None, "meeting_type": "workshop"}
        cfg = config_with_defaults(default=45)
        result = _resolve_duration(parsed, cfg)
        assert result["duration_minutes"] == 45

    def test_no_defaults_section_returns_unchanged(self):
        parsed = {"duration_minutes": None, "meeting_type": "interview"}
        result = _resolve_duration(parsed, BASE_CONFIG)
        assert result["duration_minutes"] is None

    def test_empty_defaults_section_returns_unchanged(self):
        parsed = {"duration_minutes": None, "meeting_type": "interview"}
        cfg = config_with_defaults()
        result = _resolve_duration(parsed, cfg)
        assert result["duration_minutes"] is None

    def test_does_not_mutate_original(self):
        parsed = {"duration_minutes": None, "meeting_type": "1on1"}
        cfg = config_with_defaults(**{"1on1": 30})
        result = _resolve_duration(parsed, cfg)
        assert parsed["duration_minutes"] is None  # original unchanged
        assert result["duration_minutes"] == 30

    def test_zero_duration_treated_as_missing(self):
        # 0 is falsy; should be replaced by default
        parsed = {"duration_minutes": 0, "meeting_type": "standup"}
        cfg = config_with_defaults(standup=15)
        result = _resolve_duration(parsed, cfg)
        assert result["duration_minutes"] == 15


# ---------------------------------------------------------------------------
# Integration: poll loop books event using per-type default
# ---------------------------------------------------------------------------

BASE_PARSED = {
    "attendees": [SARAH],
    "proposed_times": [{"text": "Thursday 2pm", "datetimes": [THU_2PM]}],
    "ambiguities": [],
    "urgency": "medium",
    "all_day": False,
    "event_type": None,
    "new_proposed_times": [],
    "times_explicitly_specified": True,
}


class TestPollDurationDefaults:
    def _seed(self, thread_id="t1"):
        gmail = FakeGmailClient(my_email=MY_EMAIL)
        gmail.seed_thread(
            thread_id,
            [FakeMsg(MY_EMAIL, MY_EMAIL, "Schedule meeting", "EA: schedule it")],
        )
        return gmail

    def test_interview_default_used_when_duration_missing(self):
        """meeting_type=interview, no duration → 60-min event via config default."""
        gmail = self._seed()
        calendar = free_calendar()
        parsed_result = {
            **BASE_PARSED,
            "intent": "meeting_request",
            "topic": "Technical interview",
            "duration_minutes": None,
            "meeting_type": "interview",
        }

        cfg = config_with_defaults(interview=60, default=30)
        run_poll(
            gmail=gmail,
            calendar=calendar,
            state=StateStore(path=None),
            config=cfg,
            parser=lambda _: parsed_result,
        )

        assert len(calendar.events_created) == 1
        evt = calendar.events_created[0]
        start = datetime.fromisoformat(evt["start"]["dateTime"])
        end = datetime.fromisoformat(evt["end"]["dateTime"])
        assert (end - start).total_seconds() == 3600  # 60 minutes

    def test_global_default_used_when_no_meeting_type(self):
        """meeting_type=null → 30-min global default."""
        gmail = self._seed()
        calendar = free_calendar()
        parsed_result = {
            **BASE_PARSED,
            "intent": "meeting_request",
            "topic": "Catch-up call",
            "duration_minutes": None,
            "meeting_type": None,
        }

        cfg = config_with_defaults(default=30)
        run_poll(
            gmail=gmail,
            calendar=calendar,
            state=StateStore(path=None),
            config=cfg,
            parser=lambda _: parsed_result,
        )

        assert len(calendar.events_created) == 1
        evt = calendar.events_created[0]
        start = datetime.fromisoformat(evt["start"]["dateTime"])
        end = datetime.fromisoformat(evt["end"]["dateTime"])
        assert (end - start).total_seconds() == 1800  # 30 minutes

    def test_no_defaults_config_still_ambiguous(self):
        """Without [schedule.duration_defaults], missing duration → ambiguous → notified."""
        gmail = self._seed()
        calendar = free_calendar()
        parsed_result = {
            **BASE_PARSED,
            "intent": "meeting_request",
            "topic": "Mystery meeting",
            "duration_minutes": None,
            "meeting_type": "interview",  # type present but no config defaults
        }

        run_poll(
            gmail=gmail,
            calendar=calendar,
            state=StateStore(path=None),
            config=BASE_CONFIG,  # no duration_defaults
            parser=lambda _: parsed_result,
        )

        assert len(calendar.events_created) == 0
        assert gmail.has_label("t1", "ea-notified")
