# Error-Context Integration (Laravel → Chatbot)

This documents a small, additive change to `POST /chat` that lets Magicard (Laravel)
tell the chatbot about a specific failure a user just experienced (e.g. a failed card
creation), so the bot can explain *that* instead of only answering from the static
knowledge base. **Already implemented in this repo** — this file is the reference to
hand to anyone working on the chatbot project so they understand what changed and why.

## What changed

`POST /chat` now accepts an optional `context` object on the request body, alongside
the existing `question`:

```json
{
  "question": "why did my card creation fail?",
  "context": {
    "provider": "sudo",
    "error_code": "400",
    "message": "The customer doesn't create properly,Contact with owner",
    "created_at": "2026-09-16T14:32:05+00:00"
  }
}
```

- `context` is **fully optional**. Omitting it (or any field inside it) behaves
  exactly as before — nothing about existing behavior changes for a plain question.
- All four fields inside `context` are optional strings; send whichever you have.
- This is a one-shot, per-request field — it is **not** stored as part of the ongoing
  conversation memory. Laravel is expected to decide per-request whether to include it
  (see "Where this comes from" below).

## Where this comes from (Laravel side)

Laravel now has a new `error_logs` table that records structured failures from card
creation (currently wired for the Sudo payment gateway only). Before calling
`POST /chat`, Laravel's `ChatbotService::ask()` looks up the calling user's most recent
error (within a configurable window, default 30 minutes) and — if one exists — attaches
it as `context`. If the user has no recent error, `context` is omitted entirely and the
request looks exactly like it always has.

This repo has **no knowledge of Laravel's database** and never will — Laravel does the
lookup and hands over a flat, pre-shaped object. The chatbot's job is purely to use
whatever `context` it's given, or ignore it if absent.

## What the chatbot does with it

- `app/api/chat.py` — `ChatRequest` gained an optional `context: ChatContext | None`
  field (`ChatContext` = `provider`, `error_code`, `message`, `created_at`, all
  `str | None`). It's passed through to `generate_answer(...)` as `error_context`.
- `app/services/generation.py` — a `_build_error_context()` helper formats the
  context into a `"User's recent error:\n..."` block, appended to the user message
  alongside the retrieved knowledge chunks (not injected into the system prompt, since
  it varies per request). `SYSTEM_PROMPT` instructs the model that when that block is
  present, the answer must be exactly two clauses — a short plain-language reason with
  **no technical details from the block** (no provider name, HTTP status code, error
  code, "Bad Request", timestamp), followed by the matching knowledge chunk's guidance
  copied almost verbatim; the existing refusal rule still applies if *neither* has
  anything relevant, and always wins even over that two-clause format.
- These answers also use a tighter token cap (`ERROR_CONTEXT_MAX_TOKENS = 80` vs. the
  general `max_answer_tokens` default of 300) as a structural backstop, since prompting
  alone didn't reliably keep GPT from padding the answer with filler.

No changes were made to embeddings, conversation history, or missed-question logging -
this remains additive to prompt-building. `REFUSAL` and `FALLBACK_ERROR` semantics are
unchanged (`REFUSAL` is additionally enforced by a hard post-processing check in
`generate_answer()`, so it's always returned byte-for-byte even if the model tries to
prepend something to it).

## Sudo error knowledge (fixed)

`knowledge/sudo_errors.txt` now holds a Q&A entry per Sudo route (derived from
`sudo-error-response.json`) — purpose, failure behavior, and a user-facing
`user_solution` — ingested into Qdrant like any other knowledge file via
`scripts/ingest.py`. There's no reliable join key between Laravel's `context`
(provider/error_code/message) and a specific Sudo route, so this isn't an exact-match
lookup: `app/services/retrieval.py`'s `retrieve()` takes an `extra_context` param,
and `app/api/chat.py` passes `body.context.message` into it whenever context is
present, folding the error message into the embedding search alongside the question
so the right `sudo_errors.txt` chunk surfaces via normal semantic retrieval.

## Cache/context fix (previously a known limitation)

The semantic answer cache used to key purely on the question's text/embedding, not on
`context` — a question answered using one user's error context could have been cached
and served to a different user asking something semantically similar without their
own context attached. This is now fixed: `app/api/chat.py` skips both the cache
lookup and the cache store whenever `body.context` is present (`and not body.context`
alongside the existing `not history` check), so context-grounded answers are never
cached or reused across users.

## Testing this locally

Send a request with `context` manually and confirm the answer references it:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-User-Id: test-user-1" \
  -d '{
    "question": "why did my card creation fail?",
    "context": {
      "provider": "sudo",
      "error_code": "400",
      "message": "The customer does not create properly, contact with owner",
      "created_at": "2026-09-16T14:32:05+00:00"
    }
  }'
```

Expected: a short (one or two sentence), non-technical answer that speaks to the
specific failure - no mention of "Sudo", "400", "Bad Request", or other raw details
from `context` - not a generic knowledge-base answer. A request with the same
question but no `context` should still work exactly as before (grounded in
`knowledge/*.txt`, or the refusal string if nothing relevant exists there).

## Scope note

Today only the Sudo payment gateway writes to Laravel's `error_logs` table, so
`context.provider` will currently only ever be `"sudo"`. Nothing on this side needs to
change when Laravel adds Stripe/Strowallet/CardyFie later — `context` is already
provider-agnostic (a free-form `provider` string), so those will just start showing up
the same way.

## Sudo Africa API — external endpoints MagicCard calls

This is the actual third-party API MagicCard talks to for card issuing (not MagicCard's
own routes — see the next section for those). Confirmed directly from
`app/Http/Helpers/sudo-card.php` and `app/Http/Controllers/AppMobile/MobileController.php`.

**Base URL** — stored per-environment in `virtual_card_apis.config.sudo_url` (DB-configured, not hardcoded):
- Production: `https://api.sudo.africa`
- Sandbox (seeder default): `https://api.sandbox.sudo.cards`

**Auth** — `Authorization: Bearer <sudo_api_key>` header on every call (key also DB-configured, same `virtual_card_apis.config`, IP-whitelisted to the 3 production servers only).

| Method | Path | Helper function | Purpose |
|---|---|---|---|
| POST | `/fundingsources` | `funding_source_create()` | Create a "default" type funding source |
| GET | `/fundingsources` | `get_funding_source()` | List funding sources |
| POST | `/accounts` | `create_sudo_account()` | Create a Sudo business account in a given currency |
| GET | `/accounts?type=account` | `get_sudo_accounts()` | List business accounts |
| GET | `/accounts/{id}/balance` | `getSudoBalance()` | Settlement account available balance |
| POST | `/accounts/transfer` | `sudoFundCard()`, `sudoWithdrawCard()` | Move funds settlement↔card account (funding a card / pulling balance back) |
| POST | `/customers` | `create_sudo_customer()` | Create a KYC'd customer (name, DOB, identity, billing address) |
| POST | `/cards` | `create_virtual_card()` | Mint a new virtual card for a customer |
| GET | `/cards/{id}` | `getSudoCard()` | Full card detail (status, PAN, expiry, 2FA flags, etc.) |
| PUT | `/cards/{id}` | `cardUpdate()` | Change card status (`active` / `inactive` / `canceled`) |
| GET | `/cards/{id}/token` | `getCardToken()` | Short-lived token used to reveal secure data |
| GET | `/cards/{id}/transactions` | `getCardTransactions()` | Paginated settled-transaction history |
| GET | `/cards/{id}/balance` | inline in `MobileController::sudoCardBalance()` | Live card balance (8s timeout, cached back to `sudo_virtual_cards.balance` on success) |
| GET | `/cards/{id}/authorizations?page=X&limit=100` | inline in `MobileController` (`/app/cards/{id}` tx view) | Auth/3DS attempt history (approved + declined), merged with settled transactions |
| GET | `/cards/{id}/secure-data/number`, `/secure-data/cvv2` | inline in `MobileController::cardRevealData()` | Real PAN / CVV reveal — requires the card-token Bearer from `/cards/{id}/token`, not the main API key |

**Inbound (Sudo calling us, not us calling Sudo):** `POST /sudo/webhook` on `ay.cards` only — Sudo delivers every event (transactions, Apple Pay OTP `authorization.code`, disputes) to one fixed URL, which fans out to `magicard.io/sudo/webhook` and `turqpay.com/sudo/webhook` if the card isn't local to ay.cards. See `app/Http/Controllers/Sudo/SudoWebhookController.php`.

**Not actually called from code today** (documented in README as an ops/support tinker snippet only): `PUT /customers/{id}` to fix a rejected customer's name/DOB/identity after a failed card creation — a support person runs this manually via `php artisan tinker`, the app itself never calls it automatically.

**USDT deposits are NOT a Sudo API call.** `SudoUsdtDepositController` uses one **fixed, hardcoded** Sudo-provided TRC20 address (`SudoUsdtDepositController::USDT_ADDRESS`, a constant in that file) — not a per-user address minted via the Sudo API. Verification of the user-submitted tx hash goes straight to **TronGrid** (`https://api.trongrid.io`), not Sudo, so a USDT deposit failure would never show up as a `sudo`-provider error in `error_logs` even if that flow gets instrumented later.

## Sudo card routes (user-facing) — where errors can be generated

For reference: these are the routes a logged-in Magicard user hits for Sudo virtual
card operations. All three surfaces funnel through the same underlying `cardBuy()`
logic that now writes to `error_logs` on failure, so a user asking "why did my card
creation fail?" could have come from any of them.

**Main web dashboard** (`routes/user.php`, prefix `/user/sudo-virtual-card`):
| Route | Purpose |
|---|---|
| `GET /user/sudo-virtual-card/` | List the user's cards |
| `GET /user/sudo-virtual-card/create` | Create-card form |
| `POST /user/sudo-virtual-card/create` | Submit card creation (`SudoVirtualCardController::cardBuy` — instrumented) |
| `GET /user/sudo-virtual-card/details/{card_id}` | Card detail view |
| `GET /user/sudo-virtual-card/transaction/{card_id}` | Card transaction history |
| `GET /user/sudo-virtual-card/fund/page/{id}` | Top-up form |
| `POST /user/sudo-virtual-card/fund` | Submit top-up |
| `PUT /user/sudo-virtual-card/change/status` | Block/unblock card |
| `POST /user/sudo-virtual-card/make/default/remove/default` | Toggle default card |

**Mobile PWA** (`routes/app-mobile.php`, prefix `/app`) — the primary surface most
users actually use day to day:
| Route | Purpose |
|---|---|
| `GET /app/cards` | Card list |
| `GET /app/cards/order` | Create-card form |
| `POST /app/cards/create` | Submit card creation (`MobileController::cardCreate` → internally calls the same instrumented `SudoVirtualCardController::cardBuy`) |
| `GET /app/cards/{id}` | Card detail |
| `GET /app/cards/{id}/reveal` | Reveal full PAN/CVV |
| `POST /app/cards/fund` | Top-up |
| `POST /app/cards/withdraw` | Pull card balance back to wallet |
| `POST /app/cards/cancel` | Cancel/terminate card |

**REST API** (`routes/api.php`, Passport-authenticated, prefix `/api/user/my-card/sudo`)
— used by a potential native mobile client, has its own separate controller
(`Api\User\SudoVirtualCardController`, also instrumented):
| Route | Purpose |
|---|---|
| `GET /api/user/my-card/sudo/` | List cards |
| `GET /api/user/my-card/sudo/charges` | Fee schedule |
| `GET /api/user/my-card/sudo/details` | Card detail |
| `POST /api/user/my-card/sudo/create` | Submit card creation (instrumented) |
| `POST /api/user/my-card/sudo/fund` | Submit top-up |
| `GET /api/user/my-card/sudo/transaction` | Transaction history |
| `POST /api/user/my-card/sudo/block` / `/unblock` | Freeze/unfreeze |
| `POST /api/user/my-card/sudo/make-remove/default` | Toggle default |

**USDT deposit** (funds the wallet that pays for card creation/top-up — a failure
here isn't logged to `error_logs` yet, only card-buy failures are):
| Route | Purpose |
|---|---|
| `GET /user/add-money/usdt-trc20` | Deposit address/QR page |
| `POST /user/add-money/usdt-trc20/submit` | Submit tx hash for on-chain verification |
