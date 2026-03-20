# EA Feature Roadmap

Features are grouped by priority. "High" items address gaps in the current core
flow. "Medium" items improve quality of life. "Lower" items are polish. "Bigger
swings" require new infrastructure.

---

## High Impact — Core Scheduling Gaps

### 1. Recurring meeting support
Teach the parser to recognize recurrence patterns ("weekly", "every Thursday",
"biweekly"). Pass a `recurrence` field (RRULE format) to `create_event`.

- Parser: add `recurrence` field to JSON schema (e.g. `"RRULE:FREQ=WEEKLY"`)
- `CalendarClient.create_event`: accept and forward `recurrence` param
- Example: "EA: set up a weekly 1:1 with sarah@ on Thursdays at 10am"

### 2. Reschedule / cancel existing events ✅ DONE
New intents `cancel_event` and `reschedule` are fully implemented.

**New `CalendarClient` methods** (`list_events`, `delete_event`, `update_event`) work
in both fixture (tests) and live (Google API) modes. Fixture deep-copies event dicts
to prevent test-isolation mutations.

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
for tests. **Parser** updated with new intents and `new_proposed_times` field
(same normalized→UTC pipeline as `proposed_times`).

### 3. Meeting prep buffer
Config option to auto-block time before/after meetings. Creates a second solo
block event around the main calendar event. No new parser work needed — just a
post-booking step in `handle_inbound_result` and `handle_external_reply`.

```toml
[schedule]
prep_buffer_minutes  = 15   # block before meeting
debrief_buffer_minutes = 10 # block after
```

### 4. Travel time blocking
When a meeting has a physical location (non-video), auto-block travel time
before and after. Configurable duration. Detected from the `location` field
returned by the parser.

```toml
[schedule]
travel_buffer_minutes = 30  # applied when location is a physical address
```

### 5. Auto-suggest when all proposed slots are busy ✅ DONE
When `outcome="busy"`, `handle_inbound_result` now runs `find_slots` before
giving up. If alternatives are found, they are sent to the external party on
the original thread and state is written as `pending_external_reply` — the
same flow as `suggest_times`. If no alternatives exist, the owner is notified
as before ("conflict found, no alternatives in next 7 days").

---

## Medium Impact — Quality of Life

### 6. Daily / weekly digest
A scheduled email (not triggered by a thread) summarizing upcoming meetings.
Needs a new entry point — either a cron trigger or a CLI command:

```
python ea.py digest          # send today's agenda
python ea.py digest --week   # send this week's agenda
```

Reads from `CalendarClient.list_events` and formats a plain-text summary.

### 7. Timezone-aware invite bodies ✅ DONE
When the attendee's timezone (from `parsed["timezone"]`) differs from the
owner's, both times now appear in every scheduling email.

- **Emails to owner** (`_local_slot_desc`): owner's tz primary, attendee's in
  parens — e.g. `"Thursday Mar 19, 02:00–02:30 PM EDT (11:00–11:30 AM PDT for them)"`
- **Emails to external party** (`_format_slot_suggestions`): attendee's tz
  primary, owner's in parens — e.g. `"Thursday Mar 19, 11:00–11:30 AM PDT (2:00–2:30 PM EDT my time)"`
- `attendee_tz` is saved to state so counter-proposal resends also use it.
- When timezones match (or attendee tz is unknown), single-tz format is used.

### 8. Meeting duration defaults by topic type
When the parser can't determine duration, fall back to a config-driven default
based on detected meeting type rather than failing with an ambiguity.

```toml
[schedule.duration_defaults]
coffee_chat  = 30
interview    = 60
1on1         = 30
board        = 90
default      = 30
```

Parser returns a `meeting_type` hint; responder looks up the default before
treating missing duration as an ambiguity.

### 9. Group scheduling (multiple attendees)
`get_freebusy` already accepts multiple attendees, but the parser and responder
only handle one external attendee today. Allow the parser to return multiple
`attendees` and thread them through `find_slots` and `create_event`.

Main work: `handle_suggest_times_trigger` and `handle_inbound_result` currently
assume a single attendee when composing the reply.

### 10. Waitlist / retry after busy
When `outcome="busy"`, offer to find alternatives. Reply to the thread owner:
"All proposed times are taken. Reply 'yes' and I'll suggest some open slots."
Writes a lightweight `pending_confirmation` state with `action="find_slots"`.

### 10a. Slot validity monitoring during pending_external_reply
While waiting for the external party to confirm one of the offered time slots,
periodically verify that the suggested slots are still free. If the owner's
calendar fills up before a reply arrives, the confirmed slot could conflict with
a newly booked meeting.

**Current state:** `pending_external_reply` entries already store `suggested_slots`
as a list of `{"start": ISO, "end": ISO, "slot_type": ...}` dicts. Pass 3 checks
for new replies from the external party but never re-validates the slots.

**New behavior in Pass 3:** on each poll cycle, before checking for a reply,
call `get_freebusy` across all `suggested_slots` windows and compare against the
owner's calendar. Three cases:

- **All slots still free** — do nothing, continue waiting for a reply.
- **Some slots now busy** — remove the busy ones from `suggested_slots` in state.
  If at least one slot remains free, send an updated message to the external
  party on the same thread:
  > "Just a heads-up — a couple of the times I suggested are no longer
  > available. The remaining open slots are: [updated list]. Do any of these
  > still work?"
- **All slots now busy** — run `find_slots` to generate fresh alternatives.
  If new slots are found, send them to the external party and update
  `suggested_slots` in state. If no alternatives exist within the lookahead
  window, notify the owner and apply `ea-notified`.

**Implementation:**
- `ea/responder.py` — add `_check_slot_validity(entry, calendar, config) -> (still_valid, busy_indices)`
  helper. Called at the top of Pass 3 processing, before the reply-check.
- Update `suggested_slots` in state after each check so the classifier in Pass 3
  still uses the current list when the external party eventually replies.
- Add a `slots_last_checked` timestamp to state so the validity check only runs
  once per poll cycle (not on every message arrival).
- No parser changes — this is purely a Pass 3 / responder concern.

**Config (optional):** a `check_slot_validity = true` flag under `[schedule]`
to opt out if the extra freebusy call per pending entry is undesirable.

### 10b. Pending reply reminders
While waiting for a response (either from the owner on a `pending_confirmation`
or from the external party on a `pending_external_reply`), periodically send a
nudge if no reply has arrived.

**Two delivery approaches — tradeoffs:**

**Option A — Standalone reminder on the relevant thread (recommended):**
Send a new reply on the same thread where the original ask was made.

- For `pending_confirmation`: reply on the owner's confirmation thread —
  "Just a reminder — I'm still waiting for your approval on the after-hours
  slot for Coffee chat with sarah@example.com (Thursday 6pm). Reply yes/no."
- For `pending_external_reply`: reply on the original email thread to the
  external party — "Just checking in — do any of the times I suggested for
  our meeting still work for you?"

Pros: each reminder is in the correct thread context; replies are naturally
processed by the existing Pass 2 / Pass 3 logic with no changes.
Cons: sends a new email to the external party, which may feel pushy.

**Option B — Piggyback onto other outgoing EA emails:**
When EA sends any notification email to the owner (e.g. "EA: booked — Fred
meeting"), append a digest of pending items at the bottom:
> "Still waiting for your reply on: Coffee chat with sarah@example.com."

Pros: no extra email volume; owner sees reminders in context.
Cons: the reply arrives on the Fred thread, not the Sarah thread — the state
machine has no way to route it. The owner would need to go back to the original
thread to respond. This makes it a read-only nudge, not an actionable one, and
is confusing enough that it is not recommended.

**Recommended approach: Option A with rate-limiting.**

**State changes:**
Add `last_reminded_at` (ISO timestamp) to each pending entry. Set to `null`
when state is first written (the initial ask counts as the first contact).
Updated each time a reminder is sent.

**Config:**
```toml
[schedule]
reminder_interval_hours   = 24    # how often to send reminders
reminder_max_count        = 2     # stop reminding after N nudges (let expiry handle it)
reminders_enabled         = true  # set false to disable entirely
```

**Reminder cadence example** with `interval=24, max=2` and a 7-day expiry
window:
- Day 0: initial ask sent
- Day 1: first reminder
- Day 2: second (final) reminder
- Day 7: expiry notification (item #14)

**Implementation:**
- `ea/state.py` — no changes; `last_reminded_at` and `reminder_count` are
  just additional fields in the entry dict.
- `ea/responder.py` — new `send_reminder(thread_id, entry, gmail, config)`
  helper. Composes the reminder body from the entry's topic, attendees, and
  suggested slots (for `pending_external_reply`) or slot time (for
  `pending_confirmation`). Updates `last_reminded_at` and increments
  `reminder_count` in state.
- `ea/poll.py` — reminder check runs after the expiry check and before the
  three passes. For each pending entry where `last_reminded_at` is more than
  `reminder_interval_hours` ago and `reminder_count < reminder_max_count`,
  call `send_reminder`. No reply processing needed — reminders are one-way.

### 10c. Duplicate meeting detection
Before booking a new meeting, check whether an existing calendar event already
involves the same attendee(s) within a nearby time window. If a likely duplicate
is found, warn the owner before proceeding rather than silently creating a
second event.

**What counts as a duplicate:**
Two signals are checked independently and combined:

1. **Attendee overlap** — the proposed meeting shares one or more attendees
   with an existing event. The owner's own email is excluded from this
   comparison (they're in every event).

2. **Proximity** — the existing event falls within a configurable window around
   the proposed date. Three useful windows:
   - *Same day* — clearest signal; scheduling two 1:1s with Sarah on Thursday
   - *Same week* — softer signal; worth flagging but less likely to be an error
   - *Adjacent slots* — existing event ends within 30 minutes of the proposed
     start, or starts within 30 minutes of the proposed end (back-to-back
     detection)

Topic similarity (using the word-overlap scoring already in `find_matching_event`)
can be used as a tiebreaker: same attendee + similar topic on the same day is a
near-certain duplicate; same attendee + different topic + same day is worth a
warning but less urgent.

**Interaction with the booking flow:**
The check runs in `handle_inbound_result` and `handle_block_time_result` after
`outcome="open"` is confirmed but before `calendar.create_event` is called.

Three possible outcomes:

- **No duplicates found** — proceed to book as normal.
- **Likely duplicate** (same attendee, same day, similar topic) — do not book.
  Email the owner:
  > "You already have 'Coffee chat' with sarah@example.com on Thursday at 2pm.
  > Is this a different meeting? Reply 'yes, different' to book it anyway, or
  > 'no, cancel' to drop this request."
  Writes `pending_confirmation` state with `action="confirm_duplicate"` and
  the candidate duplicate event stored for the confirmation handler.

- **Possible duplicate** (same attendee, same week, different topic) — book the
  meeting but include a note in the "EA: booked" email to the owner:
  > "Note: you also have 'Q2 planning' with sarah@example.com on Wednesday."
  No confirmation required; the owner can decide if it matters.

**Config:**
```toml
[schedule]
duplicate_check_days     = 1    # 0 = same day only, 7 = same week
duplicate_require_confirm = true # false = warn-only, never block booking
```

**Implementation:**
- `ea/scheduler.py` — new `find_duplicate_events(attendees, proposed_start, calendar, tz_name, window_days)`.
  Calls `list_events` over the window, filters by attendee overlap, scores by
  topic similarity. Returns `(level, event)` where level is `"likely"`,
  `"possible"`, or `None`.
- `ea/responder.py` — `handle_inbound_result` and `handle_block_time_result`
  call `find_duplicate_events` before `create_event`. Branch on level.
- `ea/poll.py` — Pass 2 confirmation handler gains a `"confirm_duplicate"`
  action branch: on "yes" reply, create the event; on "no" reply, apply
  `ea-cancelled`.
- No parser changes needed.

---

## Lower Impact — Polish

### 11. Calendar event descriptions
Created events currently have no description body. Populate it with:
- The original email thread snippet
- The EA: command that triggered it
- Attendee names/emails

Small addition to `create_event` calls in `responder.py`.

### 12. Decline on behalf
When the owner writes "EA: decline" on an inbound thread, send a polite
decline email to the sender and apply `ea-cancelled`. New intent value
`decline` in the parser, new handler in `responder.py`.

### 13. Hold / tentative blocks
"EA: hold Thursday 2-4pm for prep" — creates an event with
`transparency: "transparent"` so the owner sees it but it doesn't block
others' scheduling. Useful for protecting focus time.

New intent or a modifier on `block_time`: `block_type = "hold" | "hard"`.

### 14. Smart expiry — configurable window and deadline-aware
**Current behavior:** expiry is a hardcoded 48-hour fixed window (`EXPIRY_HOURS = 48`
in `responder.py`). Every time state is written, `_expiry()` stamps
`now + 48h` as `expires_at`. The expiry check at the top of each poll cycle
compares that timestamp against `now`. It is purely clock-based — it has no
knowledge of the proposed meeting times. A request for "let's meet Friday at
2pm" and one for "let's meet sometime next month" both expire at the same time.

**Problems with the current approach:**

1. **Not configurable.** 48 hours is hardcoded; some workflows need longer
   (a board meeting being arranged weeks out) and some shorter (a same-day
   request that's no longer relevant by evening).

2. **Not deadline-aware.** If all proposed times have already passed, the
   state entry is stale but won't expire for up to 48 hours. Worse, if
   someone replies "yes, Thursday 2pm" after Thursday has passed, EA would
   try to book it.

3. **Terse notification.** The expiry email says only "No reply received —
   the pending scheduling request has expired." It contains no context about
   what the request was.

**Proposed improvements:**

**A. Configurable reply window (replaces hardcoded 48h):**
```toml
[schedule]
pending_confirmation_hours  = 48   # how long to wait for owner's yes/no
pending_external_reply_days = 7    # how long to wait for external party's reply
```
Different state types have meaningfully different urgency: the owner should
respond to an after-hours confirmation quickly, while an external party might
take several days to reply to time suggestions.

**B. Deadline-aware expiry:**
When state is written, also store the latest proposed datetime as
`latest_proposed_at`. The expiry check uses whichever comes first:
`expires_at` (the reply window) or `latest_proposed_at + buffer` (e.g. 1 hour
after the last proposed slot has passed). This means a "let's meet Friday at
2pm" request auto-expires Friday evening regardless of the reply window.

Implementation: `_expiry()` becomes `_expiry(parsed=None)` and accepts the
parsed dict to extract the last datetime from `proposed_times[*].datetimes`.
`state.expired()` checks both `expires_at` and `latest_proposed_at`.

**C. Richer expiry notification:**
Replace the generic expiry email body with one that includes full context:
```
EA: request expired — Coffee chat with sarah@example.com

The scheduling request below received no reply within the allowed window.
Proposed times: Thursday Mar 19 at 2:00 PM, Friday Mar 20 at 10:00 AM
Attendees: sarah@example.com

Reply "retry" to this email and EA will find new available slots.
```
The "retry" option writes a new `pending_confirmation` state with
`action="find_slots"` — the same flow as item #10.

**Implementation touchpoints:**
- `ea/responder.py` — `EXPIRY_HOURS` constant removed; `_expiry(parsed, config)`
  reads from config and optionally extracts deadline from `parsed`.
- `ea/state.py` — `expired()` checks both `expires_at` and `latest_proposed_at`.
- `ea/poll.py` — expiry email body replaced with the richer template above;
  "retry" keyword detected and dispatched to `find_slots`.

### 15. `ea status` command ✅ DONE
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

### 16. Ignore / dismiss a request
An escape hatch for dropping any pending scheduling request without taking
action. Useful when a reply asks for more information and the owner just wants
to walk away from it, or when EA misidentified a thread and created state that
should be cleared.

**Trigger:** reply to the thread (or the EA confirmation email) with:
```
EA: ignore
EA: dismiss
EA: never mind
EA: forget it
```

**What it does:**
1. Removes the thread from `StateStore` — no further processing on any poll cycle.
2. Applies the `ea-cancelled` label so Pass 1 never picks it up again.
3. Sends a brief confirmation email to the owner:
   > EA: dismissed — Coffee chat with sarah@example.com

**Handles all pending states:**
- `pending_confirmation` — owner changed their mind after EA asked for after-hours approval
- `pending_external_reply` — owner wants to withdraw the time suggestions already sent
- Any thread with no existing state — owner wants to permanently suppress it

For `pending_external_reply`, the dismiss does **not** send a cancellation email
to the external party (that is left to the owner to handle manually, since EA
doesn't know how far the conversation has progressed). The confirmation email to
the owner notes this: "Note: no cancellation was sent to sarah@example.com."

**Implementation:**
- `ea/parser/meeting_parser.py` — add `"ignore"` to the intent enum. No new
  fields needed; topic is extracted for the confirmation email.
- `ea/responder.py` — new `handle_ignore_result(parsed, thread, gmail, state, config)`:
  reads existing state entry (if any) for context, deletes state, applies
  `ea-cancelled`, sends confirmation.
- `ea/poll.py` Pass 1 — add `elif intent == "ignore"` dispatch branch.
- Pass 2 / Pass 3 — the `pending_confirmation` and `pending_external_reply`
  loops only look at the owner's latest reply, so an `EA: ignore` reply on
  the confirmation thread is naturally picked up by Pass 1 on the *original*
  thread on the next cycle. No changes needed to Pass 2 / Pass 3.

**CLI alternative:** `python ea.py dismiss <thread_id>` — same effect without
needing to send an email, useful when the thread has already been archived or
when debugging via `ea status`.

---

## Bigger Swings

### 17. Proactive availability — no EA: command needed

Detect when an inbound email is asking for availability ("when are you free
next week?") without an explicit `EA:` command. Classify with a lightweight
Claude call; if confidence is high, auto-trigger the `suggest_times` flow.

Requires a pre-filter classifier before the `EA:` trigger check in Pass 1.
Risk: false positives. Needs a confidence threshold and a config flag to
opt in.

### 18. Slack / iMessage integration
Same `EA:` command syntax, different ingestion layer. The poll loop,
responder, and scheduler are reusable as-is.

- Slack: poll a DM channel via Slack API; reply in-thread
- iMessage: harder — no official API; would require Shortcuts or AppleScript

### 19. Smart no-meeting window protection
Detect calendar fragmentation (many short gaps) and proactively suggest
blocking focus time. Could run as part of the weekly digest or as a
separate `ea protect` command.

```
python ea.py protect --day friday --hours 09:00-12:00
```

Creates a recurring hold block if that window is consistently free.

---

## Operations / Production

Items needed for reliable deployment and maintenance — not product features.

### OP-1. Graceful network error handling ✅ DONE
`ea/network.py` — module-level retry utility.

- `configure(attempts, base_delay, cap)` — sets retry policy. Default: `attempts=1`
  (no retry), suitable for cron/poll mode. `run_loop()` calls
  `configure(attempts=3, base_delay=1.0, cap=poll_interval_seconds)` so backoff
  never exceeds the next scheduled cycle.
- `call_with_retry(fn)` — wraps a callable; retries on transient errors with
  exponential backoff; raises immediately on permanent errors.
- `is_transient_error(exc)` — recognizes `requests.ConnectionError/Timeout`,
  `googleapiclient.errors.HttpError` (429 / 5xx), `anthropic.APIConnectionError`,
  `anthropic.RateLimitError`, `socket.timeout`, `TimeoutError`, OS network errno.
- All live API calls in `gmail.py`, `calendar.py`, `meeting_parser.py`, and
  `classifier.py` are wrapped with `call_with_retry`.
- `run_once()` catches transient errors and logs `"Poll skipped — network
  unavailable"` instead of a traceback before re-raising.

### OP-2. Run-once / cron mode ✅ DONE
`python ea.py poll --quiet` is now safe for unattended invocation.

- **Exit codes:** exits `0` on clean run (nothing to process counts as clean),
  exits `1` on any unrecoverable error. Error message goes to stderr before exit.
- **`--quiet` flag:** suppresses all stdout — no summary lines, no "nothing to
  process" message. All output routes to `ea.log` only via `configure(quiet=True)`.
  Use this whenever launchd/cron invokes the process.
- **Lock interaction:** when `.state.lock` is held, `run_once()` logs a warning
  to `ea.log` and returns an empty summary. In quiet mode nothing reaches stdout,
  so no spurious notification email is triggered. Exits `0`.
- **Cron example** (every 5 minutes, quiet):
  ```
  */5 * * * * cd /path/to/ea && .venv/bin/python ea.py poll --quiet
  ```
- **Google Cloud Functions / Cloud Run Jobs:** single invocation of `run_once()`
  maps directly; ensure `state.json` and `token.json` are on a mounted volume or
  Cloud Storage (requires a small adapter).

### OP-3. macOS launchd startup file ✅ DONE
Uses `StartInterval` (cron-like: start process → one cycle → exit) rather than
`KeepAlive` (persistent process). This pairs with `poll --quiet` so no stdout
reaches launchd and no notification emails are triggered.

Files:
- `docs/launchd/com.ea.poll.plist` — plist template (fill in 3 CHANGEME values)
- `scripts/install-launchd.sh` — install script; auto-fills paths, prompts for
  API key (or sources `~/.ea-env` if present), loads the agent

Quick setup:
```
bash scripts/install-launchd.sh
```

Manual setup:
```bash
cp docs/launchd/com.ea.poll.plist ~/Library/LaunchAgents/
# edit the plist: set PROJECT_DIR, VENV_PYTHON, ANTHROPIC_API_KEY
launchctl load ~/Library/LaunchAgents/com.ea.poll.plist
```

Key plist settings:
- `StartInterval = 300` — run one poll cycle every 5 minutes (match `poll_interval_seconds`)
- `RunAtLoad = true` — runs immediately on load / reboot
- `StandardOutPath = /dev/null` — stdout suppressed by `--quiet`, nothing to capture
- `StandardErrorPath = ea-launchd.err` — catches Python startup errors before logging is configured
- `EnvironmentVariables` — sets `ANTHROPIC_API_KEY`; for better security use a wrapper
  script that sources `~/.ea-env`

Commands:
```bash
launchctl list com.ea.poll          # check status / last exit code
launchctl start com.ea.poll         # trigger one cycle immediately
launchctl stop com.ea.poll          # stop current run
launchctl unload ~/Library/LaunchAgents/com.ea.poll.plist   # remove agent
tail -f ea.log                       # watch structured JSON logs
```

### OP-4. Linux systemd unit file
For VPS or home server deployment.

File: `docs/systemd/ea.service`

```ini
[Unit]
Description=EA Executive Assistant poll loop
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ea
WorkingDirectory=/home/ea/ea
ExecStart=/home/ea/ea/.venv/bin/python ea.py run
Restart=always
RestartSec=10
EnvironmentFile=/home/ea/.ea-env   # contains ANTHROPIC_API_KEY=...
StandardOutput=append:/home/ea/ea/ea.log
StandardError=append:/home/ea/ea/ea-error.log

[Install]
WantedBy=multi-user.target
```

Commands: `systemctl enable ea` / `systemctl start ea` / `systemctl status ea` /
`journalctl -u ea -f`.

Note: `After=network-online.target` ensures the service waits for network before
starting (important for OP-1).

### OP-5. Log rotation
`ea.log` grows without bound. Add a `logrotate` config (Linux) and equivalent
for macOS.

File: `docs/logrotate/ea` (copy to `/etc/logrotate.d/ea`):
```
/home/ea/ea/ea.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    postrotate
        systemctl kill -s HUP ea 2>/dev/null || true
    endscript
}
```

For macOS with launchd: use `newsyslog` or simply cap log size in the `_JsonlHandler`
in `ea/log.py` by switching to `RotatingFileHandler` (max 10 MB, 5 backups).

### OP-6. OAuth token expiry and revocation handling
Google OAuth tokens auto-refresh via the `google-auth` library as long as the refresh
token is valid. But the refresh token can be revoked (user revokes access in Google
account settings, or too many tokens issued). Currently this would crash with an
`google.auth.exceptions.RefreshError`.

Required:
- Catch `RefreshError` in `runner.py` at the top of `run_once()`.
- Send a self-email (if possible) or write to stderr: "EA: Google auth expired —
  run `python ea.py auth` to re-authenticate."
- Exit cleanly rather than retrying (retrying won't help).

### OP-7. Config validation at startup
Currently a missing or malformed `config.toml` causes confusing crashes mid-poll
(e.g., `KeyError: 'email'`). Validate the config once at startup before any API
calls are made.

Required checks:
- `user.email` is present and looks like an email address
- `auth.credentials_file` and `auth.token_file` paths exist (for `poll`/`run` commands)
- `schedule.timezone` is a valid IANA timezone name
- Working/preferred hours are valid `HH:MM` strings and start < end

Fail fast with a clear message: `"Config error: user.email is required in config.toml"`

### OP-8. Health monitoring / dead-man's switch
No current way to know if the process died silently. Options (pick one):

- **Healthchecks.io ping** (free tier): send a GET request to a unique URL at the
  end of each successful poll cycle. If no ping in N minutes, healthchecks.io sends
  an alert email. Zero infrastructure required.
- **Uptime Kuma** (self-hosted): same concept.
- **Heartbeat email**: once per day, send a self-email "EA: alive — N threads
  processed today." Visible in Gmail without any external service.

### OP-9. Configurable LLM model and provider abstraction
Currently the Anthropic model name (`claude-sonnet-4-20250514`) is hard-coded in
three places: `meeting_parser.py`, `classifier.py` (twice). Two levels of change:

**Level 1 — configurable model name** (low effort, high value):
Move the model string to `config.toml` so it can be updated without a code change:

```toml
[llm]
model = "claude-sonnet-4-20250514"
```

Each call site reads `config["llm"]["model"]` (with a sensible default). Also
allows switching to Haiku for cost savings or Opus for accuracy.

**Level 2 — provider abstraction** (more effort, optional):
Introduce a thin `ea/llm.py` wrapper with a single `complete(system, user, max_tokens, config)` function. The implementation is selected by `config["llm"]["provider"]`:

```toml
[llm]
provider = "anthropic"   # or "openai", "ollama", etc.
model    = "claude-sonnet-4-20250514"
```

All three call sites (`parse_meeting_request`, `classify_confirmation_reply`,
`classify_external_reply`) call `llm.complete(...)` instead of the SDK directly.
The `anthropic` SDK import moves inside the provider implementation, so the rest
of the codebase has no hard dependency on it.

Suggested order: do Level 1 first (one-line change per call site + config key),
then Level 2 only if a second provider is actually needed.

### OP-10. Pluggable state and lock backends
Currently `state.json` and `.state.lock` are always local files, which breaks
Cloud Run / Cloud Functions deployments where the filesystem is ephemeral and
multiple instances could run concurrently.

**Current implementation:**
- `ea/state.py` — `StateStore` reads/writes a local `state.json`
- `ea/runner.py` — `_acquire_lock` / `_release_lock` use `fcntl.flock` on
  `.state.lock` (Unix-only, local filesystem only)

**Required abstraction:**
Introduce a `StateBackend` protocol with two implementations selected by
`config["state"]["backend"]`:

```toml
[state]
backend = "local"   # default; or "gcs"

# GCS backend settings (only needed when backend = "gcs"):
gcs_bucket = "my-ea-state"
gcs_prefix = "ea/"          # optional key prefix
```

`StateBackend` interface (duck-typed, no ABC required):
```python
def load() -> dict          # read full state dict
def save(state: dict)       # write full state dict
def acquire_lock() -> bool  # True if lock acquired
def release_lock()
```

**Local backend** — current behavior, wraps existing `state.json` + `fcntl.flock`.

**GCS backend:**
- `load` / `save`: read/write a single JSON object at `gs://{bucket}/{prefix}state.json`
  using `google-cloud-storage` (already an indirect dep via `google-auth`).
- `acquire_lock`: write `gs://{bucket}/{prefix}.state.lock` with
  `if_generation_match=0` (atomic create — GCS's built-in compare-and-swap).
  If the object already exists, lock is held by another instance → return False.
- `release_lock`: delete the lock object.

This gives correct mutual exclusion across multiple Cloud Run instances without
any additional infrastructure.

**Migration path:**
1. Refactor `StateStore.__init__` to accept a backend object instead of a path.
2. Extract lock logic from `runner.py` into the backend.
3. `runner.py` instantiates the backend based on config before calling `run_once()`.
4. No changes to `poll.py`, `responder.py`, or tests (they inject `StateStore`
   directly and don't touch the backend).

### OP-11. Configurable command sentinel
The string `EA:` is hardcoded in three distinct roles and would need to change
if the user wants a different trigger word (e.g. their own name `"Nick:"`,
`"JARVIS:"`, or just `"sched:"`).

**Where it appears:**

1. **Trigger detection** — two separate regex sites:
   - `ea/triggers.py:65` — `re.search(r'EA:\s*(.+)', message.body, re.IGNORECASE)`
   - `ea/poll.py:302` — same pattern, in `_find_ea_trigger_in_messages`

2. **Outgoing email subjects** — `responder.py` and `poll.py` use `"EA:"` as
   the prefix for all notification emails back to the owner (e.g.
   `"EA: booked — Coffee chat"`, `"EA: cancelled — Standup"`). These are
   useful as a filter/label in Gmail, so the prefix should match the sentinel.

3. **Parser system prompt** — `meeting_parser.py` has `EA:` baked into the
   worked examples Claude uses to understand the trigger format. The configured
   sentinel must be injected here so the model recognizes the right prefix.

**Config key:**

```toml
[user]
email    = "you@gmail.com"
name     = "Your Name"
sentinel = "EA"       # trigger word; the colon is added automatically
```

Default to `"EA"` so existing setups require no change.

**Implementation:**

- `config.py`: expose `get_sentinel(config) -> str` returning
  `config["user"].get("sentinel", "EA").rstrip(":")` (strip accidental colon).
- `ea/triggers.py` and `ea/poll.py`: replace the hardcoded `r'EA:\s*(.+)'`
  with `rf'{re.escape(sentinel)}:\s*(.+)'`. Both sites already receive `config`
  or can be passed the sentinel directly.
- `ea/responder.py` and `ea/poll.py` subjects: replace the `"EA: "` prefix
  string with `f"{sentinel}: "`. Straightforward search-and-replace within
  each `send_email` call.
- `ea/parser/meeting_parser.py`: inject the sentinel into `SYSTEM_PROMPT` at
  parse time (turn the module-level constant into a function that accepts the
  sentinel, or use `.format(sentinel=sentinel)` with a placeholder).

### OP-12. Gmail push notifications (replace polling)
Currently EA polls Gmail on a fixed interval. Gmail's Watch API can push a
notification the moment a new message arrives, eliminating polling latency
entirely and reducing unnecessary API calls.

**How Gmail push works:**
1. Call `users.watch()` on the Gmail API, specifying a Google Cloud Pub/Sub
   topic as the delivery target.
2. Gmail publishes a lightweight notification to that Pub/Sub topic whenever
   the mailbox changes (new message, label applied, etc.). The payload contains
   only `historyId` — not the message itself.
3. Your code fetches the actual changes via `users.history.list()` using the
   `historyId` from the notification, then processes new messages.
4. Watch subscriptions expire after **7 days** and must be renewed (e.g. via a
   daily cron job calling `users.watch()` again).

**Two delivery modes for Pub/Sub:**

- **Push (Cloud Run / Cloud Functions):** Pub/Sub sends an HTTP POST to your
  endpoint when a notification arrives. No polling at all — the process starts,
  handles the event, and exits. Pairs naturally with OP-10 (GCS state backend).
  Requires a public HTTPS endpoint.

- **Pull (Mac / local):** Your process calls `subscriptions.pull()` in a loop,
  blocking until a message arrives (long-poll). Replaces `time.sleep()` in
  `run_loop()` with a blocking Pub/Sub pull. No public endpoint needed — works
  on a local Mac behind NAT.

**Required infrastructure (one-time setup):**
```
gcloud pubsub topics create ea-gmail-push
gcloud pubsub subscriptions create ea-gmail-sub --topic ea-gmail-push

# Grant Gmail permission to publish to the topic
gcloud pubsub topics add-iam-policy-binding ea-gmail-push \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

**Config:**
```toml
[gmail]
mode = "poll"   # or "pubsub_pull" (local) / "pubsub_push" (Cloud Run)
pubsub_project      = "my-gcp-project"
pubsub_subscription = "ea-gmail-sub"
pubsub_topic        = "ea-gmail-push"
watch_renew_days    = 6   # renew the watch before 7-day expiry
```

**Code changes:**
- `ea/runner.py` — `run_loop()` gains a `pubsub_pull` mode: instead of
  `time.sleep(interval)`, block on `subscription.pull(max_messages=1,
  timeout=600)`. On receipt, call `run_once()` immediately, then ack the
  message.
- New `ea/gmail_watch.py` — `ensure_watch(gmail_service, config)` calls
  `users.watch()` and stores the returned `historyId` in state. Called once at
  startup and renewed daily.
- New `ea/entrypoints/pubsub_push.py` — a minimal Flask/Functions Framework
  handler for Cloud Run push mode. Decodes the Pub/Sub envelope, calls
  `run_once()`, returns 200.
- `poll.py` / `runner.py` — no changes to the three-pass loop itself; the
  notification only tells EA *that* something changed. `run_once()` still does
  the full scan via `list_threads`, so the existing logic is reused as-is.

**Watch renewal:**
A separate daily trigger (cron job or Cloud Scheduler) calls
`python ea.py watch-renew` which calls `users.watch()` and updates the stored
`historyId`. Without renewal the push notifications silently stop after 7 days.

**Suggested order:** implement `pubsub_pull` first (works locally, no public
endpoint required, minimal infrastructure). Add `pubsub_push` only when
deploying to Cloud Run.

---

## Implementation Order (suggested)

```
#11 event descriptions    — small, immediate value, touches existing code
#15 ea status command     — useful for debugging today
#12 decline               — closes an obvious gap
#2  reschedule/cancel     — high frequency need
#5  auto-suggest on busy  — closes the biggest flow gap
#3  prep buffer           — low complexity, high daily value
#8  duration defaults     — reduces ambiguity failures
#6  daily digest          — new capability, no external deps beyond Calendar
#1  recurring meetings    — moderate complexity
#9  group scheduling      — extends existing freebusy logic
```
