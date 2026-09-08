"""Run: python -m tests.test_retrieval
Requires OPENAI_API_KEY and a running Qdrant with knowledge already ingested
(scripts/ingest.py). Not wired into pytest since it needs live services.
"""

from app.services.retrieval import retrieve
from tests.eval_questions import EVAL_QUESTIONS


def main() -> None:
    hits = 0
    for question, expected_source in EVAL_QUESTIONS:
        results = retrieve(question, top_k=3)
        top_sources = [r.source for r in results]
        ok = expected_source in top_sources or expected_source == "__none__"
        hits += ok
        status = "OK  " if ok else "MISS"
        print(f"[{status}] {question!r} -> expected {expected_source}, got {top_sources}")

    print(f"\n{hits}/{len(EVAL_QUESTIONS)} passed")


if __name__ == "__main__":
    main()
