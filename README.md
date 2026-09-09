# Magicard AI Chatbot

RAG-based support chatbot for Magicard. FastAPI + Qdrant + OpenAI, called by Laravel over HTTP.

## Current status

What's built and working today:

- **Knowledge ingestion** — `knowledge/*.txt` → chunk → embed → upsert into Qdrant Cloud. Idempotent, safe to re-run.
- **Retrieval** — Qdrant vector search for the top-k most relevant chunks per question.
- **Generation** — GPT answers using only retrieved context; refuses (fixed string) when nothing relevant is found.
- **`POST /chat`** — validated (question length, required headers), rate-limited per user, graceful fallback answer if OpenAI/Qdrant fail (never a raw 500).
- **`GET /admin`** — Basic-auth protected page to view/edit live config and review missed questions, no redeploy needed.
- **Missed-question logging** — every refused question saved with an Israel-time timestamp, auto-rotated once the log grows past a threshold.
- **Tests** — offline (`test_api.py`, mocks OpenAI/Qdrant) and live (`test_retrieval.py`, `test_chat.py`) eval scripts.

Not yet built: nothing outstanding from the original scope — remaining work is deployment (where this actually runs in production, see the Cloudways discussion) and any product decisions (multi-turn conversation memory, etc.) beyond what's listed above.

## How it works

1. **`knowledge/*.txt`** — the source of truth. Q&A-style files use `Q:`/`A:` lines; other files are plain paragraphs (one blank-line-separated statement per idea).
2. **Chunking** splits each file into small pieces (one Q&A pair, or one paragraph), tagged with `source`/`category`/`title`.
3. **Embedding** turns each chunk into a 1536-dim vector via OpenAI `text-embedding-3-small`.
4. **Ingestion** upserts chunks into Qdrant. Point IDs are a hash of `(source, text)`, so re-running after editing `knowledge/` updates changed chunks and removes deleted ones — it never duplicates.
5. **Retrieval** embeds a user's question and asks Qdrant for the top-k most similar chunks (`retrieval_top_k`, live-configurable — see below).
6. **Generation** — GPT (`openai_chat_model`, default `gpt-4o-mini`) answers using only the retrieved chunks as context, capped at `max_answer_tokens`. Instructed to paraphrase/infer from context but never use outside knowledge, and to reply with a fixed refusal string when the context has nothing relevant. A refused question is logged for review (see **Missed questions**, below).
7. **`/chat` endpoint** — `POST /chat` exposes the pipeline over HTTP for Laravel to call. Validates the question, rate-limits per user, and falls back to a friendly error message if OpenAI/Qdrant fail — see below for the full contract.

## Setup

```bash
py -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # then fill in OPENAI_API_KEY at minimum
```

Qdrant (local, via Docker):

```bash
docker run -d --name magicard-qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest
```

## Commands

Run all of these from the project root with the venv activated (`.venv\Scripts\activate`).

| Command | What it does |
|---|---|
| `uvicorn app.main:app --reload` | Start the API server (http://127.0.0.1:8000, docs at `/docs`) |
| `python -m scripts.ingest` | **Sync the knowledge base into Qdrant.** Chunks + embeds + upserts everything in `knowledge/`. Run this every time you add or edit a file in `knowledge/` — it updates changed chunks and prunes removed ones, safe to re-run anytime. Must be run as a module (`-m scripts.ingest`), not `python scripts/ingest.py` — the latter fails with `ModuleNotFoundError: No module named 'app'`. |
| `python -m app.knowledge_processing.chunker` | Preview how `knowledge/*.txt` gets split into chunks, without touching Qdrant or OpenAI. Useful when writing a new doc, to sanity-check the split before ingesting. |
| `python -m tests.test_retrieval` | Runs the golden eval question set (`tests/eval_questions.py`) against Qdrant and reports which questions retrieved a chunk from the expected source file. Run after ingesting, or after editing `knowledge/`, to confirm retrieval quality didn't regress. |
| `python -m tests.test_chat` | Runs the same golden question set through the full pipeline (retrieve + GPT generation) and reports which answers look right — in-scope questions should get a real (non-refusal) answer, and the out-of-scope question should get the exact refusal string. Run after touching `app/services/generation.py` or the prompt, or after ingesting. |
| `python -m tests.test_api` | Offline tests for `/chat` and `/admin` — validation, rate limiting, error fallback, missed-question logging, admin auth — all with OpenAI/Qdrant mocked out. No API keys or live services needed; safe to run anytime, e.g. in CI. |

## Project layout

```
app/
  main.py                    FastAPI app, mounts the chat + admin routers
  config.py                  Settings (reads .env) — API keys, secrets, fixed config
  runtime_config.json        live-editable settings (gitignored, created on first use)
  runtime_config.py          reads/writes runtime_config.json
  knowledge_processing/      chunker.py
  services/                  embeddings.py, qdrant_client.py, retrieval.py, generation.py,
                              rate_limit.py, miss_log.py
  api/                       chat.py (POST /chat), admin.py (GET /admin, POST /admin/config)
knowledge/                   source .txt docs — edit these, then run scripts/ingest.py
scripts/ingest.py            chunk -> embed -> upsert into Qdrant (idempotent)
logs/missed_questions.jsonl  questions the bot couldn't answer (gitignored, created on first miss)
tests/
  eval_questions.py          golden question set, shared by both eval scripts below
  test_retrieval.py          runs the eval set against live Qdrant (retrieval only)
  test_chat.py               runs the eval set through retrieval + generation (full pipeline)
  test_api.py                offline tests for /chat and /admin (validation, rate limit, auth)
```

## Updating the knowledge base

This is a **manual** routine — editing or adding a file in `knowledge/` does nothing on its own. Nothing watches the folder or runs automatically; you must run the ingest command yourself every time.

1. Edit/add a `.txt` file in `knowledge/`.
2. *(Optional)* `python -m app.knowledge_processing.chunker` — preview how it'll get split into chunks, without touching Qdrant or OpenAI.
3. `python -m scripts.ingest` — chunk, embed, and upsert into Qdrant. Idempotent: updates changed chunks, prunes removed ones, safe to re-run anytime.
4. `python -m tests.test_retrieval` — run the golden eval question set against live Qdrant to confirm retrieval quality didn't regress.
5. `python -m tests.test_chat` — run the same questions through GPT generation to confirm answers still read right. A retrieval regression usually shows up here too (as a wrong or refused answer), so this catches more than step 4 alone.

## Calling `/chat`

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Chat-Secret: <value of CHAT_SHARED_SECRET, if set>" \
  -H "X-User-Id: <end user's id>" \
  -d '{"question": "What is KYC?"}'
# -> {"answer": "...", "sources": ["kyc.txt"]}
```

Headers:
- **`X-Chat-Secret`** — only required if `CHAT_SHARED_SECRET` is non-empty in `.env`; leave it unset for local dev. Missing/wrong -> `401`.
- **`X-User-Id`** — **always required.** Identifies the end user for rate limiting; Laravel must forward its own user id here (this is server-to-server, so we can't see the real caller otherwise). Missing -> `400`.

What `/chat` checks, in order:
1. Shared secret (if configured) -> `401` on mismatch.
2. `X-User-Id` present -> `400` if missing.
3. Question non-empty and within `max_question_words` -> `400` if empty or too long.
4. Rate limit: one request per `rate_limit_seconds` per `X-User-Id` -> `429` (with a "try again in Ns" message) if too soon.
5. Retrieval + generation. If OpenAI or Qdrant errors out (timeout, rate limit, outage), `/chat` still returns `200` with a friendly fallback answer (`app.services.generation.FALLBACK_ERROR`) rather than a 500 — the error is logged server-side (see `logger.exception` in `app/api/chat.py`) but Laravel/the end user just sees a normal chat message, no special error-handling needed on Laravel's side.
6. If the model refuses (nothing relevant in the knowledge base), the question is recorded to the missed-questions log (below) so it can be reviewed and turned into new `knowledge/` content.

## Live config

Some values are meant to be tuned without a redeploy:

| Key | Default | What it controls |
|---|---|---|
| `max_question_words` | 20 | Longest question `/chat` will accept before returning `400`. |
| `max_answer_tokens` | 300 | Caps GPT's response length (`max_tokens` on the OpenAI call). |
| `rate_limit_seconds` | 10 | Minimum gap between requests from the same `X-User-Id`. |
| `retrieval_top_k` | 4 | How many chunks Qdrant returns per question. |
| `temperature` | 0.0 | GPT sampling temperature — 0 is deterministic/literal; raise it for more varied phrasing. |
| `missed_questions_rotate_at` | 200 | Once `logs/missed_questions.jsonl` reaches this many lines, it's archived and a fresh file starts (see **Missed questions**, below). |

These live in `runtime_config.json` at the project root (auto-created with defaults from `app/runtime_config.py` on first read; gitignored since it's runtime state, not source).

Change them live via the admin panel below, or by editing/creating `runtime_config.json` directly and restarting the server (defaults are only used for keys the file doesn't have — a hand-edited partial file is fine).

**Caveat:** this is a single JSON file with an in-process lock — fine for one `uvicorn` worker, but if this ever runs with multiple worker processes, writes from the admin panel won't propagate to the other workers until they happen to reread the file. Same caveat applies to rate limiting, which is a plain in-memory dict per process.

## Admin panel

`GET /admin` — a small HTML page (HTTP Basic auth, credentials from `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env`) showing:
- The current live config values, editable inline and saved via a form (`POST /admin/config`).
- The missed-questions log (most recent first, up to 200) — the easiest way to see what real users are asking that the bot can't answer, in production or locally.

Set `ADMIN_PASSWORD` in `.env` to enable it; leave it blank and the panel returns `503` (disabled by default so it's never accidentally exposed with no password).

```bash
open http://127.0.0.1:8000/admin   # browser will prompt for the Basic auth credentials
```

## Missed questions

Every question the model refuses to answer (its context had nothing relevant) is appended to `logs/missed_questions.jsonl` — one JSON object per line, `{"timestamp": ..., "question": ...}`, timestamped in Israel local time (`Asia/Jerusalem`, e.g. `2026-09-08T18:19:41+03:00` — date + time, no microseconds). This is the log to check periodically (via the admin panel, or `tail -f logs/missed_questions.jsonl` on the server) to find real gaps in `knowledge/` and turn them into new docs.

**Rotation:** once the current file reaches `missed_questions_rotate_at` lines (default 200), it's renamed to `logs/missed_questions_<UTC timestamp>.jsonl` and a fresh `missed_questions.jsonl` starts — so no single file grows unbounded. The admin panel only shows the *current* file; older ones stay in `logs/` and can be opened directly on the server (`ls logs/`) if you need to look further back.
