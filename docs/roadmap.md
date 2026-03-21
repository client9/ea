# EA Feature Roadmap

Features are grouped by priority. "High" items address gaps in the current core
flow. "Medium" items improve quality of life. "Lower" items are polish. "Bigger
swings" require new infrastructure.

Completed features live in [changelog.md](changelog.md).

---

## High Impact — Core Scheduling Gaps

### 1. Recurring meeting support
Teach the parser to recognize recurrence patterns ("weekly", "every Thursday",
"biweekly"). Pass a `recurrence` field (RRULE format) to `create_event`.

- Parser: add `recurrence` field to JSON schema (e.g. `"RRULE:FREQ=WEEKLY"`)
- `CalendarClient.create_event`: accept and forward `recurrence` param
- Example: "EA: set up a weekly 1:1 with sarah@ on Thursdays at 10am"

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

### 7. Calendly link detection and slot matching
When an external party replies to a scheduling thread and includes a Calendly
URL (`https://calendly.com/...`), EA should fetch their available slots,
cross-reference them against the owner's Google Calendar freebusy, and either
book a matching slot automatically or surface the best options.

**Why it matters:** Calendly links in replies are the dominant "here's my
availability" pattern in professional email. Without this, EA ignores the link
and the owner has to click through manually — defeating the purpose.

**How Calendly's public API works (no auth required):**
Calendly scheduling pages load availability from a public JSON endpoint that
requires no API key:
```
GET https://calendly.com/api/booking/event_types/{uuid}/calendar/range
    ?timezone=America/Los_Angeles&range_start=2026-03-20&range_end=2026-03-27
```
The response contains a list of available spots with `start_time` / `end_time`
in UTC. This is the same call the browser makes when you open a Calendly link —
it's stable and publicly accessible.

The event type UUID is embedded in the Calendly URL:
`https://calendly.com/username/event-name` → resolve to UUID via a redirect or
the public event type lookup endpoint.

**Flow:**
1. **Detect** — in Pass 3 (`pending_external_reply`), before checking for a
   plain-text slot confirmation, scan the reply body for a Calendly URL with
   a regex like `r'https://calendly\.com/[\w/-]+'`.
2. **Fetch** — `GET` the Calendly availability API for the next 7 days.
3. **Cross-reference** — call `get_freebusy` on the owner's calendar over the
   same window. Filter Calendly slots to those where the owner is also free.
4. **Act** — three sub-cases:
   - **One free slot** — book it or email the external party confirming that time.
   - **Multiple free slots** — pick the best (preferred hours first), book or
     confirm. Optionally email the owner: "EA booked via their Calendly."
   - **No overlap** — notify the owner with fresh available slots.

**Config (optional):**
```toml
[integrations]
calendly_enabled  = true    # set false to disable Calendly detection entirely
calendly_auto_book = false  # true: attempt to POST the booking; false: email confirmation only
```

**Implementation:**
- `ea/calendly.py` — new module. `extract_calendly_url(text) -> str | None`,
  `fetch_slots(url, tz_name, lookahead_days) -> list[dict]`. Isolated so it can be
  stubbed in tests.
- `ea/poll.py` — Pass 3 processing: before the existing reply-classification
  logic, check `extract_calendly_url(reply_text)`. If found, call
  `fetch_slots` and `_cross_reference(calendly_slots, owner_freebusy)` then
  dispatch.
- `ea/responder.py` — new `handle_calendly_match(slots, entry, gmail,
  calendar, state, config)` handler for the three sub-cases above.
- No parser changes needed — Calendly detection is purely in the poll loop.

**Testing:**
`fetch_slots` is injectable (pass a stub returning canned slot dicts) so the
full flow can be covered without network calls.

---

## Medium Impact — Quality of Life

### 10. Group scheduling (multiple attendees)
`get_freebusy` already accepts multiple attendees, but the parser and responder
only handle one external attendee today. Allow the parser to return multiple
`attendees` and thread them through `find_slots` and `create_event`.

Main work: `handle_suggest_times_trigger` and `handle_inbound_result` currently
assume a single attendee when composing the reply.

### 11. Waitlist / retry after busy
When `outcome="busy"`, offer to find alternatives. Reply to the thread owner:
"All proposed times are taken. Reply 'yes' and I'll suggest some open slots."
Writes a lightweight `pending_confirmation` state with `action="find_slots"`.

### 11a. Slot validity monitoring during pending_external_reply
While waiting for the external party to confirm one of the offered time slots,
periodically verify that the suggested slots are still free.

**New behavior in Pass 3:** on each poll cycle, before checking for a reply,
call `get_freebusy` across all `suggested_slots` windows. Three cases:

- **All slots still free** — do nothing.
- **Some slots now busy** — remove the busy ones from `suggested_slots` in state.
  If at least one slot remains free, send an updated message to the external party.
- **All slots now busy** — run `find_slots` to generate fresh alternatives.
  If no alternatives exist, notify the owner and apply `ea-notified`.

**Implementation:**
- `ea/responder.py` — add `_check_slot_validity(entry, calendar, config) -> (still_valid, busy_indices)`
  helper. Called at the top of Pass 3 processing, before the reply-check.
- Add a `slots_last_checked` timestamp to state so the validity check only runs
  once per poll cycle.

**Config (optional):**
```toml
[schedule]
check_slot_validity = true
```

### 11b. Pending reply reminders
While waiting for a response, periodically send a nudge if no reply has arrived.

**Recommended approach: standalone reminder on the relevant thread (Option A).**

- For `pending_confirmation`: reply on the owner's confirmation thread.
- For `pending_external_reply`: reply on the original email thread.

Pros: each reminder is in the correct thread context; replies are naturally
processed by the existing Pass 2 / Pass 3 logic with no changes.

**State changes:**
Add `last_reminded_at` (ISO timestamp) and `reminder_count` to each pending entry.

**Config:**
```toml
[schedule]
reminder_interval_hours   = 24    # how often to send reminders
reminder_max_count        = 2     # stop reminding after N nudges (let expiry handle it)
reminders_enabled         = true  # set false to disable entirely
```

**Implementation:**
- `ea/responder.py` — new `send_reminder(thread_id, entry, gmail, config)` helper.
- `ea/poll.py` — reminder check runs after the expiry check and before the
  three passes.

### 11c. Duplicate meeting detection
Before booking a new meeting, check whether an existing calendar event already
involves the same attendee(s) within a nearby time window.

**What counts as a duplicate:**
1. **Attendee overlap** — shared attendees (excluding the owner).
2. **Proximity** — within a configurable window around the proposed date.

Topic similarity (word-overlap scoring from `find_matching_event`) as a tiebreaker.

**Interaction with the booking flow:**
Runs in `handle_inbound_result` and `handle_block_time_result` after
`outcome="open"` but before `calendar.create_event`.

Three possible outcomes:
- **No duplicates found** — proceed to book.
- **Likely duplicate** (same attendee, same day, similar topic) — do not book.
  Email owner for confirmation; writes `pending_confirmation` with `action="confirm_duplicate"`.
- **Possible duplicate** (same attendee, same week, different topic) — book the
  meeting but include a note in the "EA: booked" email.

**Config:**
```toml
[schedule]
duplicate_check_days     = 1    # 0 = same day only, 7 = same week
duplicate_require_confirm = true # false = warn-only, never block booking
```

**Implementation:**
- `ea/scheduler.py` — new `find_duplicate_events(attendees, proposed_start, calendar, tz_name, window_days)`.
- `ea/responder.py` — `handle_inbound_result` and `handle_block_time_result` call
  `find_duplicate_events` before `create_event`.
- `ea/poll.py` — Pass 2 confirmation handler gains a `"confirm_duplicate"` action branch.

---

## Lower Impact — Polish

### 14. Calendar event descriptions
Created events currently have no description body. Populate it with:
- The original email thread snippet
- The EA: command that triggered it
- Attendee names/emails

Small addition to `create_event` calls in `responder.py`.

### 16. Hold / tentative blocks
"EA: hold Thursday 2-4pm for prep" — creates an event with
`transparency: "transparent"` so the owner sees it but it doesn't block
others' scheduling. Useful for protecting focus time.

New intent or a modifier on `block_time`: `block_type = "hold" | "hard"`.

### 17. Smart expiry — configurable window and deadline-aware
**Current behavior:** expiry is a hardcoded 48-hour fixed window (`EXPIRY_HOURS = 48`
in `responder.py`). It is purely clock-based — it has no knowledge of the proposed
meeting times.

**Problems with the current approach:**
1. Not configurable.
2. Not deadline-aware — if all proposed times have already passed, the state
   entry is stale but won't expire for up to 48 hours.
3. Terse expiry notification with no context.

**Proposed improvements:**

**A. Configurable reply window:**
```toml
[schedule]
pending_confirmation_hours  = 48   # how long to wait for owner's yes/no
pending_external_reply_days = 7    # how long to wait for external party's reply
```

**B. Deadline-aware expiry:**
Store `latest_proposed_at` when state is written. The expiry check uses
whichever comes first: `expires_at` or `latest_proposed_at + buffer` (e.g.
1 hour after the last proposed slot has passed).

**C. Richer expiry notification:**
```
EA: request expired — Coffee chat with sarah@example.com

The scheduling request below received no reply within the allowed window.
Proposed times: Thursday Mar 19 at 2:00 PM, Friday Mar 20 at 10:00 AM
Attendees: sarah@example.com

Reply "retry" to this email and EA will find new available slots.
```

**Implementation touchpoints:**
- `ea/responder.py` — `EXPIRY_HOURS` removed; `_expiry(parsed, config)` reads from config.
- `ea/state.py` — `expired()` checks both `expires_at` and `latest_proposed_at`.
- `ea/poll.py` — expiry email body replaced with richer template; "retry" keyword dispatched.

---

## Bigger Swings

### 20. Proactive availability — no EA: command needed
Detect when an inbound email is asking for availability ("when are you free
next week?") without an explicit `EA:` command. Classify with a lightweight
Claude call; if confidence is high, auto-trigger the `suggest_times` flow.

Requires a pre-filter classifier before the `EA:` trigger check in Pass 1.
Risk: false positives. Needs a confidence threshold and a config flag to opt in.

### 21. Slack / iMessage integration
Same `EA:` command syntax, different ingestion layer. The poll loop,
responder, and scheduler are reusable as-is.

- Slack: poll a DM channel via Slack API; reply in-thread
- iMessage: harder — no official API; would require Shortcuts or AppleScript

### 22. Microsoft 365 support (Outlook + Exchange Calendar)
Add support for Microsoft 365 as an alternative to Gmail + Google Calendar.
The poll loop, scheduler, responder, and parser are all provider-agnostic
today — they work through duck-typed `gmail` and `calendar` interfaces.

**What needs abstracting:**

*Email interface* (currently: `LiveGmailClient` / `FakeGmailClient`):
```python
list_threads(exclude_label_ids) -> list[Thread]
get_thread(thread_id)           -> Thread | None
send_email(to, subject, body, thread_id, extra_headers) -> Message
apply_label(thread_id, label)   -> None
```

*Calendar interface* (currently: `CalendarClient`):
```python
get_freebusy(time_min, time_max, attendees) -> dict
create_event(topic, start, end, attendees, ...) -> dict
list_events(time_min, time_max)              -> list[dict]
delete_event(event_id, send_updates)         -> None
update_event(event_id, new_start, new_end)   -> None
```

**New implementations needed:**
- `ea/outlook.py` — `OutlookMailClient` via Microsoft Graph
- `ea/msgraph_calendar.py` — `GraphCalendarClient` via `GET /me/calendar/getSchedule`
- `ea/auth_microsoft.py` — MSAL-based OAuth2 flow

**Auth config:**
```toml
[provider]
type = "google"      # or "microsoft"

[auth]
# Microsoft (new):
client_id     = "your-azure-app-client-id"
tenant_id     = "common"
token_file_ms = "token_ms.json"
```

**`runner.py` changes:**
Instantiate the correct client pair based on `config["provider"]["type"]`.

**Label mapping:**
Google labels map to Outlook **categories** (colored tags). The Graph API
supports `POST /me/outlook/masterCategories` and `PATCH /me/messages/{id}`.

**Suggested order:**
1. Rename `GmailMessage`/`GmailThread` to provider-neutral names
2. Implement `OutlookMailClient` with tests
3. Implement `GraphCalendarClient` with tests
4. Add `auth_microsoft.py` + `python ea.py auth --provider microsoft`
5. Wire into `runner.py` behind the config flag

### 23. Smart no-meeting window protection
Detect calendar fragmentation (many short gaps) and proactively suggest
blocking focus time. Could run as part of the weekly digest or as a
separate `ea protect` command.

```
python ea.py protect --day friday --hours 09:00-12:00
```

Creates a recurring hold block if that window is consistently free.

---

## Operations / Production

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
EnvironmentFile=/home/ea/.ea-env
StandardOutput=append:/home/ea/ea/ea.log
StandardError=append:/home/ea/ea/ea-error.log

[Install]
WantedBy=multi-user.target
```

Commands: `systemctl enable ea` / `systemctl start ea` / `systemctl status ea` /
`journalctl -u ea -f`.

Note: `After=network-online.target` ensures the service waits for network before
starting (important for OP-1).

### OP-6. OAuth token expiry and revocation handling
Google OAuth tokens auto-refresh via `google-auth` as long as the refresh
token is valid. If revoked, this would crash with `google.auth.exceptions.RefreshError`.

Required:
- Catch `RefreshError` in `runner.py` at the top of `run_once()`.
- Send a self-email or write to stderr: "EA: Google auth expired — run
  `python ea.py auth` to re-authenticate."
- Exit cleanly rather than retrying.

### OP-7. Auto-detect timezone
`schedule.timezone` is currently a required manual entry in `config.toml`.

**Proposed behavior:** make `schedule.timezone` optional. Resolution order:

1. `config.toml` value — explicit override always wins.
2. Google Calendar primary calendar `timeZone` — preferred auto-source.
3. OS local timezone via symlink resolution or `tzlocal` — fallback.
4. Error: "Cannot determine timezone — set `schedule.timezone` in config.toml."

**Implementation:**
- `ea/config.py` — new `resolve_timezone(config, creds=None) -> str` function.
- `ea/runner.py` — call `resolve_timezone(config, creds)` after loading creds.

**Dependency:** `tzlocal` covers the OS fallback on macOS, Linux, and Windows.

### OP-8. Config validation at startup
Currently a missing or malformed `config.toml` causes confusing crashes mid-poll.
Validate the config once at startup before any API calls are made.

Required checks:
- `user.email` is present and looks like an email address
- `auth.credentials_file` and `auth.token_file` paths exist
- `schedule.timezone` is a valid IANA timezone name
- Working/preferred hours are valid `HH:MM` strings and start < end

Fail fast: `"Config error: user.email is required in config.toml"`

### OP-9. Health monitoring / dead-man's switch
No current way to know if the process died silently. Options (pick one):

- **Healthchecks.io ping** (free tier): GET a unique URL at the end of each
  successful poll cycle. Zero infrastructure required.
- **Uptime Kuma** (self-hosted): same concept.
- **Heartbeat email**: once per day, send a self-email "EA: alive — N threads
  processed today."

### OP-10. Configurable LLM model and provider abstraction
Currently the Anthropic model name is hard-coded in three places:
`meeting_parser.py`, `classifier.py` (twice).

**Level 1 — configurable model name** (low effort, high value):
```toml
[llm]
model = "claude-sonnet-4-20250514"
```

**Level 2 — provider abstraction** (optional):
Introduce `ea/llm.py` with a single `complete(system, user, max_tokens, config)` function.

```toml
[llm]
provider = "anthropic"   # or "openai", "ollama", etc.
model    = "claude-sonnet-4-20250514"
```

Suggested order: do Level 1 first, then Level 2 only if a second provider is
actually needed.

### OP-11. Pluggable state and lock backends
`state.json` and `.state.lock` are local files only, which breaks Cloud Run /
Cloud Functions deployments.

**Required abstraction:**
```toml
[state]
backend = "local"   # default; or "gcs"

# GCS backend settings:
gcs_bucket = "my-ea-state"
gcs_prefix = "ea/"
```

`StateBackend` interface:
```python
def load() -> dict
def save(state: dict)
def acquire_lock() -> bool
def release_lock()
```

**GCS backend:**
- `load` / `save`: read/write a single JSON object at `gs://{bucket}/{prefix}state.json`
- `acquire_lock`: write the lock object with `if_generation_match=0` (atomic create).

**Migration path:**
1. Refactor `StateStore.__init__` to accept a backend object instead of a path.
2. Extract lock logic from `runner.py` into the backend.
3. `runner.py` instantiates the backend based on config.

### OP-12. Configurable command sentinel
The string `EA:` is hardcoded in trigger detection, outgoing email subjects,
and the parser system prompt.

**Config key:**
```toml
[user]
sentinel = "EA"       # trigger word; the colon is added automatically
```

**Implementation:**
- `config.py`: expose `get_sentinel(config) -> str`.
- `ea/triggers.py` and `ea/poll.py`: replace hardcoded `r'EA:\s*(.+)'`
  with `rf'{re.escape(sentinel)}:\s*(.+)'`.
- `ea/responder.py` and `ea/poll.py` subjects: replace `"EA: "` with `f"{sentinel}: "`.
- `ea/parser/meeting_parser.py`: inject the sentinel into `SYSTEM_PROMPT` at parse time.

### OP-13. Gmail push notifications (replace polling)
Gmail's Watch API can push a notification the moment a new message arrives,
eliminating polling latency and reducing unnecessary API calls.

**How Gmail push works:**
1. Call `users.watch()` on the Gmail API, specifying a Google Cloud Pub/Sub topic.
2. Gmail publishes a notification to Pub/Sub when the mailbox changes.
3. Your code fetches the actual changes via `users.history.list()`.
4. Watch subscriptions expire after **7 days** and must be renewed.

**Two delivery modes:**
- **Push (Cloud Run / Cloud Functions):** Pub/Sub sends an HTTP POST to your endpoint.
- **Pull (Mac / local):** blocking `subscriptions.pull()` replaces `time.sleep()`.

**Required infrastructure (one-time setup):**
```
gcloud pubsub topics create ea-gmail-push
gcloud pubsub subscriptions create ea-gmail-sub --topic ea-gmail-push
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
watch_renew_days    = 6
```

**Code changes:**
- `ea/runner.py` — `run_loop()` gains a `pubsub_pull` mode.
- New `ea/gmail_watch.py` — `ensure_watch(gmail_service, config)`.
- New `ea/entrypoints/pubsub_push.py` — minimal Flask/Functions Framework handler.

**Suggested order:** implement `pubsub_pull` first; add `pubsub_push` only when
deploying to Cloud Run.

Note: OP-13 also fixes the "Send and Archive" limitation described in OP-14.

### OP-14. Inbox-only constraint and "Send and Archive" limitation
EA's Pass 1 queries `in:inbox` exclusively. If the owner has "Send and Archive"
enabled (or manually archives before the next poll), EA never sees the `EA:`
command in the reply.

**Workaround (current):** Ensure the thread remains in your inbox until after
the next poll cycle. Disable "Send and Archive" in Gmail settings.

**Correct fix (future, via OP-13):** Switch Pass 1 to an incremental
`users.history.list()` scan keyed on the last-seen `historyId`. The history
API records every mailbox change including sent messages.

---

## Security

### SEC-3. Authenticated-received-chain (ARC) / SPF+DKIM verification
SEC-1 confirms the From address string matches the owner's email, but a
determined attacker who can forge headers could still craft a matching From.
Gmail's `Authentication-Results` headers record SPF and DKIM check results.

**Implementation sketch:**
- `ea/gmail.py` — `_parse_message` extended to capture `Authentication-Results`.
- `ea/poll.py` — after `parseaddr` confirms the From address, also check for
  `dkim=pass` and/or `spf=pass`.
- Make the check opt-in via config:
  ```toml
  [security]
  require_dkim_pass = false   # set true to reject commands without DKIM=pass
  ```

**Caveats:**
- Email forwarding strips or invalidates DKIM signatures.
- For a personal, non-forwarded Gmail inbox, SEC-1 is already very strong.
  This item is only worth implementing if the threat model includes a
  sophisticated attacker with header-forging capability.

---

## Lower Priority / Notes

### Language detection for multilingual scheduling
`dateparser` supports 200+ languages, but the normalizer is configured with a
static `languages = ["en"]` list. Users receiving meeting requests in other
languages would benefit from automatic language detection.

**Proposed approach:**
```toml
[parser]
language_detection = true   # auto-detect; false = use languages list only
languages = ["en"]          # fallback / hint list when detection is off
```

Candidate detection library: [`langdetect`](https://pypi.org/project/langdetect/)
or [`lingua-language-detector`](https://pypi.org/project/lingua-language-detector/).

Note: `langdetect` is non-deterministic by default; call `DetectorFactory.seed = 0`
for reproducibility in tests.

### Consider `whenever` for datetime/timezone arithmetic (if pain arises)
[`whenever`](https://github.com/ariebovenberg/whenever) is a modern Python
datetime library with distinct types for UTC instants, local times, and zoned
datetimes that make incorrect combinations a static/runtime error.

**Do not adopt preemptively.** Only revisit if the project encounters real
problems such as a DST-related scheduling bug or ambiguous wall-clock time
handling during fall-back.

If adopted, main integration points: `ea/scheduler.py`, `ea/parser/date_normalizer.py`,
`ea/digest.py`.


### OP-5. Log rotation

This is only needed if it's running as a long lived server.  The current
approach is to use a cron-like system.

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

For macOS with launchd: use `newsyslog` or switch to `RotatingFileHandler`
in `ea/log.py` (max 10 MB, 5 backups).

