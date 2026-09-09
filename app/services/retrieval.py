from dataclasses import dataclass

from app.config import settings
from app.services.conversation_history import Turn
from app.services.embeddings import embed_text
from app.services.qdrant_client import get_qdrant_client


@dataclass
class RetrievedChunk:
    text: str
    source: str
    category: str
    score: float


def retrieve(query: str, top_k: int = 4, history: list[Turn] | None = None) -> list[RetrievedChunk]:
    # Fold the previous question into the search text so a referent-less
    # follow-up ("what about the fee?") still retrieves relevant chunks.
    search_text = f"{history[-1].question}\n{query}" if history else query
    vector = embed_text(search_text)
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
