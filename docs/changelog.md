# EA Changelog

Completed features, newest first within each section.

---

## Security

### SEC-2. Scheduling-scope enforcement (prompt injection defense)
The parser calls Claude with user-controlled email text as input. A malicious
or accidental EA: command could contain instructions designed to manipulate
Claude's output — returning an intent or action that has nothing to do with
scheduling.

**Defense strategy — allowlist on the parsed output, not the input:**
Rather than trying to sanitize the raw email text before it reaches Claude
(brittle and incomplete), validate the *structured output* of `parse_meeting_request`
against a strict allowlist of permitted intents and field shapes.

**Two layers:**

**Layer 1 — Intent allowlist (`ea/poll.py`):**
`_ALLOWED_INTENTS` set gates the Pass 1 dispatch block. Any intent not in the
set is logged at WARNING ("possible prompt injection") and redirected to the
parse-error email path. Known intents: `meeting_request`, `suggest_times`,
`block_time`, `cancel_event`, `reschedule`, `ignore`, `none`.

**Layer 2 — Field content validation (`ea/parser/meeting_parser.py`):**
`validate_parsed(parsed, thread_id)` is called immediately after `json.loads`,
before datetime normalization. Raises `ValueError` on any violation. Checks:

- `topic`: string, no newlines/CR, ≤ 200 chars
- `attendees`: list of strings, no newlines/CR, each ≤ 200 chars
- `duration_minutes`: 1–480 if present
- `proposed_times[*].datetimes`: valid ISO 8601; not > 30 days past or > 2
  years future. All-day `YYYY-MM-DD` strings skip the range check.

**Tests:** `tests/test_security.py` — 36 tests covering both layers.

### SEC-1. Strict sender verification
EA only acts on `EA:` commands from the owner's own email address
(`config.user.email`). The old check used a substring match (`in`), which
allowed spoofed senders like `attacker+nickg@example.com` or
`nickg@example.com.evil.com` to inject commands.

**Fix:** `_find_ea_trigger_in_messages` now uses `email.utils.parseaddr` to
extract the bare address from the From header (stripping display names) and
compares with `==`.

**Tests added** (`tests/test_subject_trigger.py`):
- `attacker+me@example.com` → blocked
- Display name spoof (`me@example.com <attacker@evil.com>`) → blocked
- Subdomain spoof (`me@example.com.evil.com`) → blocked
- Legitimate display-name form (`Nick G <me@example.com>`) → still triggers

---

## Operations / Production

### OP-3. macOS launchd startup file
Uses `StartInterval` (cron-like: start process → one cycle → exit) rather than
`KeepAlive` (persistent process). Pairs with `poll --quiet` so no stdout
reaches launchd and no notification emails are triggered.

Files:
- `docs/launchd/com.ea.poll.plist` — plist template (fill in 3 CHANGEME values)
- `scripts/install-launchd.sh` — install script; auto-fills paths, prompts for
  API key (or sources `~/.ea-env` if present), loads the agent

Quick setup:
```
bash scripts/install-launchd.sh
```

Key plist settings:
- `StartInterval = 300` — run one poll cycle every 5 minutes (match `poll_interval_seconds`)
- `RunAtLoad = true` — runs immediately on load / reboot
- `StandardOutPath = /dev/null` — stdout suppressed by `--quiet`, nothing to capture
- `StandardErrorPath = ea-launchd.err` — catches Python startup errors before logging is configured

Commands:
```bash
launchctl list com.ea.poll          # check status / last exit code
launchctl start com.ea.poll         # trigger one cycle immediately
launchctl stop com.ea.poll          # stop current run
launchctl unload ~/Library/LaunchAgents/com.ea.poll.plist   # remove agent
tail -f ea.log                       # watch structured JSON logs
```

### OP-2. Run-once / cron mode
`python ea.py poll --quiet` is safe for unattended invocation.

- **Exit codes:** exits `0` on clean run; exits `1` on any unrecoverable error.
- **`--quiet` flag:** suppresses all stdout. All output routes to `ea.log` only.
  Use this whenever launchd/cron invokes the process.
- **Lock interaction:** when `.state.lock` is held, `run_once()` logs a warning
  and returns an empty summary. Exits `0`.
- **Cron example** (every 5 minutes, quiet):
  ```
  */5 * * * * cd /path/to/ea && .venv/bin/python ea.py poll --quiet
  ```

### OP-1. Graceful network error handling
`ea/network.py` — module-level retry utility.

- `configure(attempts, base_delay, cap)` — sets retry policy. Default: `attempts=1`
  (no retry), suitable for cron/poll mode. `run_loop()` calls
  `configure(attempts=3, base_delay=1.0, cap=poll_interval_seconds)`.
- `call_with_retry(fn)` — wraps a callable; retries on transient errors with
  exponential backoff; raises immediately on permanent errors.
- `is_transient_error(exc)` — recognizes `requests.ConnectionError/Timeout`,
  `googleapiclient.errors.HttpError` (429 / 5xx), `anthropic.APIConnectionError`,
  `anthropic.RateLimitError`, `socket.timeout`, `TimeoutError`, OS network errno.
- All live API calls in `gmail.py`, `calendar.py`, `meeting_parser.py`, and
  `classifier.py` are wrapped with `call_with_retry`.

---

## Polish

### 19. Ignore / dismiss a request
An escape hatch for dropping any pending scheduling request without taking
action.

**Trigger:** reply to the thread (or the EA confirmation email) with:
```
EA: ignore
EA: dismiss
EA: never mind
EA: forget it
```

**What it does:**
1. Removes the thread from `StateStore`.
2. Applies the `ea-cancelled` label so Pass 1 never picks it up again.
3. Sends a brief confirmation email to the owner.

For `pending_external_reply`, the dismiss does **not** send a cancellation email
to the external party. The confirmation email to the owner notes this.

**Implementation notes:**
- **Pass 1 bypass:** `_find_ea_trigger_in_messages` is called *before* the
  `state.get(thread.id)` skip check. If the ea_cmd matches `_DISMISS_RE`
  (`ignore|dismiss|never mind|forget it`), the thread is allowed through even
  when in state.
- **`pending_confirmation` dismissal:** `handle_ignore_result` scans all state
  entries to find the one whose `confirmation_thread_id` matches.
- **`"ignore"` intent** added to the parser schema. `_DISMISS_RE` in `poll.py`
  provides fast pre-parse detection without an API call.
- **CLI `ea dismiss <thread_id>`**: deletes from state, optionally applies
  Gmail label. Prints a note if `pending_external_reply`.

### 18. `ea status` command
CLI command that prints all pending state entries as a human-readable table.

```
python ea.py status
```

Output:
```
THREAD           TYPE                    TOPIC              ATTENDEES                   EXPIRES
18e4a3c2f1b0    pending_confirmation    Coffee chat         sarah@example.com           12h 2m
18e4a3c2f2a1    pending_external_reply  Q2 budget review    boss@company.com            6d 3h
18e4a3c2f3b2    pending_confirmation    Board meeting       chair@board.org             EXPIRED
```

Implemented in `ea.py` (`_run_status()`). Reads from `StateStore.all()`.
Expiry shown as relative time (e.g. `12h 2m`, `6d 3h`, `EXPIRED`).

### 13. EA command in email subject
When `_find_ea_trigger_in_messages` finds no `EA:` command in any message body,
also check each message's subject line. Only matches if the subject starts with
`EA:` after stripping reply/forward prefixes (`Re:`, `Fwd:`, etc.).

Also fixed latent bug: quoted lines (lines prefixed with `>`) are now stripped
from message bodies before scanning, preventing re-matching old commands from
quoted replies.

**Implementation:**
- `ea/poll.py` — `_find_ea_trigger_in_messages`:
  1. Strip quoted lines (`^>.*`) from each message body before the body scan.
  2. After the body scan returns `None`, check `msg.subject` using
     `r'^(?:Re|Fwd?|AW|WG):\s*'` (case-insensitive) to detect and skip prefixed subjects.

### 12. Custom email footer
Appends a configurable text block to every outgoing EA email.

```toml
[user]
email_footer = "I'm testing an AI scheduling assistant — please bear with any rough edges."
```

**Implementation:** `FooterGmailClient` in `ea/gmail.py` is a decorator that
wraps any gmail client and intercepts `send_email`, appending
`"\n\n---\n{footer}"`. `runner.py` wraps the live client when the config key
is present — one change point, zero changes to `responder.py` or `poll.py`.

---

## Quality of Life

### 9. Meeting duration defaults by topic type
When the parser can't determine duration, fall back to a config-driven default
based on detected meeting type rather than failing with an ambiguity.

```toml
[schedule.duration_defaults]
coffee_chat  = 30
interview    = 60
1on1         = 30
board        = 90
standup      = 15
default      = 30   # global fallback when meeting_type is unknown or not in table
```

The parser returns a `meeting_type` field (inferred from email context by Claude
during the existing parse call — no extra API call). `poll.py`'s `_resolve_duration()`
helper fills in `duration_minutes` before `evaluate_parsed()` is called.

**`meeting_type` values:** `"coffee_chat"` | `"interview"` | `"1on1"` | `"board"` |
`"standup"` | `"workshop"` | `"lunch"` | `null`

**Implementation:**
- `ea/parser/meeting_parser.py` — `meeting_type` field added to system prompt schema
- `ea/poll.py` — `_resolve_duration(parsed, config)` helper; called before
  `evaluate_parsed()` for `meeting_request` and `block_time`, and before
  `handle_suggest_times_trigger()` for `suggest_times`

### 8. Timezone-aware invite bodies
When the attendee's timezone (from `parsed["timezone"]`) differs from the
owner's, both times now appear in every scheduling email.

- **Emails to owner** (`_local_slot_desc`): owner's tz primary, attendee's in
  parens — e.g. `"Thursday Mar 19, 02:00–02:30 PM EDT (11:00–11:30 AM PDT for them)"`
- **Emails to external party** (`_format_slot_suggestions`): attendee's tz
  primary, owner's in parens.
- `attendee_tz` is saved to state so counter-proposal resends also use it.
- When timezones match (or attendee tz is unknown), single-tz format is used.

### 8. Daily digest
An automated daily email (not triggered by a thread) summarizing today's
calendar events and pending EA state entries.

**Implementation:**
- `ea/digest.py` — all digest logic: `should_send_digest`, `already_sent_today`,
  `mark_sent_today`, `get_today_window`, `format_event_line`, `build_digest`
- `ea/runner.py` — digest check runs inside `run_once()` after `run_poll()`,
  inside the existing `.state.lock`; sends once per day when `send_time` is reached
- `ea.py digest` — CLI subcommand prints body to stdout (preview only, no email)
- `digest_sent.json` — dedup file; stores `{"last_sent": "YYYY-MM-DD"}`

**Config** (`config.toml`):
```toml
[digest]
days      = ["monday", "tuesday", "wednesday", "thursday", "friday"]
send_time = "08:00"   # local time; defaults to "08:00" if omitted
```
Omit `[digest]` or set `days = []` to disable.

---

## Core Scheduling

### 6. All-day and multi-day events (out of office)
Parser, scheduler, and calendar client extended to support all-day and
multi-day events.

**Two sub-types:**
- **Blocking all-day** — `transparency: "opaque"`. Used for OOO, vacation, sick days.
- **Informational all-day** — `transparency: "transparent"`. Used for conferences,
  public holidays, reminders.

**Parser changes:**
- `"all_day": true | false` field added to output schema.
- `"event_type": "ooo" | "vacation" | "conference" | "holiday" | "block"` field
  added as a transparency hint.
- `proposed_times[*].datetimes` contains date-only ISO strings (`"2026-04-10"`)
  for all-day events. For ranges: two entries `[start_date, end_date_inclusive]`.
- `block_time` intent is reused; `"all_day": true` distinguishes the case.

**`CalendarClient.create_event` changes:**
- Accept `all_day: bool = False` and `end_date: str = None` parameters.
- When `all_day=True`, build `start/end` with `date` keys; omit `timeZone`.
- Set `transparency` from `event_type`: `"ooo"` / `"vacation"` / `"block"` →
  `"opaque"`; `"conference"` / `"holiday"` → `"transparent"`.

**`handle_block_time_result` changes:**
Working-hours and preferred-hours checks are skipped for all-day events.
The `needs_confirmation` path is also skipped.

**Example commands:**
```
EA: I'm out of office Monday
EA: mark next week as vacation (Mon-Fri)
EA: out of office April 10th through April 14th
EA: add PyCon April 15-17 (informational, still free)
```

### 5. Auto-suggest when all proposed slots are busy
When `outcome="busy"`, `handle_inbound_result` now runs `find_slots` before
giving up. If alternatives are found, they are sent to the external party on
the original thread and state is written as `pending_external_reply`. If no
alternatives exist, the owner is notified ("conflict found, no alternatives in
next 7 days").

### 2. Reschedule / cancel existing events
New intents `cancel_event` and `reschedule` are fully implemented.

**New `CalendarClient` methods** (`list_events`, `delete_event`, `update_event`) work
in both fixture (tests) and live (Google API) modes.

**`find_matching_event(topic, search_datetimes, calendar, tz_name)`** in `scheduler.py`:
searches the whole local day of the proposed time; scores events by title word overlap;
returns a single dict (exact match), list (ambiguous), or None (not found).

**Handlers in `responder.py`**:
- `handle_cancel_result` — deletes event, notifies owner; handles not-found and
  ambiguous cases gracefully.
- `handle_reschedule_result` — checks new slot free via `check_slot`; updates event;
  preserves existing duration when `duration_minutes` is null; handles not-found,
  ambiguous, busy, and missing-new-time cases.

**`poll.py`** dispatches both intents in Pass 1 with an injectable `find_event_fn`
for tests. **Parser** updated with new intents and `new_proposed_times` field.
