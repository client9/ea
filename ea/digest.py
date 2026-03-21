"""
digest.py

Daily digest: builds a plain-text summary of today's calendar events and
pending EA state entries.

Automated sending is integrated into run_once() in runner.py — on each poll
cycle, if [digest] is configured, today is a send day, the send_time has
passed, and no digest has been sent yet today, the email is sent automatically.

The CLI command `python ea.py digest` calls build_digest() and prints the body
to stdout (no email sent).
"""

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DIGEST_STATE_FILE = "digest_sent.json"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def should_send_digest(config: dict, now_local: datetime) -> bool:
    """Return True if the digest should be sent right now.

    Conditions (all must hold):
    - [digest] section is present in config
    - days list is non-empty and today's day name is in it
    - now_local.time() >= the configured send_time (default "08:00")
    """
    digest_cfg = config.get("digest")
    if not digest_cfg:
        return False

    days = digest_cfg.get("days", [])
    if not days:
        return False

    day_name = now_local.strftime("%A").lower()
    if day_name not in [d.lower() for d in days]:
        return False

    send_time_str = digest_cfg.get("send_time", "08:00")
    hour, minute = (int(p) for p in send_time_str.split(":"))
    send_time = time(hour, minute)
    return now_local.time() >= send_time


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def already_sent_today(today_str: str, path: str = DIGEST_STATE_FILE) -> bool:
    """Return True if the digest was already sent on `today_str` (YYYY-MM-DD)."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
        return data.get("last_sent") == today_str
    except (json.JSONDecodeError, OSError):
        return False


def mark_sent_today(today_str: str, path: str = DIGEST_STATE_FILE) -> None:
    """Record that the digest was sent on `today_str`."""
    Path(path).write_text(json.dumps({"last_sent": today_str}))


# ---------------------------------------------------------------------------
# Event window
# ---------------------------------------------------------------------------


def get_today_window(
    tz_name: str, for_date: date | None = None
) -> tuple[datetime, datetime]:
    """Return (midnight_utc, next_midnight_utc) for `for_date` (default: today)
    in the user's timezone."""
    tz = ZoneInfo(tz_name)
    if for_date is None:
        for_date = datetime.now(tz).date()
    today_local = datetime(for_date.year, for_date.month, for_date.day, tzinfo=tz)
    tomorrow_local = today_local.replace(day=today_local.day + 1)
    return (
        today_local.astimezone(timezone.utc),
        tomorrow_local.astimezone(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_event_line(event: dict, tz_name: str, my_email: str) -> str:
    """Format one calendar event as a single summary line.

    Timed events:   "  9:00 – 10:00 AM PDT   Standup  (sarah@example.com)"
    All-day events: "  (all day)              Out of Office"
    """
    from ea.responder import _fmt_range

    summary = (event.get("summary") or "(no title)").strip()

    # All-day events have a "date" key instead of "dateTime"
    start_raw = event.get("start", {})
    if "date" in start_raw:
        time_col = "(all day)"
    else:
        tz = ZoneInfo(tz_name)
        start_dt = datetime.fromisoformat(start_raw["dateTime"]).astimezone(tz)
        end_dt = datetime.fromisoformat(event["end"]["dateTime"]).astimezone(tz)
        time_col = _fmt_range(start_dt, end_dt)

    # External attendees (omit owner's own email)
    attendees = [
        a["email"]
        for a in event.get("attendees", [])
        if a.get("email", "").lower() != my_email.lower()
    ]
    attendee_str = f"  ({', '.join(attendees)})" if attendees else ""

    return f"  {time_col:<32}  {summary}{attendee_str}"


def _expiry_str(expires_raw: str) -> str:
    """Convert an ISO 8601 expiry timestamp to a human-readable relative string."""
    try:
        expires_dt = datetime.fromisoformat(expires_raw)
        delta = expires_dt - datetime.now(timezone.utc)
        total_sec = int(delta.total_seconds())
        if total_sec < 0:
            return "EXPIRED"
        if total_sec < 3600:
            return f"{total_sec // 60}m"
        if total_sec < 86400:
            return f"{total_sec // 3600}h {(total_sec % 3600) // 60}m"
        return f"{total_sec // 86400}d {(total_sec % 86400) // 3600}h"
    except (ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Build digest
# ---------------------------------------------------------------------------


def build_digest(
    config: dict, calendar, state, for_date: date | None = None
) -> tuple[str, str]:
    """Return (subject, body) for the daily digest.

    calendar:  CalendarClient (live or fixture)
    state:     StateStore
    for_date:  date to generate digest for; defaults to today in user's timezone
    """
    tz_name = config.get("schedule", {}).get("timezone", "UTC")
    my_email = config.get("user", {}).get("email", "")

    tz = ZoneInfo(tz_name)
    if for_date is None:
        for_date = datetime.now(tz).date()
    target_dt = datetime(for_date.year, for_date.month, for_date.day, tzinfo=tz)
    date_heading = target_dt.strftime("%A, %B %-d, %Y")
    date_short = target_dt.strftime("%A, %B %-d")

    subject = f"EA: Daily digest — {date_short}"

    # --- Events for the target date ---
    time_min, time_max = get_today_window(tz_name, for_date=for_date)
    events = calendar.list_events(time_min.isoformat(), time_max.isoformat())

    # Sort timed events by start time; all-day events first
    def _sort_key(ev):
        start = ev.get("start", {})
        if "dateTime" in start:
            return datetime.fromisoformat(start["dateTime"]).astimezone(timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    events_sorted = sorted(events, key=_sort_key)

    if events_sorted:
        event_lines = "\n".join(
            format_event_line(ev, tz_name, my_email) for ev in events_sorted
        )
        calendar_section = f"Today's calendar:\n\n{event_lines}"
    else:
        calendar_section = "No meetings scheduled today."

    # --- Pending EA actions ---
    entries = state.all()
    pending_lines = []
    for thread_id, entry in entries.items():
        entry_type = entry.get("type", "unknown")
        sr = entry.get("schedule_result") or {}
        topic = entry.get("topic") or sr.get("topic") or "(no topic)"
        expires_raw = entry.get("expires_at", "")
        exp = _expiry_str(expires_raw)
        exp_str = f", expires in {exp}" if exp else ""
        pending_lines.append(f"  • {topic} ({entry_type}{exp_str})")

    if pending_lines:
        pending_section = "Pending EA actions:\n\n" + "\n".join(pending_lines)
    else:
        pending_section = "No pending EA items."

    body = (
        f"EA Daily Digest — {date_heading}\n\n{calendar_section}\n\n{pending_section}\n"
    )

    return subject, body
