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
    "missed_questions_rotate_at": 200,
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


def update_config(changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        current = _load()
        for key, value in changes.items():
            if key not in DEFAULTS:
                continue
            current[key] = type(DEFAULTS[key])(value)
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return current
