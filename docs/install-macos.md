# Installing EA on macOS with launchd

This guide walks through getting EA running as a background service on macOS.
The setup uses launchd's `StartInterval` feature — launchd starts a poll cycle
every 5 minutes, EA runs, and exits. This is the macOS equivalent of cron and
is the recommended approach for a personal Mac.

---

## Prerequisites

- macOS 12 or later
- Python 3.11 or later (`python3 --version`)
- A Google account with Gmail and Google Calendar
- An Anthropic API key (`sk-ant-...`) from [console.anthropic.com](https://console.anthropic.com)

---

## Step 1 — Google Cloud credentials

EA needs permission to read your Gmail and manage your Google Calendar. This
requires a one-time setup in Google Cloud Console.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create
   a new project (or reuse an existing one).

2. Enable two APIs:
   - **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable
   - **Google Calendar API**: same path, search "Google Calendar API" → Enable

3. Create OAuth credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Name: anything (e.g. "EA Assistant")
   - Download the JSON file — it will be named something like
     `client_secret_XXXXXXX.apps.googleusercontent.com.json`

4. On the OAuth consent screen, add your own Gmail address as a **test user**
   (required while the app is in "Testing" status).

Keep the downloaded JSON file — you'll reference it in `config.toml`.

---

## Step 2 — Install the project

```bash
git clone <repo-url> ~/projects/ea
cd ~/projects/ea

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e ".[google]"
```

---

## Step 3 — Configure

Copy the credentials file into the project root (or leave it wherever you
downloaded it and use the full path in `config.toml`).

Edit `config.toml`:

```toml
[auth]
credentials_file = "client_secret_XXXXXXX.apps.googleusercontent.com.json"
token_file       = "token.json"

[user]
email = "you@gmail.com"
name  = "Your Name"

[schedule]
timezone              = "America/Los_Angeles"   # IANA timezone name
poll_interval_seconds = 300                     # 5 minutes
timeout_seconds       = 30                      # per-call API timeout

# Days not listed are unavailable for scheduling.
[schedule.working_hours]
monday    = { start = "09:00", end = "17:00" }
tuesday   = { start = "09:00", end = "17:00" }
wednesday = { start = "09:00", end = "17:00" }
thursday  = { start = "09:00", end = "17:00" }
friday    = { start = "09:00", end = "17:00" }

# Preferred hours are offered first when suggesting times.
[schedule.preferred_hours]
monday    = { start = "10:00", end = "16:00" }
tuesday   = { start = "10:00", end = "16:00" }
wednesday = { start = "10:00", end = "16:00" }
thursday  = { start = "10:00", end = "16:00" }
friday    = { start = "10:00", end = "16:00" }
```

Set your `ANTHROPIC_API_KEY` — see the API key options in Step 5 below before
deciding where to put it.

---

## Step 4 — Authorize Google

Run the one-time OAuth flow. This opens a browser window asking you to approve
access to your Gmail and Calendar:

```bash
source .venv/bin/activate
python ea.py auth
```

This saves `token.json` in the project root. The token auto-refreshes; you
should not need to repeat this step unless you explicitly revoke access in your
Google account settings.

To verify auth is working:

```bash
python ea.py auth --check
```

---

## Step 5 — Test a manual poll

Before setting up launchd, confirm a poll cycle completes successfully:

```bash
source .venv/bin/activate
python ea.py poll --dry-run
```

`--dry-run` shows what EA would do without sending any emails or creating
calendar events. If you see "Poll complete — nothing to process" (or a list of
thread actions), the setup is working.

Run a real poll cycle:

```bash
python ea.py poll
```

---

## Step 6 — Set up the launchd agent

### Option A — Install script (recommended)

The install script fills in all paths automatically and loads the agent:

```bash
bash scripts/install-launchd.sh
```

The script:
1. Checks that `.venv`, `config.toml`, and `token.json` exist
2. Reads your API key (from `$ANTHROPIC_API_KEY`, prompts if not set, or uses
   `~/.ea-env` if it exists — see API key security note below)
3. Writes a filled-in plist to `~/Library/LaunchAgents/com.ea.poll.plist`
4. Loads the agent — EA starts polling immediately

### Option B — Manual install

```bash
cp docs/launchd/com.ea.poll.plist ~/Library/LaunchAgents/
```

Edit `~/Library/LaunchAgents/com.ea.poll.plist` and replace the three
`CHANGEME_*` values:

| Placeholder | Example value |
|---|---|
| `CHANGEME_VENV_PYTHON` | `/Users/you/projects/ea/.venv/bin/python` |
| `CHANGEME_PROJECT_DIR` | `/Users/you/projects/ea` |
| `CHANGEME_ANTHROPIC_API_KEY` | `sk-ant-...` |

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.ea.poll.plist
```

### API key security note

Embedding the key directly in the plist stores it in plain text in
`~/Library/LaunchAgents/`. That is readable by your user account only, which
is acceptable for a personal machine. If you prefer not to store it there,
create `~/.ea-env`:

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' > ~/.ea-env
chmod 600 ~/.ea-env
```

If `~/.ea-env` exists when you run `install-launchd.sh`, the script
automatically creates a wrapper script that sources it instead of embedding
the key in the plist.

---

## Step 7 — Verify the agent is running

```bash
launchctl list com.ea.poll
```

Output looks like:

```
{
    "LimitLoadToSessionType" = "Aqua";
    "Label" = "com.ea.poll";
    "OnDemand" = true;
    "LastExitStatus" = 0;
    "PID" = -;
    "Program" = "/Users/you/projects/ea/.venv/bin/python";
};
```

`LastExitStatus = 0` means the last run succeeded. Any non-zero value
indicates a failure — check `ea-launchd.err` (see Troubleshooting below).

Trigger one cycle immediately without waiting for the interval:

```bash
launchctl start com.ea.poll
```

Watch the log in real time:

```bash
tail -f ~/projects/ea/ea.log
```

Check pending scheduling requests:

```bash
cd ~/projects/ea && source .venv/bin/activate
python ea.py status
```

---

## How it works

launchd starts `python ea.py poll --quiet` every 300 seconds (matching
`poll_interval_seconds` in `config.toml`). Each invocation:

1. Acquires a lock so concurrent runs can't overlap
2. Scans Gmail inbox for `EA:` commands you've added to email threads
3. Checks Google Calendar availability for any proposed meeting times
4. Books meetings, suggests times, or asks for confirmation as appropriate
5. Exits — launchd starts the next cycle in 5 minutes

The `--quiet` flag suppresses all stdout. All activity is logged as structured
JSON to `ea.log` in the project root. launchd only sends notification emails on
stdout output, so quiet mode keeps your inbox free of cron noise.

---

## Managing the agent

```bash
# Check status and last exit code
launchctl list com.ea.poll

# Trigger one poll cycle immediately
launchctl start com.ea.poll

# Stop a currently-running cycle
launchctl stop com.ea.poll

# Remove the agent (stops polling until re-loaded)
launchctl unload ~/Library/LaunchAgents/com.ea.poll.plist

# Re-load after editing the plist or after unload
launchctl unload ~/Library/LaunchAgents/com.ea.poll.plist 2>/dev/null; \
launchctl load  ~/Library/LaunchAgents/com.ea.poll.plist

# Watch logs live
tail -f ~/projects/ea/ea.log

# View recent log entries formatted
python -c "
import json, sys
for line in open('ea.log'):
    r = json.loads(line)
    print(r['ts'][:19], r['level'], r['msg'])
" | tail -50
```

---

## Troubleshooting

### `LastExitStatus` is non-zero

```bash
cat ~/projects/ea/ea-launchd.err
```

This file captures Python startup errors — import failures, missing `.venv`,
syntax errors — that occur before EA's own logging is configured. Common causes:

- **`.venv` not found**: the plist `VENV_PYTHON` path is wrong, or the venv
  was recreated. Re-run `install-launchd.sh`.
- **`ModuleNotFoundError`**: run `pip install -e ".[google]"` inside the venv.
- **`config.toml` missing**: EA must run from the project root. Check
  `WorkingDirectory` in the plist.

### Google auth expired

If `token.json` is revoked (you can check in your Google Account → Security →
Third-party apps), re-run the auth flow:

```bash
cd ~/projects/ea && source .venv/bin/activate
python ea.py auth
```

The agent will resume working at the next poll cycle.

### Nothing is happening

1. Check `launchctl list com.ea.poll` — confirm the agent is loaded and
   `LastExitStatus` is 0.
2. Check `ea.log` — if it's empty, the agent hasn't run yet. Trigger manually:
   `launchctl start com.ea.poll`.
3. Check `python ea.py status` — if there are no pending entries and no log
   activity, EA may simply have nothing to process. Add an `EA: schedule` reply
   to an email thread to test.
4. Run `python ea.py poll` interactively (without `--quiet`) to see full output.

### Polling interval mismatch

The `StartInterval` in the plist (default 300 seconds) and
`poll_interval_seconds` in `config.toml` are independent settings. They should
be kept in sync. The plist controls how often launchd starts the process;
`poll_interval_seconds` controls the retry backoff cap for transient network
errors. If you change one, update the other.

---

## Uninstalling

```bash
launchctl unload ~/Library/LaunchAgents/com.ea.poll.plist
rm ~/Library/LaunchAgents/com.ea.poll.plist
```

To also remove the state and logs:

```bash
cd ~/projects/ea
rm -f state.json ea.log ea-launchd.err .state.lock
```
