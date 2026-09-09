"""Simple per-user rate limiting: one request per `rate_limit_seconds`.

In-memory, per-process - resets on restart and isn't shared across multiple
worker processes. Fine for a single-worker deployment; swap for Redis if this
ever runs with multiple workers.
"""

from __future__ import annotations

import math
import threading
import time

_last_request: dict[str, float] = {}
_lock = threading.Lock()


def check_rate_limit(user_id: str, window_seconds: float) -> int | None:
    """Returns None if the request is allowed, otherwise whole seconds to wait.

    Rounded up (not down) so callers never retry before the window has
    actually elapsed.
    """
    now = time.monotonic()
    with _lock:
        last = _last_request.get(user_id)
        if last is not None:
            elapsed = now - last
            if elapsed < window_seconds:
                return math.ceil(window_seconds - elapsed)
        _last_request[user_id] = now
    return None
