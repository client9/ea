# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python CLI tool that acts as an AI-powered Executive Assistant. It monitors Gmail for `EA:` commands in email threads, parses meeting requests using the Claude API, checks Google Calendar availability, and automatically schedules meetings, suggests times, or blocks calendar time.

## Setup

```bash
pip install -e .
pip install -e ".[google]"    # Gmail + Calendar API support
export ANTHROPIC_API_KEY=your_key_here
python ea.py auth             # one-time Google OAuth flow
```

## Commands

```bash
# Live polling
python ea.py poll             # run one poll cycle
python ea.py poll --dry-run   # show actions without sending/creating anything
python ea.py poll --quiet     # suppress all stdout (for launchd/cron — logs to ea.log only)
python ea.py run              # loop continuously (interval from config.toml)
python ea.py status           # show pending state entries (topic, attendees, expiry)
python ea.py dismiss <id>     # dismiss a pending entry by thread ID (clears state, labels thread)
python ea.py digest           # print today's calendar digest to stdout (preview, no email)
python ea.py reset            # clear state.json to start fresh

# Google auth
python ea.py auth             # browser OAuth2 flow, saves token.json
python ea.py auth --check     # show current auth status

# Parser debugging (no Gmail/Calendar)
python ea.py parse "Can we meet Thursday at 2pm?"
python ea.py parse --file testdata/03_multi_turn_thread.txt
python ea.py testdata         # run all files in testdata/
```

## Configuration

`config.toml` at the project root.

```toml
[auth]
credentials_file = "client_secret_....json"  # downloaded from GCP Console
token_file = "token.json"

[user]
email = "you@example.com"
name  = "Your Name"

[schedule]
timezone              = "America/Los_Angeles"
poll_interval_seconds = 300      # launchd StartInterval should match this
timeout_seconds       = 30       # per-call timeout for Anthropic and Google APIs

[schedule.working_hours]
monday = { start = "09:00", end = "17:00" }
# Days not listed are unavailable for scheduling.

[schedule.preferred_hours]
monday = { start = "10:00", end = "16:00" }

# Optional: duration defaults when parser can't determine meeting length.
# meeting_type key first, then "default" as global fallback.
# Without this section, missing duration still triggers the ambiguous flow.
[schedule.duration_defaults]
coffee_chat = 30
interview   = 60
1on1        = 30
board       = 90
standup     = 15
default     = 30
```

## Architecture

- **`ea.py`** — CLI entry point. Subcommands: `auth`, `poll`, `run`, `reset`, `parse`, `testdata`.
- **`ea/auth.py`** — `run_auth_flow()` / `check_auth()` / `load_creds()`. Handles Google OAuth2, saves `token.json`, auto-refreshes on expiry.
- **`ea/config.py`** — Loads `config.toml`. `load_config()` returns the full dict; `get_my_email()` is a convenience accessor.
- **`ea/log.py`** — `configure(log_file, quiet=False)` sets up logging once: JSON-lines to `ea.log` (all levels) and human-readable to stdout (INFO+). Pass `quiet=True` to suppress the stdout handler (used by `poll --quiet` for launchd/cron). `get_logger(name)` returns a standard `logging.Logger`.
- **`ea/gmail.py`** — `LiveGmailClient` and `FakeGmailClient` share a duck-typed interface: `list_threads`, `get_thread`, `send_email`, `apply_label`. `thread_to_text(thread)` flattens a `GmailThread` to a string for the parser.
- **`ea/calendar.py`** — `CalendarClient(creds, fixture_path, fixture_data)`. Pass `creds` for live API, `fixture_data` (dict) or `fixture_path` (file) for tests. `get_freebusy(time_min, time_max, attendees)` returns the Google Calendar freebusy shape. `create_event(topic, start, end, attendees, location, self_email)` books the meeting; setting `self_email` auto-accepts that attendee and suppresses invite emails for solo blocks.
- **`ea/triggers.py`** — `find_ea_trigger(text, my_email)` returns the command after `EA:` if the user sent one, else `None`.
- **`ea/parser/meeting_parser.py`** — `parse_meeting_request(text, tz_name)` calls Claude (`claude-sonnet-4-20250514`) and returns structured JSON. Claude outputs plain-English time phrases (`normalized`); `parsedatetime` converts them to UTC ISO strings post-call. Callers see a `datetimes` list on each `proposed_times` entry. `validate_parsed(parsed, thread_id)` runs immediately after `json.loads` (before datetime normalization) and raises `ValueError` on malformed or out-of-range fields — topic length/newlines, attendee list shape, duration range, datetime plausibility — as a prompt injection defense (SEC-2).
- **`ea/scheduler.py`** — Three levels:
  - `check_slot(start, end, attendees, working_hours, preferred_hours, calendar, timezone) -> SlotResult` — is one specific slot free, and what type?
  - `evaluate_parsed(parsed, working_hours, preferred_hours, timezone, calendar, my_email) -> ScheduleResult` — given a parsed dict and a calendar, return one of four outcomes.
  - `find_slots(parsed, config, calendar, restrict_to_date) -> list[dict]` — find available slots in a lookahead window; `restrict_to_date` limits the search to a single day.
  - `ScheduleResult.outcome`: `"ambiguous"` (missing info), `"open"` (free preferred slot, ready to book), `"busy"` (all slots taken), `"needs_confirmation"` (free but outside preferred hours — needs human approval).
- **`ea/responder.py`** — One handler per intent outcome: `handle_inbound_result`, `handle_block_time_result`, `handle_suggest_times_trigger`, `handle_confirmation_reply`, `handle_external_reply`. All take injected `gmail`, `calendar`, `state`, `config` — no global state.
- **`ea/classifier.py`** — `classify_confirmation_reply(text, entry, config)` and `classify_external_reply(text, entry, config)`. Call Claude to parse my confirmation reply or the other party's reply, return a `ScheduleResult` or `(action, slot)` tuple respectively.
- **`ea/network.py`** — Retry and timeout utility. `configure(attempts, base_delay, cap, api_timeout)` sets module-level policy: `run_loop()` uses 3 attempts with backoff; `run_once()` (cron/poll) uses 1 attempt (fail fast). `call_with_retry(fn)` wraps any callable — timeout errors retry immediately (no backoff); connection errors retry with exponential backoff. `get_api_timeout()` returns the configured timeout for Anthropic client construction. `socket.setdefaulttimeout()` is set automatically to cover Google's httplib2 calls.
- **`ea/poll.py`** — `run_poll(gmail, calendar, state, config, ...)` runs one full three-pass cycle. All dependencies are injected (testable without real APIs). Each thread's processing is wrapped in try/except — a crash on one thread logs the error and continues rather than aborting the cycle. `_ALLOWED_INTENTS` gates Pass 1 dispatch — unknown intents are logged as WARNING ("possible prompt injection") and treated as parse errors (SEC-2).
- **`ea/state.py`** — `StateStore(path)`. In-memory dict backed by `state.json`. Only threads actively awaiting a reply are stored; completed threads are removed and marked with Gmail labels. Pass `path=None` for in-memory-only (tests).
- **`ea/runner.py`** — `run_once()` wires live clients, calls `network.configure(attempts=1, api_timeout=timeout_seconds)` (no retry in cron mode), and calls `run_poll()`. Acquires an exclusive `fcntl` lock on `.state.lock` before polling; logs a warning and returns immediately if already locked. After `run_poll()`, checks digest conditions and sends the daily digest email if due (unless `--dry-run`). `run_loop()` calls `network.configure(attempts=3, cap=poll_interval_seconds)` for retry with backoff, then loops `run_once()` at `poll_interval_seconds`.
- **`ea/digest.py`** — Daily digest. `should_send_digest(config, now_local)` checks `[digest]` section, configured days, and `send_time` (default `"08:00"` local). `already_sent_today` / `mark_sent_today` use `digest_sent.json` for deduplication. `build_digest(config, calendar, state)` returns `(subject, body)` with today's events (sorted, all-day first) and pending state entries. `get_today_window(tz_name)` returns the UTC midnight-to-midnight window for today in the user's timezone. The `digest` CLI subcommand prints the body to stdout (preview only — no email sent).

## Poll loop (three passes)

Each `run_poll` cycle:
1. **Expiry check** — threads past `expires_at` get `ea-expired` label, owner notified, removed from state.
2. **Pass 1 — New triggers** — scans unlabeled threads for `EA:` commands from `my_email`. Dispatches by intent: `meeting_request` → `handle_inbound_result`, `suggest_times` → `handle_suggest_times_trigger`, `block_time` → `handle_block_time_result`, `cancel_event` → `handle_cancel_result`, `reschedule` → `handle_reschedule_result`, `ignore` → `handle_ignore_result`. Unknown intents send a parse-error email and apply `ea-notified`. Note: dismiss commands (`_DISMISS_RE`) bypass the "skip if in state" check so the owner can dismiss `pending_external_reply` threads or `pending_confirmation` via the confirmation thread.
3. **Pass 2 — Pending confirmations** — threads in `pending_confirmation` state waiting for my yes/no reply.
4. **Pass 3 — Pending external replies** — threads in `pending_external_reply` state waiting for the other party's slot confirmation.

## Intent → outcome flow

- `meeting_request` + `open` → create event, send invite, email me "booked", apply `ea-scheduled`
- `meeting_request` + `needs_confirmation` → email me to confirm after-hours slot, write `pending_confirmation` state
- `meeting_request` + `ambiguous` → email me with missing details + "Reply with details and EA will try again", apply `ea-notified`
- `meeting_request` + `busy` → auto-find alternatives; if found, send to external party and write `pending_external_reply`; if none, email me "conflict found", apply `ea-notified`
- `suggest_times` → find slots, send to recipient on thread, write `pending_external_reply` state
- `block_time` → create solo calendar event (auto-accepted, no invite), apply `ea-scheduled`
- `cancel_event` → find matching event via `find_matching_event`; delete it and notify me, or notify me if not found / ambiguous
- `reschedule` → find matching event; check new slot free; update event and notify me; handles not-found, ambiguous, busy, missing-new-time cases
- `ignore` / `dismiss` / `never mind` / `forget it` → delete state entry, apply `ea-cancelled`, email me "EA: dismissed — {topic}". If `pending_external_reply`, notes that no cancellation was sent to the external party. Also available as `python ea.py dismiss <thread_id>`.

## Running Tests

```bash
python -m pytest tests/ -v
```

Tests use `FakeGmailClient` (in-memory), `CalendarClient(fixture_data=...)`, and `StateStore(path=None)` — no real APIs needed. Parser and classifier calls are replaced with injected lambda functions.

**Test fixtures** live in `testdata/`. Each email thread has a `.txt` file and a paired `.json` calendar fixture. The `_comment` field in each JSON describes the intended scheduler outcome.

## Output Schema

`parse_meeting_request` returns:

- `intent`: `"meeting_request"` | `"suggest_times"` | `"block_time"` | `"cancel_event"` | `"reschedule"` | `"none"`
- `topic`: meeting purpose, block label, or title of existing event to find
- `proposed_times`: list of `{"text": "...", "datetimes": ["ISO 8601 UTC", ...]}`. Each `datetimes` entry is one distinct start time (e.g. "at 3 or 5pm" → two entries).
- `new_proposed_times`: same shape as `proposed_times`; populated for `reschedule` intent only (the new desired time). Always present, empty list if not applicable.
- `duration_minutes`, `location`, `timezone`, `urgency`
- `meeting_type`: `"coffee_chat"` | `"interview"` | `"1on1"` | `"board"` | `"standup"` | `"workshop"` | `"lunch"` | `null` — inferred meeting category. Used by `_resolve_duration` in `poll.py` to look up a default duration from `[schedule.duration_defaults]` when `duration_minutes` is null.
- `all_day`: `true` for all-day / multi-day events; `false` (default) for timed events
- `event_type`: `"ooo"` | `"vacation"` | `"conference"` | `"holiday"` | `"block"` | `null` — all-day event subtype; null for timed events. Opaque types (block scheduling): `ooo`, `vacation`, `block`. Transparent types (informational, still free): `conference`, `holiday`.
- `proposed_times[*].datetimes`: UTC ISO 8601 strings for timed events; `YYYY-MM-DD` local date strings for all-day events. For all-day ranges, two date entries: `[start_date, end_date_inclusive]`.
- `attendees`: only for `meeting_request` and `suggest_times`
- `ambiguities`: only for `meeting_request`

## Deployment (macOS)

For local always-on use, see `docs/install-macos.md` for the full setup guide.
Quick version:
```bash
bash scripts/install-launchd.sh   # fills paths, loads launchd agent
launchctl list com.ea.poll        # check status
tail -f ea.log                    # watch structured JSON logs
```
The launchd agent uses `StartInterval` (cron-like: start → one cycle → exit) with
`poll --quiet`. `StartInterval` in the plist should match `poll_interval_seconds`
in `config.toml`.

## Key Constraints

- The Claude system prompt must request **raw JSON only** — no markdown fences — because the response is passed directly to `json.loads()`.
- `ANTHROPIC_API_KEY` must be set for parse/classify calls.
- Every parse and classify call makes a live Claude API call (no caching).
- All live API calls are wrapped with `network.call_with_retry()`. Timeout errors retry immediately; connection errors use exponential backoff. Cron/poll mode uses 1 attempt (fail fast); loop mode uses 3 attempts.
- State file locking uses `fcntl.flock` (Unix/macOS only).
- `ea.log` is written to the working directory (project root when run via `ea.py`).
- Dependencies are declared in `pyproject.toml` only (`requirements.txt` has been removed). Use `pip install -e ".[google]"` for all deps.
