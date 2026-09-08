# Magicard AI Chatbot

RAG-based support chatbot for Magicard. FastAPI + Qdrant + OpenAI, called by Laravel over HTTP.

## How it works

1. **`knowledge/*.txt`** — the source of truth. Q&A-style files use `Q:`/`A:` lines; other files are plain paragraphs (one blank-line-separated statement per idea).
2. **Chunking** splits each file into small pieces (one Q&A pair, or one paragraph), tagged with `source`/`category`/`title`.
3. **Embedding** turns each chunk into a 1536-dim vector via OpenAI `text-embedding-3-small`.
4. **Ingestion** upserts chunks into Qdrant. Point IDs are a hash of `(source, text)`, so re-running after editing `knowledge/` updates changed chunks and removes deleted ones — it never duplicates.
5. **Retrieval** embeds a user's question and asks Qdrant for the top-k most similar chunks.
6. **Generation** *(not yet built)* — GPT answers using only the retrieved chunks as context.
7. **`/chat` endpoint** *(not yet built)* — exposes the pipeline over HTTP for Laravel to call.

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
| `python scripts/ingest.py` | **Sync the knowledge base into Qdrant.** Chunks + embeds + upserts everything in `knowledge/`. Run this every time you add or edit a file in `knowledge/` — it updates changed chunks and prunes removed ones, safe to re-run anytime. |
| `python -m app.knowledge_processing.chunker` | Preview how `knowledge/*.txt` gets split into chunks, without touching Qdrant or OpenAI. Useful when writing a new doc, to sanity-check the split before ingesting. |
| `python -m tests.test_retrieval` | Runs the golden eval question set (`tests/eval_questions.py`) against Qdrant and reports which questions retrieved a chunk from the expected source file. Run after ingesting, or after editing `knowledge/`, to confirm retrieval quality didn't regress. |

## Project layout

```
app/
  main.py                    FastAPI app
  config.py                  Settings (reads .env)
  knowledge_processing/      chunker.py
  services/                  embeddings.py, qdrant_client.py, retrieval.py
  api/                       (chat endpoint — WIP)
knowledge/                   source .txt docs — edit these, then run scripts/ingest.py
scripts/ingest.py            chunk -> embed -> upsert into Qdrant (idempotent)
tests/
  eval_questions.py          golden question set for retrieval testing
  test_retrieval.py          runs the eval set against live Qdrant
```

## Updating the knowledge base

1. Edit/add a `.txt` file in `knowledge/`.
2. (Optional) `python -m app.knowledge_processing.chunker` to preview the split.
3. `python scripts/ingest.py` to sync it into Qdrant.
4. `python -m tests.test_retrieval` to confirm nothing regressed.
