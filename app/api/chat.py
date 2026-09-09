import logging

import openai
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.runtime_config import get_config
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

    try:
        chunks = retrieve(question, top_k=cfg["retrieval_top_k"])
    except Exception:
        logger.exception("retrieval failed for question=%r", question)
        return ChatResponse(answer=FALLBACK_ERROR, sources=[])

    try:
        answer = generate_answer(
            question,
            chunks,
            max_tokens=cfg["max_answer_tokens"],
            temperature=cfg["temperature"],
        )
    except openai.OpenAIError:
        logger.exception("OpenAI call failed for question=%r", question)
        return ChatResponse(answer=FALLBACK_ERROR, sources=[])
    except Exception:
        logger.exception("generation failed for question=%r", question)
        return ChatResponse(answer=FALLBACK_ERROR, sources=[])

    if answer == REFUSAL:
        log_miss(question, rotate_at=cfg["missed_questions_rotate_at"])

    sources = sorted({c.source for c in chunks})
    return ChatResponse(answer=answer, sources=sources)
