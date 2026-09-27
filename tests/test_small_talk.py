"""Run: python -m tests.test_small_talk
Requires OPENAI_API_KEY and a running Qdrant with knowledge already ingested
(scripts/ingest.py). Not wired into pytest since it needs live services.

End-to-end check that social messages ("ty", "hi", "bye") get a short friendly
reply - no canned "feel free to ask" tail, no greeting unless they greeted -
instead of the refusal string - including mid-conversation and when
Laravel sent error context - while genuinely off-topic questions still get
the exact refusal, and a thanks mixed with a real question still answers it.
"""

import re
from types import SimpleNamespace

from app.runtime_config import get_config
from app.services.conversation_history import Turn
from app.services.generation import REFUSAL, generate_answer
from app.services.retrieval import retrieve

DEPOSIT_HISTORY = [
    Turn(
        question="why did my deposit usdt didnt work? i enter a transaction id",
        answer=(
            "That happened because no matching transfer was found - the most common "
            "reasons are that the USDT was not sent on the TRC20 network to the "
            "Magicard deposit address, the transaction hash was copied incorrectly, "
            "or the amount was 1 USDT or less, which credits nothing after the fee. "
            "Please check these and try again."
        ),
    )
]
DEPOSIT_ERROR = SimpleNamespace(
    action="deposit", provider=None, error_code=None, message="No matching transfer found", created_at=None
)

SMALL_TALK = "small_talk"  # expect a short, non-refusal reply
REFUSE = "refuse"  # expect exactly REFUSAL

# (message, expectation, history, error context, regex the answer must match or None)
CASES = [
    ("ok great ty", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("ok great ty", SMALL_TALK, DEPOSIT_HISTORY, DEPOSIT_ERROR, None),
    ("thanks!", SMALL_TALK, None, None, None),
    ("thank you so much", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("hi", SMALL_TALK, None, None, None),
    ("hello there", SMALL_TALK, None, None, None),
    ("got it", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("bye", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    # Combined/free-form social messages - the reply should cover every part.
    ("ty it worked, have a nice day and bye", SMALL_TALK, DEPOSIT_HISTORY, DEPOSIT_ERROR, r"glad|great|happy"),
    ("thx", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("tysm!!", SMALL_TALK, None, None, None),
    ("awesome thanks bro", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("perfect, it works now", SMALL_TALK, DEPOSIT_HISTORY, None, r"glad|great|happy"),
    ("hey how are you?", SMALL_TALK, None, None, None),
    ("great, see you later", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("have a good one", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    # Very short messages - flagged in code by generation._is_small_talk().
    ("ok", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("lol", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    (":)", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("\U0001f44d", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("nope, nothing else", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    # Frustration and identity questions.
    ("that didn't help", SMALL_TALK, DEPOSIT_HISTORY, None, r"sorry"),
    ("you're useless", SMALL_TALK, DEPOSIT_HISTORY, None, r"sorry"),
    ("are you a bot?", SMALL_TALK, None, None, r"assistant"),
    # More real-world phrasings - the reply should cover every part of each.
    ("Thank you so much for your help, it's working now! Have a great weekend 😊", SMALL_TALK, DEPOSIT_HISTORY, None, r"weekend"),
    ("tysm it finally works 🙏", SMALL_TALK, DEPOSIT_HISTORY, None, r"glad|great|happy"),
    ("no thanks, I'm good", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("byeee", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("hahaha", SMALL_TALK, DEPOSIT_HISTORY, None, None),
    ("good morning!", SMALL_TALK, None, None, r"morning"),
    ("What's the weather like today?", REFUSE, None, None, None),
    ("Who won the football game yesterday?", REFUSE, DEPOSIT_HISTORY, None, None),
    ("thanks, and what is the withdrawal fee?", "answer", None, None, r"1% (\+|plus) \$1"),
]

MAX_SMALL_TALK_WORDS = 30
# The canned tail the prompt forbids, and a greeting the user never gave.
STOCK_CLOSER_RE = re.compile(r"feel free to|if you have any (more |other )?questions", re.IGNORECASE)
GREETING_RE = re.compile(r"(hi|hello|hey)", re.IGNORECASE)


def main() -> None:
    cfg = get_config()
    hits = 0
    for message, expectation, history, error_context, must_match in CASES:
        chunks = retrieve(
            message,
            top_k=cfg["retrieval_top_k"],
            history=history,
            extra_context=error_context.message if error_context else None,
        )
        answer = generate_answer(
            message,
            chunks,
            history=history,
            max_tokens=cfg["max_answer_tokens"],
            temperature=cfg["temperature"],
            error_context=error_context,
        )

        if expectation == SMALL_TALK:
            ok = answer != REFUSAL and len(answer.split()) <= MAX_SMALL_TALK_WORDS
            ok = ok and not STOCK_CLOSER_RE.search(answer)
            if history and not GREETING_RE.search(message):
                ok = ok and not GREETING_RE.match(answer)
            if must_match:
                ok = ok and bool(re.search(must_match, answer, re.IGNORECASE))
        elif expectation == REFUSE:
            ok = answer == REFUSAL
        else:
            ok = answer != REFUSAL and bool(re.search(must_match, answer, re.IGNORECASE))

        hits += ok
        status = "OK  " if ok else "MISS"
        label = message + (" [mid-conversation]" if history else "") + (" [error context]" if error_context else "")
        print(f"[{status}] {label!a} ({expectation}) -> {answer!a}")

    print(f"\n{hits}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
