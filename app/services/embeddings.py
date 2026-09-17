from openai import OpenAI

from app.config import settings
from app.services import metrics

_client: OpenAI | None = None


def get_openai_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    response = get_openai_client().embeddings.create(
        model=settings.openai_embedding_model,
        input=texts,
    )
    if response.usage:
        metrics.record_embedding_tokens(response.usage.total_tokens)
    return [item.embedding for item in response.data]


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]
