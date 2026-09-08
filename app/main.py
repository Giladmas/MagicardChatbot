from fastapi import FastAPI

app = FastAPI(title="Magicard AI Chatbot")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
