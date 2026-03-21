"""
meeting_parser.py

Core module for extracting structured details from meeting requests
and calendar blocking commands using the Claude API.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import anthropic

from ea.llm_util import strip_json_fences
from ea.log import get_logger
from ea.network import call_with_retry

_log = get_logger(__name__)

_MAX_TOPIC_LEN = 200
_MAX_ATTENDEE_LEN = 200
_MAX_DURATION = 480  # 8 hours


def validate_parsed(parsed: dict, thread_id: str = "") -> None:
    """Raise ValueError if any scheduling field looks malicious or malformed.

    Called immediately after json.loads in parse_meeting_request, before
    datetime normalization. Any violation is logged as a WARNING so the owner
    can distinguish legitimate parse problems from prompt injection attempts
    (SEC-2).
    """

    def _fail(field: str, value, reason: str) -> None:
        _log.warning(
            "Rejected parsed output — field validation failed",
            extra={
                "thread_id": thread_id,
                "failing_field": field,
                "value": repr(value)[:200],
            },
        )
        raise ValueError(f"{field}: {reason}")

    # --- topic ---
    topic = parsed.get("topic")
    if topic is not None:
        if not isinstance(topic, str):
            _fail("topic", topic, "must be a string")
        if "\n" in topic or "\r" in topic:
            _fail("topic", topic, "contains newline")
        if len(topic) > _MAX_TOPIC_LEN:
            _fail("topic", topic, f"exceeds {_MAX_TOPIC_LEN} chars")

    # --- attendees ---
    attendees = parsed.get("attendees")
    if attendees is not None:
        if not isinstance(attendees, list):
            _fail("attendees", attendees, "must be a list")
        for a in attendees:
            if not isinstance(a, str):
                _fail("attendees", a, "each entry must be a string")
            if "\n" in a or "\r" in a:
                _fail("attendees", a, "entry contains newline")
            if len(a) > _MAX_ATTENDEE_LEN:
                _fail("attendees", a, f"entry exceeds {_MAX_ATTENDEE_LEN} chars")

    # --- duration_minutes ---
    dur = parsed.get("duration_minutes")
    if dur is not None:
        if not isinstance(dur, (int, float)) or dur <= 0 or dur > _MAX_DURATION:
            _fail("duration_minutes", dur, f"must be 1–{_MAX_DURATION}")

    # --- proposed_times[*].datetimes ---
    now = datetime.now(timezone.utc)
    past_limit = now - timedelta(days=30)
    future_limit = now + timedelta(days=730)

    for pt in parsed.get("proposed_times") or []:
        for iso in pt.get("datetimes") or []:
            if not isinstance(iso, str):
                _fail("proposed_times.datetimes", iso, "entry must be a string")
            # All-day date strings (YYYY-MM-DD) — skip range check
            if len(iso) == 10 and iso[4:5] == "-" and iso[7:8] == "-":
                continue
            try:
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                _fail("proposed_times.datetimes", iso, "not a valid ISO 8601 datetime")
            if dt < past_limit:
                _fail(
                    "proposed_times.datetimes", iso, "too far in the past (> 30 days)"
                )
            if dt > future_limit:
                _fail(
                    "proposed_times.datetimes", iso, "too far in the future (> 2 years)"
                )


SYSTEM_PROMPT = """You are an assistant that parses five types of input:

1. Inbound meeting requests — an email thread where someone asks to meet and the user has replied with an "EA:" command such as "EA: please schedule" or "EA: find a time on Friday".
2. Outbound time suggestions — the user has added an "EA:" command such as "EA: suggest some times to meet" or "EA: suggest some times on Friday for a 1 hour meeting". The email may be to someone else OR self-addressed (the user wants a list of their own availability). No times are proposed in the thread; the EA will find slots on the user's calendar.
3. Self-directed calendar blocks — a standalone message from the user to themselves with an "EA:" command such as "EA: block Thursday 12-1pm for lunch". Also handles all-day and multi-day events: "EA: mark Monday as out of office", "EA: vacation next week", "EA: add PyCon April 15-17 (informational, still free)".
4. Cancel event — the user wants to cancel an existing calendar event: "EA: cancel my 2pm meeting with Sarah on Friday" or "EA: cancel Thursday standup".
5. Reschedule event — the user wants to move an existing event to a new time: "EA: move my Thursday standup to Friday at 4pm" or "EA: reschedule my 2pm call with Bob to next Tuesday at 3pm".
6. Dismiss / ignore — the user wants to drop a pending scheduling request with no further action: "EA: ignore", "EA: dismiss", "EA: never mind", "EA: forget it".

Return a JSON object with the following fields:

{
  "intent": "meeting_request" | "suggest_times" | "block_time" | "cancel_event" | "reschedule" | "ignore" | "none",
  "topic": "meeting purpose, block label, or title of the existing event to find",
  "attendees": ["email addresses or names of the OTHER people — omit the user themselves; empty for block_time"],
  "all_day": true or false,
  "event_type": "ooo" | "vacation" | "conference" | "holiday" | "block" | null,
  "proposed_times": [
    {
      "text": "the original natural language phrase, e.g. 'Thursday at 2pm EST' or 'at 3 or 5pm'",
      "normalized": ["simple English time/date expressions. For timed events: one per distinct START time (e.g. 'Thursday at 2pm'). For all-day events: one or two date phrases — the start date, and optionally the end date for ranges (e.g. 'Monday through Friday' → ['Monday', 'Friday']; 'next week Monday' → ['next Monday', 'next Friday']; 'Monday' → ['Monday']). Break compound timed expressions into individual items."],
      "time_window": "after" | "before" | "around" | "morning" | "afternoon" | "evening" | null
    }
  ],
  "new_proposed_times": [
    {
      "text": "for reschedule only — the new time phrase, e.g. 'Friday at 4pm'",
      "normalized": ["same format as proposed_times normalized — one entry per distinct new start time"]
    }
  ],
  "duration_minutes": null or number,
  "location": null or "location or video call platform if mentioned",
  "timezone": null or "timezone if mentioned",
  "ambiguities": ["unclear or missing details — only for meeting_request; omit for other intents"],
  "times_explicitly_specified": true or false,
  "urgency": "low/medium/high based on tone and timing",
  "meeting_type": "coffee_chat" | "interview" | "1on1" | "board" | "standup" | "workshop" | "lunch" | null
}

Rules:
- For suggest_times: if the user specifies a day or window (e.g. "on Friday", "next week"), capture it in proposed_times with normalized containing just the day/window phrase (e.g. "Friday", "next Monday"). The EA finds the specific times within that window. If no window is specified, proposed_times is empty.
- For block_time: attendees is empty; proposed_times holds the block window.
- For cancel_event: proposed_times holds the approximate time of the existing event (to find it on the calendar). attendees is empty or holds people in the event. new_proposed_times is empty.
- For reschedule: proposed_times holds the existing event's current time. new_proposed_times holds the new desired time. duration_minutes is the new duration if specified, else null (keep existing).
- For all-day events (all_day=true): duration_minutes is null. proposed_times[0].normalized contains the start date phrase and, if a range, the end date phrase as the second element. event_type captures the nature: "ooo" for out-of-office, "vacation" for vacation/holiday leave, "conference" for external conference, "holiday" for public holiday, "block" for generic blocking.
- For informational all-day events mentioned as "still free", "informational", "transparent", or "no conflicts" — set event_type to "conference" or "holiday" as appropriate. These appear free in scheduling.
- times_explicitly_specified: Set true when the owner's EA: command itself contains a specific time or date (e.g. "EA: book at 3pm", "EA: schedule Thursday at 2pm", "EA: book the 3pm slot"). Set false when the command is generic with no embedded time (e.g. "EA: please schedule", "EA: book it", "EA: schedule this meeting"). If the time only appears in the other party's message but not in the EA: command, set false. Always include this field.
- When the EA: command is generic (e.g. "book it", "please schedule", "find a time") and the other party's message is asking about availability for a day or window (e.g. "Do you have time Thursday?", "Are you free next week?") without proposing a specific time, use intent "suggest_times". Capture the day or window mentioned by the other party in proposed_times (e.g. "Thursday" → proposed_times: [{text: "Thursday", normalized: ["Thursday"]}]).
- For ignore intent: proposed_times is empty, attendees is empty, all other optional fields are null. topic may contain the subject or topic being dismissed if identifiable.
- meeting_type: classify the meeting type based on context clues. Use "coffee_chat" for social coffee/drinks, "interview" for hiring/technical screens, "1on1" for one-on-one syncs, "board" for board meetings, "standup" for standups/dailies, "workshop" for workshops/training, "lunch" for lunch/dinner. Return null if no category fits.
- Return ONLY valid JSON. No explanation, no markdown, no code fences.
- Set intent to "none" if the text is none of the above types.
- In normalized: keep expressions in plain English (e.g. "Next Friday at 1pm"). Do NOT convert to dates or UTC. A separate system handles that conversion.
- new_proposed_times should always be present in the JSON (empty list [] if not applicable).
- all_day and event_type should always be present (all_day defaults to false, event_type defaults to null for timed events).
- time_window: captures directional or fuzzy time qualifiers on proposed_times entries. Set to:
  - "after"     — "after 1pm", "from 2pm onwards", "any time after noon"
  - "before"    — "before 3pm", "by noon", "no later than 2pm"
  - "around"    — "around 2pm", "roughly 3pm", "approximately noon"
  - "morning"   — "in the morning", "morning time", no specific anchor time
  - "afternoon" — "in the afternoon", "afternoon works"
  - "evening"   — "in the evening", "evening slot"
  - null        — exact time with no qualifier (default; e.g. "Thursday at 2pm")
  For "after"/"before"/"around": normalized still contains the anchor time phrase (e.g. "after 1pm" → normalized: ["today at 1pm"], time_window: "after").
  For "morning"/"afternoon"/"evening": normalized contains only the day if specified (e.g. "Friday morning" → normalized: ["Friday"], time_window: "morning").
  Always include time_window on each proposed_times entry (null if no qualifier).
"""


def _normalized_to_dates(
    phrases: list[str], tz_name: str, now, normalizer
) -> list[str]:
    """Convert plain-English date phrases (e.g. 'Next Monday') to local date strings
    (YYYY-MM-DD). Used for all-day events where time is irrelevant."""
    results = []
    for phrase in phrases:
        d = normalizer.parse_date(phrase, tz_name, now)
        if d is not None:
            results.append(d.isoformat())
    return results


def _normalized_to_utc(phrases: list[str], tz_name: str, now, normalizer) -> list[str]:
    """Convert plain-English time phrases (e.g. 'Next Friday at 1pm') to UTC
    ISO 8601 strings."""
    from datetime import timezone

    results = []
    for phrase in phrases:
        dt = normalizer.parse_datetime(phrase, tz_name, now)
        if dt is not None:
            results.append(dt.astimezone(timezone.utc).isoformat())
    return results


def parse_meeting_request(text: str, tz_name: str = "UTC", normalizer=None) -> dict:
    """
    Parse a meeting request or calendar block command from free-form text.

    Args:
        text:       The email thread or message text to parse.
        tz_name:    The user's local timezone (IANA name, e.g. 'America/Los_Angeles').
        normalizer: DateNormalizer instance for phrase→datetime conversion.
                    Defaults to ParsedatetimeNormalizer if not provided.

    Returns:
        A dictionary with structured intent and scheduling details.
        proposed_times[*].datetimes and new_proposed_times[*].datetimes contain
        UTC ISO 8601 strings (the normalized→UTC conversion is invisible to callers).
    """
    if normalizer is None:
        from ea.parser.date_normalizer import DateparserNormalizer

        normalizer = DateparserNormalizer()
    import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    now_local = datetime.datetime.now(tz=tz)
    today = now_local.date().isoformat()
    day_of_week = now_local.strftime("%A")

    from ea.network import get_api_timeout

    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        timeout=get_api_timeout(),
    )

    user_content = (
        f"User's local timezone: {tz_name}\n"
        f"Today's date: {today} ({day_of_week})\n\n"
        f"Parse this text:\n\n{text}"
    )
    message = call_with_retry(
        lambda: client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences the model occasionally adds despite instructions.
    raw = strip_json_fences(raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "error": f"Failed to parse Claude response as JSON: {e}",
            "raw_response": raw,
        }

    try:
        validate_parsed(parsed)
    except ValueError as e:
        return {"error": f"Field validation failed: {e}", "raw_response": raw}

    # Convert normalized phrases → datetimes (UTC for timed events, local dates for all-day).
    parsed.setdefault("times_explicitly_specified", False)
    all_day = parsed.get("all_day", False)
    for entry in parsed.get("proposed_times") or []:
        normalized = entry.pop("normalized", None) or []
        if all_day:
            entry["datetimes"] = _normalized_to_dates(
                normalized, tz_name, now_local, normalizer
            )
        else:
            entry["datetimes"] = _normalized_to_utc(
                normalized, tz_name, now_local, normalizer
            )

    for entry in parsed.get("new_proposed_times") or []:
        normalized = entry.pop("normalized", None) or []
        entry["datetimes"] = _normalized_to_utc(
            normalized, tz_name, now_local, normalizer
        )

    return parsed
