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
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_time(dt: datetime) -> str:
    """Format time as ' 9:19 AM' or '12:30 PM' — 5-char field, space-padded."""
    return f"{dt.strftime('%-I:%M'):>5} {dt.strftime('%p')}"


def _fmt_duration(minutes: int) -> str:
    """Format a duration as '1h 30m', '1h', or '45m'."""
    if minutes >= 60:
        h, m = divmod(minutes, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{minutes}m"


def _timed_events(events_sorted: list) -> list:
    return [e for e in events_sorted if "dateTime" in e.get("start", {})]


def _allday_events(events_sorted: list) -> list:
    return [e for e in events_sorted if "date" in e.get("start", {})]


def _attendees_for(event: dict, my_email: str) -> list[str]:
    return [
        a["email"]
        for a in event.get("attendees", [])
        if a.get("email", "").lower() != my_email.lower()
    ]


def _attendee_pairs(event: dict, my_email: str) -> list[tuple[str, str]]:
    """Return (display_name, email) for each attendee except the owner."""
    result = []
    for a in event.get("attendees", []):
        email = a.get("email", "")
        if email.lower() == my_email.lower():
            continue
        name = a.get("displayName", "").strip()
        result.append((name, email))
    return result


def _fmt_attendee_lines(
    pairs: list[tuple[str, str]], first_prefix: str, cont_prefix: str
) -> list[str]:
    """Format (name, email) pairs as vertically aligned lines.

    Name column width is computed from the longest name (capped at 30).
    Lines with no name show only the email.
    """
    if not pairs:
        return []
    lines = []
    for i, (name, email) in enumerate(pairs):
        prefix = first_prefix if i == 0 else cont_prefix
        if name:
            lines.append(f"{prefix}{name}  {email}")
        else:
            lines.append(f"{prefix}{email}")
    return lines


# ---------------------------------------------------------------------------
# Style: plain (original)
# ---------------------------------------------------------------------------


def format_event_line(event: dict, tz_name: str, my_email: str) -> str:
    """Format one calendar event as a single summary line.

    Timed events:   "9:00 – 10:00 AM PDT   Standup  (sarah@example.com)"
    All-day events: "(all day)              Out of Office"
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

    return f"{time_col:<32}  {summary}{attendee_str}"


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
# Style: table (Option B)
# ---------------------------------------------------------------------------


def _format_table_section(events_sorted: list, tz_name: str, my_email: str) -> str:
    """Start-time | title | duration."""
    tz = ZoneInfo(tz_name)
    SEP = "─" * 56
    lines = []

    all_day = _allday_events(events_sorted)
    timed = _timed_events(events_sorted)

    if all_day:
        lines.append(SEP)
        for ev in all_day:
            summary = (ev.get("summary") or "(no title)").strip()
            lines.append(f" all day    {summary}")
            if location := " ".join((ev.get("location") or "").split()):
                lines.append(f"{'':12}{location}")
        lines.append(SEP)
        lines.append("\n")

    for ev in timed:
        start_dt = datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(tz)
        end_dt = datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(tz)
        summary = (ev.get("summary") or "(no title)").strip()
        duration_min = max(1, int((end_dt - start_dt).total_seconds() / 60))
        dur_str = _fmt_duration(duration_min)

        time_str = _fmt_time(start_dt)
        title = summary[:44]

        lines.append(f"{time_str:<10}  {title:<44}  {dur_str:>6}")

        att_prefix = " " * 12
        if location := " ".join((ev.get("location") or "").split()):
            lines.append(f"{att_prefix}{location}")

        lines.extend(
            _fmt_attendee_lines(_attendee_pairs(ev, my_email), att_prefix, att_prefix)
        )
        lines.append("\n")

    if not lines:
        return "No meetings scheduled."
    return "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Style: list (Option C)
# ---------------------------------------------------------------------------


def _format_list_section(events_sorted: list, tz_name: str, my_email: str) -> str:
    """Multi-line card per event."""
    tz = ZoneInfo(tz_name)
    cards = []

    for ev in events_sorted:
        start_raw = ev.get("start", {})
        summary = "\n" + (ev.get("summary") or "(no title)").strip()
        underline = "═" * len(summary) + "\n"

        if "date" in start_raw:
            time_line = "all day"
        else:
            start_dt = datetime.fromisoformat(start_raw["dateTime"]).astimezone(tz)
            end_dt = datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(tz)
            duration_min = max(1, int((end_dt - start_dt).total_seconds() / 60))
            tz_abbr = start_dt.strftime("%Z")
            s = start_dt.strftime("%-I:%M")
            e = end_dt.strftime("%-I:%M %p")
            time_line = f"{s} – {e} {tz_abbr}  ·  {_fmt_duration(duration_min)}"

        card_lines = [summary, underline, f"when  {time_line}"]

        if location := " ".join((ev.get("location") or "").split()):
            card_lines.append(f"at    {location}")

        card_lines.extend(
            _fmt_attendee_lines(_attendee_pairs(ev, my_email), "with  ", "      ")
        )
        cards.append("\n".join(card_lines))

    if not cards:
        return "No meetings scheduled."

    return "\n\n".join(cards)


# ---------------------------------------------------------------------------
# Style: free
# ---------------------------------------------------------------------------


def _format_free_section(
    events_sorted: list, tz_name: str, config: dict, for_date
) -> str:
    """Show free blocks >= 30 min within working hours (or 8 AM–9 PM)."""
    from datetime import date as _date

    tz = ZoneInfo(tz_name)
    fd: _date = for_date

    day_name = datetime(fd.year, fd.month, fd.day, tzinfo=tz).strftime("%A").lower()
    wh = config.get("schedule", {}).get("working_hours", {}).get(day_name, {})
    start_str = wh.get("start", "08:00") if wh else "08:00"
    end_str = wh.get("end", "21:00") if wh else "21:00"

    def _parse_hm(s):
        h, m = map(int, s.split(":"))
        return datetime(fd.year, fd.month, fd.day, h, m, tzinfo=tz)

    day_start = _parse_hm(start_str)
    day_end = _parse_hm(end_str)

    # Collect and merge timed busy intervals
    busy = []
    for ev in events_sorted:
        if "dateTime" not in ev.get("start", {}):
            continue
        s = datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(tz)
        e = datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(tz)
        busy.append((s, e))

    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(busy):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    # Find gaps within [day_start, day_end]
    free_slots = []
    cursor = day_start
    for bs, be in merged:
        gap_end = min(bs, day_end)
        if gap_end > cursor:
            gap_min = int((gap_end - cursor).total_seconds() / 60)
            if gap_min >= 30:
                free_slots.append((cursor, gap_end))
        cursor = max(cursor, be)
    if cursor < day_end:
        gap_min = int((day_end - cursor).total_seconds() / 60)
        if gap_min >= 30:
            free_slots.append((cursor, day_end))

    tz_abbr = day_start.strftime("%Z")
    heading = f"Free windows (≥ 30 min, {start_str}–{end_str} {tz_abbr})"

    if not free_slots:
        return f"{heading}\n\nNo free windows found."

    lines = [f"{heading}", ""]
    for slot_s, slot_e in free_slots:
        s_str = _fmt_time(slot_s)
        e_str = _fmt_time(slot_e)
        gap_min = int((slot_e - slot_s).total_seconds() / 60)
        lines.append(f"{s_str} – {e_str}   {_fmt_duration(gap_min)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Style: timeline (Option A)
# ---------------------------------------------------------------------------


def _format_timeline_section(events_sorted: list, tz_name: str) -> str:
    """Horizontal ASCII timeline: event bars on the left, labels on the right."""
    CPH = 4  # chars per hour

    tz = ZoneInfo(tz_name)
    all_day = _allday_events(events_sorted)
    timed = _timed_events(events_sorted)

    if not timed:
        lines = [f"◈ {(e.get('summary') or '').strip()}  (all day)" for e in all_day]
        lines.append("No timed events scheduled.")
        return "\n".join(lines)

    starts = [
        datetime.fromisoformat(e["start"]["dateTime"]).astimezone(tz) for e in timed
    ]
    ends = [datetime.fromisoformat(e["end"]["dateTime"]).astimezone(tz) for e in timed]

    h_start = min(dt.hour for dt in starts)
    h_end = max(dt.hour + (1 if dt.minute > 0 else 0) for dt in ends)
    h_end = max(h_end, h_start + 4)
    total_w = (h_end - h_start) * CPH

    def _col(dt: datetime) -> int:
        minutes = (dt.hour - h_start) * 60 + dt.minute
        return min(total_w, round(minutes / 60 * CPH))

    # Header: hour labels right-aligned to their | position
    hdr = [" "] * (total_w + 1)
    for h in range(h_start, h_end + 1):
        col = (h - h_start) * CPH
        label = str(h)
        for j, c in enumerate(label):
            pos = col - len(label) + 1 + j
            if 0 <= pos <= total_w:
                hdr[pos] = c

    # Separator: | at each hour boundary
    sep = "".join("|" if i % CPH == 0 else " " for i in range(total_w + 1))

    # Event rows
    event_lines = []
    for ev in timed:
        s_dt = datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(tz)
        e_dt = datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(tz)

        s_col = max(0, _col(s_dt))
        e_col = _col(e_dt)

        bar = [" "] * (total_w + 1)
        if e_col > s_col + 1:
            bar[s_col] = "["
            for i in range(s_col + 1, e_col):
                bar[i] = "━"
            bar[e_col] = "]"
        elif e_col == s_col + 1:
            bar[s_col] = "["
            bar[e_col] = "]"
        else:
            bar[s_col] = "|"

        summary = (ev.get("summary") or "(no title)").strip()
        s_t = _fmt_time(s_dt)
        desc = f"{s_t}  {summary}"

        event_lines.append(f"{''.join(bar)}  {desc}")

    lines = []
    if all_day:
        for ev in all_day:
            lines.append(f"◈ {(ev.get('summary') or '').strip()}  (all day)")
        lines.append("")
    lines.append("".join(hdr))
    lines.append(sep)
    lines.extend(event_lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build digest
# ---------------------------------------------------------------------------


def build_digest(
    config: dict,
    calendar,
    state,
    for_date: date | None = None,
    style: str = "plain",
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

    if style == "timeline":
        calendar_section = _format_timeline_section(events_sorted, tz_name)
    elif style == "table":
        calendar_section = _format_table_section(events_sorted, tz_name, my_email)
    elif style == "list":
        calendar_section = _format_list_section(events_sorted, tz_name, my_email)
    elif style == "free":
        calendar_section = _format_free_section(
            events_sorted, tz_name, config, for_date
        )
    else:  # "plain"
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
        pending_lines.append(f"• {topic} ({entry_type}{exp_str})")

    if pending_lines:
        pending_section = "Pending EA actions:\n\n" + "\n".join(pending_lines)
    else:
        pending_section = "No pending EA items."

    body = (
        f"EA Daily Digest — {date_heading}\n\n{calendar_section}\n\n{pending_section}\n"
    )

    return subject, body
