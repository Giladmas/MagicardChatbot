"""Per-user rate limiting for the "clear conversation" action: at most
`limit` clears within a rolling `window_seconds` window (e.g. 3 per 10 min).

Sliding window, tracked as a per-user list of clear timestamps - old
timestamps outside the window are dropped on every check. In-memory,
per-process - same single-worker caveat as rate_limit.py and
conversation_history.py.
"""

from __future__ import annotations

import math
import threading
import time

_clears: dict[str, list[float]] = {}
_lock = threading.Lock()


class ClearLimitResult:
    def __init__(self, allowed: bool, remaining: int, limit: int, window_seconds: float, retry_after: int | None):
        self.allowed = allowed
        self.remaining = remaining
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after = retry_after


def check_and_record_clear(user_id: str, limit: int, window_seconds: float) -> ClearLimitResult:
    """Checks whether `user_id` may clear their conversation now, and if so,
    records the attempt immediately (so a burst of concurrent requests can't
    all slip through before any of them is recorded).

    Returns a ClearLimitResult describing the outcome and, either way, how
    many clears remain in the current window and when the oldest one in the
    window will fall out of it (only meaningful when `allowed` is False).
    """
    now = time.monotonic()
    with _lock:
        timestamps = [t for t in _clears.get(user_id, []) if now - t < window_seconds]
        if len(timestamps) >= limit:
            oldest = min(timestamps)
            retry_after = math.ceil(window_seconds - (now - oldest))
            _clears[user_id] = timestamps
            return ClearLimitResult(False, 0, limit, window_seconds, retry_after)
        timestamps.append(now)
        _clears[user_id] = timestamps
        return ClearLimitResult(True, limit - len(timestamps), limit, window_seconds, None)


def clear_all() -> None:
    """Wipes every user's clear-attempt tracking. Used by the admin "Clear all conversation history" action."""
    with _lock:
        _clears.clear()
