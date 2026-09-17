import difflib
import re
import time

from app.config import settings
from app.runtime_config import DEFAULTS
from app.services import metrics
from app.services.conversation_history import Turn
from app.services.embeddings import get_openai_client
from app.services.retrieval import RetrievedChunk

REFUSAL = "I don't have information about that in the Magicard knowledge base."
FALLBACK_ERROR = "Sorry, I'm having trouble answering right now. Please try again shortly."

# Error-context answers must stay to the two-clause template described in
# SYSTEM_PROMPT; capping tokens well below the general answer budget backs that
# up structurally, since prompting alone doesn't fully pin down GPT's verbosity.
ERROR_CONTEXT_MAX_TOKENS = 80

# Whether a question is "a repeat" is decided here in code, not left to the model -
# testing showed GPT reliably over-applies a callback to any follow-up regardless of
# topic (e.g. adding "just to go over that again" to a brand-new question) once a
# conversation has a couple of turns. A plain text-similarity match against each
# prior question in this conversation is a cheap, deterministic signal instead.
_REPEAT_SIMILARITY_THRESHOLD = 0.5
_BACKREFERENCE_RE = re.compile(
    r"\b(again|earlier|before|previously|you said|you mentioned|you told me|last time)\b",
    re.IGNORECASE,
)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _detect_repeat(question: str, history: list[Turn] | None) -> Turn | None:
    if not history:
        return None
    best_turn, best_score = None, 0.0
    for turn in history:
        score = _similarity(question, turn.question)
        if score > best_score:
            best_score, best_turn = score, turn
    if best_score >= _REPEAT_SIMILARITY_THRESHOLD:
        return best_turn
    if best_turn is not None and _BACKREFERENCE_RE.search(question):
        return best_turn
    return None


SYSTEM_PROMPT = (
    "You are the Magicard support assistant. Answer the user's question using ONLY "
    "the context chunks below - never use outside knowledge. You may paraphrase, "
    "combine, and draw direct conclusions from the context (e.g. if the context says "
    "a type of transaction cannot normally be reversed, that answers a question about "
    "cancelling it) - you don't need a verbatim sentence that matches the question.\n"
    "RULE, checked first, before anything else below: each user message ends with a "
    "bracketed note like \"(conversation note: ...)\" telling you, as ground truth "
    "decided outside your judgment, whether this is a genuine repeat of an earlier "
    "question in this conversation. Trust that note completely - do not use a "
    "callback unless the note explicitly says this question is a repeat, and never "
    "skip a callback the note tells you to use. When the note confirms a repeat, "
    "open with a short natural line acknowledging it - vary the wording each time "
    "instead of reusing the same phrase (\"Like I mentioned before, ...\", \"As I "
    "mentioned earlier, ...\", \"Same as before - ...\", \"Just to go over that "
    "again, ...\", \"As I advised, ...\") - then give the answer, and never repeat "
    "the exact same sentence used for that question earlier. This rule never "
    "applies to the exact refusal string below - see that rule instead.\n"
    "If a \"User's recent error\" block is present, answer in exactly two clauses "
    "joined by a comma or dash: (1) a short plain-language reason, under 10 words, "
    "with no technical details from the block (no provider names, HTTP status codes, "
    "error codes, \"Bad Request\", timestamps, or system wording); (2) the matching "
    "context chunk's guidance copied almost word-for-word. Nothing before, between, "
    "or after those two clauses - no extra sentence of elaboration, no restating the "
    "question, no sign-off. Example: \"That happened because your profile details "
    "didn't go through - please verify your account and profile information are "
    "complete and try again. If the issue persists, contact support.\"\n"
    f'If neither the context nor the error block has anything relevant, your ENTIRE '
    f'reply must be exactly: "{REFUSAL}" - nothing before it, nothing after it, no '
    f'callback, no matter what the conversation note says.\n'
    "Keep every answer brief - one sentence in almost every case, two at most. Never "
    "use filler that adds length without information: \"it looks like\", \"it seems\", "
    "\"the error message indicates\", \"this could be due to\", \"to resolve this\", "
    "\"I recommend\", \"feel free to\", \"for further assistance\", or similar. Be "
    "warm and human, not wordy or robotic: no flat acknowledgements like "
    "\"Understood\"/\"Noted\", and no padding just to sound friendlier - a natural, "
    "conversational word choice is enough on its own."
)


def _build_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n".join(f"[{c.source}]\n{c.text}" for c in chunks)


def _build_error_context(ctx) -> str:
    if not ctx:
        return ""
    parts = [
        p
        for p in [
            f"provider={ctx.provider}" if ctx.provider else None,
            f"error_code={ctx.error_code}" if ctx.error_code else None,
            f"message={ctx.message}" if ctx.message else None,
            f"occurred_at={ctx.created_at}" if ctx.created_at else None,
        ]
        if p
    ]
    return "User's recent error:\n" + "; ".join(parts) if parts else ""


def generate_answer(
    question: str,
    chunks: list[RetrievedChunk],
    history: list[Turn] | None = None,
    max_tokens: int = DEFAULTS["max_answer_tokens"],
    temperature: float = DEFAULTS["temperature"],
    error_context=None,
) -> str:
    context = _build_context(chunks)
    error_block = _build_error_context(error_context)
    repeat_turn = _detect_repeat(question, history)
    if repeat_turn is not None:
        conversation_note = (
            "(conversation note: this question IS a repeat of an earlier one in "
            f'this conversation ("{repeat_turn.question}"). Open with a brief '
            "varied callback acknowledging that, then answer - unless the refusal "
            "rule applies, which always wins.)"
        )
    else:
        conversation_note = (
            "(conversation note: this question is NOT a repeat of anything asked "
            "before in this conversation, even if earlier turns are shown above - "
            "answer plainly, no callback.)"
        )
    user_message = (
        f"Context:\n{context}\n\n{error_block}\n\nQuestion: {question}\n\n{conversation_note}"
        if error_block
        else f"Context:\n{context}\n\nQuestion: {question}\n\n{conversation_note}"
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history or []:
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer})
    messages.append({"role": "user", "content": user_message})

    start = time.monotonic()
    response = get_openai_client().chat.completions.create(
        model=settings.openai_chat_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    latency_ms = (time.monotonic() - start) * 1000
    answer = response.choices[0].message.content.strip()

    # Hard guarantee independent of prompt compliance: chat.py's cache/miss-log
    # logic depends on an exact REFUSAL match, so never let a stray callback or
    # other prefix/suffix around the refusal text break that contract.
    if REFUSAL in answer and answer != REFUSAL:
        answer = REFUSAL

    usage = response.usage
    metrics.record_generation(
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        refusal=(answer == REFUSAL),
    )
    return answer
