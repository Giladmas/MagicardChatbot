from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "magicard_knowledge"

    chat_shared_secret: str = ""
    admin_username: str = "admin"
    admin_password: str = ""


settings = Settings()


def validate_required_settings(s: Settings = settings) -> list[str]:
    """Returns human-readable problems with the current settings, empty if none.

    Checked at server startup (see app/main.py) so a missing key fails fast
    with a clear message instead of surfacing later as an OpenAI/Qdrant error
    on the first real request.
    """
    problems = []
    if not s.openai_api_key:
        problems.append("OPENAI_API_KEY is not set (required for chat completions and embeddings).")

    is_local_qdrant = "localhost" in s.qdrant_url or "127.0.0.1" in s.qdrant_url
    if not s.qdrant_api_key and not is_local_qdrant:
        problems.append(
            f"QDRANT_API_KEY is not set, but QDRANT_URL ({s.qdrant_url!r}) doesn't look like a "
            "local instance. Qdrant Cloud requires an API key."
        )
    return problems
