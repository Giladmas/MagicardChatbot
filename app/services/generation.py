from app.config import settings
from app.runtime_config import DEFAULTS
from app.services.embeddings import get_openai_client
from app.services.retrieval import RetrievedChunk

REFUSAL = "I don't have information about that in the Magicard knowledge base."
FALLBACK_ERROR = "Sorry, I'm having trouble answering right now. Please try again shortly."

SYSTEM_PROMPT = (
    "You are the Magicard support assistant. Answer the user's question using ONLY "
    "the context chunks below - never use outside knowledge. You may paraphrase, "
    "combine, and draw direct conclusions from the context (e.g. if the context says "
    "a type of transaction cannot normally be reversed, that answers a question about "
    "cancelling it) - you don't need a verbatim sentence that matches the question.\n"
    f'Only if the context truly has nothing relevant to the question, reply with exactly: "{REFUSAL}"\n'
    "Keep answers short and direct."
)


def _build_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n".join(f"[{c.source}]\n{c.text}" for c in chunks)


def generate_answer(
    question: str,
    chunks: list[RetrievedChunk],
    max_tokens: int = DEFAULTS["max_answer_tokens"],
    temperature: float = DEFAULTS["temperature"],
) -> str:
    context = _build_context(chunks)
    user_message = f"Context:\n{context}\n\nQuestion: {question}"

    response = get_openai_client().chat.completions.create(
        model=settings.openai_chat_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()
