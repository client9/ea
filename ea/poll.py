"""
poll.py

Three-pass poll loop (see docs/state-machine.md for the full description).

  Pass 1 — New EA: triggers on unlabeled threads
  Pass 2 — Pending confirmations (inbound after-hours slot)
  Pass 3 — Pending external replies (outbound time suggestions)

All external dependencies (gmail, calendar, state, parser, classifiers) are
injected, making the loop fully testable without real APIs.
"""

import logging
import re
from datetime import datetime, timezone as tz_utc
from email.utils import parseaddr

from ea.gmail import thread_to_text
from ea.responder import (
    handle_inbound_result,
    handle_block_time_result,
    handle_suggest_times_trigger,
    handle_cancel_result,
    handle_reschedule_result,
    handle_confirmation_reply,
    handle_external_reply,
    _expiry,
)
from ea.scheduler import evaluate_parsed
from ea.state import StateStore

EA_TERMINAL_LABELS = {"ea-scheduled", "ea-notified", "ea-cancelled", "ea-expired"}


def _item(thread_id, action, **extra) -> dict:
    return {
        "thread_id": thread_id,
        "action": action,
        "timestamp": datetime.now(tz_utc.utc).strftime("%H:%M:%S"),
        **extra,
    }


def run_poll(
    gmail,           # GmailClient (live or FakeGmailClient)
    calendar,        # CalendarClient (live or fixture)
    state: StateStore,
    config: dict,
    *,
    parser=None,              # fn(thread_text) -> parsed dict; defaults to parse_meeting_request
    confirm_eval_fn=None,     # fn(reply_text, entry) -> ScheduleResult; for modification replies
    external_reply_fn=None,   # fn(reply_text, entry) -> (action, slots); for outbound replies
    find_slots_fn=None,       # fn(parsed, config, calendar) -> list[dict]; for suggest_times
    find_event_fn=None,       # fn(parsed, calendar, tz_name) -> dict|list|None; for cancel/reschedule
    dry_run: bool = False,    # log actions but skip all sends/creates/labels
) -> dict:
    """
    Run one full poll cycle.

    Returns a summary dict:
      {
        "pass1": [{"thread_id": ..., "action": ...}, ...],
        "pass2": [{"thread_id": ..., "action": ...}, ...],
        "pass3": [{"thread_id": ..., "action": ...}, ...],
        "expired": [{"thread_id": ..., "action": "expired"}, ...],
      }
    """
    my_email = config["user"]["email"]
    schedule = config.get("schedule", {})

    if parser is None:
        from ea.parser.meeting_parser import parse_meeting_request
        tz_name = schedule.get("timezone", "UTC")
        parser = lambda text: parse_meeting_request(text, tz_name=tz_name)
    working_hours = schedule.get("working_hours", {})
    preferred_hours = schedule.get("preferred_hours", {})
    timezone_name = schedule.get("timezone", "UTC")

    summary = {"pass1": [], "pass2": [], "pass3": [], "expired": []}
    _log = logging.getLogger("ea.poll")

    # ------------------------------------------------------------------
    # Expiry check — runs before passes so expired entries are removed
    # ------------------------------------------------------------------
    for thread_id, entry in state.expired():
        topic = (entry.get("schedule_result") or entry).get("topic")
        if dry_run:
            print(f"[dry-run] would expire {thread_id}")
        else:
            gmail.send_email(
                to=my_email,
                subject="EA: reply window lapsed",
                body="No reply received — the pending scheduling request has expired.",
            )
            gmail.apply_label(thread_id, "ea-expired")
            state.delete(thread_id)
        _log.info(
            "expired %s: %s", thread_id, topic or "(no topic)",
            extra={"thread_id": thread_id, "action": "expired", "topic": topic},
        )
        summary["expired"].append(_item(
            thread_id, "expired",
            state_type=entry.get("type"),
            topic=topic,
        ))

    # ------------------------------------------------------------------
    # Pass 1 — New EA: triggers
    # ------------------------------------------------------------------
    threads = gmail.list_threads(exclude_label_ids=EA_TERMINAL_LABELS)
    for thread in threads:
        # Skip threads already in state (already being handled)
        if state.get(thread.id):
            continue

        ea_cmd = _find_ea_trigger_in_messages(thread.messages, my_email)
        if ea_cmd is None:
            continue

        subject = thread.messages[0].subject if thread.messages else ""
        try:
            thread_text = thread_to_text(thread)
            parsed = parser(thread_text)
            intent = parsed.get("intent")
            topic  = parsed.get("topic") or subject

            if dry_run:
                print(f"[dry-run] thread {thread.id}: intent={intent}, subject={subject!r}")
                action = f"dry-run-{intent or 'none'}"
            elif intent == "suggest_times":
                action = handle_suggest_times_trigger(
                    parsed, thread, gmail, calendar, state, config,
                    find_slots_fn=find_slots_fn,
                )
            elif intent == "block_time":
                if parsed.get("all_day"):
                    from ea.responder import handle_allday_block
                    action = handle_allday_block(parsed, thread, gmail, calendar, config)
                else:
                    result = evaluate_parsed(
                        parsed=parsed,
                        working_hours=working_hours,
                        preferred_hours=preferred_hours,
                        timezone=timezone_name,
                        calendar=calendar,
                        my_email=my_email,
                    )
                    action = handle_block_time_result(result, thread, gmail, calendar, state, config)
            elif intent == "meeting_request":
                result = evaluate_parsed(
                    parsed=parsed,
                    working_hours=working_hours,
                    preferred_hours=preferred_hours,
                    timezone=timezone_name,
                    calendar=calendar,
                    my_email=my_email,
                )
                action = handle_inbound_result(result, thread, gmail, calendar, state, config,
                                           find_slots_fn=find_slots_fn)
            elif intent in ("cancel_event", "reschedule"):
                from ea.scheduler import find_matching_event
                search_dts = []
                for pt in parsed.get("proposed_times") or []:
                    search_dts.extend(pt.get("datetimes") or [])
                if find_event_fn:
                    match = find_event_fn(parsed, calendar, timezone_name)
                else:
                    match = find_matching_event(
                        topic=parsed.get("topic") or "",
                        search_datetimes=search_dts,
                        calendar=calendar,
                        tz_name=timezone_name,
                    )
                if intent == "cancel_event":
                    action = handle_cancel_result(match, parsed, thread, gmail, calendar, state, config)
                else:
                    action = handle_reschedule_result(match, parsed, thread, gmail, calendar, state, config)
            else:
                if not dry_run:
                    import json as _json
                    gmail.send_email(
                        to=my_email,
                        subject=f"EA: could not parse — {subject}",
                        body=(
                            f"EA found an EA: command but could not determine what to do.\n\n"
                            f"Intent returned: {intent!r}\n\n"
                            f"Parsed output:\n{_json.dumps(parsed, indent=2, default=str)}"
                        ),
                        thread_id=thread.id,
                    )
                    gmail.apply_label(thread.id, "ea-notified")
                action = "notified-parse-error"
        except Exception as exc:
            _log.error(
                f"Pass 1 error on thread {thread.id} ({subject!r}): {exc}",
                exc_info=True,
                extra={"thread_id": thread.id},
            )
            intent = None
            topic  = subject
            action = "error"

        _log.info(
            "pass1 %s: %s (intent=%s topic=%r subject=%r)",
            thread.id, action, intent, topic, subject,
            extra={"thread_id": thread.id, "action": action, "intent": intent, "topic": topic},
        )
        summary["pass1"].append(_item(
            thread.id, action,
            intent=intent,
            topic=topic,
            subject=subject,
        ))

    # ------------------------------------------------------------------
    # Pass 2 — Pending confirmations (inbound)
    # ------------------------------------------------------------------
    for thread_id, entry in state.pending_confirmations():
        conf_thread = gmail.get_thread(entry["confirmation_thread_id"])
        if conf_thread is None:
            continue
        seen = entry.get("confirmation_messages_seen", 1)
        new_messages = conf_thread.messages[seen:]
        my_replies = [m for m in new_messages if m.from_addr.lower() == my_email.lower()]
        if not my_replies:
            continue

        try:
            reply_text = my_replies[-1].body
            action = handle_confirmation_reply(
                reply_text=reply_text,
                original_thread_id=thread_id,
                entry=entry,
                gmail=gmail,
                calendar=calendar,
                state=state,
                config=config,
                evaluate_fn=confirm_eval_fn,
            )
            # Update seen count (only if thread still exists in state)
            if state.get(thread_id):
                state.update(thread_id, {
                    "confirmation_messages_seen": len(conf_thread.messages),
                })
        except Exception as exc:
            _log.error(
                f"Pass 2 error on thread {thread_id}: {exc}",
                exc_info=True,
                extra={"thread_id": thread_id},
            )
            action = "error"

        _log.info(
            "pass2 %s: %s (topic=%r)",
            thread_id, action, entry.get("schedule_result", {}).get("topic"),
            extra={"thread_id": thread_id, "action": action,
                   "topic": entry.get("schedule_result", {}).get("topic")},
        )
        summary["pass2"].append(_item(
            thread_id, action,
            state_type="pending_confirmation",
            topic=entry.get("schedule_result", {}).get("topic"),
        ))

    # ------------------------------------------------------------------
    # Pass 3 — Pending external replies (outbound)
    # ------------------------------------------------------------------
    for thread_id, entry in state.pending_external_replies():
        orig_thread = gmail.get_thread(thread_id)
        if orig_thread is None:
            continue
        seen = entry.get("original_messages_seen", 1)
        new_messages = orig_thread.messages[seen:]
        their_replies = [m for m in new_messages if m.from_addr.lower() != my_email.lower()]
        if not their_replies:
            continue

        try:
            reply_text = their_replies[-1].body
            action = handle_external_reply(
                reply_text=reply_text,
                original_thread_id=thread_id,
                entry=entry,
                gmail=gmail,
                calendar=calendar,
                state=state,
                config=config,
                find_slots_fn=external_reply_fn,
            )
            if state.get(thread_id):
                state.update(thread_id, {
                    "original_messages_seen": len(orig_thread.messages),
                })
        except Exception as exc:
            _log.error(
                f"Pass 3 error on thread {thread_id}: {exc}",
                exc_info=True,
                extra={"thread_id": thread_id},
            )
            action = "error"

        _log.info(
            "pass3 %s: %s (topic=%r)",
            thread_id, action, entry.get("topic"),
            extra={"thread_id": thread_id, "action": action, "topic": entry.get("topic")},
        )
        summary["pass3"].append(_item(
            thread_id, action,
            state_type="pending_external_reply",
            topic=entry.get("topic"),
        ))

    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_REPLY_PREFIX_RE = re.compile(r'^(?:Re|Fwd?|AW|WG):\s*', re.IGNORECASE)
_QUOTED_LINE_RE  = re.compile(r'^>.*$', re.MULTILINE)


def _find_ea_trigger_in_messages(messages, my_email: str) -> str | None:
    """Scan messages from my_email for an EA: command.

    Body is checked first (quoted lines stripped so a prior EA: command in a
    reply chain doesn't re-fire). Subject is checked as a fallback, but only
    when it starts directly with 'EA:' — subjects that begin with Re:/Fwd:/etc.
    are ignored so reply threads on an EA-subject email don't re-trigger.
    """
    subject_cmd = None
    for msg in messages:
        _, addr = parseaddr(msg.from_addr)
        if addr.lower() != my_email.lower():
            continue

        # Body scan — strip quoted lines (lines starting with >) first
        clean_body = _QUOTED_LINE_RE.sub("", msg.body)
        match = re.search(r'EA:\s*(.+)', clean_body, re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # Subject scan — only on first qualifying message; skip reply prefixes
        if subject_cmd is None:
            subject = msg.subject.strip()
            if not _REPLY_PREFIX_RE.match(subject):
                match = re.match(r'EA:\s*(.+)', subject, re.IGNORECASE)
                if match:
                    subject_cmd = match.group(1).strip()

    return subject_cmd
