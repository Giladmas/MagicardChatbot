"""Run: python -m tests.test_failure_answers
Requires OPENAI_API_KEY and a running Qdrant with knowledge already ingested
(scripts/ingest.py). Not wired into pytest since it needs live services.

End-to-end check that "I tried X and it failed" questions (card creation,
card top-up, USDT deposit) get an answer that actually lists the likely
causes, not just a generic "contact support". Each case asks the question
through the same retrieve -> generate path as /chat (live runtime config),
then checks the answer mentions every required fact. A fact is a list of
alternative regexes; any one matching (case-insensitive) counts.
"""

import re
from types import SimpleNamespace

from app.runtime_config import get_config
from app.services.generation import REFUSAL, generate_answer
from app.services.retrieval import retrieve

KYC = [r"\bKYC\b", r"identity verification", r"verif"]
WALLET_BALANCE = [r"balance"]
CARD_FEE = [r"3% \+ \$3", r"\bfee"]
CARD_MIN = [r"\$10\b", r"\b10\b"]
CARD_LIMIT = [r"\b3 active", r"three active", r"\b3 cards", r"three cards", r"maximum of 3", r"maximum of three"]
TOPUP_MIN = [r"\$1\b", r"\b1 minimum", r"minimum of \$?1\b"]
TRC20 = [r"TRC-?20"]
TXID = [r"transaction hash", r"\bTxID\b", r"\bhash\b"]
CONFIRMED = [r"confirm"]
DEPOSIT_MIN = [r"1 USDT", r"\$1\b"]
SUPPORT = [r"support"]

CARD_CREATION_FACTS = [KYC, WALLET_BALANCE, CARD_MIN, CARD_LIMIT]
DEPOSIT_FACTS = [TRC20, TXID, CONFIRMED, SUPPORT]

# (question, expected source among retrieved chunks, required facts, error context or None)
CASES = [
    ("I tried to create a card and it failed", "cards.txt", CARD_CREATION_FACTS, None),
    ("card creation failed", "cards.txt", CARD_CREATION_FACTS, None),
    ("why can't I create a card?", "cards.txt", CARD_CREATION_FACTS, None),
    ("I can't make a new card, it gives an error", "sudo_errors.txt", CARD_CREATION_FACTS, None),
    ("Do I need KYC to create a card?", "cards.txt", [KYC], None),
    ("I tried to top up my card and it failed", "cards.txt", [WALLET_BALANCE, CARD_FEE, TOPUP_MIN], None),
    ("I tried to deposit and it failed", "deposits.txt", DEPOSIT_FACTS + [DEPOSIT_MIN], None),
    ("my deposit failed", "deposits.txt", DEPOSIT_FACTS, None),
    ("my USDT deposit was rejected", "deposits.txt", DEPOSIT_FACTS, None),
    ("I sent USDT but my balance didn't update", "deposits.txt", [TXID, CONFIRMED, SUPPORT], None),
    ("why was my TxID rejected?", "deposits.txt", [TXID, CONFIRMED], None),
    # Laravel error context (see ERROR_CONTEXT_INTEGRATION.md): generic question,
    # the failure details come from context.message.
    (
        "why did it fail?",
        "sudo_errors.txt",
        [KYC, WALLET_BALANCE, CARD_LIMIT],
        SimpleNamespace(action="card", provider="sudo", error_code="400", message="Card creation failed", created_at=None),
    ),
]


def _missing_facts(answer: str, facts: list[list[str]]) -> list[str]:
    return [
        alternatives[0]
        for alternatives in facts
        if not any(re.search(pattern, answer, re.IGNORECASE) for pattern in alternatives)
    ]


def main() -> None:
    cfg = get_config()
    hits = 0
    for question, expected_source, facts, error_context in CASES:
        chunks = retrieve(
            question,
            top_k=cfg["retrieval_top_k"],
            extra_context=error_context.message if error_context else None,
        )
        answer = generate_answer(
            question,
            chunks,
            max_tokens=cfg["max_answer_tokens"],
            temperature=cfg["temperature"],
            error_context=error_context,
        )
        sources = [c.source for c in chunks]

        problems = []
        if expected_source not in sources:
            problems.append(f"expected {expected_source} in sources {sources}")
        if answer == REFUSAL:
            problems.append("got refusal")
        missing = _missing_facts(answer, facts)
        if missing:
            problems.append(f"missing facts {missing}")

        ok = not problems
        hits += ok
        status = "OK  " if ok else "MISS"
        label = f"{question!r} (with error context)" if error_context else repr(question)
        print(f"[{status}] {label} -> {answer!r}")
        for problem in problems:
            print(f"         {problem}")

    print(f"\n{hits}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
