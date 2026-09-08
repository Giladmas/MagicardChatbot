from dataclasses import dataclass

from app.config import settings
from app.services.embeddings import embed_text
from app.services.qdrant_client import get_qdrant_client


@dataclass
class RetrievedChunk:
    text: str
    source: str
    category: str
    score: float


def retrieve(query: str, top_k: int = 4) -> list[RetrievedChunk]:
    vector = embed_text(query)
    client = get_qdrant_client()
    results = client.query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        limit=top_k,
        with_payload=True,
    ).points
    return [
        RetrievedChunk(
            text=r.payload["text"],
            source=r.payload["source"],
            category=r.payload["category"],
            score=r.score,
        )
        for r in results
    ]
