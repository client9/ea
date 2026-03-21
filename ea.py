#!/usr/bin/env python3
"""
ea.py — Executive Assistant CLI tool

Usage:
  python ea.py "Can we meet Thursday at 2pm?"
  python ea.py --file thread.txt
  python ea.py --interactive
  python ea.py --testdata          # runs all files in testdata/
"""

import sys
import os
import argparse
from pathlib import Path

from ea.parser.meeting_parser import parse_meeting_request
from ea.triggers import find_ea_trigger
from ea.config import get_my_email


def _print_times(proposed_times: list, label: str = "PROPOSED TIMES"):
    if not proposed_times:
        return
    print(f"\n  🕐 {label}:")
    for entry in proposed_times:
        print(f"     • {entry.get('text', '?')}")
        for iso in entry.get("datetimes", []):
            print(f"       {iso}")


def print_result(text: str, result: dict, source_label: str = ""):
    """Pretty print the parsed meeting result."""
    width = 60
    print("\n" + "=" * width)
    if source_label:
        print(f"  SOURCE: {source_label}")
    print("=" * width)

    if "error" in result:
        print(f"  ❌ ERROR: {result['error']}")
        if "raw_response" in result:
            print(f"\n  Raw response:\n{result['raw_response']}")
        return

    intent = result.get("intent")

    if intent == "none" or not intent:
        print("  ℹ️  This does not appear to be a meeting request or calendar command.")
        print("=" * width + "\n")
        return

    print(f"  🎯 INTENT:      {intent}")

    if intent == "block_time":
        print(f"  🗓️  BLOCK:       {result.get('topic', 'N/A')}")
        print(
            f"  ⏱️  DURATION:    {result.get('duration_minutes', 'Not specified')} min"
        )
        print(f"  🌍 TIMEZONE:    {result.get('timezone', 'Not specified')}")
        _print_times(result.get("proposed_times", []), label="TIME")
        print("=" * width + "\n")
        return

    # meeting_request / suggest_times / etc.
    print(f"  📅 TOPIC:       {result.get('topic', 'N/A')}")
    print(f"  ⏱️  DURATION:    {result.get('duration_minutes', 'Not specified')} min")
    print(f"  🌍 TIMEZONE:    {result.get('timezone', 'Not specified')}")
    print(f"  📍 LOCATION:    {result.get('location', 'Not specified')}")
    print(f"  🚨 URGENCY:     {result.get('urgency', 'N/A')}")

    attendees = result.get("attendees", [])
    if attendees:
        print("\n  👥 ATTENDEES:")
        for a in attendees:
            print(f"     • {a}")

    _print_times(result.get("proposed_times", []), label="PROPOSED TIMES")

    ambiguities = result.get("ambiguities", [])
    if ambiguities:
        print("\n  ⚠️  AMBIGUITIES:")
        for a in ambiguities:
            print(f"     • {a}")

    print("=" * width + "\n")


def _print_suggest_preview(result: dict, config: dict | None = None):
    """Show what the suggest_times email would look like using the live calendar."""
    from ea.auth import load_creds
    from ea.calendar import CalendarClient
    from ea.scheduler import find_slots
    from ea.responder import _format_slot_suggestions
    from datetime import datetime
    from zoneinfo import ZoneInfo

    width = 60
    print("\n" + "-" * width)
    print("  EMAIL PREVIEW (based on your calendar)")
    print("-" * width)

    try:
        if config is None:
            from ea.config import load_config

            config = load_config()
        creds = load_creds()
        calendar = CalendarClient(creds=creds)
    except Exception as e:
        print(f"  (skipped — could not load calendar: {e})")
        print("-" * width + "\n")
        return

    schedule = config.get("schedule", {})
    tz_name = schedule.get("timezone", "UTC")
    duration_minutes = result.get("duration_minutes") or 30
    attendees_parsed = result.get("attendees") or []
    my_email = config["user"]["email"]
    all_attendees = [my_email] + [a for a in attendees_parsed if a != my_email]

    restrict_to_date = None
    proposed_times = result.get("proposed_times") or []
    if proposed_times:
        raw_dt = (proposed_times[0].get("datetimes") or [None])[0]
        if raw_dt:
            restrict_to_date = (
                datetime.fromisoformat(raw_dt).astimezone(ZoneInfo(tz_name)).date()
            )

    try:
        slots = find_slots(
            attendees=all_attendees,
            duration_minutes=duration_minutes,
            working_hours=schedule.get("working_hours", {}),
            preferred_hours=schedule.get("preferred_hours", {}),
            tz_name=tz_name,
            calendar=calendar,
            restrict_to_date=restrict_to_date,
        )
    except Exception as e:
        print(f"  (skipped — calendar lookup failed: {e})")
        print("-" * width + "\n")
        return

    if not slots:
        print("  No available slots found in the next 7 days.")
        print("-" * width + "\n")
        return

    body = _format_slot_suggestions(
        slots, owner_tz=tz_name, attendee_tz=result.get("timezone")
    )
    print(body)
    print("-" * width + "\n")


def run_text(text: str, label: str = ""):
    try:
        from ea.config import load_config

        config = load_config()
        tz_name = config.get("schedule", {}).get("timezone", "UTC")
    except Exception:
        config = None
        tz_name = "UTC"
    result = parse_meeting_request(text, tz_name=tz_name)
    print_result(text, result, source_label=label or text[:60])
    if result.get("intent") == "suggest_times":
        _print_suggest_preview(result, config=config)


def run_file(filepath: str):
    path = Path(filepath)
    if not path.exists():
        print(f"❌ File not found: {filepath}")
        sys.exit(1)
    text = path.read_text()
    trigger = find_ea_trigger(text, get_my_email())
    if trigger is None:
        width = 60
        print("\n" + "=" * width)
        print(f"  SOURCE: {path.name}")
        print("=" * width)
        print("  ⏭️  No EA: trigger found — skipping.")
        print("=" * width + "\n")
        return
    run_text(text, label=path.name)


def run_interactive():
    print("\n🤖 EA Assistant — Interactive Mode")
    print("Paste your meeting request text. Enter a blank line when done.")
    print("Type 'quit' to exit.\n")

    while True:
        lines = []
        print("📝 Input (blank line to submit):")
        while True:
            line = input()
            if line.lower() == "quit":
                print("Goodbye!")
                sys.exit(0)
            if line == "":
                break
            lines.append(line)

        if lines:
            text = "\n".join(lines)
            run_text(text, label="interactive input")
        else:
            print("(No input received, try again)\n")


def run_testdata():
    testdata_dir = Path(__file__).parent / "testdata"
    files = sorted(testdata_dir.glob("*.txt"))
    if not files:
        print(f"No .txt files found in {testdata_dir}")
        return
    print(f"\n🧪 Running {len(files)} test file(s) from testdata/\n")
    for f in files:
        run_file(str(f))


def _run_status():
    from datetime import datetime, timezone
    from ea.state import StateStore

    state = StateStore()
    entries = state.all()

    if not entries:
        print("No pending state entries.")
        return

    now = datetime.now(tz=timezone.utc)

    # Column widths
    COL = {"thread": 17, "type": 24, "topic": 22, "attendees": 28, "expires": 22}
    header = (
        f"{'THREAD':<{COL['thread']}}"
        f"{'TYPE':<{COL['type']}}"
        f"{'TOPIC':<{COL['topic']}}"
        f"{'ATTENDEES':<{COL['attendees']}}"
        f"EXPIRES"
    )
    print(header)
    print("-" * (sum(COL.values()) + 10))

    for thread_id, entry in entries.items():
        entry_type = entry.get("type", "unknown")

        # topic lives in different places depending on entry type
        sr = entry.get("schedule_result") or {}
        topic = (entry.get("topic") or sr.get("topic") or "")[: COL["topic"] - 2]

        # attendees
        attendees = entry.get("attendees") or sr.get("attendees") or []
        attendees_str = ", ".join(attendees)[: COL["attendees"] - 2]

        # expiry — show relative time
        expires_raw = entry.get("expires_at", "")
        if expires_raw:
            try:
                expires_dt = datetime.fromisoformat(expires_raw)
                delta = expires_dt - now
                total_sec = int(delta.total_seconds())
                if total_sec < 0:
                    expires_str = "EXPIRED"
                elif total_sec < 3600:
                    expires_str = f"{total_sec // 60}m"
                elif total_sec < 86400:
                    expires_str = f"{total_sec // 3600}h {(total_sec % 3600) // 60}m"
                else:
                    expires_str = (
                        f"{total_sec // 86400}d {(total_sec % 86400) // 3600}h"
                    )
            except ValueError:
                expires_str = expires_raw[:19]
        else:
            expires_str = ""

        print(
            f"{thread_id[: COL['thread'] - 1]:<{COL['thread']}}"
            f"{entry_type:<{COL['type']}}"
            f"{topic:<{COL['topic']}}"
            f"{attendees_str:<{COL['attendees']}}"
            f"{expires_str}"
        )

    print(f"\n{len(entries)} pending entry/entries.")


def _run_dismiss(thread_id: str, credentials_file=None, token_file=None):
    from ea.state import StateStore
    from ea.runner import _acquire_lock, _release_lock, LOCK_FILE

    lock_f = _acquire_lock(LOCK_FILE)
    if lock_f is None:
        print("A poll cycle is currently running. Wait a moment and try again.")
        return

    try:
        state = StateStore()
        entry = state.get(thread_id)

        if entry is None:
            print(f"No pending entry found for thread {thread_id}.")
            print("(Use 'ea status' to see pending entries.)")
            return

        sr = entry.get("schedule_result") or {}
        topic = entry.get("topic") or sr.get("topic") or "(no topic)"
        entry_type = entry.get("type", "unknown")
        attendees = entry.get("attendees") or sr.get("attendees") or []

        state.delete(thread_id)
    finally:
        _release_lock(lock_f)

    print(f"Dismissed: {topic} ({entry_type})")

    try:
        from ea.auth import load_creds
        from ea.gmail import LiveGmailClient

        creds = load_creds(credentials_file=credentials_file, token_file=token_file)
        gmail = LiveGmailClient(creds)
        gmail.apply_label(thread_id, "ea-cancelled")
        print(f"Applied 'ea-cancelled' label to thread {thread_id}.")
    except Exception as e:
        print(f"Note: could not apply Gmail label ({e}). State was cleared.")

    if attendees and entry_type == "pending_external_reply":
        print(f"Note: no cancellation was sent to {', '.join(attendees)}.")


def print_help():
    """Print nicely formatted help."""
    lines = [
        "",
        "  EA Assistant — AI-powered scheduling via email",
        "",
        "  DAILY USE",
        "    poll                  Run one poll cycle",
        "    poll --dry-run        Show what would happen without sending anything",
        "    poll --quiet          Suppress stdout (for launchd/cron)",
        "    run                   Loop continuously (interval from config.toml)",
        "    status                Show pending entries (topic, attendees, expiry)",
        "    dismiss <id>          Dismiss a pending entry by thread ID",
        "    digest                Print today's calendar digest",
        '    digest <date>         Digest for a specific date (e.g. "tomorrow")',
        "",
        "  SETUP",
        "    auth                  Run the Google OAuth2 browser flow",
        "    auth --check          Show current auth status",
        "",
        "  DEBUGGING",
        '    parse "<text>"        Parse a meeting request from text',
        "    parse --file FILE      Parse from a file",
        "    testdata              Parse all .txt files in testdata/",
        "    reset                 Clear state.json to start fresh",
        "",
        "  SHARED OPTIONS  (auth, poll, run, dismiss, digest)",
        "    --credentials FILE    Path to credentials JSON (overrides config.toml)",
        "    --token FILE          Path to token.json (overrides config.toml)",
        "",
    ]
    print("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(
        description="EA Assistant — scheduling via email",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="store_true", help="Show this help message"
    )
    subparsers = parser.add_subparsers(dest="command")

    def _add_auth_args(p):
        p.add_argument(
            "--credentials",
            metavar="FILE",
            help="Path to credentials.json (overrides config.toml [auth])",
        )
        p.add_argument(
            "--token",
            metavar="FILE",
            help="Path to token.json (overrides config.toml [auth])",
        )

    # --- auth subcommand ---
    auth_parser = subparsers.add_parser("auth", help="Manage Google OAuth credentials")
    auth_parser.add_argument(
        "--check", action="store_true", help="Show current auth status"
    )
    _add_auth_args(auth_parser)

    # --- poll subcommand ---
    poll_parser = subparsers.add_parser("poll", help="Run one poll cycle")
    poll_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without sending anything",
    )
    poll_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all stdout; errors go to ea.log only. "
        "Use with launchd/cron to avoid notification emails.",
    )
    _add_auth_args(poll_parser)

    # --- run subcommand ---
    run_parser = subparsers.add_parser("run", help="Run the poll loop continuously")
    _add_auth_args(run_parser)

    # --- status subcommand ---
    subparsers.add_parser("status", help="Show all pending state entries")

    # --- dismiss subcommand ---
    dismiss_parser = subparsers.add_parser(
        "dismiss", help="Dismiss a pending state entry by thread ID"
    )
    dismiss_parser.add_argument(
        "thread_id", help="Thread ID to dismiss (from 'ea status')"
    )
    _add_auth_args(dismiss_parser)

    # --- digest subcommand ---
    digest_parser = subparsers.add_parser(
        "digest", help="Print a calendar digest to stdout (default: today)"
    )
    digest_parser.add_argument(
        "date",
        nargs="?",
        metavar="DATE",
        help='Date to generate digest for, e.g. "tomorrow", "next monday", "2026-04-01"',
    )
    _add_auth_args(digest_parser)

    # --- reset subcommand ---
    subparsers.add_parser("reset", help="Clear state.json to start fresh")

    # --- help subcommand ---
    subparsers.add_parser("help", help="Show this help message")

    # --- Legacy positional / file commands (parser debugging) ---
    subparsers.add_parser("testdata", help="Parse all files in testdata/")
    file_p = subparsers.add_parser(
        "parse", help="Parse a meeting request from text or file"
    )
    file_p.add_argument("text", nargs="?", help="Meeting request text")
    file_p.add_argument("--file", "-f", help="Path to a text file")

    args = parser.parse_args()

    if args.help or args.command in (None, "help"):
        print_help()
        sys.exit(0)

    # auth / poll / run don't need ANTHROPIC_API_KEY at startup —
    # but parse commands do.
    if args.command in ("parse", "testdata"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY environment variable is not set.")
            sys.exit(1)

    if args.command == "auth":
        from ea.auth import check_auth, run_auth_flow

        if args.check:
            check_auth(token_file=args.token)
        else:
            run_auth_flow(credentials_file=args.credentials, token_file=args.token)

    elif args.command == "poll":
        from ea.log import configure
        from ea.runner import run_once, _format_item

        quiet = args.quiet
        configure(quiet=quiet)
        try:
            summary = run_once(
                dry_run=args.dry_run,
                credentials_file=args.credentials,
                token_file=args.token,
            )
        except Exception as exc:
            if not quiet:
                print(f"Poll failed: {exc}", file=sys.stderr)
            sys.exit(1)

        if not quiet:
            total = sum(len(v) for v in summary.values())
            if total:
                for pass_name, items in summary.items():
                    for item in items:
                        print(_format_item(pass_name, item))
            else:
                print("Poll complete — nothing to process.")
        sys.exit(0)

    elif args.command == "run":
        from ea.runner import run_loop

        run_loop(credentials_file=args.credentials, token_file=args.token)

    elif args.command == "status":
        _run_status()

    elif args.command == "dismiss":
        _run_dismiss(
            args.thread_id,
            credentials_file=getattr(args, "credentials", None),
            token_file=getattr(args, "token", None),
        )

    elif args.command == "digest":
        from ea.auth import load_creds
        from ea.calendar import CalendarClient
        from ea.config import load_config
        from ea.digest import build_digest
        from ea.state import StateStore

        config = load_config()

        for_date = None
        if args.date:
            import datetime as _dt

            from ea.parser.date_normalizer import make_normalizer

            tz_name = config.get("schedule", {}).get("timezone", "UTC")
            normalizer = make_normalizer(config)
            now = _dt.datetime.now(_dt.timezone.utc)
            for_date = normalizer.parse_date(args.date, tz_name, now)
            if for_date is None:
                print(f"Could not parse date: {args.date!r}", file=sys.stderr)
                sys.exit(1)

        creds = load_creds(
            credentials_file=getattr(args, "credentials", None),
            token_file=getattr(args, "token", None),
        )
        _, body = build_digest(
            config, CalendarClient(creds=creds), StateStore(), for_date=for_date
        )
        print(body)

    elif args.command == "reset":
        from ea.state import DEFAULT_STATE_FILE

        path = Path(DEFAULT_STATE_FILE)
        if path.exists():
            path.unlink()
            print(f"Cleared {path}")
        else:
            print(f"Nothing to clear ({path} does not exist).")

    elif args.command == "testdata":
        run_testdata()

    elif args.command == "parse":
        if args.file:
            run_file(args.file)
        elif args.text:
            run_text(args.text)
        else:
            file_p.print_help()

    else:
        print_help()


if __name__ == "__main__":
    main()
