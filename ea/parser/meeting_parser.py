"""
meeting_parser.py

Core module for extracting structured details from meeting requests
and calendar blocking commands using the Claude API.
"""

import os
import re
import json
import anthropic

from ea.network import call_with_retry

SYSTEM_PROMPT = """You are an assistant that parses five types of input:

1. Inbound meeting requests — an email thread where someone asks to meet and the user has replied with an "EA:" command such as "EA: please schedule" or "EA: find a time on Friday".
2. Outbound time suggestions — the user has added an "EA:" command such as "EA: suggest some times to meet" or "EA: suggest some times on Friday for a 1 hour meeting". The email may be to someone else OR self-addressed (the user wants a list of their own availability). No times are proposed in the thread; the EA will find slots on the user's calendar.
3. Self-directed calendar blocks — a standalone message from the user to themselves with an "EA:" command such as "EA: block Thursday 12-1pm for lunch". Also handles all-day and multi-day events: "EA: mark Monday as out of office", "EA: vacation next week", "EA: add PyCon April 15-17 (informational, still free)".
4. Cancel event — the user wants to cancel an existing calendar event: "EA: cancel my 2pm meeting with Sarah on Friday" or "EA: cancel Thursday standup".
5. Reschedule event — the user wants to move an existing event to a new time: "EA: move my Thursday standup to Friday at 4pm" or "EA: reschedule my 2pm call with Bob to next Tuesday at 3pm".

Return a JSON object with the following fields:

{
  "intent": "meeting_request" | "suggest_times" | "block_time" | "cancel_event" | "reschedule" | "none",
  "topic": "meeting purpose, block label, or title of the existing event to find",
  "attendees": ["email addresses or names of the OTHER people — omit the user themselves; empty for block_time"],
  "all_day": true or false,
  "event_type": "ooo" | "vacation" | "conference" | "holiday" | "block" | null,
  "proposed_times": [
    {
      "text": "the original natural language phrase, e.g. 'Thursday at 2pm EST' or 'at 3 or 5pm'",
      "normalized": ["simple English time/date expressions. For timed events: one per distinct START time (e.g. 'Thursday at 2pm'). For all-day events: one or two date phrases — the start date, and optionally the end date for ranges (e.g. 'Monday through Friday' → ['Monday', 'Friday']; 'next week Monday' → ['next Monday', 'next Friday']; 'Monday' → ['Monday']). Break compound timed expressions into individual items."]
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
  "urgency": "low/medium/high based on tone and timing"
}

Rules:
- For suggest_times: if the user specifies a day or window (e.g. "on Friday", "next week"), capture it in proposed_times with normalized containing just the day/window phrase (e.g. "Friday", "next Monday"). The EA finds the specific times within that window. If no window is specified, proposed_times is empty.
- For block_time: attendees is empty; proposed_times holds the block window.
- For cancel_event: proposed_times holds the approximate time of the existing event (to find it on the calendar). attendees is empty or holds people in the event. new_proposed_times is empty.
- For reschedule: proposed_times holds the existing event's current time. new_proposed_times holds the new desired time. duration_minutes is the new duration if specified, else null (keep existing).
- For all-day events (all_day=true): duration_minutes is null. proposed_times[0].normalized contains the start date phrase and, if a range, the end date phrase as the second element. event_type captures the nature: "ooo" for out-of-office, "vacation" for vacation/holiday leave, "conference" for external conference, "holiday" for public holiday, "block" for generic blocking.
- For informational all-day events mentioned as "still free", "informational", "transparent", or "no conflicts" — set event_type to "conference" or "holiday" as appropriate. These appear free in scheduling.
- Return ONLY valid JSON. No explanation, no markdown, no code fences.
- Set intent to "none" if the text is none of the above types.
- In normalized: keep expressions in plain English (e.g. "Next Friday at 1pm"). Do NOT convert to dates or UTC. A separate system handles that conversion.
- new_proposed_times should always be present in the JSON (empty list [] if not applicable).
- all_day and event_type should always be present (all_day defaults to false, event_type defaults to null for timed events).
"""


def _normalized_to_dates(phrases: list[str], tz_name: str, now) -> list[str]:
    """
    Convert plain-English date phrases (e.g. 'Next Monday') to local date strings
    (YYYY-MM-DD) using parsedatetime. Used for all-day events where time is irrelevant.
    """
    import parsedatetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    cal = parsedatetime.Calendar()
    results = []
    for phrase in phrases:
        dt, status = cal.parseDT(phrase, sourceTime=now, tzinfo=tz)
        if status:
            results.append(dt.astimezone(tz).date().isoformat())
    return results


def _normalized_to_utc(phrases: list[str], tz_name: str, now) -> list[str]:
    """
    Convert plain-English time phrases (e.g. 'Next Friday at 1pm') to UTC
    ISO 8601 strings using parsedatetime.

    parsedatetime handles 'next', 'this', 'tomorrow', etc. correctly when
    given a reference time (sourceTime=now). The tzinfo argument makes the
    returned datetime timezone-aware, and ZoneInfo applies correct DST offsets.
    """
    import parsedatetime
    from datetime import timezone
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    cal = parsedatetime.Calendar()
    results = []
    for phrase in phrases:
        dt, status = cal.parseDT(phrase, sourceTime=now, tzinfo=tz)
        if status:
            results.append(dt.astimezone(timezone.utc).isoformat())
    return results


def parse_meeting_request(text: str, tz_name: str = "UTC") -> dict:
    """
    Parse a meeting request or calendar block command from free-form text.

    Args:
        text:    The email thread or message text to parse.
        tz_name: The user's local timezone (IANA name, e.g. 'America/Los_Angeles').
                 Used so Claude can decompose time expressions and dateparser can
                 convert them to UTC with correct DST handling.

    Returns:
        A dictionary with structured intent and scheduling details.
        proposed_times[*].datetimes and new_proposed_times[*].datetimes contain
        UTC ISO 8601 strings (the normalized→UTC conversion is invisible to callers).
    """
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
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```\s*$', '', raw)
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "error": f"Failed to parse Claude response as JSON: {e}",
            "raw_response": raw
        }

    # Convert normalized phrases → datetimes (UTC for timed events, local dates for all-day).
    all_day = parsed.get("all_day", False)
    for entry in parsed.get("proposed_times") or []:
        normalized = entry.pop("normalized", None) or []
        if all_day:
            entry["datetimes"] = _normalized_to_dates(normalized, tz_name, now_local)
        else:
            entry["datetimes"] = _normalized_to_utc(normalized, tz_name, now_local)

    for entry in parsed.get("new_proposed_times") or []:
        normalized = entry.pop("normalized", None) or []
        entry["datetimes"] = _normalized_to_utc(normalized, tz_name, now_local)

    return parsed
