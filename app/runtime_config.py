"""Live-editable settings, separate from app/config.py's .env-sourced Settings.

These are values an operator may want to tweak without redeploying: word/token
limits, rate-limit window, retrieval breadth. Persisted to a JSON file so
changes survive a restart. Not safe across multiple worker processes (last
writer wins, others keep stale in-memory values until they reread the file)
- fine for a single-worker deployment, worth revisiting if this ever runs
with multiple uvicorn workers.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parents[1] / "runtime_config.json"

DEFAULTS: dict[str, Any] = {
    "max_question_words": 20,
    "max_answer_tokens": 300,
    "rate_limit_seconds": 10,
    "retrieval_top_k": 4,
    "temperature": 0.0,
    "knowledge_gaps_rotate_at": 200,
    "history_enabled": True,
    "history_max_turns": 6,
    "history_ttl_seconds": 1800,
    "cache_enabled": True,
    "cache_similarity_threshold": 0.95,
    "conversation_log_enabled": True,
    "conversations_rotate_at": 1000,
}

# Unit/range hints for the admin page - purely descriptive, not enforced here.
# Every duration in this app is in seconds; called out explicitly so nobody
# types "30" into a *_seconds field meaning minutes.
CONFIG_HELP: dict[str, str] = {
    "max_question_words": "words",
    "max_answer_tokens": "tokens (GPT max_tokens)",
    "rate_limit_seconds": "seconds between requests, per user",
    "retrieval_top_k": "chunks retrieved from Qdrant",
    "temperature": "GPT sampling temperature, 0.0-2.0 (0 = deterministic)",
    "knowledge_gaps_rotate_at": "lines before the knowledge-gaps log rotates",
    "history_enabled": "on/off",
    "history_max_turns": "prior turns replayed to GPT per user",
    "history_ttl_seconds": "seconds of inactivity before a conversation resets",
    "cache_enabled": "on/off",
    "cache_similarity_threshold": "cosine similarity, 0.0-1.0 (higher = stricter match)",
    "conversation_log_enabled": "on/off - saves a viewable record of chats, separate from GPT's own memory",
    "conversations_rotate_at": "lines before the conversation log rotates",
}

_lock = threading.Lock()


def _load() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    return {**DEFAULTS, **data}


def get_config() -> dict[str, Any]:
    with _lock:
        return _load()


_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}


def _coerce(default: Any, value: Any) -> Any:
    # bool must be special-cased before the generic type(default)(value) path:
    # bool("False") is True in Python (any non-empty string is truthy).
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
        raise ValueError(f"{value!r} is not a valid bool")
    return type(default)(value)


def update_config(changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        current = _load()
        errors: list[str] = []
        for key, value in changes.items():
            if key not in DEFAULTS:
                continue
            try:
                current[key] = _coerce(DEFAULTS[key], value)
            except (TypeError, ValueError):
                errors.append(f"{key!r}: {value!r} is not a valid {type(DEFAULTS[key]).__name__}")
        if errors:
            raise ValueError("; ".join(errors))
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return current
