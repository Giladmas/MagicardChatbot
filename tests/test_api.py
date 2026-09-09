"""Run: python -m tests.test_api
Offline tests for validation, rate limiting, error fallback, and the admin
panel. Mocks retrieve()/generate_answer() so no OpenAI/Qdrant calls happen -
safe to run anytime, no live services or API keys required.
"""

from __future__ import annotations

import re
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
        conversations_log_path = log_dir / "conversations.jsonl"

        # Fake Qdrant-backed answer cache so admin tests don't need live Qdrant.
        fake_cache: list[dict] = []

        def fake_list_cached(limit: int = 200) -> list[dict]:
            return list(fake_cache)

        def fake_delete_cached(entry_id: str) -> bool:
            before = len(fake_cache)
            fake_cache[:] = [e for e in fake_cache if e["id"] != entry_id]
            return len(fake_cache) != before

        def fake_clear_cache() -> None:
            fake_cache.clear()

        with (
            patch("app.runtime_config.CONFIG_PATH", config_path),
            patch("app.services.miss_log.LOG_DIR", log_dir),
            patch("app.services.miss_log.LOG_PATH", log_path),
            patch("app.services.conversation_log.LOG_DIR", log_dir),
            patch("app.services.conversation_log.LOG_PATH", conversations_log_path),
            patch("app.services.rate_limit._last_request", {}),
            patch("app.services.conversation_history._history", {}),
            patch("app.api.chat.lookup", return_value=None),
            patch("app.api.chat.store"),
            patch("app.api.admin.list_cached", side_effect=fake_list_cached),
            patch("app.api.admin.delete_cached", side_effect=fake_delete_cached),
            patch("app.api.admin.clear_cache", side_effect=fake_clear_cache),
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

                from app.services.conversation_log import read_conversations as _read_conversations

                logged_turn = next(
                    (c for c in _read_conversations() if c["question"] == "what is magicard"), None
                )
                check("successful turn saved to conversation log", logged_turn is not None)
                check("logged turn records the user id", logged_turn is not None and logged_turn["user_id"] == "u2")
                check("logged turn records the answer", logged_turn is not None and logged_turn["answer"] == "a real answer")

                # --- rate limit: same user again immediately ---
                r = client.post("/chat", json={"question": "another one"}, headers={"X-User-Id": "u2"})
                check("second request same user -> 429", r.status_code == 429)
                detail = r.json().get("detail", "")
                match = re.search(r"(\d+) second", detail)
                check("429 wait time is a whole number", bool(match))
                check("429 message mentions seconds", "second" in detail)

                # different user is unaffected
                r = client.post("/chat", json={"question": "hello"}, headers={"X-User-Id": "u3"})
                check("different user not rate-limited", r.status_code == 200)

            # --- error fallback ---
            with patch("app.api.chat.retrieve", side_effect=RuntimeError("boom")):
                r = client.post("/chat", json={"question": "will this fail"}, headers={"X-User-Id": "u4"})
                check("pipeline exception -> 200 fallback", r.status_code == 200)
                check("fallback message returned", r.json().get("answer") == FALLBACK_ERROR)

                from app.services.conversation_log import read_conversations as _read_conversations_2

                check(
                    "fallback error is not saved to conversation log",
                    all(c["question"] != "will this fail" for c in _read_conversations_2()),
                )

            # --- missed-question logging ---
            with (
                patch("app.api.chat.retrieve", return_value=FAKE_CHUNKS),
                patch("app.api.chat.generate_answer", return_value=REFUSAL),
            ):
                client.post("/chat", json={"question": "unknown topic"}, headers={"X-User-Id": "u5"})
                logged = log_path.exists() and "unknown topic" in log_path.read_text(encoding="utf-8")
                check("missed question logged", logged)

            # --- conversation history: second call from the same user sees the first turn ---
            with (
                patch("app.api.chat.check_rate_limit", return_value=None),
                patch("app.api.chat.retrieve", return_value=FAKE_CHUNKS),
            ):
                seen_history = []

                def fake_generate(question, chunks, history=None, **kwargs):
                    seen_history.append(history)
                    return f"answer to {question}"

                with patch("app.api.chat.generate_answer", side_effect=fake_generate):
                    client.post("/chat", json={"question": "first question"}, headers={"X-User-Id": "u6"})
                    client.post("/chat", json={"question": "second question"}, headers={"X-User-Id": "u6"})

                check("first call has no history", seen_history[0] == [])
                check(
                    "second call sees the first turn",
                    len(seen_history[1]) == 1 and seen_history[1][0].question == "first question",
                )

            # --- conversation history: expires after inactivity ---
            from app.services.conversation_history import append_turn, get_history

            with patch("time.monotonic", return_value=1000.0):
                append_turn("u_expiry", "q", "a", ttl_seconds=5, max_turns=6)
            with patch("time.monotonic", return_value=1010.0):
                check("history expires after ttl", get_history("u_expiry", ttl_seconds=5) == [])

            # --- semantic cache: hit skips retrieval/generation, miss falls through and stores ---
            with (
                patch("app.api.chat.check_rate_limit", return_value=None),
                patch("app.api.chat.lookup", return_value=("cached answer", ["faq.txt"])) as mock_lookup,
                patch("app.api.chat.retrieve") as mock_retrieve,
                patch("app.api.chat.generate_answer") as mock_generate,
            ):
                r = client.post("/chat", json={"question": "cached question"}, headers={"X-User-Id": "u7"})
                check("cache hit returns cached answer", r.json().get("answer") == "cached answer")
                check("cache hit skips retrieval", not mock_retrieve.called)
                check("cache hit skips generation", not mock_generate.called)

            with (
                patch("app.api.chat.check_rate_limit", return_value=None),
                patch("app.api.chat.lookup", return_value=None),
                patch("app.api.chat.store") as mock_store,
                patch("app.api.chat.retrieve", return_value=FAKE_CHUNKS),
                patch("app.api.chat.generate_answer", return_value="fresh answer"),
            ):
                client.post("/chat", json={"question": "uncached question"}, headers={"X-User-Id": "u8"})
                check("cache miss stores the fresh answer", mock_store.called)

            # --- admin panel (checked before rotation pushes "unknown topic" into an archive) ---
            r = client.get("/admin")
            check("admin no auth -> 401", r.status_code == 401)

            r = client.get("/admin", auth=("admin", "wrong"))
            check("admin wrong password -> 401", r.status_code == 401)

            r = client.get("/admin", auth=("admin", "test-pass"))
            check("admin correct auth -> 200", r.status_code == 200)
            check("admin page shows missed question", "unknown topic" in r.text)
            check("admin section renamed to Knowledge Gaps", "Knowledge Gaps" in r.text)
            check("admin shows seconds unit hint", "seconds of inactivity" in r.text)
            check("admin shows humanized duration for history_ttl_seconds", "= 30 min" in r.text)
            check("admin rotate-at key renamed", "knowledge_gaps_rotate_at" in r.text)
            check("admin old rotate-at key gone", "missed_questions_rotate_at" not in r.text)
            check("admin sidebar renamed to Dictionary", "Dictionary" in r.text)
            check("admin has test chatbox", "Test Chatbot" in r.text and "chat-messages" in r.text)
            check("admin has no lingering Saved banner on plain GET", "Saved." not in r.text)

            # --- timestamp shown as "YYYY-MM-DD, HH:MM:SS", not raw ISO-8601 ---
            # "unknown topic" also appears inside a data-search="..." attribute before the
            # visible cell now, so use the last (visible-cell) occurrence as the anchor.
            row_start = r.text.rfind("unknown topic")
            row_html = r.text[max(0, row_start - 400) : row_start]
            check("timestamp uses ', ' between date and time", re.search(r"\d{4}-\d{2}-\d{2}, \d{2}:\d{2}:\d{2}", row_html) is not None)
            check("timestamp has no raw ISO 'T' separator", "T" not in re.search(r"\d{4}-\d{2}-\d{2}[^<]*", row_html).group())

            # --- delete a single knowledge-gap entry ---
            from app.services.miss_log import read_misses as _read_misses

            target_id = next(m["id"] for m in _read_misses() if m["question"] == "unknown topic")
            r = client.post(
                "/admin/misses/delete",
                data={"id": target_id},
                auth=("admin", "test-pass"),
            )
            check("delete miss -> 200", r.status_code == 200)
            check("deleted entry no longer present", all(m["id"] != target_id for m in _read_misses()))

            # --- clear all knowledge-gap entries ---
            r = client.post("/admin/misses/clear", auth=("admin", "test-pass"))
            check("clear all -> 200", r.status_code == 200)
            check("all entries cleared", _read_misses() == [])

            # --- cached answers: shown on the admin page, deletable, clearable ---
            fake_cache.append(
                {
                    "id": "cache-1",
                    "question": "cached question",
                    "answer": "cached answer text",
                    "sources": ["faq.txt"],
                    "cached_at": "2026-01-01T12:00:00+03:00",
                }
            )
            r = client.get("/admin", auth=("admin", "test-pass"))
            check("admin shows Cached Answers section", "Cached Answers" in r.text)
            check("admin page lists cached question", "cached question" in r.text)

            r = client.post(
                "/admin/cache/delete",
                data={"id": "cache-1"},
                auth=("admin", "test-pass"),
            )
            check("delete cached entry -> 200", r.status_code == 200)
            check("cached entry removed", fake_cache == [])

            fake_cache.append(
                {
                    "id": "cache-2",
                    "question": "another cached question",
                    "answer": "another cached answer",
                    "sources": [],
                    "cached_at": "2026-01-01T12:00:00+03:00",
                }
            )
            r = client.post("/admin/cache/clear", auth=("admin", "test-pass"))
            check("clear cache -> 200", r.status_code == 200)
            check("cache cleared", fake_cache == [])
            check("clear cache shows confirmation", "Answer cache cleared" in r.text)

            # --- clear all conversation history ---
            from app.services.conversation_history import _history as history_store
            from app.services.conversation_history import append_turn as _append_turn

            _append_turn("some-user", "q", "a", ttl_seconds=1800, max_turns=6)
            check("history has an entry before clearing", len(history_store) > 0)
            r = client.post("/admin/history/clear", auth=("admin", "test-pass"))
            check("clear history -> 200", r.status_code == 200)
            check("history empty after clearing", len(history_store) == 0)
            check("clear history shows confirmation", "Conversation history cleared" in r.text)

            # --- main /admin page previews only the latest 5, "View all" links to the full list ---
            from app.services.miss_log import log_miss as _log_miss_preview

            for i in range(7):
                _log_miss_preview(f"preview gap {i}", rotate_at=200)
            for i in range(7):
                fake_cache.append(
                    {
                        "id": f"preview-cache-{i}",
                        "question": f"preview cached question {i}",
                        "answer": f"preview cached answer {i}",
                        "sources": [],
                        "cached_at": "2026-01-01T12:00:00+03:00",
                    }
                )

            r = client.get("/admin", auth=("admin", "test-pass"))
            check(
                "main page previews only 5 knowledge gaps",
                sum(r.text.count(f"preview gap {i}") for i in range(7)) == 5 * 2,  # cell + data-search
            )
            check(
                "main page previews only 5 cached answers",
                sum(r.text.count(f"preview cached question {i}") for i in range(7)) == 5 * 2,
            )
            check("main page shows View all link for knowledge gaps", 'href="/admin/misses"' in r.text)
            check("main page shows View all link for cached answers", 'href="/admin/cache"' in r.text)
            check("View all link opens a new tab", 'target="_blank"' in r.text)

            r = client.get("/admin/misses", auth=("admin", "test-pass"))
            check("full knowledge-gaps page -> 200", r.status_code == 200)
            check(
                "full knowledge-gaps page shows all 7",
                all(f"preview gap {i}" in r.text for i in range(7)),
            )
            check("full knowledge-gaps page has search box", 'id="search"' in r.text)

            r = client.get("/admin/cache", auth=("admin", "test-pass"))
            check("full cache page -> 200", r.status_code == 200)
            check(
                "full cache page shows all 7",
                all(f"preview cached question {i}" in r.text for i in range(7)),
            )

            from app.services.miss_log import clear_misses as _clear_misses_preview

            _clear_misses_preview()
            fake_cache.clear()

            # --- conversation log: save/display/search/delete/clear, mirroring Knowledge Gaps ---
            from app.services.conversation_log import (
                clear_conversations as _clear_conversations,
            )
            from app.services.conversation_log import (
                log_turn as _log_turn,
            )
            from app.services.conversation_log import (
                read_conversations as _read_conversations_3,
            )

            _clear_conversations()
            for i in range(7):
                _log_turn(f"conv-user-{i}", f"logged question {i}", f"logged answer {i}", rotate_at=1000)

            r = client.get("/admin", auth=("admin", "test-pass"))
            check("admin page shows Conversations section", "Conversations" in r.text)
            check(
                "main page previews only 5 conversations",
                sum(r.text.count(f"logged question {i}") for i in range(7)) == 5 * 2,  # cell + data-search
            )
            check("main page shows View all link for conversations", 'href="/admin/conversations"' in r.text)
            check("admin shows conversation log config keys", "conversation_log_enabled" in r.text and "conversations_rotate_at" in r.text)
            check("dictionary explains Conversation Log", "Conversation Log" in r.text)

            r = client.get("/admin/conversations", auth=("admin", "test-pass"))
            check("full conversations page -> 200", r.status_code == 200)
            check(
                "full conversations page shows all 7",
                all(f"logged question {i}" in r.text for i in range(7)),
            )
            check("full conversations page shows the user id", "conv-user-3" in r.text)

            target_conv_id = next(c["id"] for c in _read_conversations_3() if c["question"] == "logged question 3")
            r = client.post(
                "/admin/conversations/delete",
                data={"id": target_conv_id},
                auth=("admin", "test-pass"),
            )
            check("delete conversation entry -> 200", r.status_code == 200)
            check(
                "deleted conversation entry no longer present",
                all(c["id"] != target_conv_id for c in _read_conversations_3()),
            )

            r = client.post("/admin/conversations/clear", auth=("admin", "test-pass"))
            check("clear conversations -> 200", r.status_code == 200)
            check("all conversations cleared", _read_conversations_3() == [])
            check("clear conversations shows confirmation", "Conversation log cleared" in r.text)

            # --- conversation_log_enabled=false stops new turns from being saved ---
            r = client.post(
                "/admin/config",
                data={"conversation_log_enabled": "false"},
                auth=("admin", "test-pass"),
            )
            with (
                patch("app.api.chat.retrieve", return_value=FAKE_CHUNKS),
                patch("app.api.chat.generate_answer", return_value="an answer while logging is off"),
            ):
                client.post(
                    "/chat", json={"question": "should not be logged"}, headers={"X-User-Id": "u-log-off"}
                )
            check(
                "no entry saved while conversation_log_enabled is false",
                all(c["question"] != "should not be logged" for c in _read_conversations_3()),
            )
            client.post(
                "/admin/config",
                data={"conversation_log_enabled": "true"},
                auth=("admin", "test-pass"),
            )

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
            check("admin config update redirected via PRG", str(r.url).endswith("/admin?msg=saved"))
            check("admin shows save confirmation", "Saved." in r.text)
            from app.runtime_config import get_config

            check("config value updated", get_config()["max_question_words"] == 15)

            # --- refreshing the page afterwards (plain GET, no query params) must not
            # keep showing "Saved" - proves the banner is a one-time flash, not stuck state ---
            r = client.get("/admin", auth=("admin", "test-pass"))
            check("Saved banner does not persist across a plain refresh", "Saved." not in r.text)

            # --- admin config: malformed value should not crash the server ---
            r = client.post(
                "/admin/config",
                data={"max_question_words": "not-a-number"},
                auth=("admin", "test-pass"),
            )
            check("admin config bad value -> 200 (no crash)", r.status_code == 200)
            check("admin page shows validation error", "Error:" in r.text)
            check("bad value did not overwrite config", get_config()["max_question_words"] == 15)

            # --- admin config: bool toggle (checkbox unchecked sends "false" via hidden input) ---
            r = client.post(
                "/admin/config",
                data={"cache_enabled": "false"},
                auth=("admin", "test-pass"),
            )
            check("admin bool toggle -> 200", r.status_code == 200)
            check("cache_enabled turned off", get_config()["cache_enabled"] is False)

            r = client.post(
                "/admin/config",
                data={"cache_enabled": "true"},
                auth=("admin", "test-pass"),
            )
            check("cache_enabled turned back on", get_config()["cache_enabled"] is True)

    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}")


if __name__ == "__main__":
    main()
