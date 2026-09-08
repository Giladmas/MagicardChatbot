"""Splits the .txt files in knowledge/ into small, self-contained chunks.

Two source formats are supported:
- Q&A files (lines starting with "Q:" / "A:") -> one chunk per Q&A pair.
- Prose files -> one chunk per blank-line-separated paragraph.

Every chunk keeps the document's title line as context so it still makes
sense in isolation once embedded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"


@dataclass
class Chunk:
    text: str
    source: str
    category: str
    title: str


def _category_from_filename(path: Path) -> str:
    return path.stem


def _split_qa(body_lines: list[str]) -> list[str]:
    chunks: list[str] = []
    question: str | None = None
    answer_lines: list[str] = []

    def flush() -> None:
        if question is not None and answer_lines:
            chunks.append(f"Q: {question}\nA: {' '.join(answer_lines).strip()}")

    for line in body_lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("Q:"):
            flush()
            question = line[2:].strip()
            answer_lines = []
        elif line.startswith("A:"):
            answer_lines.append(line[2:].strip())
        elif question is not None:
            answer_lines.append(line)

    flush()
    return chunks


def _split_prose(body_lines: list[str]) -> list[str]:
    text = "\n".join(body_lines)
    paragraphs = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


def chunk_file(path: Path) -> list[Chunk]:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    lines = [ln.rstrip() for ln in raw_lines]

    title = lines[0].strip() if lines else path.stem
    body_lines = lines[1:]

    is_qa = any(ln.strip().startswith("Q:") for ln in body_lines)
    pieces = _split_qa(body_lines) if is_qa else _split_prose(body_lines)

    category = _category_from_filename(path)
    return [
        Chunk(text=f"{title}\n{piece}", source=path.name, category=category, title=title)
        for piece in pieces
    ]


def chunk_all(knowledge_dir: Path = KNOWLEDGE_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(knowledge_dir.glob("*.txt")):
        chunks.extend(chunk_file(path))
    return chunks


if __name__ == "__main__":
    all_chunks = chunk_all()
    print(f"Produced {len(all_chunks)} chunks from {len(list(KNOWLEDGE_DIR.glob('*.txt')))} files\n")
    for c in all_chunks:
        print(f"--- [{c.category}] ---")
        print(c.text)
        print()
