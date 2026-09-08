"""Simple per-user rate limiting: one request per `rate_limit_seconds`.

In-memory, per-process - resets on restart and isn't shared across multiple
worker processes. Fine for a single-worker deployment; swap for Redis if this
ever runs with multiple workers.
"""

from __future__ import annotations

import threading
import time

_last_request: dict[str, float] = {}
_lock = threading.Lock()


def check_rate_limit(user_id: str, window_seconds: float) -> float | None:
    """Returns None if the request is allowed, otherwise seconds to wait."""
    now = time.monotonic()
    with _lock:
        last = _last_request.get(user_id)
        if last is not None:
            elapsed = now - last
            if elapsed < window_seconds:
                return round(window_seconds - elapsed, 1)
        _last_request[user_id] = now
    return None
