"""
runner.py

Wires together live Gmail + Calendar clients and runs the poll loop.
Used by the --poll and --run CLI commands.

Locking: run_once() acquires an exclusive advisory lock on .state.lock before
calling run_poll(). If the lock is already held (another poll cycle is running),
run_once() logs a warning and returns an empty summary immediately. The lock is
released automatically when the process exits, even on crash.
"""

import fcntl
import time as _time
from pathlib import Path

LOCK_FILE = ".state.lock"


def _acquire_lock(path: str):
    """
    Try to acquire an exclusive non-blocking flock on path.
    Returns the open file object on success, None if already locked.
    """
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    import os
    f.write(str(os.getpid()))
    f.flush()
    return f


def _release_lock(f) -> None:
    fcntl.flock(f, fcntl.LOCK_UN)
    f.close()


def run_once(
    config: dict = None,
    dry_run: bool = False,
    credentials_file=None,
    token_file=None,
) -> dict:
    """
    Run one full poll cycle with live Gmail and Calendar clients.

    Returns the summary dict from run_poll(), or an empty summary if the
    lock is already held by another process.
    """
    from ea.auth import load_creds
    from ea.calendar import CalendarClient
    from ea.classifier import classify_confirmation_reply, classify_external_reply
    from ea.config import load_config
    from ea.gmail import LiveGmailClient
    from ea.log import get_logger
    from ea.poll import run_poll
    from ea.state import StateStore

    if config is None:
        config = load_config()

    log = get_logger("ea.runner")

    from ea import network
    timeout_sec = config.get("schedule", {}).get("timeout_seconds", 30)
    network.configure(attempts=1, api_timeout=float(timeout_sec))

    lock_f = _acquire_lock(LOCK_FILE)
    if lock_f is None:
        log.warning(f"Poll skipped — another poll cycle is already running ({LOCK_FILE} held)")
        return {"pass1": [], "pass2": [], "pass3": [], "expired": []}

    try:
        creds    = load_creds(credentials_file=credentials_file, token_file=token_file)
        gmail    = LiveGmailClient(creds)
        footer   = config.get("user", {}).get("email_footer", "")
        if footer:
            from ea.gmail import FooterGmailClient
            gmail = FooterGmailClient(gmail, footer)
        calendar = CalendarClient(creds=creds)
        state    = StateStore()

        log.info("Poll cycle started")
        summary = run_poll(
            gmail,
            calendar,
            state,
            config,
            confirm_eval_fn=lambda text, entry: classify_confirmation_reply(text, entry, config),
            external_reply_fn=lambda text, entry: classify_external_reply(text, entry, config),
            dry_run=dry_run,
        )
        total = sum(len(v) for v in summary.values())
        if total:
            parts = [
                f"{pass_name}:[{','.join(i['action'] for i in items)}]"
                for pass_name, items in summary.items()
                if items
            ]
            log.info("Poll cycle complete — %s", " ".join(parts))
        else:
            log.info("Poll cycle complete — nothing to process")
        return summary
    except Exception as exc:
        from ea.network import is_transient_error
        if is_transient_error(exc):
            log.error("Poll skipped — network unavailable: %s", exc)
        else:
            log.error("Poll failed: %s", exc, exc_info=True)
        raise
    finally:
        _release_lock(lock_f)


def run_loop(
    config: dict = None,
    credentials_file=None,
    token_file=None,
) -> None:
    """
    Poll continuously at the configured interval. Runs until interrupted.
    """
    from ea.config import load_config
    from ea.log import configure, get_logger

    if config is None:
        config = load_config()

    configure()
    log = get_logger("ea.runner")

    interval_sec = config.get("schedule", {}).get("poll_interval_seconds", 300)
    log.info(f"Starting poll loop (every {interval_sec}s). Press Ctrl+C to stop.")

    from ea import network
    timeout_sec = config.get("schedule", {}).get("timeout_seconds", 30)
    network.configure(attempts=3, base_delay=1.0, cap=float(interval_sec), api_timeout=float(timeout_sec))

    while True:
        try:
            summary = run_once(
                config=config,
                credentials_file=credentials_file,
                token_file=token_file,
            )
            total = sum(len(v) for v in summary.values())
            if total:
                _log_summary(summary)
        except KeyboardInterrupt:
            log.info("Poll loop stopped by user.")
            break
        except Exception as e:
            log.error(f"Unhandled poll error: {e}", exc_info=True)
        _time.sleep(interval_sec)


def _log_summary(summary: dict) -> None:
    for pass_name, items in summary.items():
        for item in items:
            print(_format_item(pass_name, item))


def _format_item(pass_name: str, item: dict) -> str:
    ts      = item.get("timestamp", "??:??:??")
    tid     = item["thread_id"][:12]
    action  = item["action"]
    topic   = item.get("topic") or ""
    intent  = item.get("intent") or item.get("state_type") or ""

    context = f"  {intent}" if intent else ""
    label   = f"  \"{topic}\"" if topic else ""
    return f"{ts}  [{pass_name}]{context}{label}  →  {action}  ({tid})"
