"""Run: python -m tests.test_chat
Requires OPENAI_API_KEY and a running Qdrant with knowledge already ingested
(scripts/ingest.py). Not wired into pytest since it needs live services.

End-to-end check of the full RAG flow: retrieve -> generate. Confirms
in-scope questions get a non-refusal answer, and the out-of-scope question
gets the exact refusal string from app.services.generation.
"""

from app.services.generation import REFUSAL, generate_answer
from app.services.retrieval import retrieve
from tests.eval_questions import EVAL_QUESTIONS


def main() -> None:
    hits = 0
    for question, expected_source in EVAL_QUESTIONS:
        chunks = retrieve(question, top_k=4)
        answer = generate_answer(question, chunks)

        if expected_source == "__none__":
            ok = answer == REFUSAL
        else:
            ok = answer != REFUSAL and len(answer) > 0

        hits += ok
        status = "OK  " if ok else "MISS"
        print(f"[{status}] {question!r} -> {answer!r}")

    print(f"\n{hits}/{len(EVAL_QUESTIONS)} passed")


if __name__ == "__main__":
    main()
