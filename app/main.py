from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router

app = FastAPI(title="Magicard AI Chatbot")
app.include_router(chat_router)
app.include_router(admin_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
