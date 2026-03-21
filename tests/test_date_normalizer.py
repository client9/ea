"""
tests/test_date_normalizer.py

Unit tests for DateparserNormalizer and make_normalizer.

Reference time: 2026-03-21 09:00:00 America/Los_Angeles (PDT, UTC-7).
  - Today is Saturday March 21 2026.
  - "next Monday" → March 23 2026
  - "tomorrow"    → March 22 2026 (Sunday)
  - "Thursday"    → March 26 2026 (next Thursday)
"""

import datetime
from zoneinfo import ZoneInfo

from ea.parser.date_normalizer import DateparserNormalizer, make_normalizer

TZ = "America/Los_Angeles"
PDT = ZoneInfo(TZ)
UTC = datetime.timezone.utc

# Fixed reference: 2026-03-21 09:00 PDT  (UTC-7, so 16:00 UTC)
NOW = datetime.datetime(2026, 3, 21, 9, 0, 0, tzinfo=PDT)

N = DateparserNormalizer()


class TestParseDateTime:
    def test_explicit_date_and_time(self):
        dt = N.parse_datetime("March 26 at 2pm", TZ, NOW)
        assert dt is not None
        utc = dt.astimezone(UTC)
        assert utc.date() == datetime.date(2026, 3, 26)
        assert utc.hour == 21  # 2pm PDT = 21:00 UTC

    def test_tomorrow(self):
        dt = N.parse_datetime("tomorrow at 9am", TZ, NOW)
        assert dt is not None
        utc = dt.astimezone(UTC)
        assert utc.date() == datetime.date(2026, 3, 22)
        assert utc.hour == 16  # 9am PDT = 16:00 UTC

    def test_next_monday(self):
        dt = N.parse_datetime("next Monday at 10am", TZ, NOW)
        assert dt is not None
        utc = dt.astimezone(UTC)
        assert utc.date() == datetime.date(2026, 3, 23)
        assert utc.hour == 17  # 10am PDT = 17:00 UTC

    def test_this_thursday(self):
        dt = N.parse_datetime("this Thursday at 2pm", TZ, NOW)
        assert dt is not None
        assert dt.astimezone(UTC).date() == datetime.date(2026, 3, 26)

    def test_returns_none_on_garbage(self):
        assert N.parse_datetime("xyzzy frobozz blorg", TZ, NOW) is None

    def test_returns_timezone_aware(self):
        dt = N.parse_datetime("tomorrow at 9am", TZ, NOW)
        assert dt is not None
        assert dt.tzinfo is not None

    def test_dst_boundary(self):
        # 2026-03-08 is US spring-forward (clocks move to PDT at 2am).
        pre_dst = datetime.datetime(2026, 3, 7, 9, 0, 0, tzinfo=ZoneInfo(TZ))
        dt = N.parse_datetime("March 8 at 3pm", TZ, pre_dst)
        assert dt is not None
        # 3pm PDT (UTC-7) = 22:00 UTC
        assert dt.astimezone(UTC).hour == 22


class TestParseDate:
    def test_next_monday_date(self):
        assert N.parse_date("next Monday", TZ, NOW) == datetime.date(2026, 3, 23)

    def test_explicit_date(self):
        d = N.parse_date("March 25", TZ, NOW)
        assert d == datetime.date(2026, 3, 25)

    def test_tomorrow_date(self):
        assert N.parse_date("tomorrow", TZ, NOW) == datetime.date(2026, 3, 22)

    def test_returns_none_on_garbage(self):
        assert N.parse_date("xyzzy frobozz", TZ, NOW) is None


class TestMakeNormalizer:
    def test_default_returns_dateparser(self):
        assert isinstance(make_normalizer({}), DateparserNormalizer)

    def test_languages_passed_through(self):
        n = make_normalizer({"parser": {"languages": ["en", "es"]}})
        assert isinstance(n, DateparserNormalizer)
        assert n._languages == ["en", "es"]

    def test_default_language_is_english(self):
        n = make_normalizer({})
        assert n._languages == ["en"]


class TestUtcConversion:
    def test_phrase_to_utc_string(self):
        dt = N.parse_datetime("March 26 at 2pm", TZ, NOW)
        assert dt is not None
        utc_str = dt.astimezone(UTC).isoformat()
        assert "2026-03-26" in utc_str
        assert utc_str.endswith("+00:00")

    def test_phrase_to_date_string(self):
        d = N.parse_date("next Monday", TZ, NOW)
        assert d is not None
        assert d.isoformat() == "2026-03-23"
