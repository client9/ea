"""
triggers.py

Parses email thread text into individual messages and detects EA: trigger
commands from the user's own email address.
"""

import re
from dataclasses import dataclass


@dataclass
class Message:
    from_addr: str
    to_addr: str
    date: str
    body: str


def parse_thread(thread_text: str) -> list[Message]:
    """Split a raw email thread into individual Message objects."""
    raw_messages = re.split(r'\n-{3,}\n', thread_text)
    messages = []
    for raw in raw_messages:
        raw = raw.strip()
        if not raw:
            continue

        lines = raw.splitlines()
        headers: dict[str, str] = {}
        body_start = 0

        for i, line in enumerate(lines):
            if line.strip() == "":
                body_start = i + 1
                break
            match = re.match(r'^([\w-]+):\s*(.+)$', line)
            if match:
                headers[match.group(1).lower()] = match.group(2).strip()

        body = "\n".join(lines[body_start:]).strip()
        messages.append(Message(
            from_addr=headers.get("from", ""),
            to_addr=headers.get("to", ""),
            date=headers.get("date", ""),
            body=body,
        ))

    return messages


def find_ea_trigger(thread_text: str, my_email: str) -> str | None:
    """
    Scan a parsed email thread for an EA: command sent by my_email.
    Returns the command text following 'EA:' if found, None otherwise.

    TODO: Handle multiple EA: replies in a thread — currently returns the
    first match; should use the latest one once multi-command threads are
    supported.
    """
    messages = parse_thread(thread_text)
    for message in messages:
        if my_email.lower() not in message.from_addr.lower():
            continue
        match = re.search(r'EA:\s*(.+)', message.body, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None
