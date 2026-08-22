# Jo Bingo

Real-money multiplayer bingo on Telegram for the Ethiopian market. The full
product/architecture spec lives in [`idea.md`](idea.md) (see especially the
"Jo Bingo" sections starting at line 4776 — that's the authoritative
blueprint this repo follows; see [`DECISIONS.md`](DECISIONS.md) for why).

This repo is being built phase by phase. **Phase 0 (foundations + ledger),
Phase 1 (Telegram bot), Phase 2 (game engine), and Phase 3 (realtime
gateway) are done.** Everything else — the Mini App, payments, admin — is
scaffolded as empty packages under `services/` and gets filled in phase by
phase (see `DECISIONS.md` and the plan referenced there for the full
roadmap).

## Repository layout

```
services/bot/          Phase 1: Telegram bot (aiogram 3, webhook mode) -- DONE
services/gateway/      Phase 3: realtime WebSocket gateway (FastAPI) -- DONE
services/engine/       Phase 2: game engine / room state machine workers -- DONE
services/wallet/       Phase 1: wallet API surface over packages/core/ledger.py
services/payments/     Phase 5-6: deposit/withdrawal provider adapters
services/admin/        Phase 7: admin console API
packages/core/         Shared, framework-free domain logic (ledger, bingo, config, logging, redis, telegram auth)
web/miniapp/           Phase 4: Telegram Mini App
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
```

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

Covered end to end by `tests/`, including the tests that actually matter: a
35-player round settling to the cent (700 pot → 560 derash → 140 house), two
simultaneous claims splitting a derash evenly, a killed engine mid-round
recovering to a full refund on the next worker startup, Postgres itself (not
Python) rejecting an unbalanced ledger transaction, a full auth→join→stake→
round-settle flow over a real WebSocket, 1,000 real concurrent WebSocket
connections in one room receiving a call in ~130–190ms on a good run (spec
budget: 300ms — see `DECISIONS.md` for why this one occasionally reads
higher on a busy shared host), a contact-mismatch registration rejected
end to end through a real aiogram Dispatcher, and a duplicate Telegram
`update_id` processed exactly once. `mypy --strict` is clean across the
whole codebase.
