import logging

import openai
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.runtime_config import get_config
from app.services.answer_cache import lookup, store
from app.services.conversation_history import append_turn, get_history
from app.services.conversation_log import log_turn
from app.services.generation import FALLBACK_ERROR, REFUSAL, generate_answer
from app.services.miss_log import log_miss
from app.services.rate_limit import check_rate_limit
from app.services.retrieval import retrieve

router = APIRouter()
logger = logging.getLogger("magicard.chat")


class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    x_chat_secret: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> ChatResponse:
    if settings.chat_shared_secret and x_chat_secret != settings.chat_shared_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing shared secret")

    if not x_user_id:
        raise HTTPException(status_code=400, detail="X-User-Id header is required")

    cfg = get_config()

    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    word_count = len(question.split())
    if word_count > cfg["max_question_words"]:
        raise HTTPException(
            status_code=400,
            detail=f"question is too long ({word_count} words, max {cfg['max_question_words']})",
        )

    wait_seconds = check_rate_limit(x_user_id, cfg["rate_limit_seconds"])
    if wait_seconds is not None:
        unit = "second" if wait_seconds == 1 else "seconds"
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {wait_seconds} {unit}.",
        )

    history = get_history(x_user_id, cfg["history_ttl_seconds"]) if cfg["history_enabled"] else []

    # Cache only applies to the first turn of a conversation - follow-ups are
    # conversation-specific and unlikely to recur verbatim across users.
    if cfg["cache_enabled"] and not history:
        try:
            cached = lookup(question, cfg["cache_similarity_threshold"])
        except Exception:
            logger.exception("cache lookup failed for question=%r", question)
            cached = None
        if cached is not None:
            answer, sources = cached
            if cfg["history_enabled"]:
                append_turn(x_user_id, question, answer, cfg["history_ttl_seconds"], cfg["history_max_turns"])
            if cfg["conversation_log_enabled"]:
                log_turn(x_user_id, question, answer, rotate_at=cfg["conversations_rotate_at"])
            return ChatResponse(answer=answer, sources=sources)

    try:
        chunks = retrieve(question, top_k=cfg["retrieval_top_k"], history=history)
    except Exception:
        logger.exception("retrieval failed for question=%r", question)
        return ChatResponse(answer=FALLBACK_ERROR, sources=[])

    try:
        answer = generate_answer(
            question,
            chunks,
            history=history,
            max_tokens=cfg["max_answer_tokens"],
            temperature=cfg["temperature"],
        )
    except openai.OpenAIError:
        logger.exception("OpenAI call failed for question=%r", question)
        return ChatResponse(answer=FALLBACK_ERROR, sources=[])
    except Exception:
        logger.exception("generation failed for question=%r", question)
        return ChatResponse(answer=FALLBACK_ERROR, sources=[])

    sources = sorted({c.source for c in chunks})

    if cfg["history_enabled"]:
        append_turn(x_user_id, question, answer, cfg["history_ttl_seconds"], cfg["history_max_turns"])

    if cfg["conversation_log_enabled"]:
        log_turn(x_user_id, question, answer, rotate_at=cfg["conversations_rotate_at"])

    if answer == REFUSAL:
        log_miss(question, rotate_at=cfg["knowledge_gaps_rotate_at"])
    elif cfg["cache_enabled"] and not history:
        try:
            store(question, answer, sources)
        except Exception:
            logger.exception("cache store failed for question=%r", question)

    return ChatResponse(answer=answer, sources=sources)
