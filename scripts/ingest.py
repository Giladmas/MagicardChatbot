"""Chunk every file in knowledge/, embed the chunks, and upsert them into Qdrant.

Safe to re-run: each point's id is a deterministic hash of (source, text), so
editing a knowledge doc and re-running this script updates that point instead
of creating a duplicate. Stale points (chunks removed from the source docs)
are pruned by deleting any existing point for a source file whose id is not
in the freshly produced set.
"""

from __future__ import annotations

import hashlib
import uuid

from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from app.knowledge_processing.chunker import chunk_all
from app.services.answer_cache import clear_cache
from app.services.embeddings import embed_texts
from app.services.qdrant_client import ensure_collection, get_qdrant_client
from app.config import settings

BATCH_SIZE = 64


def chunk_id(source: str, text: str) -> str:
    digest = hashlib.sha256(f"{source}::{text}".encode("utf-8")).hexdigest()
    return str(uuid.UUID(digest[:32]))


def prune_stale_points(source: str, keep_ids: set[str]) -> None:
    client = get_qdrant_client()
    existing, _ = client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))]),
        limit=1000,
        with_payload=False,
    )
    stale_ids = [str(p.id) for p in existing if str(p.id) not in keep_ids]
    if stale_ids:
        client.delete(collection_name=settings.qdrant_collection, points_selector=stale_ids)
        print(f"  pruned {len(stale_ids)} stale point(s) for {source}")


def main() -> None:
    ensure_collection()
    chunks = chunk_all()
    print(f"Chunked {len(chunks)} pieces from knowledge/")

    ids_by_source: dict[str, set[str]] = {}
    for c in chunks:
        ids_by_source.setdefault(c.source, set()).add(chunk_id(c.source, c.text))

    client = get_qdrant_client()
    for start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[start : start + BATCH_SIZE]
        vectors = embed_texts([c.text for c in batch])
        points = [
            PointStruct(
                id=chunk_id(c.source, c.text),
                vector=vector,
                payload={
                    "text": c.text,
                    "source": c.source,
                    "category": c.category,
                    "title": c.title,
                },
            )
            for c, vector in zip(batch, vectors)
        ]
        client.upsert(collection_name=settings.qdrant_collection, points=points)
        print(f"  upserted {len(points)} point(s) ({start + len(points)}/{len(chunks)})")

    for source, keep_ids in ids_by_source.items():
        prune_stale_points(source, keep_ids)

    clear_cache()
    print("Cleared answer cache (stale after a knowledge update).")

    print("Ingestion complete.")


if __name__ == "__main__":
    main()
