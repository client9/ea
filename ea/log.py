"""
log.py

Structured logging for the EA poll loop.

  ea.log  — JSON-lines, one object per record (machine-readable, for auditing)
  stdout  — human-readable summary lines (INFO and above only)

Usage:
    from ea.log import configure, get_logger
    configure()                    # call once at startup
    log = get_logger("ea.poll")
    log.info("Poll started")
    log.error("Thread failed", extra={"thread_id": tid})
"""

import json
import logging
import sys
from datetime import datetime, timezone


class _JsonlHandler(logging.FileHandler):
    """Writes one JSON object per log record."""

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        for key in ("thread_id", "action", "intent", "topic"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        try:
            self.stream.write(json.dumps(entry) + "\n")
            self.stream.flush()
        except Exception:
            self.handleError(record)


def configure(log_file: str = "ea.log", quiet: bool = False) -> None:
    """
    Set up file (JSON) and stdout (human-readable) handlers. Safe to call multiple times.

    quiet: when True, omit the stdout handler. All output goes to log_file only.
           Use for cron/launchd invocations where stdout triggers notification emails.
    """
    root = logging.getLogger("ea")
    if root.handlers:
        return  # already configured

    root.setLevel(logging.DEBUG)

    fh = _JsonlHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    if not quiet:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(sh)


def get_logger(name: str = "ea") -> logging.Logger:
    return logging.getLogger(name)
