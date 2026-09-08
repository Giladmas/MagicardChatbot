"""Run: python -m tests.test_api
Offline tests for validation, rate limiting, error fallback, and the admin
panel. Mocks retrieve()/generate_answer() so no OpenAI/Qdrant calls happen -
safe to run anytime, no live services or API keys required.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.services.generation import FALLBACK_ERROR, REFUSAL
from app.services.retrieval import RetrievedChunk

FAKE_CHUNKS = [RetrievedChunk(text="some text", source="faq.txt", category="faq", score=0.9)]

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    status = "OK  " if condition else "FAIL"
    if not condition:
        failures += 1
    print(f"[{status}] {label}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "runtime_config.json"
        log_dir = Path(tmp) / "logs"
        log_path = log_dir / "missed_questions.jsonl"

        with (
            patch("app.runtime_config.CONFIG_PATH", config_path),
            patch("app.services.miss_log.LOG_DIR", log_dir),
            patch("app.services.miss_log.LOG_PATH", log_path),
            patch("app.services.rate_limit._last_request", {}),
            patch.object(settings, "admin_password", "test-pass"),
            patch.object(settings, "admin_username", "admin"),
            patch.object(settings, "chat_shared_secret", ""),
        ):
            from app.main import app

            client = TestClient(app)

            # --- validation ---
            r = client.post("/chat", json={"question": "hi"})
            check("missing X-User-Id -> 400", r.status_code == 400)

            r = client.post("/chat", json={"question": "   "}, headers={"X-User-Id": "u1"})
            check("empty question -> 400", r.status_code == 400)

            long_question = " ".join(["word"] * 25)
            r = client.post("/chat", json={"question": long_question}, headers={"X-User-Id": "u1"})
            check("over word limit -> 400", r.status_code == 400)

            # --- happy path (mocked pipeline) ---
            with (
                patch("app.api.chat.retrieve", return_value=FAKE_CHUNKS),
                patch("app.api.chat.generate_answer", return_value="a real answer"),
            ):
                r = client.post("/chat", json={"question": "what is magicard"}, headers={"X-User-Id": "u2"})
                check("valid question -> 200", r.status_code == 200)
                check("answer echoed", r.json().get("answer") == "a real answer")
                check("sources included", r.json().get("sources") == ["faq.txt"])

                # --- rate limit: same user again immediately ---
                r = client.post("/chat", json={"question": "another one"}, headers={"X-User-Id": "u2"})
                check("second request same user -> 429", r.status_code == 429)

                # different user is unaffected
                r = client.post("/chat", json={"question": "hello"}, headers={"X-User-Id": "u3"})
                check("different user not rate-limited", r.status_code == 200)

            # --- error fallback ---
            with patch("app.api.chat.retrieve", side_effect=RuntimeError("boom")):
                r = client.post("/chat", json={"question": "will this fail"}, headers={"X-User-Id": "u4"})
                check("pipeline exception -> 200 fallback", r.status_code == 200)
                check("fallback message returned", r.json().get("answer") == FALLBACK_ERROR)

            # --- missed-question logging ---
            with (
                patch("app.api.chat.retrieve", return_value=FAKE_CHUNKS),
                patch("app.api.chat.generate_answer", return_value=REFUSAL),
            ):
                client.post("/chat", json={"question": "unknown topic"}, headers={"X-User-Id": "u5"})
                logged = log_path.exists() and "unknown topic" in log_path.read_text(encoding="utf-8")
                check("missed question logged", logged)

            # --- admin panel (checked before rotation pushes "unknown topic" into an archive) ---
            r = client.get("/admin")
            check("admin no auth -> 401", r.status_code == 401)

            r = client.get("/admin", auth=("admin", "wrong"))
            check("admin wrong password -> 401", r.status_code == 401)

            r = client.get("/admin", auth=("admin", "test-pass"))
            check("admin correct auth -> 200", r.status_code == 200)
            check("admin page shows missed question", "unknown topic" in r.text)

            # --- rotation: log_miss directly, force a low threshold ---
            from app.services.miss_log import log_miss, read_misses

            for i in range(5):
                log_miss(f"filler question {i}", rotate_at=3)
            archived = list(log_dir.glob("missed_questions_*.jsonl"))
            check("log rotated after threshold", len(archived) >= 1)
            check("fresh file stays within rotate_at lines", len(read_misses()) <= 3)

            r = client.post(
                "/admin/config",
                data={"max_question_words": "15"},
                auth=("admin", "test-pass"),
            )
            check("admin config update -> 200", r.status_code == 200)
            from app.runtime_config import get_config

            check("config value updated", get_config()["max_question_words"] == 15)

    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}")


if __name__ == "__main__":
    main()
