from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from app.config import settings

EMBEDDING_DIM = 1536

_client: QdrantClient | None = None


def get_qdrant_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
        )
    return _client


def ensure_collection() -> None:
    client = get_qdrant_client()
    if client.collection_exists(settings.qdrant_collection):
        return
    client.create_collection(
        collection_name=settings.qdrant_collection,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )
