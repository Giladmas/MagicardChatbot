from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router
from app.config import validate_required_settings


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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
