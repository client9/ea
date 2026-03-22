"""
config.py

Loads and validates configuration from config.toml at the project root.
"""

import re
import tomllib
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CONFIG_PATH = Path(__file__).parent.parent / "config.toml"
_HH_MM_RE = re.compile(r"^\d{2}:\d{2}$")


# ---------------------------------------------------------------------------
# Schema models — private; used only for validation
# ---------------------------------------------------------------------------


class _TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _valid_hhmm(cls, v: str) -> str:
        if not _HH_MM_RE.match(v):
            raise ValueError(f"must be HH:MM, got {v!r}")
        h, m = int(v[:2]), int(v[3:])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"time out of range: {v!r}")
        return v

    @model_validator(mode="after")
    def _start_before_end(self) -> "_TimeRange":
        if self.start >= self.end:
            raise ValueError(f"start ({self.start}) must be before end ({self.end})")
        return self


class _HoursConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monday: Optional[_TimeRange] = None
    tuesday: Optional[_TimeRange] = None
    wednesday: Optional[_TimeRange] = None
    thursday: Optional[_TimeRange] = None
    friday: Optional[_TimeRange] = None
    saturday: Optional[_TimeRange] = None
    sunday: Optional[_TimeRange] = None


class _DurationDefaultsConfig(BaseModel):
    # 1on1 is not a valid Python identifier, so we use an alias.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    coffee_chat: Optional[int] = None
    interview: Optional[int] = None
    one_on_one: Optional[int] = Field(None, alias="1on1")
    board: Optional[int] = None
    standup: Optional[int] = None
    workshop: Optional[int] = None
    lunch: Optional[int] = None
    dinner: Optional[int] = None
    default: Optional[int] = None


class _ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str
    poll_interval_seconds: int = 300
    timeout_seconds: int = 30
    working_hours: _HoursConfig = Field(default_factory=_HoursConfig)
    preferred_hours: _HoursConfig = Field(default_factory=_HoursConfig)
    duration_defaults: _DurationDefaultsConfig = Field(
        default_factory=_DurationDefaultsConfig
    )

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(
                f"unknown timezone {v!r} — use an IANA name like 'America/Los_Angeles'"
            )
        return v


class _AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credentials_file: str
    token_file: str


_VALID_DAYS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}


class _UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    name: str
    email_footer: str = ""

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError(f"does not look like an email address: {v!r}")
        return v


class _DigestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: List[str] = Field(default_factory=list)
    send_time: str = "08:00"

    @field_validator("days", mode="before")
    @classmethod
    def _valid_days(cls, v: list) -> list:
        for day in v:
            if str(day).lower() not in _VALID_DAYS:
                raise ValueError(
                    f"unknown day {day!r} — use a full day name like 'monday'"
                )
        return [str(d).lower() for d in v]

    @field_validator("send_time")
    @classmethod
    def _valid_hhmm(cls, v: str) -> str:
        if not _HH_MM_RE.match(v):
            raise ValueError(f"must be HH:MM, got {v!r}")
        return v


class _ParserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    languages: List[str] = Field(default_factory=lambda: ["en"])


class _EAConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auth: _AuthConfig
    user: _UserConfig
    schedule: _ScheduleConfig
    digest: _DigestConfig = Field(default_factory=_DigestConfig)
    parser: _ParserConfig = Field(default_factory=_ParserConfig)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(raw: dict) -> None:
    from pydantic import ValidationError

    try:
        _EAConfig.model_validate(raw)
    except ValidationError as exc:
        lines = ["Config error in config.toml:"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            msg = err["msg"]
            lines.append(f"  {loc}: {msg}")
        raise SystemExit("\n".join(lines)) from None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise SystemExit(
            f"config.toml not found at {_CONFIG_PATH}\n"
            "Copy the example from docs/install-macos.md and edit it before running EA."
        )
    with open(_CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    _validate(raw)
    return raw


def get_my_email() -> str:
    return load_config()["user"]["email"]
