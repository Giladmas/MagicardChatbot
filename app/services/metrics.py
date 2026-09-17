"""Aggregated operational metrics for the admin dashboard.

Tracks OpenAI token usage, cache hit rate, latency, and request volume - all
derived from data the chat pipeline already produces but previously didn't
count anywhere. Persisted to logs/metrics.json, same single-writer JSON-file
pattern as runtime_config.py, so numbers survive a restart. Not safe across
multiple worker processes, like the rest of this app's in-process state.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METRICS_PATH = Path(__file__).resolve().parents[2] / "logs" / "metrics.json"

_DAILY_HISTORY_DAYS = 30
_DAILY_DISPLAY_DAYS = 14

_EMPTY: dict[str, Any] = {
    "chat_requests": 0,
    "cache_hits": 0,
    "refusals": 0,
    "errors": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "embedding_tokens": 0,
    "total_latency_ms": 0.0,
    "generation_count": 0,
    "daily": {},  # "YYYY-MM-DD" -> {"requests": int, "total_tokens": int}
}

_lock = threading.Lock()


def _empty() -> dict[str, Any]:
    return json.loads(json.dumps(_EMPTY))


def _load() -> dict[str, Any]:
    if not METRICS_PATH.exists():
        return _empty()
    try:
        data = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    merged = {**_empty(), **data}
    merged["daily"] = data.get("daily", {})
    return merged


def _save(data: dict[str, Any]) -> None:
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _bump_day(data: dict[str, Any], tokens: int = 0) -> None:
    day = data["daily"].setdefault(_today(), {"requests": 0, "total_tokens": 0})
    day["requests"] += 1
    day["total_tokens"] += tokens
    if len(data["daily"]) > _DAILY_HISTORY_DAYS:
        for old_day in sorted(data["daily"])[:-_DAILY_HISTORY_DAYS]:
            del data["daily"][old_day]


def record_chat_request() -> None:
    with _lock:
        data = _load()
        data["chat_requests"] += 1
        _save(data)


def record_cache_hit() -> None:
    with _lock:
        data = _load()
        data["cache_hits"] += 1
        _bump_day(data)
        _save(data)


def record_generation(prompt_tokens: int, completion_tokens: int, latency_ms: float, refusal: bool) -> None:
    with _lock:
        data = _load()
        data["generation_count"] += 1
        data["prompt_tokens"] += prompt_tokens
        data["completion_tokens"] += completion_tokens
        data["total_latency_ms"] += latency_ms
        if refusal:
            data["refusals"] += 1
        _bump_day(data, tokens=prompt_tokens + completion_tokens)
        _save(data)


def record_embedding_tokens(tokens: int) -> None:
    if not tokens:
        return
    with _lock:
        data = _load()
        data["embedding_tokens"] += tokens
        _save(data)


def record_error() -> None:
    with _lock:
        data = _load()
        data["errors"] += 1
        _save(data)


def get_summary() -> dict[str, Any]:
    with _lock:
        data = _load()
    total_tokens = data["prompt_tokens"] + data["completion_tokens"] + data["embedding_tokens"]
    avg_latency_ms = (data["total_latency_ms"] / data["generation_count"]) if data["generation_count"] else 0.0
    total_answered = data["generation_count"] + data["cache_hits"]
    cache_hit_rate = (data["cache_hits"] / total_answered * 100) if total_answered else 0.0
    return {
        "chat_requests": data["chat_requests"],
        "cache_hits": data["cache_hits"],
        "cache_hit_rate": cache_hit_rate,
        "refusals": data["refusals"],
        "errors": data["errors"],
        "prompt_tokens": data["prompt_tokens"],
        "completion_tokens": data["completion_tokens"],
        "embedding_tokens": data["embedding_tokens"],
        "total_tokens": total_tokens,
        "avg_latency_ms": avg_latency_ms,
        "daily": dict(sorted(data["daily"].items())[-_DAILY_DISPLAY_DAYS:]),
    }


def reset_metrics() -> None:
    with _lock:
        _save(_empty())
