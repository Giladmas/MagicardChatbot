# Magicard AI Chatbot

RAG-based support chatbot for Magicard. FastAPI + Qdrant + OpenAI, called by Laravel over HTTP.

## Current status

What's built and working today:

- **Knowledge ingestion** — `knowledge/*.txt` → chunk → embed → upsert into Qdrant Cloud. Idempotent, safe to re-run.
- **Retrieval** — Qdrant vector search for the top-k most relevant chunks per question.
- **Generation** — GPT answers using only retrieved context; refuses (fixed string) when nothing relevant is found.
- **`POST /chat`** — validated (question length, required headers), rate-limited per user, graceful fallback answer if OpenAI/Qdrant fail (never a raw 500).
- **Conversation memory** — recent turns are kept per `X-User-Id` and replayed to GPT (and folded into retrieval) so follow-up questions work; expires after `history_ttl_seconds` of inactivity so a returning user starts fresh.
- **Semantic answer cache** — standalone (first-turn) questions are checked against a Qdrant-backed cache of previously answered questions; a close-enough match (`cache_similarity_threshold`) skips retrieval and generation entirely. Cleared automatically on every knowledge re-ingest.
- **Conversation log** — every successful chat turn is saved to a browsable, admin-viewable record (`logs/conversations.jsonl`), separate from the live GPT-facing conversation memory above. Lets an operator review real exchanges, including a single user's full thread by searching their `X-User-Id`.
- **`GET /admin`** — Basic-auth protected page to view/edit live config and review missed questions, no redeploy needed.
- **Missed-question logging** — every refused question saved with an Israel-time timestamp, auto-rotated once the log grows past a threshold.
- **Tests** — offline (`test_api.py`, mocks OpenAI/Qdrant) and live (`test_retrieval.py`, `test_chat.py`) eval scripts.

## How it works

1. **`knowledge/*.txt`** — the source of truth. Q&A-style files use `Q:`/`A:` lines; other files are plain paragraphs (one blank-line-separated statement per idea).
2. **Chunking** splits each file into small pieces (one Q&A pair, or one paragraph), tagged with `source`/`category`/`title`.
3. **Embedding** turns each chunk into a 1536-dim vector via OpenAI `text-embedding-3-small`.
4. **Ingestion** upserts chunks into Qdrant. Point IDs are a hash of `(source, text)`, so re-running after editing `knowledge/` updates changed chunks and removes deleted ones — it never duplicates.
5. **Retrieval** embeds a user's question (folded together with the previous question, if any, so referent-less follow-ups like "what about the fee?" still retrieve the right chunks) and asks Qdrant for the top-k most similar chunks (`retrieval_top_k`, live-configurable — see below).
6. **Generation** — GPT (`openai_chat_model`, default `gpt-4o-mini`) answers using the retrieved chunks plus any recent conversation turns as context, capped at `max_answer_tokens`. Instructed to paraphrase/infer from context but never use outside knowledge, and to reply with a fixed refusal string when the context has nothing relevant. A refused question is logged for review (see **Missed questions**, below).
7. **`/chat` endpoint** — `POST /chat` exposes the pipeline over HTTP for Laravel to call. Validates the question, rate-limits per user, checks the answer cache and conversation history, and falls back to a friendly error message if OpenAI/Qdrant fail — see below for the full contract.

## Conversation memory

Each `X-User-Id` gets its own rolling conversation, kept in memory on the server (no change needed on Laravel's side — it already sends `X-User-Id` on every call):

- Every answered question/answer pair is appended to that user's history, capped at `history_max_turns` (oldest dropped first).
- Prior turns are replayed to GPT as plain conversation messages (not re-injecting their retrieved chunks) so follow-ups are answered with context.
- A conversation expires after `history_ttl_seconds` of inactivity — a user returning after a long gap starts fresh rather than dragging in stale context.
- Turn off entirely with `history_enabled` if needed.
- Wipe every user's conversation at once from the admin panel's "Reset actions" section (e.g. after a knowledge update that changes how something should be answered).
- This is **live and ephemeral** — it's what the AI actually uses to answer follow-ups, kept in memory, gone on restart. For a saved, browsable record of past exchanges, see **Conversation log**, below — a different, persisted feature.

## Conversation log

Separate from the live memory above: every successful chat turn (a real answer, a cache hit, or a refusal — but never an infra-failure fallback) is also appended to `logs/conversations.jsonl`, purely for an operator to review what people are actually asking and getting:

- One JSON line per turn: `{"id": ..., "timestamp": ..., "user_id": ..., "question": ..., "answer": ...}`.
- Browsable from the admin panel's "Conversations" section — the 5 most recent across all users, with **Delete**, **Clear all**, and **View all (N)** the same way as Knowledge Gaps.
- On the full "View all" page, search matches user id too, so typing a specific `X-User-Id` into the search box shows just that person's thread in order.
- Rotates once it reaches `conversations_rotate_at` lines (default 1000), same scheme as the knowledge-gaps log.
- Turn off entirely with `conversation_log_enabled` if you don't want chat content persisted to disk. This is fully independent of `history_enabled` — you can keep live GPT memory on while turning off the saved log, or vice versa.
- Clearing this log has **no effect** on live conversations — it's a read-only audit trail, not what GPT reads from.

## Semantic answer cache

Many users ask the same or near-identical standalone questions (e.g. "What is KYC?"). To avoid re-running retrieval + generation every time:

- On the **first turn of a conversation** (no history yet), the question's embedding is checked against a Qdrant collection of previously answered questions.
- A match scoring at or above `cache_similarity_threshold` (cosine similarity) returns the cached answer directly — no OpenAI generation call, no retrieval.
- A cache miss runs the normal pipeline, then stores the fresh answer for next time. Refusals are never cached, since a refusal cached under one phrasing shouldn't block a differently-phrased question that might retrieve something useful.
- The cache is cleared automatically every time `python -m scripts.ingest` runs, since cached answers are only valid for the knowledge content they were generated from.
- Turn off entirely with `cache_enabled` if needed.
- Browse, delete individual entries, or clear the whole cache from the admin panel's "Cached Answers" section — useful when a cached answer turns out wrong or stale before the next re-ingest.

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
                              rate_limit.py, miss_log.py, conversation_history.py, answer_cache.py,
                              conversation_log.py
  api/                       chat.py (POST /chat), admin.py (GET /admin, POST /admin/config)
knowledge/                   source .txt docs — edit these, then run scripts/ingest.py
scripts/ingest.py            chunk -> embed -> upsert into Qdrant (idempotent)
logs/missed_questions.jsonl  questions the bot couldn't answer (gitignored, created on first miss)
logs/conversations.jsonl     saved record of chat turns for admin review (gitignored, created on first chat)
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
3. `python -m scripts.ingest` — chunk, embed, and upsert into Qdrant. Idempotent: updates changed chunks, prunes removed ones, safe to re-run anytime. Also clears the semantic answer cache, since cached answers are only valid for the knowledge content they were generated from.
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
| `knowledge_gaps_rotate_at` | 200 | Once `logs/missed_questions.jsonl` reaches this many lines, it's archived and a fresh file starts (see **Knowledge Gaps**, below). |
| `history_enabled` | true | Turns conversation memory on/off entirely. |
| `history_max_turns` | 6 | Max prior turns replayed to GPT per user; oldest dropped first. |
| `history_ttl_seconds` | 1800 | Inactivity window before a user's conversation resets (30 min). |
| `cache_enabled` | true | Turns the semantic answer cache on/off entirely. |
| `cache_similarity_threshold` | 0.95 | Minimum cosine similarity for a cache hit — lower catches more paraphrases but risks a wrong-answer match; raise it if you see bad cache hits. |
| `conversation_log_enabled` | true | Turns the saved, admin-viewable conversation log on/off entirely (independent of `history_enabled`). |
| `conversations_rotate_at` | 1000 | Once `logs/conversations.jsonl` reaches this many lines, it's archived and a fresh file starts. |

These live in `runtime_config.json` at the project root (auto-created with defaults from `app/runtime_config.py` on first read; gitignored since it's runtime state, not source).

Change them live via the admin panel below (bool settings render as a checkbox, everything else as a text field), or by editing/creating `runtime_config.json` directly and restarting the server (defaults are only used for keys the file doesn't have — a hand-edited partial file is fine).

**Caveat:** this is a single JSON file with an in-process lock — fine for one `uvicorn` worker, but if this ever runs with multiple worker processes, writes from the admin panel won't propagate to the other workers until they happen to reread the file. Same caveat applies to rate limiting and conversation history, which are plain in-memory dicts per process (`app/services/rate_limit.py`, `app/services/conversation_history.py`) — a user's follow-up could land on a different worker and see no history. The answer cache doesn't have this problem since it lives in Qdrant, shared across workers.

## Admin panel

`GET /admin` — an HTML page (HTTP Basic auth, credentials from `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env`), built for a non-technical operator to run day-to-day without touching code:
- **Guide** — a "Guide" button at the top-left opens a slide-out panel with a plain step-by-step walkthrough of what to check on this page and in what order (try the chatbot → check Knowledge Gaps → skim Cached Answers → review Conversations → adjust settings carefully → use Reset actions with care). Click the button again (or the ✕, or click outside the panel) to close it. Meant for someone who's never seen this page before — e.g. handing `/admin` access to a new tester or client.
- **Test Chatbot** — a built-in chat widget at the top of the page that sends real `POST /chat` requests from the browser, with editable `X-User-Id` and `X-Chat-Secret` fields — a quick way to try the bot (including multi-turn follow-ups) without Postman or curl. A random `X-User-Id` is filled in automatically so conversation memory can be tested immediately; change it to simulate a different user.
- **Live configuration** — every tunable in the table above, editable inline (toggles for on/off settings, text fields for everything else) and saved via one "Save changes" button, with a one-time confirmation banner on success (a page refresh afterwards won't re-show it or re-submit the save — see **Note on the confirmation banners**, below). Each setting shows a plain-English unit hint, and any `*_seconds` value shows its live equivalent in minutes/hours so nobody mistakes seconds for minutes.
- **Knowledge Gaps** (renamed from "missed questions" for clarity) — the 5 most recent questions the bot couldn't answer, each with its own **Delete** button, plus a **Clear all** and a **View all (N)** link. "View all" opens a dedicated full-list page (in a new tab) with every entry and a live search box that filters as you type — useful once the list grows past a handful of items.
- **Cached Answers** — the 5 most recently cached entries (question, answer, when it was cached), each with its own **Delete**, plus **Clear all** and **View all (N)** the same way as Knowledge Gaps.
- **Conversations** — the 5 most recent saved chat turns (when, user id, question, answer), each with its own **Delete**, plus **Clear all** and **View all (N)**. On the full-list page, search matches the user id too — type someone's `X-User-Id` to see their whole thread.
- **Reset actions** — one-click **Clear all conversation history** (forgets every in-progress conversation; next message from anyone just starts fresh) and **Clear entire cache** (same effect as re-ingesting: every question gets a fresh answer until re-cached).
- **Dictionary** — a sidebar on the right-hand side of the page with plain-English explanations of every term used on the page (Knowledge Gap, Conversation Memory, Conversation Log, Semantic Cache, Similarity Threshold, Rate Limit, Retrieval/Chunks, Temperature, Tokens, TTL), so a client who doesn't read code can use the panel confidently.

Set `ADMIN_PASSWORD` in `.env` to enable it; leave it blank and the panel returns `503` (disabled by default so it's never accidentally exposed with no password).

**Note on the confirmation banners:** every action (save, delete, clear) redirects back to `GET /admin` afterwards instead of re-rendering the result page directly (the "Post/Redirect/Get" pattern). This means refreshing the page after an action just reloads the plain admin page — it won't re-show a stale "Saved." banner or accidentally resubmit the action a second time, which is what a direct re-render would do on a browser refresh.

```bash
open http://127.0.0.1:8000/admin   # browser will prompt for the Basic auth credentials
```

## Knowledge Gaps (missed questions)

Every question the model refuses to answer (its context had nothing relevant) is appended to `logs/missed_questions.jsonl` — one JSON object per line, `{"id": ..., "timestamp": ..., "question": ...}`, timestamped in Israel local time (`Asia/Jerusalem`, e.g. `2026-09-08T18:19:41+03:00` internally, shown on the admin page as `2026-09-08, 18:19:41`). This is the log to check periodically (via the admin panel's "Knowledge Gaps" section, or `tail -f logs/missed_questions.jsonl` on the server) to find real gaps in `knowledge/` and turn them into new docs. Once you've handled one (added the info to `knowledge/`, or decided it's out of scope), delete it from the admin page — or clear the whole list with "Clear all".

**Rotation:** once the current file reaches `knowledge_gaps_rotate_at` lines (default 200), it's renamed to `logs/missed_questions_<UTC timestamp>.jsonl` and a fresh `missed_questions.jsonl` starts — so no single file grows unbounded (the underlying filename stays `missed_questions.jsonl` — only the admin-facing config key and UI label were renamed). The admin panel only shows the *current* file; older ones stay in `logs/` and can be opened directly on the server (`ls logs/`) if you need to look further back.
