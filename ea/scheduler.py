"""
scheduler.py

Two levels of scheduling logic:

  check_slot()     — low-level: is one specific slot free, and what type is it?
  evaluate_parsed() — mid-level: given a Claude-parsed thread dict and a calendar,
                      return one of four outcomes (ambiguous / open / busy /
                      needs_confirmation).
  process_thread()  — high-level: full pipeline from raw thread text to outcome.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from ea.calendar import CalendarClient

SlotType = Literal["preferred", "working", "after_hours"]
Outcome  = Literal["ambiguous", "open", "busy", "needs_confirmation"]

_SLOT_PRIORITY = {"preferred": 0, "working": 1, "after_hours": 2}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class SlotResult:
    free: bool
    slot_type: SlotType
    busy_attendees: list[str] = field(default_factory=list)


@dataclass
class ScheduleResult:
    outcome: Outcome

    # Populated for open / needs_confirmation
    slot_start: datetime | None = None
    slot_end:   datetime | None = None
    slot_type:  SlotType | None = None

    # From the parsed meeting
    topic:            str | None       = None
    attendees:        list[str]        = field(default_factory=list)
    duration_minutes: int | None       = None

    # Populated for ambiguous
    ambiguities: list[str] = field(default_factory=list)

    # Populated for busy
    busy_attendees: list[str] = field(default_factory=list)

    # Raw Claude parse — always present for debugging
    parsed: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# High-level pipeline
# ---------------------------------------------------------------------------

def process_thread(
    thread_text: str,
    config: dict,
    calendar: CalendarClient,
) -> ScheduleResult:
    """
    Full pipeline: detect EA: trigger → parse thread → check calendar → outcome.

    Args:
        thread_text: Raw email thread text (must contain an EA: reply).
        config:      Loaded config.toml dict (user.email, schedule.*).
        calendar:    CalendarClient instance (fixture or live).

    Raises:
        ValueError if no EA: trigger is found in the thread.
    """
    from ea.triggers import find_ea_trigger
    from ea.parser.meeting_parser import parse_meeting_request

    my_email = config["user"]["email"]
    if find_ea_trigger(thread_text, my_email) is None:
        raise ValueError("No EA: trigger found in thread")

    parsed = parse_meeting_request(thread_text)

    schedule = config.get("schedule", {})
    return evaluate_parsed(
        parsed=parsed,
        working_hours=schedule.get("working_hours", {}),
        preferred_hours=schedule.get("preferred_hours", {}),
        timezone=schedule.get("timezone", "UTC"),
        calendar=calendar,
        my_email=my_email,
    )


# ---------------------------------------------------------------------------
# Mid-level: evaluate an already-parsed dict
# ---------------------------------------------------------------------------

def evaluate_parsed(
    parsed: dict,
    working_hours: dict[str, dict],
    preferred_hours: dict[str, dict],
    timezone: str,
    calendar: CalendarClient,
    my_email: str | None = None,
) -> ScheduleResult:
    """
    Given a parsed meeting dict (from parse_meeting_request), check the calendar
    and return a ScheduleResult.

    Separating this from process_thread() makes it directly testable without
    hitting the Claude API — pass in a hand-crafted parsed dict and a fixture
    CalendarClient.

    Args:
        parsed:          Dict returned by parse_meeting_request().
        working_hours:   Per-day normal hours from config.
        preferred_hours: Per-day preferred hours from config.
        timezone:        IANA timezone name from config.
        calendar:        CalendarClient instance.
        my_email:        User's own email. When provided it is always included
                         in the attendee check so the user's own calendar is
                         never skipped (important for block_time where the
                         parsed attendees list is empty).
    """
    topic            = parsed.get("topic")
    duration_minutes = parsed.get("duration_minutes")
    ambiguities      = parsed.get("ambiguities") or []
    proposed_times   = parsed.get("proposed_times") or []
    intent           = parsed.get("intent")

    # Build the attendees list, always including the user's own calendar.
    attendees = list(parsed.get("attendees") or [])
    if my_email and my_email not in attendees:
        attendees.insert(0, my_email)

    base = dict(topic=topic, attendees=attendees,
                duration_minutes=duration_minutes, parsed=parsed)

    # --- Ambiguity checks ---
    if intent == "none" or not intent:
        return ScheduleResult(outcome="ambiguous",
                              ambiguities=["Could not determine intent from the message"],
                              **base)

    if ambiguities:
        return ScheduleResult(outcome="ambiguous", ambiguities=ambiguities, **base)

    if not proposed_times:
        return ScheduleResult(outcome="ambiguous",
                              ambiguities=["No specific times were proposed"],
                              **base)

    if not duration_minutes:
        return ScheduleResult(outcome="ambiguous",
                              ambiguities=["Duration is not specified"],
                              **base)

    # --- Check each proposed datetime against the calendar ---
    free_slots:        list[tuple[datetime, SlotResult]] = []
    all_busy_attendees: set[str]                         = set()
    evaluated_any = False

    for time_entry in proposed_times:
        for iso_str in time_entry.get("datetimes", []):
            try:
                start = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            evaluated_any = True
            end = start + timedelta(minutes=duration_minutes)
            result = check_slot(start, end, attendees,
                                working_hours, preferred_hours,
                                calendar, timezone)
            if result.free:
                free_slots.append((start, result))
            else:
                all_busy_attendees.update(result.busy_attendees)

    if not evaluated_any:
        return ScheduleResult(outcome="ambiguous",
                              ambiguities=["Proposed times could not be resolved to specific datetimes"],
                              **base)

    if not free_slots:
        return ScheduleResult(outcome="busy",
                              busy_attendees=sorted(all_busy_attendees),
                              **base)

    # Pick the best free slot: preferred > working > after_hours,
    # preserving proposal order within the same tier.
    free_slots.sort(key=lambda x: _SLOT_PRIORITY[x[1].slot_type])
    best_start, best_slot = free_slots[0]
    best_end = best_start + timedelta(minutes=duration_minutes)

    times_explicit = parsed.get("times_explicitly_specified", False)
    outcome = "needs_confirmation" if (best_slot.slot_type == "after_hours" and not times_explicit) else "open"

    return ScheduleResult(
        outcome=outcome,
        slot_start=best_start,
        slot_end=best_end,
        slot_type=best_slot.slot_type,
        **base,
    )


# ---------------------------------------------------------------------------
# Mid-level: find available slots over a future window
# ---------------------------------------------------------------------------

def find_slots(
    attendees: list[str],
    duration_minutes: int,
    working_hours: dict[str, dict],
    preferred_hours: dict[str, dict],
    tz_name: str,
    calendar: "CalendarClient",
    n: int = 3,
    lookahead_days: int = 7,
    now: datetime | None = None,
    restrict_to_date: "date | None" = None,
) -> list[dict]:
    """
    Return up to n free slots over the next lookahead_days, sorted by
    priority (preferred → working).

    restrict_to_date: if set, only return slots on that specific local date,
    ignoring working_hours day restrictions (so e.g. Friday slots are found
    even if Friday isn't in working_hours).

    Makes a single freebusy call for the entire window, then walks candidate
    30-minute-aligned slots within each day's working hours.

    Returns:
        List of dicts: [{"start": ISO, "end": ISO, "slot_type": SlotType}, ...]
    """
    if now is None:
        now = datetime.now(timezone.utc)

    tz = ZoneInfo(tz_name)
    window_end = now + timedelta(days=lookahead_days)

    time_min = now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = window_end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    freebusy = calendar.get_freebusy(time_min, time_max, attendees)

    candidates: list[dict] = []
    current_day = now.astimezone(tz).date()
    end_day = window_end.astimezone(tz).date()

    if restrict_to_date is not None:
        current_day = restrict_to_date
        end_day = restrict_to_date

    # Default search hours when restrict_to_date is used and the day has no
    # working_hours entry — search a broad window so the user's explicit day
    # choice is honoured even if it falls outside normal working hours.
    _FALLBACK_HOURS = {"start": "08:00", "end": "18:00"}

    while current_day <= end_day and len(candidates) < n * 3:
        day_name = current_day.strftime("%A").lower()
        wh = working_hours.get(day_name)
        if not wh:
            if restrict_to_date is None:
                current_day += timedelta(days=1)
                continue
            wh = _FALLBACK_HOURS  # user explicitly requested this day

        wh_start = time.fromisoformat(wh["start"])
        wh_end   = time.fromisoformat(wh["end"])

        # Walk in 30-minute increments across the working day
        cursor = datetime.combine(current_day, wh_start, tzinfo=tz)
        day_end = datetime.combine(current_day, wh_end, tzinfo=tz)

        while cursor + timedelta(minutes=duration_minutes) <= day_end:
            slot_start = cursor
            slot_end   = cursor + timedelta(minutes=duration_minutes)

            # Only consider future slots
            if slot_start <= now:
                cursor += timedelta(minutes=30)
                continue

            slot_type = _classify_slot(slot_start, slot_end, preferred_hours, working_hours)
            busy = _find_busy_attendees(
                slot_start.astimezone(ZoneInfo("UTC")),
                slot_end.astimezone(ZoneInfo("UTC")),
                freebusy,
            )
            if not busy:
                candidates.append({
                    "start": slot_start.isoformat(),
                    "end":   slot_end.isoformat(),
                    "slot_type": slot_type,
                })

            cursor += timedelta(minutes=30)

        current_day += timedelta(days=1)

    candidates.sort(key=lambda x: _SLOT_PRIORITY[x["slot_type"]])
    return candidates[:n]


# ---------------------------------------------------------------------------
# Mid-level: find an existing calendar event by topic + time window
# ---------------------------------------------------------------------------

# Words too generic to use for event matching
_STOP_WORDS = {
    "meeting", "call", "with", "my", "the", "a", "an", "and", "or",
    "at", "on", "in", "for", "to", "of", "re", "about",
}


def find_matching_event(
    topic: str,
    search_datetimes: list[str],
    calendar: "CalendarClient",
    tz_name: str,
) -> "dict | list[dict] | None":
    """
    Find an existing calendar event that best matches a topic and approximate time.

    Args:
        topic:            Partial event title from the parser (e.g. "standup", "Sarah call").
        search_datetimes: UTC ISO strings from proposed_times[*].datetimes — used to
                          build the search window. If empty, searches the next 14 days.
        calendar:         CalendarClient instance.
        tz_name:          User's IANA timezone name, for day-boundary calculations.

    Returns:
        dict       — exactly one event matched (best result).
        list[dict] — multiple events tied for best match (caller handles ambiguity).
        None       — no events scored above zero.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    now = datetime.now(timezone.utc)

    if search_datetimes:
        try:
            ref = datetime.fromisoformat(search_datetimes[0].replace("Z", "+00:00"))
        except ValueError:
            ref = now
        # Search the whole local day that contains the reference datetime
        local_date = ref.astimezone(tz).date()
        t_min = datetime.combine(local_date, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
        t_max = datetime.combine(local_date + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    else:
        # No time hint — search the next 14 days
        t_min = now
        t_max = now + timedelta(days=14)

    time_min = t_min.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = t_max.strftime("%Y-%m-%dT%H:%M:%SZ")
    events = calendar.list_events(time_min, time_max)

    if not events:
        return None

    # Score each event by title similarity to topic
    topic_words = {w for w in topic.lower().split() if w not in _STOP_WORDS and len(w) > 1}
    scored = []
    for ev in events:
        summary = (ev.get("summary") or "").lower()
        summary_words = {w for w in summary.split() if w not in _STOP_WORDS and len(w) > 1}

        if topic.lower() in summary or summary in topic.lower():
            score = 2
        elif topic_words & summary_words:
            score = 1
        else:
            score = 0

        if score > 0:
            scored.append((score, ev))

    if not scored:
        return None

    best_score = max(s for s, _ in scored)
    best = [ev for s, ev in scored if s == best_score]
    return best[0] if len(best) == 1 else best


# ---------------------------------------------------------------------------
# Low-level: single slot check
# ---------------------------------------------------------------------------

def check_slot(
    start: datetime,
    end: datetime,
    attendees: list[str],
    working_hours: dict[str, dict],
    preferred_hours: dict[str, dict],
    calendar: CalendarClient,
    timezone: str,
) -> SlotResult:
    """
    Check whether a time slot is free and classify it against working/preferred hours.

    Args:
        start:           Slot start (timezone-aware datetime).
        end:             Slot end (timezone-aware datetime).
        attendees:       Email addresses to check availability for.
        working_hours:   Per-day normal hours {"monday": {"start": "09:00", "end": "17:00"}, ...}
                         Days absent are treated as unavailable.
        preferred_hours: Per-day preferred hours, same shape as working_hours.
        calendar:        CalendarClient instance (fixture or live).
        timezone:        IANA timezone name for interpreting working/preferred hours.

    Returns:
        SlotResult with free status, slot_type, and list of busy attendees.
    """
    tz = ZoneInfo(timezone)
    local_start = start.astimezone(tz)
    local_end   = end.astimezone(tz)

    slot_type = _classify_slot(local_start, local_end, preferred_hours, working_hours)

    time_min = start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    freebusy       = calendar.get_freebusy(time_min, time_max, attendees)
    busy_attendees = _find_busy_attendees(start, end, freebusy)

    return SlotResult(
        free=len(busy_attendees) == 0,
        slot_type=slot_type,
        busy_attendees=busy_attendees,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _classify_slot(
    local_start: datetime,
    local_end: datetime,
    preferred_hours: dict,
    working_hours: dict,
) -> SlotType:
    if local_start.date() != local_end.date():
        return "after_hours"

    day          = local_start.strftime("%A").lower()
    slot_start_t = local_start.time().replace(second=0, microsecond=0)
    slot_end_t   = local_end.time().replace(second=0, microsecond=0)

    if _within_hours(slot_start_t, slot_end_t, preferred_hours.get(day)):
        return "preferred"
    if _within_hours(slot_start_t, slot_end_t, working_hours.get(day)):
        return "working"
    return "after_hours"


def _within_hours(slot_start: time, slot_end: time, hours: dict | None) -> bool:
    if not hours:
        return False
    h_start = time.fromisoformat(hours["start"])
    h_end   = time.fromisoformat(hours["end"])
    return slot_start >= h_start and slot_end <= h_end


def _find_busy_attendees(start: datetime, end: datetime, freebusy: dict) -> list[str]:
    busy = []
    for email, data in freebusy.get("calendars", {}).items():
        for block in data.get("busy", []):
            block_start = datetime.fromisoformat(block["start"].replace("Z", "+00:00"))
            block_end   = datetime.fromisoformat(block["end"].replace("Z", "+00:00"))
            if block_start < end and block_end > start:
                busy.append(email)
                break
    return busy
