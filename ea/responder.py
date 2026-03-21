"""
responder.py

Acts on a ScheduleResult: creates calendar events, sends emails, applies
Gmail labels, and writes/updates the state store.

All functions accept a `gmail` and `calendar` argument that satisfy the
GmailClient / CalendarClient duck-type interface, making them testable
with FakeGmailClient and CalendarClient(fixture_data=...).
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ea.scheduler import ScheduleResult
from ea.state import StateStore

EXPIRY_HOURS = 48


def _fmt_range(start: datetime, end: datetime) -> str:
    """Format a time range as 'Thursday Mar 19, 02:00–03:00 PM PDT'."""
    return (
        start.strftime("%A %b %d, %I:%M") +
        end.strftime("–%I:%M %p ") +
        start.strftime("%Z")
    )


def _send_calendar_error(gmail, my_email: str, thread_id: str, subject: str, action: str, exc: Exception) -> str:
    """Send a calendar-error notification email and return 'calendar-error'."""
    gmail.send_email(
        to=my_email,
        subject=f"EA: calendar error — {subject}",
        body=f"EA tried to {action} but the calendar API returned an error:\n\n{exc}",
        thread_id=thread_id,
    )
    gmail.apply_label(thread_id, "ea-notified")
    return "calendar-error"


def _handle_event_not_found(gmail, my_email: str, thread_id: str, topic: str) -> str:
    gmail.send_email(
        to=my_email,
        subject=f"EA: event not found — {topic}",
        body=(
            f"EA could not find a calendar event matching '{topic}'.\n"
            "Check the event title and time, then try again."
        ),
        thread_id=thread_id,
    )
    gmail.apply_label(thread_id, "ea-notified")
    return "notified-not-found"


def _handle_event_ambiguous(gmail, my_email: str, thread_id: str, topic: str, match: list, intent_label: str) -> str:
    lines = "\n".join(
        f"  • {ev.get('summary', '?')}  —  "
        f"{(ev.get('start') or {}).get('dateTime', '?')}"
        for ev in match
    )
    gmail.send_email(
        to=my_email,
        subject=f"EA: ambiguous {intent_label} — {topic}",
        body=(
            f"EA found multiple events matching '{topic}':\n\n{lines}\n\n"
            "Please be more specific (include the exact time or full title)."
        ),
        thread_id=thread_id,
    )
    gmail.apply_label(thread_id, "ea-notified")
    return "notified-ambiguous"


def _local_slot_desc(start: datetime, end: datetime, config: dict, attendee_tz: str | None = None) -> str:
    """
    Format a slot in the owner's local timezone.
    If attendee_tz is set and differs from the owner's tz, appends their local
    time in parentheses: "Thursday Mar 19, 02:00–03:00 PM PDT (5:00–6:00 PM EDT for them)"
    """
    tz_name = config.get("schedule", {}).get("timezone", "UTC")
    tz = ZoneInfo(tz_name)
    local_start = start.astimezone(tz)
    local_end   = end.astimezone(tz)
    desc = _fmt_range(local_start, local_end)
    if attendee_tz and attendee_tz != tz_name:
        try:
            att_tz = ZoneInfo(attendee_tz)
            att_start = start.astimezone(att_tz)
            att_end   = end.astimezone(att_tz)
            desc += (
                " (" +
                att_start.strftime("%I:%M") +
                att_end.strftime("–%I:%M %p ") +
                att_start.strftime("%Z") +
                " for them)"
            )
        except ZoneInfoNotFoundError:
            pass  # unrecognised tz string — skip the annotation
    return desc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=EXPIRY_HOURS)).isoformat()


# ---------------------------------------------------------------------------
# Inbound: handle the result of an EA: please schedule trigger
# ---------------------------------------------------------------------------

def handle_inbound_result(
    result: ScheduleResult,
    original_thread,     # GmailThread
    gmail,               # GmailClient (live or fake)
    calendar,            # CalendarClient (live or fake)
    state: StateStore,
    config: dict,
    find_slots_fn=None,  # injectable: fn(parsed, config, calendar) -> list[dict]
) -> str:
    """
    Act on a ScheduleResult from an inbound EA: trigger.
    Returns a short action token describing what happened.
    """
    my_email = config["user"]["email"]
    thread_id = original_thread.id
    first_msg = original_thread.messages[0]
    subject = first_msg.subject
    attendee_tz = result.parsed.get("timezone")

    if result.outcome == "open":
        topic = result.topic or "Meeting"
        try:
            calendar.create_event(
                topic=topic,
                start=result.slot_start.isoformat(),
                end=result.slot_end.isoformat(),
                attendees=result.attendees,
                self_email=my_email,
            )
        except Exception as e:
            return _send_calendar_error(gmail, my_email, thread_id, subject, "schedule a meeting", e)
        slot_desc = _local_slot_desc(result.slot_start, result.slot_end, config, attendee_tz)
        gmail.send_email(
            to=my_email,
            subject=f"EA: booked — {topic}",
            body=f"Booked on your calendar:\n\n  {slot_desc}\n  {topic}",
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-scheduled")
        return "scheduled"

    if result.outcome == "ambiguous":
        body = (
            "EA could not schedule — the request was ambiguous:\n\n"
            + "\n".join(f"  • {a}" for a in result.ambiguities)
            + "\n\nReply to this email with the missing details and EA will try again."
        )
        gmail.send_email(
            to=my_email,
            subject=f"EA: needs more info — {subject}",
            body=body,
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "notified-ambiguous"

    if result.outcome == "busy":
        from ea.scheduler import find_slots
        schedule = config.get("schedule", {})

        if find_slots_fn:
            slots = find_slots_fn(result.parsed, config, calendar)
        else:
            slots = find_slots(
                attendees=result.attendees,
                duration_minutes=result.duration_minutes,
                working_hours=schedule.get("working_hours", {}),
                preferred_hours=schedule.get("preferred_hours", {}),
                tz_name=schedule.get("timezone", "UTC"),
                calendar=calendar,
            )

        if slots:
            # Send alternatives to the other party on the original thread
            first_msg = original_thread.messages[0]
            if first_msg.from_addr.lower() == my_email.lower():
                recipient = first_msg.to_addr or my_email
            else:
                recipient = first_msg.from_addr
            owner_tz = config.get("schedule", {}).get("timezone", "UTC")

            gmail.send_email(
                to=recipient,
                subject=subject,
                body=_format_slot_suggestions(
                    slots,
                    preamble="The originally proposed times aren't available. Here are some alternatives:",
                    owner_tz=owner_tz,
                    attendee_tz=attendee_tz,
                ),
                thread_id=thread_id,
            )
            state.set(thread_id, {
                "type": "pending_external_reply",
                "created_at": _now(),
                "expires_at": _expiry(),
                "original_messages_seen": len(original_thread.messages),
                "topic": result.topic,
                "recipient": recipient,
                "subject": subject,
                "attendees": result.attendees,
                "duration_minutes": result.duration_minutes,
                "suggested_slots": slots,
                "attendee_tz": attendee_tz,
            })
            return "alternatives-sent"

        # No alternatives found — notify the owner
        body = (
            "EA could not schedule — the following attendees are busy:\n\n"
            + "\n".join(f"  • {a}" for a in result.busy_attendees)
            + "\n\nNo alternative slots were found in the next 7 days."
        )
        gmail.send_email(
            to=my_email,
            subject=f"EA: conflict found — {subject}",
            body=body,
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "notified-busy"

    if result.outcome == "needs_confirmation":
        slot_desc = _local_slot_desc(result.slot_start, result.slot_end, config, attendee_tz)
        body = (
            f"EA found a slot outside working hours:\n\n"
            f"  {slot_desc} ({result.slot_type})\n\n"
            "Reply 'yes' to confirm, 'no' to cancel, or describe a different time."
        )
        # Capture count before send so both Fake (mutates in-place) and Live
        # (doesn't mutate the Python object) clients are handled correctly.
        msgs_before_send = len(original_thread.messages)
        conf_msg = gmail.send_email(
            to=my_email,
            subject=f"EA: confirm slot — {subject}",
            body=body,
            thread_id=thread_id,
            extra_headers={"X-EA-Original-Thread": thread_id},
        )
        # Gmail may ignore the threadId hint when the subject differs and create a
        # new thread instead.  confirmation_messages_seen must be relative to
        # whichever thread the confirmation actually landed in.
        if conf_msg.thread_id == thread_id:
            # Same thread as original — skip original messages + this one.
            msgs_seen = msgs_before_send + 1
        else:
            # New thread created by Gmail — skip only EA's own confirmation message.
            msgs_seen = 1
            # Prevent Pass 1 from treating the new thread as a fresh EA: command.
            gmail.apply_label(conf_msg.thread_id, "ea-notified")
        state.set(thread_id, {
            "type": "pending_confirmation",
            "confirmation_thread_id": conf_msg.thread_id,
            "created_at": _now(),
            "expires_at": _expiry(),
            "confirmation_messages_seen": msgs_seen,
            "schedule_result": {
                "outcome": result.outcome,
                "slot_start": result.slot_start.isoformat(),
                "slot_end": result.slot_end.isoformat(),
                "slot_type": result.slot_type,
                "topic": result.topic,
                "attendees": result.attendees,
                "duration_minutes": result.duration_minutes,
            },
        })
        return "pending-confirmation"

    return "no-action"


# ---------------------------------------------------------------------------
# Inbound: handle a block_time result (solo calendar block, no invites)
# ---------------------------------------------------------------------------

def handle_block_time_result(
    result: ScheduleResult,
    original_thread,     # GmailThread
    gmail,
    calendar,
    state: StateStore,
    config: dict,
) -> str:
    """
    Act on a ScheduleResult from a block_time EA: trigger.
    Always creates a solo event — no invites, no after-hours confirmation.
    """
    my_email = config["user"]["email"]
    thread_id = original_thread.id

    subject = original_thread.messages[0].subject

    if result.outcome in ("open", "needs_confirmation"):
        try:
            calendar.create_event(
                topic=result.topic or "Block",
                start=result.slot_start.isoformat(),
                end=result.slot_end.isoformat(),
                attendees=[my_email],
                self_email=my_email,
            )
        except Exception as e:
            return _send_calendar_error(gmail, my_email, thread_id, subject, "block time", e)

        slot_desc = _local_slot_desc(result.slot_start, result.slot_end, config)
        gmail.send_email(
            to=my_email,
            subject=f"EA: blocked — {result.topic or subject}",
            body=f"Blocked on your calendar:\n\n  {slot_desc}\n  {result.topic or subject}",
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-scheduled")
        return "scheduled"

    if result.outcome == "ambiguous":
        body = (
            "EA could not block time — the request was ambiguous:\n\n"
            + "\n".join(f"  • {a}" for a in result.ambiguities)
        )
        gmail.send_email(
            to=my_email,
            subject=f"EA: needs more info — {subject}",
            body=body,
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "notified-ambiguous"

    if result.outcome == "busy":
        gmail.send_email(
            to=my_email,
            subject=f"EA: conflict found — {subject}",
            body="EA could not block time — you already have something scheduled then.",
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "notified-busy"

    return "no-action"


# ---------------------------------------------------------------------------
# Inbound: handle an all-day or multi-day block_time result
# ---------------------------------------------------------------------------

_TRANSPARENT_EVENT_TYPES = {"conference", "holiday"}
_DEFAULT_ALLDAY_TOPICS = {
    "ooo":        "Out of Office",
    "vacation":   "Vacation",
    "conference": None,   # use parsed topic
    "holiday":    None,   # use parsed topic
    "block":      "All Day Block",
}


def handle_allday_block(
    parsed: dict,
    original_thread,
    gmail,
    calendar,
    config: dict,
) -> str:
    """
    Create an all-day or multi-day calendar block from a parsed all_day=True result.

    Bypasses working-hours and confirmation checks entirely — OOO/vacation events
    are always created regardless of time-of-day rules.
    """
    my_email  = config["user"]["email"]
    thread_id = original_thread.id
    subject   = original_thread.messages[0].subject

    # Extract start (and optional inclusive end) date strings
    proposed = parsed.get("proposed_times") or []
    if not proposed or not proposed[0].get("datetimes"):
        gmail.send_email(
            to=my_email,
            subject=f"EA: needs more info — {subject}",
            body="EA could not determine the date(s) for the all-day event. Please specify a date.",
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "notified-ambiguous"

    dates = proposed[0]["datetimes"]
    start_date = dates[0]
    end_date_inclusive = dates[1] if len(dates) >= 2 else start_date
    # Google Calendar end date is exclusive
    end_date_exclusive = (
        date.fromisoformat(end_date_inclusive) + timedelta(days=1)
    ).isoformat()

    event_type   = (parsed.get("event_type") or "block").lower()
    transparency = "transparent" if event_type in _TRANSPARENT_EVENT_TYPES else "opaque"

    default_topic = _DEFAULT_ALLDAY_TOPICS.get(event_type)
    topic = parsed.get("topic") or default_topic or "All Day Block"

    try:
        calendar.create_event(
            topic=topic,
            start=start_date,
            end=end_date_exclusive,
            attendees=[my_email],
            self_email=my_email,
            all_day=True,
            transparency=transparency,
        )
    except Exception as e:
        return _send_calendar_error(gmail, my_email, thread_id, subject, "create an all-day event", e)

    # Format a human-readable date description for the confirmation email
    if start_date == end_date_inclusive:
        date_desc = _format_date(start_date)
    else:
        date_desc = f"{_format_date(start_date)} – {_format_date(end_date_inclusive)}"

    visibility = "informational (free)" if transparency == "transparent" else "blocking (busy)"
    gmail.send_email(
        to=my_email,
        subject=f"EA: blocked — {topic}",
        body=f"All-day event added to your calendar ({visibility}):\n\n  {date_desc}\n  {topic}",
        thread_id=thread_id,
    )
    gmail.apply_label(thread_id, "ea-scheduled")
    return "scheduled"


def _format_date(iso_date: str) -> str:
    """Format a YYYY-MM-DD string as 'Monday Mar 23, 2026'."""
    return date.fromisoformat(iso_date).strftime("%A %b %d, %Y")


# ---------------------------------------------------------------------------
# Outbound: handle a suggest_times EA: trigger
# ---------------------------------------------------------------------------

def handle_suggest_times_trigger(
    parsed: dict,
    original_thread,     # GmailThread
    gmail,
    calendar,
    state: StateStore,
    config: dict,
    find_slots_fn=None,  # injectable: fn(parsed, config, calendar) -> list[dict]
) -> str:
    """
    Act on a suggest_times EA: trigger: find free slots and send them to the
    recipient on the existing thread, then write pending_external_reply state.
    """
    from ea.scheduler import find_slots

    my_email = config["user"]["email"]
    schedule = config.get("schedule", {})
    thread_id = original_thread.id
    first_msg = original_thread.messages[0]

    duration_minutes = parsed.get("duration_minutes") or 30
    attendees_parsed = parsed.get("attendees") or []

    # Determine recipient: the person on the other side of the thread.
    # For self-addressed emails (standalone availability check), recipient is me.
    if first_msg.from_addr.lower() == my_email.lower():
        recipient = first_msg.to_addr or my_email
    else:
        recipient = first_msg.from_addr
    if not recipient or recipient.lower() == my_email.lower():
        recipient = my_email

    # Extract optional day constraint from proposed_times (e.g. "on Friday").
    # The parser captures this as a single normalized phrase; we resolve it to
    # a local date so find_slots can restrict its search to that day.
    restrict_to_date = None
    proposed_times = parsed.get("proposed_times") or []
    if proposed_times:
        raw_dt = (proposed_times[0].get("datetimes") or [None])[0]
        if raw_dt:
            tz_name_local = schedule.get("timezone", "UTC")
            restrict_to_date = (
                datetime.fromisoformat(raw_dt)
                .astimezone(ZoneInfo(tz_name_local))
                .date()
            )

    # Find slots
    all_attendees = [my_email] + [a for a in attendees_parsed if a != my_email]
    if find_slots_fn:
        slots = find_slots_fn(parsed, config, calendar)
    else:
        slots = find_slots(
            attendees=all_attendees,
            duration_minutes=duration_minutes,
            working_hours=schedule.get("working_hours", {}),
            preferred_hours=schedule.get("preferred_hours", {}),
            tz_name=schedule.get("timezone", "UTC"),
            calendar=calendar,
            restrict_to_date=restrict_to_date,
        )

    if not slots:
        gmail.send_email(
            to=my_email,
            subject=f"EA: no availability — {first_msg.subject}",
            body=(
                "EA could not find any free slots in the next 7 days.\n"
                "Check your calendar and try again."
            ),
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "no-availability"

    owner_tz   = schedule.get("timezone", "UTC")
    attendee_tz = parsed.get("timezone")
    body = _format_slot_suggestions(slots, owner_tz=owner_tz, attendee_tz=attendee_tz)
    gmail.send_email(
        to=recipient,
        subject=first_msg.subject,
        body=body,
        thread_id=thread_id,
        extra_headers={"X-EA-Original-Thread": thread_id},
    )
    state.set(thread_id, {
        "type": "pending_external_reply",
        "created_at": _now(),
        "expires_at": _expiry(),
        "original_messages_seen": len(original_thread.messages),  # send_email already appended
        "topic": parsed.get("topic"),
        "recipient": recipient,
        "subject": first_msg.subject,
        "attendees": all_attendees,
        "duration_minutes": duration_minutes,
        "suggested_slots": slots,
        "attendee_tz": attendee_tz,
    })
    return "suggestions-sent"


# ---------------------------------------------------------------------------
# Inbound: handle a reply to the private confirmation thread
# ---------------------------------------------------------------------------

def handle_confirmation_reply(
    reply_text: str,
    original_thread_id: str,
    entry: dict,
    gmail,
    calendar,
    state: StateStore,
    config: dict,
    evaluate_fn=None,    # optional: fn(reply_text, entry) -> ScheduleResult
) -> str:
    """
    Process a reply to an EA confirmation email.

    evaluate_fn is called for modification replies. In production it would
    re-run evaluate_parsed with updated constraints. In tests, pass a lambda
    that returns a ScheduleResult directly.
    """
    my_email = config["user"]["email"]
    conf_thread_id = entry["confirmation_thread_id"]
    sr = entry.get("schedule_result", {})
    lower = reply_text.lower().strip()

    # --- No / Cancel ---
    if any(w in lower for w in ("no", "cancel", "nevermind", "never mind")):
        gmail.send_email(
            to=my_email,
            subject="EA: noted",
            body="Noted — let me know if you'd like to retry.",
            thread_id=conf_thread_id,
        )
        state.delete(original_thread_id)
        gmail.apply_label(original_thread_id, "ea-cancelled")
        return "cancelled"

    # --- Yes / Confirm ---
    if any(w in lower for w in ("yes", "go ahead", "confirm", "ok", "sounds good")):
        start = datetime.fromisoformat(sr["slot_start"])
        end = datetime.fromisoformat(sr["slot_end"])
        try:
            calendar.create_event(
                topic=sr.get("topic") or "Meeting",
                start=start.isoformat(),
                end=end.isoformat(),
                attendees=sr.get("attendees", [my_email]),
                self_email=my_email,
            )
        except Exception as e:
            gmail.send_email(
                to=my_email,
                subject="EA: calendar error — confirmed slot",
                body=f"EA tried to book the confirmed slot but the calendar API returned an error:\n\n{e}",
                thread_id=conf_thread_id,
            )
            return "calendar-error"
        state.delete(original_thread_id)
        gmail.apply_label(original_thread_id, "ea-scheduled")
        return "scheduled"

    # --- Modification ---
    if evaluate_fn:
        result = evaluate_fn(reply_text, entry)
        return _apply_modification(
            result, original_thread_id, entry, conf_thread_id,
            gmail, calendar, state, my_email,
        )

    # Fallback: couldn't classify
    gmail.send_email(
        to=my_email,
        subject="EA: still unclear",
        body=(
            f"Could not interpret your reply: '{reply_text}'.\n"
            "Please reply yes, no, or describe a different time."
        ),
        thread_id=conf_thread_id,
    )
    return "still-unclear"


def _apply_modification(
    result: ScheduleResult,
    original_thread_id: str,
    entry: dict,
    conf_thread_id: str,
    gmail,
    calendar,
    state: StateStore,
    my_email: str,
) -> str:
    if result.outcome == "open":
        calendar.create_event(
            topic=result.topic or "Meeting",
            start=result.slot_start.isoformat(),
            end=result.slot_end.isoformat(),
            attendees=result.attendees,
            self_email=my_email,
        )
        state.delete(original_thread_id)
        gmail.apply_label(original_thread_id, "ea-scheduled")
        return "scheduled"

    if result.outcome == "busy":
        gmail.send_email(
            to=my_email,
            subject="EA: still busy",
            body=f"Still busy — {', '.join(result.busy_attendees)} has a conflict.",
            thread_id=conf_thread_id,
        )
        state.update(original_thread_id, {"expires_at": _expiry()})
        return "still-busy"

    if result.outcome == "ambiguous":
        gmail.send_email(
            to=my_email,
            subject="EA: still unclear",
            body=f"Still unclear: {'; '.join(result.ambiguities)}",
            thread_id=conf_thread_id,
        )
        state.update(original_thread_id, {"expires_at": _expiry()})
        return "still-ambiguous"

    if result.outcome == "needs_confirmation":
        slot_desc = result.slot_start.strftime("%A %b %d at %I:%M %p")
        gmail.send_email(
            to=my_email,
            subject="EA: new proposed slot",
            body=(
                f"New slot: {slot_desc} (after hours). Reply yes/no."
            ),
            thread_id=conf_thread_id,
        )
        state.update(original_thread_id, {
            "expires_at": _expiry(),
            "schedule_result": {
                "outcome": result.outcome,
                "slot_start": result.slot_start.isoformat(),
                "slot_end": result.slot_end.isoformat(),
                "slot_type": result.slot_type,
                "topic": result.topic,
                "attendees": result.attendees,
                "duration_minutes": result.duration_minutes,
            },
        })
        return "needs-confirmation-updated"

    return "no-action"


# ---------------------------------------------------------------------------
# Outbound: handle a reply to an EA time-suggestion thread
# ---------------------------------------------------------------------------

def handle_external_reply(
    reply_text: str,
    original_thread_id: str,
    entry: dict,
    gmail,
    calendar,
    state: StateStore,
    config: dict,
    find_slots_fn=None,   # optional: fn(constraint, entry) -> list[dict] (suggested_slots)
) -> str:
    """
    Process a reply from the external party on an outbound suggestion thread.

    find_slots_fn(constraint_text, entry) -> list[{"start": ISO, "end": ISO, "slot_type": ...}]
    Used for counter-proposals. In tests, inject a lambda returning canned slots.
    """
    my_email    = config["user"]["email"]
    owner_tz    = config.get("schedule", {}).get("timezone", "UTC")
    attendee_tz = entry.get("attendee_tz")
    suggested_slots = entry.get("suggested_slots", [])

    lower = reply_text.lower().strip()

    # Classification is delegated to find_slots_fn (injected by the caller).
    if find_slots_fn:
        action, slots_or_slot = find_slots_fn(reply_text, entry)

        if action == "confirmed":
            slot = slots_or_slot  # single slot dict
            topic = entry.get("topic") or "Meeting"
            calendar.create_event(
                topic=topic,
                start=slot["start"],
                end=slot["end"],
                attendees=entry.get("attendees", [my_email]),
                self_email=my_email,
            )
            slot_desc = _local_slot_desc(
                datetime.fromisoformat(slot["start"]),
                datetime.fromisoformat(slot["end"]),
                config,
            )
            gmail.send_email(
                to=my_email,
                subject=f"EA: booked — {topic}",
                body=f"Booked on your calendar:\n\n  {slot_desc}\n  {topic}",
                thread_id=original_thread_id,
            )
            state.delete(original_thread_id)
            gmail.apply_label(original_thread_id, "ea-scheduled")
            return "scheduled"

        if action == "slot_taken":
            new_slots = slots_or_slot  # list of new slot dicts
            gmail.send_email(
                to=entry["recipient"],
                subject=entry.get("subject", "Re: meeting"),
                body=_format_slot_suggestions(
                    new_slots,
                    "That slot was just taken — here are some alternatives:",
                    owner_tz=owner_tz,
                    attendee_tz=attendee_tz,
                ),
                thread_id=original_thread_id,
            )
            state.update(original_thread_id, {
                "suggested_slots": new_slots,
                "expires_at": _expiry(),
            })
            return "slot-taken-new-options"

        if action == "counter":
            new_slots = slots_or_slot  # list of new slot dicts
            gmail.send_email(
                to=entry["recipient"],
                subject=entry.get("subject", "Re: meeting"),
                body=_format_slot_suggestions(new_slots, owner_tz=owner_tz, attendee_tz=attendee_tz),
                thread_id=original_thread_id,
            )
            state.update(original_thread_id, {
                "suggested_slots": new_slots,
                "expires_at": _expiry(),
            })
            return "counter-new-options"

    return "no-action"


# ---------------------------------------------------------------------------
# Cancel: handle a cancel_event EA: trigger
# ---------------------------------------------------------------------------

def handle_cancel_result(
    match,               # dict (found), list[dict] (ambiguous), or None (not found)
    parsed: dict,
    original_thread,
    gmail,
    calendar,
    state: StateStore,
    config: dict,
) -> str:
    """
    Act on the result of find_matching_event for a cancel_event intent.

    match=dict  → delete the event, notify owner, label thread ea-scheduled
    match=list  → multiple candidates found; notify owner to be more specific
    match=None  → no event found; notify owner
    """
    my_email = config["user"]["email"]
    thread_id = original_thread.id
    subject = original_thread.messages[0].subject
    topic = parsed.get("topic") or "event"

    if match is None:
        return _handle_event_not_found(gmail, my_email, thread_id, topic)

    if isinstance(match, list):
        return _handle_event_ambiguous(gmail, my_email, thread_id, topic, match, "cancel")

    # Single match — delete it
    event_id = match["id"]
    summary  = match.get("summary", topic)
    attendees = [a["email"] for a in (match.get("attendees") or [])]
    solo = not attendees or attendees == [my_email]
    try:
        calendar.delete_event(event_id, send_updates=not solo)
    except Exception as e:
        gmail.send_email(
            to=my_email,
            subject=f"EA: calendar error — {subject}",
            body=f"EA tried to cancel '{summary}' but the calendar API returned an error:\n\n{e}",
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "calendar-error"

    gmail.send_email(
        to=my_email,
        subject=f"EA: cancelled — {summary}",
        body=f"Cancelled:\n\n  {summary}",
        thread_id=thread_id,
    )
    gmail.apply_label(thread_id, "ea-scheduled")
    return "cancelled"


# ---------------------------------------------------------------------------
# Reschedule: handle a reschedule EA: trigger
# ---------------------------------------------------------------------------

def handle_reschedule_result(
    match,               # dict (found), list[dict] (ambiguous), or None (not found)
    parsed: dict,
    original_thread,
    gmail,
    calendar,
    state: StateStore,
    config: dict,
) -> str:
    """
    Act on the result of find_matching_event for a reschedule intent.

    Locates the new time from parsed["new_proposed_times"][0].datetimes[0].
    Checks if the new slot is free, then updates the event.
    """
    from ea.scheduler import check_slot

    my_email = config["user"]["email"]
    thread_id = original_thread.id
    subject = original_thread.messages[0].subject
    topic = parsed.get("topic") or "event"
    schedule = config.get("schedule", {})

    # --- handle not-found / ambiguous the same way as cancel ---
    if match is None:
        return _handle_event_not_found(gmail, my_email, thread_id, topic)

    if isinstance(match, list):
        return _handle_event_ambiguous(gmail, my_email, thread_id, topic, match, "reschedule")

    # --- resolve new time ---
    new_times = parsed.get("new_proposed_times") or []
    if not new_times or not new_times[0].get("datetimes"):
        gmail.send_email(
            to=my_email,
            subject=f"EA: no new time specified — {topic}",
            body="EA could not determine the new time. Please specify when to move the event.",
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "notified-no-new-time"

    new_start_iso = new_times[0]["datetimes"][0]
    new_start = datetime.fromisoformat(new_start_iso.replace("Z", "+00:00"))

    # Duration: use parser value if given, else derive from existing event
    duration = parsed.get("duration_minutes")
    if not duration:
        try:
            ex_start = datetime.fromisoformat(
                match["start"]["dateTime"].replace("Z", "+00:00")
            )
            ex_end = datetime.fromisoformat(
                match["end"]["dateTime"].replace("Z", "+00:00")
            )
            duration = int((ex_end - ex_start).total_seconds() / 60)
        except (KeyError, ValueError):
            duration = 30

    new_end = new_start + timedelta(minutes=duration)

    # --- check if new slot is free ---
    attendees = [a["email"] for a in (match.get("attendees") or [])]
    if not attendees:
        attendees = [my_email]

    slot = check_slot(
        new_start, new_end, attendees,
        schedule.get("working_hours", {}),
        schedule.get("preferred_hours", {}),
        calendar,
        schedule.get("timezone", "UTC"),
    )

    if not slot.free:
        gmail.send_email(
            to=my_email,
            subject=f"EA: conflict — cannot reschedule {topic}",
            body=(
                f"EA cannot move '{match.get('summary', topic)}' to the new time —\n"
                f"the following attendees are busy: {', '.join(slot.busy_attendees)}"
            ),
            thread_id=thread_id,
        )
        gmail.apply_label(thread_id, "ea-notified")
        return "notified-busy"

    # --- update the event ---
    event_id = match["id"]
    summary  = match.get("summary", topic)
    try:
        calendar.update_event(event_id, new_start.isoformat(), new_end.isoformat())
    except Exception as e:
        return _send_calendar_error(gmail, my_email, thread_id, subject, f"reschedule '{summary}'", e)

    attendee_tz = parsed.get("timezone")
    slot_desc = _local_slot_desc(new_start, new_end, config, attendee_tz)
    gmail.send_email(
        to=my_email,
        subject=f"EA: rescheduled — {summary}",
        body=f"Moved on your calendar:\n\n  {summary}\n  {slot_desc}",
        thread_id=thread_id,
    )
    gmail.apply_label(thread_id, "ea-scheduled")
    return "rescheduled"


def _format_slot_suggestions(
    slots: list[dict],
    preamble: str = "Here are some times that work:",
    owner_tz: str = "UTC",
    attendee_tz: str | None = None,
) -> str:
    """
    Format a list of slots for the external party.

    If attendee_tz is set and differs from owner_tz, each slot is shown in the
    attendee's timezone first, with the owner's time in parentheses:
      "Thursday Mar 19, 02:00–02:30 PM EDT (11:00–11:30 AM PDT my time)"
    Otherwise slots are shown in owner_tz only.
    """
    show_both = bool(attendee_tz and attendee_tz != owner_tz)
    try:
        primary_tz = ZoneInfo(attendee_tz if show_both else owner_tz)
    except ZoneInfoNotFoundError:
        primary_tz = ZoneInfo(owner_tz)
        show_both = False

    owner_tz_obj = ZoneInfo(owner_tz)

    lines = [preamble, ""]
    for slot in slots:
        start = datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
        end   = datetime.fromisoformat(slot["end"].replace("Z", "+00:00"))

        p_start = start.astimezone(primary_tz)
        p_end   = end.astimezone(primary_tz)
        line = _fmt_range(p_start, p_end)
        if show_both:
            o_start = start.astimezone(owner_tz_obj)
            o_end   = end.astimezone(owner_tz_obj)
            line += (
                " (" +
                o_start.strftime("%I:%M") +
                o_end.strftime("–%I:%M %p ") +
                o_start.strftime("%Z") +
                " my time)"
            )
        lines.append(f"  • {line}")
    lines.append("\nPlease let me know which works for you.")
    return "\n".join(lines)
