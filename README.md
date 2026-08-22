# Jo Bingo

Real-money multiplayer bingo on Telegram for the Ethiopian market. The full
product/architecture spec lives in [`idea.md`](idea.md) (see especially the
"Jo Bingo" sections starting at line 4776 — that's the authoritative
blueprint this repo follows; see [`DECISIONS.md`](DECISIONS.md) for why).

This repo is being built phase by phase. **Phase 0 (foundations + ledger),
Phase 1 (Telegram bot), Phase 2 (game engine), Phase 3 (realtime gateway),
and Phase 4 (Mini App) are done** — that's the full player-facing loop,
working end to end. What's left — payments, admin — is scaffolded as empty
packages under `services/` (see `DECISIONS.md` and the plan referenced
there for the full roadmap).

## Repository layout

```
services/bot/          Phase 1: Telegram bot (aiogram 3, webhook mode) -- DONE
services/gateway/      Phase 3: realtime WebSocket gateway (FastAPI) -- DONE
services/engine/       Phase 2: game engine / room state machine workers -- DONE
services/wallet/       Phase 1: wallet API surface over packages/core/ledger.py
services/payments/     Phase 5-6: deposit/withdrawal provider adapters
services/admin/        Phase 7: admin console API
packages/core/         Shared, framework-free domain logic (ledger, bingo, config, logging, redis, telegram auth)
web/miniapp/           Phase 4: Telegram Mini App (vanilla JS, no framework) -- DONE
migrations/            Alembic migrations (raw SQL, no ORM)
tests/unit/            Pure-function tests, no external dependencies
tests/integration/     Tests against real Postgres + Redis (docker-compose)
deploy/                docker-compose.yml and friends
```

## Running it locally

Requires Docker, Docker Compose, and Python 3.12.

```bash
# 1. Create a virtualenv and install dependencies
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2. Start Postgres and Redis
#    (mapped to host ports 5433 / 6380, not the defaults, in case another
#    project on the same machine is already using 5432 / 6379)
docker compose -f deploy/docker-compose.yml up -d postgres redis

# 3. Apply migrations
.venv/bin/alembic -c migrations/alembic.ini upgrade head

# 4. Type-check and run the tests
.venv/bin/mypy
.venv/bin/pytest tests/ -v

# 5. Real-scale gateway measurement (1000 concurrent sockets), run standalone
#    for a clean reading -- see DECISIONS.md on why this is excluded by default
.venv/bin/pytest tests/ -m load -v -s

# 6. Real-browser Mini App tests (Playwright/Chromium) -- also excluded by
#    default; install a browser once, then run explicitly
.venv/bin/playwright install chromium
.venv/bin/pytest tests/ -m e2e -v -s
```

To actually play with it: start the gateway (`uvicorn services.gateway.app:app
--reload`, from the repo root) and open `http://localhost:8000/` -- the
gateway serves the Mini App's static files itself. Outside a real Telegram
client `window.Telegram.WebApp` won't exist, so the app has nothing to
authenticate with; the E2E tests stub it (see
`tests/integration/test_miniapp_e2e.py`) the same way a real integration
would supply real `initData`.

Copy `.env.example` to `.env` if you want to override any connection
settings; everything works fine with the defaults baked into
`packages/core/config.py`, which already point at the docker-compose ports
above.

## What's actually implemented

**Foundations (Phase 0):**
- **`packages/core/ledger.py`** — the double-entry ledger. Every balance
  change is one or more signed `ledger_entries` rows written inside a
  database transaction; Postgres itself (via a deferred constraint trigger)
  rejects any transaction whose entries don't sum to zero. `post()` is
  idempotent by key, and safe under concurrency via per-account row locks
  taken in a fixed order.
- **`packages/core/bingo.py`** — pure game logic: the deterministic 100-card
  pool, win-pattern detection (rows/columns/diagonals/corners), and the
  provably-fair draw (`HMAC-SHA256`-seeded Fisher-Yates over 1–75).
- **`packages/core/cards_seed.py`** + a migration — seeds the `cards` table
  from the pool above.

**Game engine (Phase 2):**
- **`services/engine/room_lock.py`** — Redis-backed single-owner election
  for a room (`SET ... NX EX`, refreshed on a timer, compare-and-clear Lua
  scripts so a worker can never refresh or release a lock it no longer
  owns).
- **`services/engine/round_engine.py`** — the room state machine itself:
  IDLE → LOBBY → RUNNING → SETTLING → IDLE. Server-authoritative number
  calling with drift-corrected timing, claim validation that always
  recomputes the marked grid from the round's own called-numbers set (never
  from anything a caller supplies), a 50ms tie window for simultaneous
  winners, and atomic ledger-backed settlement.
- **`services/engine/settlement.py`** — pure payout math (derash/house-cut
  split, tie splitting), independently unit-tested.
- **`services/engine/refunds.py`** — the one shared, idempotent full-refund
  path used by an underfilled lobby, a round that exhausts all 75 calls with
  no winner, and crash recovery alike.
- **`services/engine/recovery.py`** + **`worker.py`** — crash safety: on
  startup, any round left in a non-terminal state whose room lock has gone
  stale gets voided and fully refunded, never silently resumed.

**Realtime gateway (Phase 3):**
- **`packages/core/telegram_auth.py`** — Mini App `initData` validation: the
  exact HMAC-SHA256 double-hash algorithm, constant-time comparison, and
  24-hour replay-window rejection. The entire auth boundary; no session
  cookie exists anywhere else.
- **`services/engine/commands.py`** — the gateway↔engine bridge: one Redis
  Stream per room carries `join`/`drop_card`/`claim`/`set_auto` requests to
  whichever engine currently owns that room, replies correlated back over a
  per-request pubsub channel with a timeout.
- **`services/gateway/fanout.py`** — one Redis `psubscribe` per gateway
  process fans out to every locally-connected socket without ever
  re-serializing a message per connection; a bounded per-connection queue
  implements the spec's backpressure rule (drop ticks, queue a fresh
  `state_sync`) so one slow reader can never delay everyone else in the room.
- **`services/gateway/connection.py`** + **`app.py`** — the FastAPI
  WebSocket endpoint: auth handshake, rate limiting (Redis token buckets),
  message dispatch, and `state_sync` served straight from Postgres (the
  durable source of truth) so reconnection works even if the room's engine
  just crashed.

**Telegram bot (Phase 1):**
- **`services/bot/registration.py`** + **`phone.py`** — the registration
  flow: a shared contact only proves phone ownership if its `user_id`
  matches the sender (any mismatch is rejected outright — Telegram lets
  anyone forward anyone's contact card), phone numbers are normalized to
  E.164, typed numbers are never accepted as a substitute for the
  Share-Phone-Number button.
- **`services/bot/notifier.py`** — the only code path allowed to call
  `bot.send_message`; paces to ~25 msg/s and backs off per-chat on a 429
  without stalling every other chat's queue behind one rate-limited one.
- **`services/bot/dedup.py`** — webhook `update_id` deduplication via
  Redis, so a retried Telegram webhook can't double-process anything.
- **`services/bot/i18n.py`** + **`locales/`** — every user-facing string
  keyed, Amharic default, English complete, `om`/`ti` stubbed with
  fallback — enforced mechanically, not just by convention, by an AST-based
  test that fails on any hardcoded string literal in `handlers.py`.
- **`services/bot/handlers.py`** + **`app.py`** — the aiogram 3 webhook
  app: full command set, referral deep links, and real reads/writes
  against the same ledger and round tables the engine and gateway use —
  `/balance` and `/history` show actual money and actual games, not
  placeholders. `/deposit`, `/withdraw`, and `/limits` honestly report
  "not available yet" rather than faking Phase 5-7 functionality that
  doesn't exist.

**Mini App (Phase 4):**
- **`web/miniapp/`** — the full player-facing client per the Mini App UI
  spec: room list, card selection (two-tap commit), the live game screen
  (75-cell call board and 5×5 card both rendered once and mutated in
  place, never re-rendered per call), spectate mode, the result screen,
  and a wallet view. Vanilla JS (ES modules), no framework, no build step.
- **`js/ws.js`** — the WebSocket client, matching the gateway's protocol
  exactly: auth handshake, transparent reconnect (re-authenticates and
  re-joins the active room, restoring state from the server's own
  `state_sync` rather than anything remembered client-side), server-time
  offset tracking for a countdown that never trusts the device clock.
- **`js/i18n.js`** + **`locales/`** — Amharic default, English complete,
  same fallback rules as the bot side. Amharic renders via a self-hosted
  `Noto Sans Ethiopic` subset (`fonts/`) — regenerable with
  `fonts/subset.sh` — cut from Google's ~200KB full delivery to ~11.5KB by
  subsetting to the codepoints the current locale files actually use
  (spec budget: 40KB).
- **`services/gateway/app.py`** now also serves the Mini App's static
  files and two REST endpoints (`/api/me`, `/api/history`) for the wallet
  screen, authenticated the same `Authorization: tma <initData>` way as
  the WebSocket handshake.

**Admin console (Phase 7):**
- **`services/admin/auth.py`** — a completely separate authentication path
  from players: username + bcrypt password + TOTP 2FA, Redis-backed session
  tokens (server-side revocable on logout, unlike a JWT), and login failures
  that all raise the same generic error regardless of which check failed
  (unknown username, wrong password, wrong code) so the error text itself
  can't be used to enumerate usernames or probe 2FA status.
- **`services/admin/rbac.py`** — a single `PERMISSIONS` dict mapping each
  permission to the roles allowed to use it (`support` / `finance` / `ops` /
  `superadmin`), checked through one `has_permission()` function on every
  route — no scattered role checks to get out of sync.
- **`services/admin/queries.py`** — every admin action that touches money
  (`adjust_balance`, `void_round_admin`) goes through
  `packages/core/ledger.py` like any other transaction, never a direct
  balance write; every mutation writes an audit log row with before/after
  state.
- **`services/admin/audit.py`** + a migration — `admin_audit_log` is
  append-only at the database level: a `BEFORE UPDATE OR DELETE` trigger
  raises rather than relying on convention.
- **`services/admin/app.py`** — the FastAPI admin API: dashboard summary,
  user search/detail/ledger history, balance adjustment, user status
  (suspend/self-exclude), round list/detail, the fairness-verification
  route (`GET /rounds/{id}/fairness` — reveals the committed `server_seed`
  once a round is terminal and independently re-verifies the draw), admin
  round voiding, room CRUD, a daily GGR report, and the audit log itself
  (restricted to `superadmin`) — plus an optional source-IP allowlist.

Building the fairness route surfaced a real gap in Phase 2:
`round_engine.py` was committing to `server_seed_hash` up front (correct)
but never actually persisting the revealed `server_seed` anywhere durable
once a round finished — it only ever went out once over an ephemeral Redis
pub/sub message. Fixed so `rounds.server_seed` is written at both terminal
points (settlement and exhausted/no-winner refund), making historical
fairness verification actually possible instead of only "provable" for
whoever happened to be connected at the exact second the round ended. See
`DECISIONS.md`.

Covered end to end by `tests/`, including the tests that actually matter: a
35-player round settling to the cent (700 pot → 560 derash → 140 house), two
simultaneous claims splitting a derash evenly, a killed engine mid-round
recovering to a full refund on the next worker startup, Postgres itself (not
Python) rejecting an unbalanced ledger transaction, a full auth→join→stake→
round-settle flow over a real WebSocket, 1,000 real concurrent WebSocket
connections in one room receiving a call in ~130–190ms on a good run (spec
budget: 300ms — see `DECISIONS.md` for why this one occasionally reads
higher on a busy shared host), a contact-mismatch registration rejected
end to end through a real aiogram Dispatcher, a duplicate Telegram
`update_id` processed exactly once, and a real Chromium browser playing an
actual round against the real backend (auth → fund → join → take a card →
watch live calls render → confirm the stake actually happened in the
ledger) — which is how three real bugs invisible to every other test got
found and fixed, and an admin console's fairness route sent over real HTTP
against a real `uvicorn` server, RBAC denying/allowing per role, session
logout actually invalidating a token, and the audit log's immutability
trigger firing on a direct `UPDATE`/`DELETE` attempt — which is how a real
Phase 2 gap (the provably-fair `server_seed` was never durably persisted)
got found and fixed; see `DECISIONS.md`. `mypy --strict` is clean across the
whole codebase.
