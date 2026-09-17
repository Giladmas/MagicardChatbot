from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router
from app.config import validate_required_settings
from app.errors import RateLimitExceeded


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = validate_required_settings()
    if problems:
        raise RuntimeError(
            "Cannot start: invalid configuration.\n- " + "\n- ".join(problems)
        )
    yield


app = FastAPI(title="Magicard AI Chatbot", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(admin_router)


@app.exception_handler(RateLimitExceeded)
def handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": exc.detail, "retry_after_seconds": exc.retry_after_seconds},
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
