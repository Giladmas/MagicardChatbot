"""Semantic cache of standalone question -> answer, backed by its own Qdrant collection.

Kept separate from the knowledge collection so cache lookups never compete
with real retrieval. Only meant for the first turn of a conversation - see
app/api/chat.py - since follow-up questions are conversation-specific and
unlikely to recur verbatim across users.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from qdrant_client.models import PointStruct

from app.config import settings
from app.services.embeddings import embed_text
from app.services.qdrant_client import ensure_named_collection, get_qdrant_client

CACHE_COLLECTION = f"{settings.qdrant_collection}_cache"
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def _cache_id(question: str) -> str:
    digest = hashlib.sha256(question.strip().lower().encode("utf-8")).hexdigest()
    return str(uuid.UUID(digest[:32]))


def lookup(question: str, threshold: float) -> tuple[str, list[str]] | None:
    """Returns (answer, sources) on a hit with score >= threshold, else None."""
    ensure_named_collection(CACHE_COLLECTION)
    vector = embed_text(question)
    hits = get_qdrant_client().query_points(
        collection_name=CACHE_COLLECTION,
        query=vector,
        limit=1,
        with_payload=True,
    ).points
    if hits and hits[0].score >= threshold:
        payload = hits[0].payload
        return payload["answer"], payload["sources"]
    return None


def store(question: str, answer: str, sources: list[str]) -> None:
    """Upserts this Q&A into the cache; re-asking the same question later updates rather than duplicates."""
    ensure_named_collection(CACHE_COLLECTION)
    vector = embed_text(question)
    point = PointStruct(
        id=_cache_id(question),
        vector=vector,
        payload={
            "question": question,
            "answer": answer,
            "sources": sources,
            "cached_at": datetime.now(ISRAEL_TZ).isoformat(timespec="seconds"),
        },
    )
    get_qdrant_client().upsert(collection_name=CACHE_COLLECTION, points=[point])


def list_cached(limit: int = 200) -> list[dict[str, Any]]:
    """Most recently cached entries first."""
    ensure_named_collection(CACHE_COLLECTION)
    points, _ = get_qdrant_client().scroll(
        collection_name=CACHE_COLLECTION,
        limit=limit,
        with_payload=True,
    )
    entries = [
        {
            "id": str(p.id),
            "question": p.payload.get("question", ""),
            "answer": p.payload.get("answer", ""),
            "sources": p.payload.get("sources", []),
            "cached_at": p.payload.get("cached_at", ""),
        }
        for p in points
    ]
    entries.sort(key=lambda e: e["cached_at"], reverse=True)
    return entries[:limit]


def delete_cached(entry_id: str) -> bool:
    """Removes one cached entry by id. Returns whether anything was deleted."""
    client = get_qdrant_client()
    ensure_named_collection(CACHE_COLLECTION)
    existing = client.retrieve(collection_name=CACHE_COLLECTION, ids=[entry_id])
    if not existing:
        return False
    client.delete(collection_name=CACHE_COLLECTION, points_selector=[entry_id])
    return True


def clear_cache() -> None:
    """Deletes and recreates the cache collection. Called after a knowledge re-ingest, since cached
    answers are only valid for the knowledge content they were generated from."""
    client = get_qdrant_client()
    if client.collection_exists(CACHE_COLLECTION):
        client.delete_collection(CACHE_COLLECTION)
    ensure_named_collection(CACHE_COLLECTION)
