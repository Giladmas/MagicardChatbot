"""Persistent, admin-viewable record of chat turns.

Distinct from app/services/conversation_history.py, which is the live
in-memory context replayed to GPT - this is a read-only audit trail so an
operator can review what people are actually asking/getting, survives a
restart, and clearing it has no effect on ongoing conversations.

Appended as JSON lines to logs/conversations.jsonl, same rotation scheme as
miss_log.py: once the current file reaches `rotate_at` lines, it's archived
(renamed with a UTC timestamp) and a fresh conversations.jsonl is started.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_PATH = LOG_DIR / "conversations.jsonl"
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

_lock = threading.Lock()


def _entry_id(timestamp: str, user_id: str, question: str) -> str:
    return hashlib.sha256(f"{timestamp}::{user_id}::{question}".encode("utf-8")).hexdigest()[:16]


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def log_turn(user_id: str, question: str, answer: str, rotate_at: int = 1000) -> None:
    timestamp = datetime.now(ISRAEL_TZ).isoformat(timespec="seconds")
    entry = {
        "id": _entry_id(timestamp, user_id, question),
        "timestamp": timestamp,
        "user_id": user_id,
        "question": question,
        "answer": answer,
    }
    with _lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if rotate_at > 0 and _line_count(LOG_PATH) >= rotate_at:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            LOG_PATH.rename(LOG_DIR / f"conversations_{stamp}.jsonl")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _read_all_entries() -> list[dict[str, Any]]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    return [json.loads(line) for line in lines if line.strip()]


def read_conversations(limit: int = 200) -> list[dict[str, Any]]:
    return list(reversed(_read_all_entries()))[:limit]


def delete_conversation_entry(entry_id: str) -> bool:
    """Removes one turn by id. Returns whether anything was deleted."""
    with _lock:
        entries = _read_all_entries()
        remaining = [e for e in entries if e["id"] != entry_id]
        if len(remaining) == len(entries):
            return False
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for entry in remaining:
                f.write(json.dumps(entry) + "\n")
        return True


def clear_conversations() -> None:
    """Deletes the entire current log file."""
    with _lock:
        LOG_PATH.unlink(missing_ok=True)
