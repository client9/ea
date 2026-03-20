# EA Project Plan

## Status

| Layer | Status |
|---|---|
| Parser (Claude API → structured dict) | Done |
| Scheduler (`check_slot`, `evaluate_parsed`, four outcomes) | Done |
| State machine (`StateStore`, `responder`, `run_poll`) | Done |
| Test infrastructure (`FakeGmailClient`, 46 tests) | Done |
| `suggest_times` outbound trigger | Phase 1 |
| `block_time` separate dispatch | Phase 1 |
| Reply classifiers (`classifier.py`) | Phase 1 |
| Google OAuth setup | Phase 2 |
| Live Calendar client | Phase 3 |
| Live Gmail client | Phase 4 |
| Runner / entry point | Phase 5 |
| Logging, dry-run, hardening | Phase 6 |

---

## Phase 1 — Close inbound gaps (no new APIs needed)

### 1a. `block_time` separate dispatch
`block_time` currently falls through `handle_inbound_result` and would try to
send calendar invites. Needs its own handler:
- `handle_block_time_result()` in `responder.py`: creates a solo event on my calendar,
  applies `ea-scheduled`. No `needs_confirmation` path — an explicit block should
  always execute regardless of time-of-day.
- Pass 1 in `poll.py` dispatches to it when `parsed["intent"] == "block_time"`.

### 1b. `suggest_times` intent + outbound trigger
Add `"suggest_times"` to the parser system prompt. Fires when I write
`EA: suggest some times to meet` on an outgoing thread — no proposed times
exist in the thread, just find my best available slots.

Add `find_slots()` to `scheduler.py`: queries freebusy for a lookahead window in
one API call, walks slots in 30-minute increments within working hours, returns
top N free slots sorted preferred → working.

Add `handle_suggest_times_trigger()` to `responder.py`:
- Calls `find_slots()` (or injected `find_slots_fn` in tests)
- If no slots found: emails me "no availability"
- Otherwise: sends slots to recipient on the original thread, writes
  `pending_external_reply` state

Pass 1 in `poll.py` dispatches to it when `parsed["intent"] == "suggest_times"`.
Add `find_slots_fn` injectable parameter to `run_poll`.

### 1c. `classifier.py` — production reply classifiers
`confirm_eval_fn` and `external_reply_fn` are currently `None` in production
(modification and outbound replies fall through to "still-unclear"/"no-action").

Add `ea/classifier.py`:

- `classify_confirmation_reply(reply_text, entry, config) -> ScheduleResult`
  Calls Claude to extract a new time constraint from my modification reply,
  re-runs `evaluate_parsed` with the updated parsed dict, returns a
  `ScheduleResult`. Handles yes/no keyword short-circuit before calling Claude.

- `classify_external_reply(reply_text, entry, config) -> tuple[str, ...]`
  Calls Claude to classify the external party's reply as one of:
  - `("confirmed", slot_dict)` — they picked a specific slot
  - `("counter", constraint_text)` — they counter-proposed; caller re-runs `find_slots`
  - `("stated_availability", times_text)` — they gave their availability; cross-reference

Wire these into `runner.py` (Phase 5) as the defaults for `run_poll`.

---

## Phase 2 — Google OAuth

Add `ea/auth.py`:

```
python ea.py auth          # browser OAuth2 flow, saves token.json
python ea.py auth --check  # print active scopes
```

Required scopes:
- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/calendar`

Flow: read `credentials.json` (downloaded from GCP Console) → OAuth2 consent →
save `token.json`. Refresh token automatically on expiry.
Both files are in `.gitignore`.

**One-time GCP setup (documented in README):**
1. Create a GCP project
2. Enable Gmail API + Google Calendar API
3. Create OAuth2 Desktop credentials
4. Download `credentials.json` to the project root

---

## Phase 3 — Live Calendar client

Implement the live mode of `CalendarClient`:

- `get_freebusy(time_min, time_max, attendees)` — call `freebusy.query`
- `create_event(topic, start, end, attendees, location)` — call `events.insert`
  with `sendUpdates="all"` so Google sends the invites automatically

`find_slots()` in `scheduler.py` already works against the `CalendarClient`
interface — no changes needed once `get_freebusy` is live.

---

## Phase 4 — Live Gmail client

Add `LiveGmailClient` to `ea/gmail.py`. Implements the same duck-type interface
as `FakeGmailClient`:

- `list_threads(exclude_label_ids)` — Gmail search query
  `-label:ea-scheduled -label:ea-notified ...`, parse into `GmailThread` objects
- `get_thread(thread_id)` — `users.threads.get(format="full")`, base64-decode
  bodies, extract headers into `GmailMessage` objects
- `send_email(to, subject, body, thread_id, extra_headers)` — build MIME
  message, base64-encode, call `users.messages.send`; set `threadId` if
  replying in-thread
- `apply_label(thread_id, label)` — `users.labels.list` to find or create
  the label, then `users.messages.modify`

Helper: `_ensure_labels_exist()` creates the four `ea-*` labels on first run
if they don't already exist.

---

## Phase 5 — Runner / entry point

Add `ea/runner.py`:

```python
def run(config, dry_run=False):
    gmail    = LiveGmailClient(creds=load_creds())
    calendar = LiveCalendarClient(creds=load_creds())
    state    = StateStore()   # reads/writes state.json
    run_poll(
        gmail, calendar, state, config,
        confirm_eval_fn=classify_confirmation_reply,
        external_reply_fn=classify_external_reply,
        dry_run=dry_run,
    )
```

Update `ea.py` CLI:

```
python ea.py --poll           # run one poll cycle
python ea.py --poll --dry-run # show actions without sending/creating
python ea.py --run            # loop every N minutes (from config)
```

Add `[schedule] poll_interval_minutes = 5` to `config.toml`.

Include a sample `launchd` plist (macOS) and `systemd` unit file (Linux) in
`docs/` for running as a background service.

---

## Phase 6 — Hardening

**Logging:** `ea/log.py` wrapping Python `logging`, structured JSON lines to
`ea.log`. Every poll cycle logs what it processed. Errors log thread ID +
traceback.

**Dry-run mode:** `dry_run: bool` passed through `run_poll` and all responder
functions. When `True`, logs "would send" / "would create" but skips API calls.

**State file locking:** Write `.state.lock` while `run_poll` is executing.
If lock exists on startup, warn and skip (prevents two cron invocations
colliding).

**Error isolation:** Each thread's processing is wrapped in try/except. A crash
on one thread logs the error and continues — doesn't abort the whole cycle.

---

## Recommended order

```
Phase 1 (gaps + classifiers)
  → Phase 2 (OAuth)
  → Phase 3 (live Calendar) + Phase 4 (live Gmail)   ← can parallelize
  → Phase 5 (runner)
  → Phase 6 (hardening)
```
