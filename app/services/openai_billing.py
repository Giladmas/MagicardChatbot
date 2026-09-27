"""Real spend and token usage, read straight from OpenAI's organization
Costs and Usage APIs - the same numbers as the billing page.

Unlike app/services/metrics.py, which counts what this one server process saw
(and so starts from zero on every Cloud Run redeploy, restart, or new
instance), this is the organization's own record, so it's complete and
survives anything that happens to the app.

Needs an Admin API key (OPENAI_ADMIN_KEY, created under Organization settings
-> Admin keys) - a regular OPENAI_API_KEY is rejected by these endpoints.
Set OPENAI_PROJECT_ID to count only this chatbot's project if the
organization has others. Results are cached in memory for a few minutes so
the admin page stays fast and OpenAI isn't called on every refresh.

Note: OpenAI's cost figures can lag real usage by a few hours; token usage is
close to real time.
"""

from __future__ import annotations

import calendar
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings

API_BASE = "https://api.openai.com/v1/organization"
CACHE_SECONDS = 300
WINDOW_DAYS = 30
_TIMEOUT_SECONDS = 15

_lock = threading.Lock()
_cached: tuple[float, dict[str, Any]] | None = None


def is_configured() -> bool:
    return bool(settings.openai_admin_key)


def _get_all_buckets(client: httpx.Client, path: str, params: dict[str, Any]) -> list[dict]:
    buckets: list[dict] = []
    page = None
    while True:
        response = client.get(f"{API_BASE}/{path}", params={**params, **({"page": page} if page else {})})
        response.raise_for_status()
        body = response.json()
        buckets.extend(body.get("data", []))
        page = body.get("next_page")
        if not body.get("has_more") or not page:
            return buckets


def _day(bucket: dict) -> str:
    return datetime.fromtimestamp(bucket["start_time"], timezone.utc).strftime("%Y-%m-%d")


def _fetch() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today.replace(day=1)
    window_start = min(month_start, today - timedelta(days=WINDOW_DAYS - 1))
    params: dict[str, Any] = {"start_time": int(window_start.timestamp()), "bucket_width": "1d", "limit": 31}
    if settings.openai_project_id:
        params["project_ids"] = [settings.openai_project_id]

    headers = {"Authorization": f"Bearer {settings.openai_admin_key}"}
    with httpx.Client(headers=headers, timeout=_TIMEOUT_SECONDS) as client:
        cost_buckets = _get_all_buckets(client, "costs", params)
        completion_buckets = _get_all_buckets(client, "usage/completions", {**params, "group_by": ["model"]})
        embedding_buckets = _get_all_buckets(client, "usage/embeddings", {**params, "group_by": ["model"]})

    empty_day = {"cost": 0.0, "requests": 0, "input_tokens": 0, "output_tokens": 0,
                 "cached_tokens": 0, "embedding_tokens": 0}
    daily: dict[str, dict] = defaultdict(lambda: dict(empty_day))
    models: dict[str, dict] = defaultdict(lambda: {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                                   "cached_tokens": 0, "kind": "chat"})

    for bucket in cost_buckets:
        daily[_day(bucket)]["cost"] += sum(float(r.get("amount", {}).get("value") or 0) for r in bucket["results"])
    for bucket in completion_buckets:
        day = daily[_day(bucket)]
        for r in bucket["results"]:
            day["requests"] += r.get("num_model_requests") or 0
            day["input_tokens"] += r.get("input_tokens") or 0
            day["output_tokens"] += r.get("output_tokens") or 0
            day["cached_tokens"] += r.get("input_cached_tokens") or 0
            m = models[r.get("model") or "unknown"]
            m["requests"] += r.get("num_model_requests") or 0
            m["input_tokens"] += r.get("input_tokens") or 0
            m["output_tokens"] += r.get("output_tokens") or 0
            m["cached_tokens"] += r.get("input_cached_tokens") or 0
    for bucket in embedding_buckets:
        day = daily[_day(bucket)]
        for r in bucket["results"]:
            day["embedding_tokens"] += r.get("input_tokens") or 0
            m = models[r.get("model") or "unknown"]
            m["kind"] = "embedding"
            m["requests"] += r.get("num_model_requests") or 0
            m["input_tokens"] += r.get("input_tokens") or 0

    # Contiguous last-30-days series (quiet days as zeros) for the chart and totals.
    window_days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(WINDOW_DAYS - 1, -1, -1)]
    last_30 = {d: daily.get(d, dict(empty_day)) for d in window_days}
    month_prefix = today.strftime("%Y-%m")
    cost_month = sum(v["cost"] for d, v in daily.items() if d.startswith(month_prefix))
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    # Project from completed days only - today's cost is usually still partial.
    completed_days = today.day - 1
    cost_before_today = cost_month - daily.get(today.strftime("%Y-%m-%d"), empty_day)["cost"]
    projected = (cost_before_today / completed_days * days_in_month) if completed_days else cost_month

    def total(key: str) -> float:
        return sum(v[key] for v in last_30.values())

    return {
        "ok": True,
        "fetched_at": time.time(),
        "project_id": settings.openai_project_id or None,
        "cost_today": last_30[window_days[-1]]["cost"],
        "cost_yesterday": last_30[window_days[-2]]["cost"],
        "cost_month": cost_month,
        "projected_month": projected,
        "cost_30d": total("cost"),
        "requests_30d": int(total("requests")),
        "input_tokens_30d": int(total("input_tokens")),
        "output_tokens_30d": int(total("output_tokens")),
        "cached_tokens_30d": int(total("cached_tokens")),
        "embedding_tokens_30d": int(total("embedding_tokens")),
        "daily": last_30,
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1]["requests"])),
    }


def get_billing(force_refresh: bool = False) -> dict[str, Any] | None:
    """OpenAI's real usage/cost figures, None if no admin key is configured,
    or {"ok": False, "error": ...} if OpenAI couldn't be reached."""
    global _cached
    if not is_configured():
        return None
    with _lock:
        if not force_refresh and _cached and time.time() - _cached[0] < CACHE_SECONDS:
            return _cached[1]
        try:
            result = _fetch()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            hint = (" - OPENAI_ADMIN_KEY must be an Admin key (sk-admin-...), not a regular API key"
                    if status in (401, 403) else "")
            result = {"ok": False, "error": f"OpenAI returned HTTP {status}{hint}."}
        except httpx.HTTPError as exc:
            result = {"ok": False, "error": f"Couldn't reach OpenAI ({type(exc).__name__})."}
        # Cache failures too, briefly, so a broken key doesn't slow every page load.
        _cached = (time.time() if result["ok"] else time.time() - CACHE_SECONDS + 30, result)
        return result
