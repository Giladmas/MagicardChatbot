"""Server-side multi-turn conversation memory, keyed by X-User-Id.

In-memory, per-process - resets on restart and isn't shared across multiple
worker processes (same caveat as rate_limit.py). Fine for a single-worker
deployment; swap for Redis if this ever runs with multiple workers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Turn:
    question: str
    answer: str


@dataclass
class _Entry:
    turns: list[Turn] = field(default_factory=list)
    last_active: float = 0.0


_history: dict[str, _Entry] = {}
_lock = threading.Lock()


def get_history(user_id: str, ttl_seconds: float) -> list[Turn]:
    """Returns prior turns for this user, or [] if none/expired.

    Expiry is lazy: checked here on read. Does not update last_active or
    evict on a fresh entry, so a mid-conversation read doesn't reset the
    idle timer - only append_turn does that.
    """
    now = time.monotonic()
    with _lock:
        entry = _history.get(user_id)
        if entry is None:
            return []
        if now - entry.last_active >= ttl_seconds:
            del _history[user_id]
            return []
        return list(entry.turns)


def append_turn(user_id: str, question: str, answer: str, ttl_seconds: float, max_turns: int) -> None:
    """Records a completed turn, resetting the inactivity clock.

    If the existing entry has already expired, starts a fresh history -
    a returning user after a gap starts clean rather than dragging in
    stale context.
    """
    now = time.monotonic()
    with _lock:
        entry = _history.get(user_id)
        if entry is None or now - entry.last_active >= ttl_seconds:
            entry = _Entry()
            _history[user_id] = entry
        entry.turns.append(Turn(question=question, answer=answer))
        if len(entry.turns) > max_turns:
            entry.turns = entry.turns[-max_turns:]
        entry.last_active = now


def clear_all_history() -> None:
    """Wipes every user's conversation history. Used by the admin "Clear history" action."""
    with _lock:
        _history.clear()
