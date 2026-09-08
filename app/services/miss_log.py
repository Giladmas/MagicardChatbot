"""Logs questions the chatbot couldn't answer from the knowledge base.

Appended as JSON lines to logs/missed_questions.jsonl - one file, easy to
tail/grep in production, and cheap for the admin page to read back. When the
current file reaches `rotate_at` lines, it's archived (renamed with a UTC
timestamp) and a fresh missed_questions.jsonl is started, so no single file
grows unbounded. Archived files stay in logs/ and aren't shown on the admin
page - browse them directly on the server if you need older history.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_PATH = LOG_DIR / "missed_questions.jsonl"
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

_lock = threading.Lock()


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def log_miss(question: str, rotate_at: int = 200) -> None:
    timestamp = datetime.now(ISRAEL_TZ).isoformat(timespec="seconds")
    entry = {"timestamp": timestamp, "question": question}
    with _lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if rotate_at > 0 and _line_count(LOG_PATH) >= rotate_at:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            LOG_PATH.rename(LOG_DIR / f"missed_questions_{stamp}.jsonl")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def read_misses(limit: int = 200) -> list[dict[str, Any]]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return list(reversed(entries))[:limit]
