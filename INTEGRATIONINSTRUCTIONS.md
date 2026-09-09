# Integrating the Magicard Chatbot into Laravel

This document is for the Laravel app that will call this chatbot API. It covers what to send, what
you'll get back, what can go wrong, and what to know before wiring it up. It assumes the chatbot API
is already deployed and reachable at some base URL (e.g. `https://chatbot.internal.magicard.com`).

The chatbot itself is a **stateless HTTP API** — Laravel calls it server-to-server for every message,
there's no session/socket to maintain, and no SDK to install. Plain HTTP works fine.

## 1. The one endpoint you need: `POST /chat`

### Request

```
POST /chat
Content-Type: application/json
X-User-Id: <string, required>

{"question": "What is KYC?"}
```

**Headers**
| Header | Required? | Purpose |
|---|---|---|
| `X-User-Id` | **Always** | Identifies the end user for per-user rate limiting. Since this is a server-to-server call, the chatbot has no other way to tell users apart — Laravel must forward its own authenticated user id (or a stable guest/session id for anonymous users). Missing → `400`. |
| `X-Chat-Secret` | Only if configured | A shared secret so random clients on the internet can't hit `/chat` directly. Ask whoever deployed the chatbot whether `CHAT_SHARED_SECRET` is set in its `.env`; if so, Laravel must send the same value on every request. Wrong/missing when required → `401`. Store it in Laravel's own `.env`, never hardcode it. |
| `Content-Type` | Yes | Must be `application/json`. |

**Body**
| Field | Type | Notes |
|---|---|---|
| `question` | string | The user's message, as typed. Leading/trailing whitespace is trimmed server-side. Empty (or whitespace-only) after trimming → `400`. |

### Response — success (`200`)

```json
{
  "answer": "KYC (Know Your Customer) is a verification process...",
  "sources": ["kyc.txt", "account_security.txt"]
}
```

- `answer` — plain text (not markdown/HTML), safe to display directly. May sometimes be the model's
  built-in refusal string ("I don't have information about that...") when nothing relevant was found —
  this is a normal `200`, not an error; see **Handling "I don't know" answers** below.
- `sources` — which knowledge-base files the answer was grounded in. Useful for debugging/logging;
  most UIs won't show this to end users, but it's handy if you want a "was this helpful?" flow tied to
  specific docs later.

Note: **every failure downstream of validation still returns `200`** with a friendly fallback message
(`"Sorry, I'm having trouble answering right now. Please try again shortly."`) instead of a `500` —
see **Error philosophy** below. Laravel does not need special handling for OpenAI/Qdrant outages; just
display `answer` as-is.

## 2. Possible inputs — restrictions to enforce or expect

| Constraint | Default | Where enforced | What happens if violated |
|---|---|---|---|
| Question max length | 20 words | Server-side (`max_question_words`, live-configurable) | `400 {"detail": "question is too long (N words, max 20)"}` |
| Question non-empty | — | Server-side | `400 {"detail": "question must not be empty"}` |
| Rate limit | 1 request per 10s per `X-User-Id` | Server-side (`rate_limit_seconds`, live-configurable) | `429 {"detail": "Too many requests. Try again in N seconds."}` (N is a whole number) |
| `X-User-Id` present | — | Server-side | `400 {"detail": "X-User-Id header is required"}` |
| `X-Chat-Secret` correct | — (only if configured) | Server-side | `401 {"detail": "Invalid or missing shared secret"}` |

**Recommendation for Laravel's own UI layer:** enforce the word limit and rate limit client-side too
(disable the send button, show a live counter) so users get instant feedback instead of round-tripping
to find out — but always treat the server's response as the source of truth, since the limits are
live-editable on the chatbot side and could change without a Laravel deploy. Don't hardcode "20 words"
or "10 seconds" anywhere in Laravel; if you want to display them, consider exposing them from your own
config rather than assuming they never change (there's currently no `/chat` endpoint that reports the
live limits back — ask if you need one, it'd be a small addition).

**Conversation memory now exists — no Laravel changes needed.** The chatbot keeps a rolling
conversation per `X-User-Id` server-side (in memory), so a follow-up like "what about for businesses?"
is automatically understood in context of the prior question/answer — Laravel does **not** need to
prepend history into `question` itself; just keep sending the same `X-User-Id` for the same user across
their session, exactly as already required for rate limiting. A conversation resets automatically after
~30 minutes of inactivity for that user (live-configurable server-side), so a returning user later just
starts fresh rather than dragging in stale context. This is transparent to the request/response
contract above — nothing changes shape, answers may just be more contextually aware.

**Semantic answer cache — also transparent to Laravel.** Repeated or near-identical standalone
questions (e.g. many different users asking "What is KYC?") may be served from a cache instead of
re-running the full AI pipeline — same request/response shape, just potentially faster. No action
needed on Laravel's side; mentioned here only so a "why was that answer instant" question has an
answer.

## 3. Possible outputs — what your UI needs to handle

| Status | Meaning | Laravel should... |
|---|---|---|
| `200` with a real answer | Success | Display `answer`. |
| `200` with the refusal string (`"I don't have information about that in the Magicard knowledge base."`) | The bot found nothing relevant — this is still `200`, not an error | Display it like a normal message (it's plain English), or optionally detect this exact string and show a custom "want to talk to a human?" CTA instead. The question was automatically logged server-side for the ops team to review, so no action needed from Laravel beyond the UI decision. |
| `200` with the fallback error string (`"Sorry, I'm having trouble answering right now. Please try again shortly."`) | OpenAI or Qdrant failed upstream (timeout, outage, quota) | Display it like a normal message — it already reads as a graceful apology. Optionally detect this exact string to trigger a "retry" button, since it may be transient. |
| `400` | Bad request (empty question, question too long, missing `X-User-Id`) | These indicate a bug in Laravel's request-building (should be prevented client-side per section 2), not something to show the end user verbatim — surface a generic "something went wrong" and log the `detail` for debugging. |
| `401` | Missing/wrong `X-Chat-Secret` | Configuration problem, not a user-facing case — should never happen in production if the secret is set correctly. Alert/log loudly if seen. |
| `429` | Rate limited | Show `detail` directly to the user (it's already phrased for end users, e.g. "Too many requests. Try again in 5 seconds.") or build your own UI around it (e.g. disable the send button for that many seconds). |
| Network error / timeout / non-JSON response | The chatbot service itself is unreachable | Not something the chatbot API can help with — this is Laravel needing its own HTTP client timeout + retry/circuit-breaker handling. Recommend a request timeout of ~15-20s (OpenAI generation can occasionally be slow) and treating a timeout the same as a `200` fallback-error case in the UI. |

Two distinct "I can't help" strings exist (**refusal** vs **fallback error**) — they mean different
things (no matching knowledge vs. an infrastructure failure) and it's worth keeping them visually or
behaviorally distinct if you build anything smarter than "just print `answer`".

## 4. Error philosophy — why this matters for Laravel's design

By design, `/chat` only returns non-`200` for things **Laravel itself** got wrong (bad input, missing
auth, rate limit). Anything that goes wrong on the AI/infra side (OpenAI down, Qdrant unreachable,
model timeout) is caught server-side and turned into a normal `200` response with an apologetic
message — see `app/api/chat.py`. This means:

- Laravel's happy-path code only needs to branch on `answer`/`sources`, never parse error bodies for
  the common failure cases.
- A `4xx`/`5xx`/network-level failure from `/chat` is *always* worth logging/alerting on Laravel's
  side, since it means something is broken in the integration itself (wrong secret, malformed request,
  or the chatbot service is fully down) — not a routine "AI had a bad day."

## 5. Example Laravel integration (Guzzle)

```php
$response = Http::withHeaders([
    'X-User-Id' => (string) auth()->id() ?? session()->getId(),
    'X-Chat-Secret' => config('services.magicard_chatbot.secret'),
])->timeout(20)->post(config('services.magicard_chatbot.url') . '/chat', [
    'question' => $userMessage,
]);

if ($response->status() === 429) {
    return response()->json(['error' => $response->json('detail')], 429);
}

if (!$response->successful()) {
    Log::error('Magicard chatbot request failed', ['status' => $response->status(), 'body' => $response->body()]);
    return response()->json(['answer' => 'Sorry, something went wrong.'], 200);
}

return response()->json([
    'answer' => $response->json('answer'),
    'sources' => $response->json('sources'),
]);
```

Add to Laravel's `config/services.php` / `.env`:
```
MAGICARD_CHATBOT_URL=https://chatbot.internal.magicard.com
MAGICARD_CHATBOT_SECRET=   # only if the chatbot's CHAT_SHARED_SECRET is set — get this from whoever deployed it
```

## 6. Operational notes worth knowing before going live

- **Health check**: `GET /health` → `{"status": "ok"}`, no auth required. Point your load balancer /
  uptime monitor at this, not `/chat` (which requires headers and would burn rate-limit/API quota).
- **Single-worker assumption**: rate limiting, conversation memory, and the live-editable config are
  all in-memory/single-file and **not safe across multiple `uvicorn` worker processes** — if the
  chatbot is ever scaled to multiple workers or instances, a user's rate limit or conversation context
  could reset unexpectedly (each worker tracks its own state) and admin config edits may not propagate
  to all workers immediately. This is documented and known; ask before assuming these are perfectly
  enforced under horizontal scaling. The semantic answer cache and the saved conversation log are the
  exceptions — they live in Qdrant / a shared file respectively, so they're consistent across workers.
- **Startup requires API keys**: the chatbot server refuses to start if `OPENAI_API_KEY` is unset, or
  if `QDRANT_API_KEY` is unset while `QDRANT_URL` doesn't look like a local instance — so if the
  chatbot service is down, check its own logs/env first before assuming it's a Laravel-side problem.
- **Admin panel** (`GET /admin`, HTTP Basic auth) is a full operator dashboard — not something Laravel
  integrates with, but worth bookmarking for whoever owns the Magicard knowledge base or needs to
  investigate a chat issue. It has:
  - Every live-tunable setting (rate limits, question length, retrieval breadth, conversation memory
    limits, cache threshold, etc.), editable with no redeploy.
  - **Knowledge Gaps** — questions the bot couldn't answer, so "the bot won't answer X" reports have a
    clear next step (add it to `knowledge/`, ask the maintainer to re-ingest).
  - **Cached Answers** and **Conversations** — browsable records of cached answers and real chat
    exchanges (searchable by `X-User-Id` — handy if Laravel reports "user X got a weird answer" and
    someone needs to see that user's actual thread).
  - A built-in **Test Chatbot** widget to manually try `/chat` (with custom headers) without needing
    Postman — useful for reproducing a reported issue quickly.
  - One-click resets to clear conversation memory or the answer cache fleet-wide, e.g. right after a
    knowledge update.
- **No streaming.** `/chat` returns the full answer in one response — there's no token-by-token
  streaming endpoint today. If a "typing" / streaming UI is wanted, that would be a chatbot-side
  addition (Server-Sent Events or chunked response), not something Laravel can get around on its own.
- **No file/image support.** The API only accepts a plain text `question` — no attachments, no rich
  content in requests or responses.
