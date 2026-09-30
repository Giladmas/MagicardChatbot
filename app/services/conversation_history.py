"""Server-side multi-turn conversation memory, keyed by (X-Site, X-User-Id).

In-memory, per-process - resets on restart and isn't shared across multiple
worker processes (same caveat as rate_limit.py). Fine for a single-worker
deployment; swap for Redis if this ever runs with multiple workers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# (site, site-local user id) - user ids collide across sites, so the pair is the key.
UserKey = tuple[str, str]


@dataclass
class Turn:
    question: str
    answer: str


@dataclass
class _Entry:
    turns: list[Turn] = field(default_factory=list)
    last_active: float = 0.0


_history: dict[UserKey, _Entry] = {}
_lock = threading.Lock()


def get_history(user_key: UserKey, ttl_seconds: float) -> list[Turn]:
    """Returns prior turns for this user, or [] if none/expired.

    Expiry is lazy: checked here on read. Does not update last_active or
    evict on a fresh entry, so a mid-conversation read doesn't reset the
    idle timer - only append_turn does that.
    """
    now = time.monotonic()
    with _lock:
        entry = _history.get(user_key)
        if entry is None:
            return []
        if now - entry.last_active >= ttl_seconds:
            del _history[user_key]
            return []
        return list(entry.turns)


def append_turn(user_key: UserKey, question: str, answer: str, ttl_seconds: float, max_turns: int) -> None:
    """Records a completed turn, resetting the inactivity clock.

    If the existing entry has already expired, starts a fresh history -
    a returning user after a gap starts clean rather than dragging in
    stale context.
    """
    now = time.monotonic()
    with _lock:
        entry = _history.get(user_key)
        if entry is None or now - entry.last_active >= ttl_seconds:
            entry = _Entry()
            _history[user_key] = entry
        entry.turns.append(Turn(question=question, answer=answer))
        if len(entry.turns) > max_turns:
            entry.turns = entry.turns[-max_turns:]
        entry.last_active = now


def clear_all_history() -> None:
    """Wipes every user's conversation history. Used by the admin "Clear history" action."""
    with _lock:
        _history.clear()


def clear_user_history(user_key: UserKey) -> None:
    """Wipes one user's conversation history. Used by the end-user "Clear conversation" action."""
    with _lock:
        _history.pop(user_key, None)
