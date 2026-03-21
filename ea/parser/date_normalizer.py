"""
date_normalizer.py

Converts plain-English date/time phrases to Python datetime objects using
dateparser (multilingual, actively maintained).
"""

import re
from abc import ABC, abstractmethod
from datetime import date, datetime

_WEEKDAY_RE = re.compile(
    r"\b(?:next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


def _preprocess_phrase(phrase: str) -> str:
    """Strip 'next '/'this ' prefixes before weekday names.

    dateparser does not recognise 'next Monday' but handles 'Monday' correctly
    when PREFER_DATES_FROM='future', which already selects the next occurrence.
    """
    return _WEEKDAY_RE.sub(r"\1", phrase)


class DateNormalizer(ABC):
    """Convert plain-English phrases to datetime/date objects."""

    @abstractmethod
    def parse_datetime(
        self, phrase: str, tz_name: str, now: datetime
    ) -> datetime | None:
        """Return a timezone-aware datetime for *phrase*, or None on failure.

        Args:
            phrase:  Natural-language expression, e.g. "Thursday at 2pm".
            tz_name: IANA timezone, e.g. "America/Los_Angeles".
            now:     Reference time for relative expressions ("tomorrow", "next week").
        """

    def parse_date(self, phrase: str, tz_name: str, now: datetime) -> date | None:
        """Return a local date for *phrase*, or None on failure."""
        from zoneinfo import ZoneInfo

        dt = self.parse_datetime(phrase, tz_name, now)
        if dt is None:
            return None
        return dt.astimezone(ZoneInfo(tz_name)).date()


class DateparserNormalizer(DateNormalizer):
    """Normalizer backed by dateparser (multilingual).

    Args:
        languages: BCP-47 language codes (e.g. ["en", "es"]).
                   Defaults to ["en"] when not provided.
    """

    def __init__(self, languages: list[str] | None = None):
        self._languages = languages or ["en"]

    def parse_datetime(
        self, phrase: str, tz_name: str, now: datetime
    ) -> datetime | None:
        import dateparser

        dt = dateparser.parse(
            _preprocess_phrase(phrase),
            languages=self._languages,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": True,
                "RELATIVE_BASE": now.replace(
                    tzinfo=None
                ),  # dateparser wants naive base
                "TIMEZONE": tz_name,
                "PREFER_DATES_FROM": "future",
            },
        )
        return dt


def make_normalizer(config: dict) -> DateNormalizer:
    """Build a DateNormalizer from a loaded config dict.

    Optional config section:

        [parser]
        languages = ["en"]   # BCP-47 codes; defaults to ["en"]
    """
    parser_cfg = config.get("parser", {})
    languages = parser_cfg.get("languages", ["en"])
    return DateparserNormalizer(languages=languages)
