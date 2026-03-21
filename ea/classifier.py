"""
classifier.py

Claude-based classifiers for reply handling.  These are the production
implementations of the injectable `confirm_eval_fn` and `external_reply_fn`
parameters in run_poll().

Both functions call the Claude API and are not covered by unit tests directly
(they're analogous to meeting_parser.py — an API wrapper).  The state machine
tests use injected lambdas instead.
"""

import json
import os

import anthropic

from ea.llm_util import strip_json_fences
from ea.network import call_with_retry
from ea.scheduler import ScheduleResult, evaluate_parsed

def _client() -> anthropic.Anthropic:
    from ea.network import get_api_timeout
    return anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        timeout=get_api_timeout(),
    )


# ---------------------------------------------------------------------------
# Confirmation reply classifier (Pass 2 — inbound pending_confirmation)
# ---------------------------------------------------------------------------

_CONFIRM_SYSTEM = """You are a scheduling assistant.
The user replied to a confirmation email asking them to approve an after-hours
meeting slot. Extract a new scheduling constraint if they modified the request.

Return JSON with one of these shapes:

If they said yes/confirmed (any affirmative):
  {"action": "yes"}

If they said no/cancel:
  {"action": "no"}

If they proposed a modification (e.g. "try Friday at 10am instead"):
  {
    "action": "modify",
    "proposed_times": [
      {"text": "...", "datetimes": ["ISO 8601 UTC strings"]}
    ],
    "duration_minutes": null or number
  }

Return ONLY valid JSON.
"""


def classify_confirmation_reply(
    reply_text: str,
    entry: dict,
    config: dict,
    calendar=None,
) -> ScheduleResult:
    """
    Parse the user's reply to a pending_confirmation email and return a
    ScheduleResult.

    This is the production implementation of confirm_eval_fn in run_poll().
    """
    schedule = config.get("schedule", {})
    my_email = config["user"]["email"]
    sr = entry.get("schedule_result", {})

    raw = strip_json_fences(call_with_retry(
        lambda: _client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=_CONFIRM_SYSTEM,
            messages=[{"role": "user", "content": reply_text}],
        )
    ).content[0].text.strip())

    try:
        classification = json.loads(raw)
    except json.JSONDecodeError:
        # Unparseable — treat as ambiguous modification
        return ScheduleResult(
            outcome="ambiguous",
            ambiguities=[f"Could not interpret reply: {reply_text!r}"],
            topic=sr.get("topic"),
            attendees=sr.get("attendees", []),
            duration_minutes=sr.get("duration_minutes"),
        )

    action = classification.get("action")

    if action == "yes":
        from datetime import datetime
        start = datetime.fromisoformat(sr["slot_start"])
        end   = datetime.fromisoformat(sr["slot_end"])
        return ScheduleResult(
            outcome="open",
            slot_start=start,
            slot_end=end,
            slot_type=sr.get("slot_type"),
            topic=sr.get("topic"),
            attendees=sr.get("attendees", []),
            duration_minutes=sr.get("duration_minutes"),
        )

    if action == "no":
        return ScheduleResult(
            outcome="ambiguous",
            ambiguities=["User declined"],
            topic=sr.get("topic"),
            attendees=sr.get("attendees", []),
            duration_minutes=sr.get("duration_minutes"),
        )

    # action == "modify"
    modified_parsed = {
        "intent": "meeting_request",
        "topic": sr.get("topic"),
        "attendees": [a for a in sr.get("attendees", []) if a != my_email],
        "proposed_times": classification.get("proposed_times") or [],
        "duration_minutes": classification.get("duration_minutes") or sr.get("duration_minutes"),
        "ambiguities": [],
        "urgency": "medium",
    }
    return evaluate_parsed(
        parsed=modified_parsed,
        working_hours=schedule.get("working_hours", {}),
        preferred_hours=schedule.get("preferred_hours", {}),
        timezone=schedule.get("timezone", "UTC"),
        calendar=calendar,
        my_email=my_email,
    )


# ---------------------------------------------------------------------------
# External reply classifier (Pass 3 — outbound pending_external_reply)
# ---------------------------------------------------------------------------

_EXTERNAL_SYSTEM = """You are a scheduling assistant.
Someone replied to a meeting time-suggestion email.  Classify their reply.

Return JSON with one of these shapes:

If they confirmed one of the suggested slots (mention a specific time):
  {"action": "confirmed", "slot_index": 0}   (0-based index into suggested_slots)

If they counter-proposed with constraints (e.g. "I can't do Thursday — Friday?"):
  {"action": "counter", "constraint": "the constraint they described"}

If they stated their own availability without confirming:
  {"action": "stated_availability", "times": "their availability description"}

Return ONLY valid JSON.
"""


def classify_external_reply(
    reply_text: str,
    entry: dict,
    config: dict,
) -> tuple:
    """
    Classify an external party's reply to a pending_external_reply thread.

    Returns (action, payload) where action is one of:
      "confirmed"  → payload is the confirmed slot dict
      "counter"    → payload is a constraint string (caller re-runs find_slots)
      "no-action"  → payload is None

    This is the production implementation of external_reply_fn in run_poll().
    """
    suggested_slots = entry.get("suggested_slots", [])

    slot_lines = "\n".join(f"  {i}: {slot['start']} – {slot['end']}" for i, slot in enumerate(suggested_slots))
    context = f"Suggested slots:\n{slot_lines}\n\nTheir reply:\n{reply_text}"

    raw = strip_json_fences(call_with_retry(
        lambda: _client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=_EXTERNAL_SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
    ).content[0].text.strip())

    try:
        classification = json.loads(raw)
    except json.JSONDecodeError:
        return ("no-action", None)

    action = classification.get("action")

    if action == "confirmed":
        idx = classification.get("slot_index", 0)
        if 0 <= idx < len(suggested_slots):
            return ("confirmed", suggested_slots[idx])
        return ("no-action", None)

    if action == "counter":
        return ("counter", classification.get("constraint", reply_text))

    if action == "stated_availability":
        return ("counter", classification.get("times", reply_text))

    return ("no-action", None)
