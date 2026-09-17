"""Logs questions the chatbot couldn't answer from the knowledge base.

Appended as JSON lines to logs/missed_questions.jsonl - one file, easy to
tail/grep in production, and cheap for the admin page to read back. When the
current file reaches `rotate_at` lines, it's archived (renamed with a UTC
timestamp) and a fresh missed_questions.jsonl is started, so no single file
grows unbounded. Archived files stay in logs/ and aren't shown on the admin
page - browse them directly on the server if you need older history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.services.email_notifier import send_email

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_PATH = LOG_DIR / "missed_questions.jsonl"
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

logger = logging.getLogger("magicard.miss_log")

_lock = threading.Lock()


def _entry_id(timestamp: str, question: str) -> str:
    return hashlib.sha256(f"{timestamp}::{question}".encode("utf-8")).hexdigest()[:16]


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _notify_threshold_reached(rotate_at: int) -> None:
    entries = _read_all_entries()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    try:
        send_email(
            subject=f"[MagiCard] knowledge gaps log reached {rotate_at} entries",
            body=f"The knowledge gaps list has reached {rotate_at} entries. Full list attached.",
            attachment_bytes=json.dumps(entries, indent=2).encode("utf-8"),
            attachment_filename=f"missed_questions_{stamp}.json",
        )
    except Exception:
        logger.exception("failed to send knowledge gaps threshold notification email")


def log_miss(question: str, rotate_at: int = 200) -> None:
    timestamp = datetime.now(ISRAEL_TZ).isoformat(timespec="seconds")
    entry = {"id": _entry_id(timestamp, question), "timestamp": timestamp, "question": question}
    with _lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if rotate_at > 0 and _line_count(LOG_PATH) >= rotate_at:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            LOG_PATH.rename(LOG_DIR / f"missed_questions_{stamp}.jsonl")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        notify = rotate_at > 0 and _line_count(LOG_PATH) == rotate_at
    if notify:
        _notify_threshold_reached(rotate_at)


def _read_all_entries() -> list[dict[str, Any]]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    entries = []
    for line in lines:
        if not line.strip():
            continue
        entry = json.loads(line)
        # Older entries logged before ids existed - derive the same id log_miss would use.
        entry.setdefault("id", _entry_id(entry["timestamp"], entry["question"]))
        entries.append(entry)
    return entries


def read_misses(limit: int = 200) -> list[dict[str, Any]]:
    return list(reversed(_read_all_entries()))[:limit]


def delete_miss(entry_id: str) -> bool:
    """Removes one entry by id. Returns whether anything was deleted."""
    with _lock:
        entries = _read_all_entries()
        remaining = [e for e in entries if e["id"] != entry_id]
        if len(remaining) == len(entries):
            return False
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for entry in remaining:
                f.write(json.dumps(entry) + "\n")
        return True


def clear_misses() -> None:
    """Deletes the entire current log file."""
    with _lock:
        LOG_PATH.unlink(missing_ok=True)
