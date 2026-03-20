"""
network.py

Retry utility for transient network and rate-limit errors.

Two categories of transient error are handled differently:

  Timeout errors   — server was reachable but slow. Retried immediately with
                     no backoff delay (waiting doesn't help a slow server).
  Connection errors — network unreachable / rate-limited. Retried with
                     exponential backoff (waiting gives the network time to
                     recover).

call_with_retry() uses module-level config set by configure().
Default: attempts=1 (no retry) — suitable for run-once / cron mode.
Call configure(attempts=3, ...) before starting the poll loop.
"""

import logging
import socket
import time

_attempts    = 1
_base_delay  = 1.0
_cap         = 0.0    # 0 = no cap
_api_timeout = 30.0   # per-call timeout in seconds

_log = logging.getLogger("ea.network")


def configure(
    attempts: int = 3,
    base_delay: float = 1.0,
    cap: float = 0.0,
    api_timeout: float = 30.0,
) -> None:
    """
    Set module-level retry and timeout config.

    attempts:    Total number of attempts (1 = no retry / cron mode).
    base_delay:  Initial backoff in seconds for connection errors; doubles
                 each attempt.
    cap:         Maximum backoff delay in seconds (0 = no cap). Pass
                 poll_interval_seconds so backoff never exceeds the next
                 scheduled cycle.
    api_timeout: Per-call timeout in seconds for Anthropic and Google API
                 calls. Also applied as the OS socket default timeout, which
                 covers httplib2 (Google API client). Read by callers via
                 get_api_timeout(). Default: 30s.
    """
    global _attempts, _base_delay, _cap, _api_timeout
    _attempts    = attempts
    _base_delay  = base_delay
    _cap         = cap
    _api_timeout = api_timeout
    socket.setdefaulttimeout(api_timeout)


def get_api_timeout() -> float:
    """Return the configured per-call API timeout in seconds."""
    return _api_timeout


def is_timeout_error(exc: BaseException) -> bool:
    """Return True if exc is a timeout (server reachable but slow)."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    try:
        import anthropic as _ant
        if isinstance(exc, _ant.APITimeoutError):
            return True
    except ImportError:
        pass
    # httplib2 raises socket.timeout, covered above.
    return False


def is_transient_error(exc: BaseException) -> bool:
    """Return True if exc is any transient error worth retrying (includes timeouts)."""
    if is_timeout_error(exc):
        return True

    try:
        import requests.exceptions as _req
        if isinstance(exc, (_req.ConnectionError, _req.Timeout)):
            return True
    except ImportError:
        pass

    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            status = int(exc.resp.status) if exc.resp else 0
            return status == 429 or status >= 500
    except ImportError:
        pass

    try:
        import anthropic as _ant
        if isinstance(exc, (_ant.APIConnectionError, _ant.RateLimitError)):
            return True
    except ImportError:
        pass

    # Generic OS-level connectivity errors (ENETUNREACH, EHOSTUNREACH, etc.)
    if isinstance(exc, OSError) and exc.errno in (
        101,  # ENETUNREACH
        110,  # ETIMEDOUT
        111,  # ECONNREFUSED
        113,  # EHOSTUNREACH
    ):
        return True

    return False


def call_with_retry(fn, *args, **kwargs):
    """
    Call fn(*args, **kwargs), retrying on transient errors.

    Timeout errors are retried immediately (no backoff delay) — waiting
    doesn't help a slow server. Connection errors use exponential backoff.

    Uses the module-level config from configure().
    Non-transient errors are raised immediately without retry.
    Raises the last exception when all attempts are exhausted.
    """
    last_exc: Exception | None = None
    delay = _base_delay

    for attempt in range(_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not is_transient_error(exc):
                raise
            last_exc = exc
            if attempt + 1 < _attempts:
                if is_timeout_error(exc):
                    _log.warning(
                        "Timeout (attempt %d/%d), retrying immediately: %s",
                        attempt + 1, _attempts, exc,
                    )
                    # No sleep — backoff doesn't help a slow server.
                else:
                    wait = delay if _cap <= 0 else min(delay, _cap)
                    _log.warning(
                        "Connection error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, _attempts, wait, exc,
                    )
                    time.sleep(wait)
                    delay *= 2

    raise last_exc
