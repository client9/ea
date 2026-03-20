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
import json
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

    if intent == "block_time":
        print(f"  🗓️  BLOCK:       {result.get('topic', 'N/A')}")
        print(f"  ⏱️  DURATION:    {result.get('duration_minutes', 'Not specified')} min")
        print(f"  🌍 TIMEZONE:    {result.get('timezone', 'Not specified')}")
        _print_times(result.get("proposed_times", []), label="TIME")
        print("=" * width + "\n")
        return

    # meeting_request
    print(f"  📅 TOPIC:       {result.get('topic', 'N/A')}")
    print(f"  ⏱️  DURATION:    {result.get('duration_minutes', 'Not specified')} min")
    print(f"  🌍 TIMEZONE:    {result.get('timezone', 'Not specified')}")
    print(f"  📍 LOCATION:    {result.get('location', 'Not specified')}")
    print(f"  🚨 URGENCY:     {result.get('urgency', 'N/A')}")

    attendees = result.get("attendees", [])
    if attendees:
        print(f"\n  👥 ATTENDEES:")
        for a in attendees:
            print(f"     • {a}")

    _print_times(result.get("proposed_times", []), label="PROPOSED TIMES")

    ambiguities = result.get("ambiguities", [])
    if ambiguities:
        print(f"\n  ⚠️  AMBIGUITIES:")
        for a in ambiguities:
            print(f"     • {a}")

    print("=" * width + "\n")


def run_text(text: str, label: str = ""):
    result = parse_meeting_request(text)
    print_result(text, result, source_label=label or text[:60])


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
    COL = {"thread": 16, "type": 24, "topic": 22, "attendees": 28, "expires": 22}
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
        topic = (entry.get("topic") or sr.get("topic") or "")[:COL["topic"] - 2]

        # attendees
        attendees = entry.get("attendees") or sr.get("attendees") or []
        attendees_str = ", ".join(attendees)[:COL["attendees"] - 2]

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
                    expires_str = f"{total_sec // 86400}d {(total_sec % 86400) // 3600}h"
            except ValueError:
                expires_str = expires_raw[:19]
        else:
            expires_str = ""

        print(
            f"{thread_id[:COL['thread']-1]:<{COL['thread']}}"
            f"{entry_type:<{COL['type']}}"
            f"{topic:<{COL['topic']}}"
            f"{attendees_str:<{COL['attendees']}}"
            f"{expires_str}"
        )

    print(f"\n{len(entries)} pending entry/entries.")


def main():
    parser = argparse.ArgumentParser(
        description="EA Assistant — scheduling via email"
    )
    subparsers = parser.add_subparsers(dest="command")

    def _add_auth_args(p):
        p.add_argument("--credentials", metavar="FILE",
                       help="Path to credentials.json (overrides config.toml [auth])")
        p.add_argument("--token", metavar="FILE",
                       help="Path to token.json (overrides config.toml [auth])")

    # --- auth subcommand ---
    auth_parser = subparsers.add_parser("auth", help="Manage Google OAuth credentials")
    auth_parser.add_argument("--check", action="store_true", help="Show current auth status")
    _add_auth_args(auth_parser)

    # --- poll subcommand ---
    poll_parser = subparsers.add_parser("poll", help="Run one poll cycle")
    poll_parser.add_argument("--dry-run", action="store_true",
                             help="Show what would happen without sending anything")
    poll_parser.add_argument("--quiet", action="store_true",
                             help="Suppress all stdout; errors go to ea.log only. "
                                  "Use with launchd/cron to avoid notification emails.")
    _add_auth_args(poll_parser)

    # --- run subcommand ---
    run_parser = subparsers.add_parser("run", help="Run the poll loop continuously")
    _add_auth_args(run_parser)

    # --- status subcommand ---
    subparsers.add_parser("status", help="Show all pending state entries")

    # --- reset subcommand ---
    subparsers.add_parser("reset", help="Clear state.json to start fresh")

    # --- Legacy positional / file commands (parser debugging) ---
    subparsers.add_parser("testdata", help="Parse all files in testdata/")
    file_p = subparsers.add_parser("parse", help="Parse a meeting request from text or file")
    file_p.add_argument("text", nargs="?", help="Meeting request text")
    file_p.add_argument("--file", "-f", help="Path to a text file")

    args = parser.parse_args()

    # auth / poll / run don't need ANTHROPIC_API_KEY at startup —
    # but parse commands do.
    if args.command in (None, "parse", "testdata"):
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
        parser.print_help()


if __name__ == "__main__":
    main()
