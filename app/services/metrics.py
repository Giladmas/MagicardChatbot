"""Aggregated operational metrics for the admin dashboard.

Tracks OpenAI token usage (and what it costs), cache hit rate, latency, and
request volume - all derived from data the chat pipeline already produces.
Persisted to logs/metrics.json, same single-writer JSON-file pattern as
runtime_config.py, so numbers survive a restart. Not safe across multiple
worker processes, like the rest of this app's in-process state.

Dollar figures are computed on read from token counts and the per-1M-token
prices in the live config, so correcting a price there re-prices all history.
"""

from __future__ import annotations

import calendar
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.runtime_config import get_config

METRICS_PATH = Path(__file__).resolve().parents[2] / "logs" / "metrics.json"

# Kept long enough to cover a full calendar month for the monthly spend figure.
_DAILY_HISTORY_DAYS = 62
_DAILY_DISPLAY_DAYS = 14
_LATENCY_SAMPLES = 500

# (context window, max output tokens) per chat model, from OpenAI's model docs.
MODEL_LIMITS: dict[str, tuple[int, int]] = {
    "gpt-4o-mini": (128_000, 16_384),
    "gpt-4o": (128_000, 16_384),
    "gpt-4.1": (1_047_576, 32_768),
    "gpt-4.1-mini": (1_047_576, 32_768),
    "gpt-4.1-nano": (1_047_576, 32_768),
    "gpt-3.5-turbo": (16_385, 4_096),
}

_EMPTY: dict[str, Any] = {
    "since": None,  # ISO timestamp of the first record since the last reset
    "chat_requests": 0,
    "cache_hits": 0,
    "refusals": 0,
    "small_talk": 0,
    "errors": 0,
    "rate_limited": 0,
    "rejected_too_long": 0,
    "truncated_answers": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "embedding_tokens": 0,  # live + ingest
    "ingest_embedding_tokens": 0,
    "max_prompt_tokens": 0,
    "max_completion_tokens": 0,
    "total_latency_ms": 0.0,
    "max_latency_ms": 0.0,
    "recent_latencies_ms": [],
    "generation_count": 0,
    "daily": {},  # "YYYY-MM-DD" -> see _DAY_FIELDS
}
_DAY_FIELDS = (
    "requests", "total_tokens", "prompt_tokens", "completion_tokens",
    "embedding_tokens", "cache_hits", "refusals", "errors",
)

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day(data: dict[str, Any]) -> dict[str, int]:
    if not data["since"]:
        data["since"] = _now().isoformat(timespec="seconds")
    day = data["daily"].setdefault(_now().strftime("%Y-%m-%d"), {})
    for field in _DAY_FIELDS:
        day.setdefault(field, 0)
    if len(data["daily"]) > _DAILY_HISTORY_DAYS:
        for old_day in sorted(data["daily"])[:-_DAILY_HISTORY_DAYS]:
            del data["daily"][old_day]
    return day


def record_chat_request() -> None:
    with _lock:
        data = _load()
        data["chat_requests"] += 1
        _day(data)
        _save(data)


def record_cache_hit() -> None:
    with _lock:
        data = _load()
        data["cache_hits"] += 1
        day = _day(data)
        day["requests"] += 1
        day["cache_hits"] += 1
        _save(data)


def record_generation(
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    refusal: bool,
    small_talk: bool = False,
    truncated: bool = False,
) -> None:
    with _lock:
        data = _load()
        data["generation_count"] += 1
        data["prompt_tokens"] += prompt_tokens
        data["completion_tokens"] += completion_tokens
        data["max_prompt_tokens"] = max(data["max_prompt_tokens"], prompt_tokens)
        data["max_completion_tokens"] = max(data["max_completion_tokens"], completion_tokens)
        data["total_latency_ms"] += latency_ms
        data["max_latency_ms"] = max(data["max_latency_ms"], latency_ms)
        data["recent_latencies_ms"] = (data["recent_latencies_ms"] + [round(latency_ms, 1)])[-_LATENCY_SAMPLES:]
        data["refusals"] += refusal
        data["small_talk"] += small_talk
        data["truncated_answers"] += truncated
        day = _day(data)
        day["requests"] += 1
        day["prompt_tokens"] += prompt_tokens
        day["completion_tokens"] += completion_tokens
        day["total_tokens"] += prompt_tokens + completion_tokens
        day["refusals"] += refusal
        _save(data)


def record_embedding_tokens(tokens: int, ingest: bool = False) -> None:
    if not tokens:
        return
    with _lock:
        data = _load()
        data["embedding_tokens"] += tokens
        if ingest:
            data["ingest_embedding_tokens"] += tokens
        day = _day(data)
        day["embedding_tokens"] += tokens
        day["total_tokens"] += tokens
        _save(data)


def record_error() -> None:
    with _lock:
        data = _load()
        data["errors"] += 1
        _day(data)["errors"] += 1
        _save(data)


def record_rate_limited() -> None:
    with _lock:
        data = _load()
        data["rate_limited"] += 1
        _save(data)


def record_rejected_too_long() -> None:
    with _lock:
        data = _load()
        data["rejected_too_long"] += 1
        _save(data)


def _cost(prompt: int, completion: int, embedding: int, prices: dict[str, float]) -> float:
    return (
        prompt * prices["input"] + completion * prices["output"] + embedding * prices["embedding"]
    ) / 1_000_000


def _day_cost(d: dict[str, int], prices: dict[str, float]) -> float:
    if "prompt_tokens" not in d:
        # Recorded before the prompt/completion split existed: price it all as input.
        return _cost(d.get("total_tokens", 0), 0, 0, prices)
    return _cost(d["prompt_tokens"], d["completion_tokens"], d.get("embedding_tokens", 0), prices)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def _pct(part: float, whole: float) -> float:
    return part / whole * 100 if whole else 0.0


def get_summary() -> dict[str, Any]:
    with _lock:
        data = _load()
    cfg = get_config()
    prices = {
        "input": cfg["chat_input_price_per_1m"],
        "output": cfg["chat_output_price_per_1m"],
        "embedding": cfg["embedding_price_per_1m"],
    }
    now = _now()
    today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    chat_cost = _cost(data["prompt_tokens"], data["completion_tokens"], 0, prices)
    embedding_cost = _cost(0, 0, data["embedding_tokens"], prices)
    ingest_cost = _cost(0, 0, data["ingest_embedding_tokens"], prices)
    total_cost = chat_cost + embedding_cost

    daily_costs = {day: _day_cost(d, prices) for day, d in data["daily"].items()}
    cost_today = daily_costs.get(today, 0.0)
    cost_month = sum(c for day, c in daily_costs.items() if day.startswith(month))
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    projected_month = cost_month / now.day * days_in_month

    answered = data["generation_count"] + data["cache_hits"]
    avg_generation_cost = (chat_cost / data["generation_count"]) if data["generation_count"] else 0.0
    budget = cfg["monthly_budget_usd"]

    since = data["since"] or (min(data["daily"]) if data["daily"] else None)
    context_window, max_output = MODEL_LIMITS.get(settings.openai_chat_model, (None, None))
    latencies = data["recent_latencies_ms"]

    # Every day in the window, including quiet ones, so gaps show as gaps.
    display_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(_DAILY_DISPLAY_DAYS - 1, -1, -1)]
    daily = {
        day: {
            "requests": data["daily"].get(day, {}).get("requests", 0),
            "total_tokens": data["daily"].get(day, {}).get("total_tokens", 0),
            "cost": daily_costs.get(day, 0.0),
        }
        for day in display_days
    }

    return {
        "since": since,
        "chat_model": settings.openai_chat_model,
        "embedding_model": settings.openai_embedding_model,
        "prices": prices,
        # Spend
        "total_cost": total_cost,
        "chat_cost": chat_cost,
        "embedding_cost": embedding_cost,
        "ingest_cost": ingest_cost,
        "cost_today": cost_today,
        "cost_month": cost_month,
        "projected_month": projected_month,
        "avg_cost_per_answer": ((total_cost - ingest_cost) / answered) if answered else 0.0,
        "cache_savings": data["cache_hits"] * avg_generation_cost,
        "monthly_budget": budget,
        "budget_remaining": max(0.0, budget - cost_month) if budget else None,
        "budget_used_pct": _pct(cost_month, budget) if budget else None,
        # Traffic
        "chat_requests": data["chat_requests"],
        "answered": answered,
        "generation_count": data["generation_count"],
        "cache_hits": data["cache_hits"],
        "cache_hit_rate": _pct(data["cache_hits"], answered),
        "small_talk": data["small_talk"],
        "refusals": data["refusals"],
        "refusal_rate": _pct(data["refusals"], data["generation_count"]),
        "errors": data["errors"],
        "error_rate": _pct(data["errors"], data["chat_requests"]),
        "rate_limited": data["rate_limited"],
        "rejected_too_long": data["rejected_too_long"],
        # Tokens & limits
        "prompt_tokens": data["prompt_tokens"],
        "completion_tokens": data["completion_tokens"],
        "embedding_tokens": data["embedding_tokens"],
        "ingest_embedding_tokens": data["ingest_embedding_tokens"],
        "total_tokens": data["prompt_tokens"] + data["completion_tokens"] + data["embedding_tokens"],
        "avg_tokens_per_answer": (
            (data["prompt_tokens"] + data["completion_tokens"]) / data["generation_count"]
            if data["generation_count"] else 0.0
        ),
        "avg_completion_tokens": (
            data["completion_tokens"] / data["generation_count"] if data["generation_count"] else 0.0
        ),
        "max_prompt_tokens": data["max_prompt_tokens"],
        "max_completion_tokens": data["max_completion_tokens"],
        "truncated_answers": data["truncated_answers"],
        "max_answer_tokens": cfg["max_answer_tokens"],
        "context_window": context_window,
        "model_max_output": max_output,
        # Performance
        "avg_latency_ms": (data["total_latency_ms"] / data["generation_count"]) if data["generation_count"] else 0.0,
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "max_latency_ms": data["max_latency_ms"],
        "daily": daily,
    }


def reset_metrics() -> None:
    with _lock:
        _save(_empty())
