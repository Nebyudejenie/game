# Decisions

Where an implementer (human or AI) deviates from `idea.md`, or makes a call
`idea.md` leaves open, it goes here with the reasoning. Newest first.

---

## 2026-08-22 — Phase 4 (Mini App, spec's Mini App UI Specification) — real bugs found by actually testing in a browser

This phase is the reason the CTO instructions insist on testing UI in a
real browser before calling it done — every one of these was invisible to
`mypy`/unit tests and would have shipped silently broken:

- **`ws.js`'s auth resolver passed the wrong object.** `authResolvers`
  were resolved with the whole `{t, user, server_time}` envelope instead
  of `message.user`, so every caller's `user.balance` read `undefined`.
  The WebSocket traffic looked perfect in isolation (`authed` arrived with
  a real balance) -- only reading the actual DOM (`#balance-amount`)
  caught it. Fixed to resolve with `message.user`, matching
  `waitForAuth()`'s other branch.
- **Taking a card never fetched the card's actual grid.** `state_sync`
  only includes `your_card_grid` once `round_entries` has a row for that
  user; the LOBBY-time `state_sync` (before taking a card) naturally has
  it `null`. The `take_card` ack handler patched `your_card` locally but
  never re-fetched state, so `your_card_grid` stayed `null` -- crashing
  `setCardGrid()` the moment `round_start` fired (`TypeError: Cannot read
  properties of null (reading '0')`, only visible in a running browser's
  console, never in a `curl`/WS-trace-level check). Fixed by re-sending
  `join` after a successful `take_card` ack, reusing the existing
  `state_sync` path rather than inventing a new one.
- **`round_start`'s stat-strip update used the wrong object.** The handler
  correctly merged `round_start`'s payload into `state.round` for storage,
  but then called `updateStatStrip(sync)` with the *raw*, unmerged
  `round_start` payload -- which has no `stake` field (only `state_sync`
  does) -- leaving the stake stat blank after the first `round_start` of a
  session. Same fix also closed a second bug in the same code path:
  `state.round.called` was never actually reset to `[]` in the global
  store across rounds (only a local copy was), which would have let a
  stale called-number from a previous round leak into the *next* round's
  client-side pattern check. Caught by reviewing an actual screenshot from
  the E2E test, not by any assertion. One merged object now, reused for
  `setState`, `enterGame`/`enterSpectate`, and `updateStatStrip` alike.
- **The E2E test's own `page.route()` handler was a no-op.**
  `lambda route: route.abort()` creates the coroutine and never awaits or
  schedules it -- a classic Python async mistake, silently doing nothing.
  Without it, index.html's real `<script src="https://telegram.org/js/
  telegram-web-app.js">` loads after the test's `add_init_script` stub and
  overwrites `window.Telegram.WebApp.initData` back to `""`, breaking auth
  in a way that looked like an app bug until traced to the test harness
  itself. Fixed with a real `async def` handler.
- **A real double-claim race, found only by running the E2E test enough
  times to hit it**: a single player can produce two independent valid
  claims for the same round through two separate paths -- the server's own
  AUTO-mode scan (`_call_next_number`) and a client-sent `claim` message
  (the Mini App's client-side AUTO toggle independently detects the same
  completed pattern and sends its own claim). Both could land in
  `_pending_winners`, crashing `round_winners`' `(round_id, user_id)`
  primary key at settlement -- `asyncpg.exceptions.UniqueViolationError`,
  visible only as an intermittent `finally: await asyncio.wait_for(task,
  ...)` failure in whichever test happened to run the engine long enough
  to hit it (roughly 1 in 3 runs of the gameplay E2E test). Fixed in
  `round_engine.py`'s `claim()`: reject a second claim from a user_id
  already in `_pending_winners`, regardless of source (`"already_claimed"`)
  -- one claim per user per round, full stop. Added a direct regression
  test (`test_same_user_double_claim_race_settles_exactly_once`) that
  fires both claim sources concurrently for the same user without needing
  a browser to reproduce it.
- **The E2E gameplay test still has residual, lower-rate flakiness**
  (~1 in 7-8 runs, after the fix above) waiting for the game screen to
  appear -- a plain timeout, no crash, no console error, and it always
  passes on retry. This matches the same category already documented for
  the load tests: five to eight consecutive heavy Playwright+uvicorn+engine
  test invocations on one shared 4-core dev box will occasionally hit real
  resource contention. Bumped the wait budget as cheap mitigation (15s →
  25s) and documenting rather than chasing further, since it shows no
  reproducible error signal to chase and the test is opt-in
  (`pytest -m e2e`), not part of the default suite or any CI gate.
- **The E2E gameplay test was itself flaky** (~2/3 failure rate) for an
  unrelated reason: the dev database accumulates rooms across every prior
  test run at the same 10.00 ETB stake, and `page.click(".room-card")`
  clicked whichever one sorted first -- non-deterministic against ties,
  so it sometimes hit some other, already-finished room whose
  `state_sync` never reports `status: "lobby"`. Fixed by adding
  `data-room-id` to each room card in `renderRoomList()` and having the
  test target its own room specifically. Confirmed with 4 consecutive
  clean runs afterward.
- **`#screen-rooms` used to default to `class="screen active"` in the raw
  HTML** (so something was visible before JS ran) and the balance
  placeholder used to be `"0.00 ETB"` -- both made the *first* E2E test
  draft pass regardless of whether auth had actually succeeded, since a
  real `"0.00 ETB"` balance is indistinguishable from the placeholder. No
  screen defaults to active now; the loading placeholder is `"-- ETB"`, so
  a genuine `"0.00 ETB"` is unambiguous proof `authed` arrived.

**Design decisions**, not bugs:

- **Amharic font is subsetted to the codepoints actually used, not the
  full Ethiopic block.** Google's full "ethiopic" delivery is ~200KB, over
  4x the spec's 40KB budget (Ethiopic is a syllabary -- hundreds of glyphs,
  not a ~26-letter alphabet). Subsetting to the ~122 characters currently
  used across `web/miniapp/locales/am.json` and the bot's `am`/`ti`
  locales gets it to ~11.5KB. Tradeoff: a new Amharic string introducing a
  character outside the current subset falls back to a system font for
  that glyph until `fonts/subset.sh` is rerun -- documented in the script
  itself and in `css/fonts.css`'s own comment, not just here.
- **Gateway REST endpoints (`/api/me`, `/api/history`) added for the
  wallet screen**, not just the WebSocket protocol. The spec's client→
  server message list (§6.2) has no request for balance snapshot or
  history; rather than force those through the realtime channel, they're
  plain authenticated REST calls (`Authorization: tma <initData>`, same
  `validate_init_data` boundary as the WS handshake) -- a normal read
  doesn't need a persistent connection, and this matches the spec's own
  architecture diagram showing "REST API" as a sibling to "WebSocket",
  not something WebSocket-only.
- **`state_sync` extended** with `stake`, `win_patterns`, `players`,
  `derash`, `your_card_grid`, and `lobby_deadline_ms` -- Phase 3's version
  only had what the (not-yet-built) Mini App's actual needs could reveal.
  All sourced from Postgres, consistent with Phase 3's existing "state_sync
  never depends on a live engine" principle.
- **Mini App static files served by the gateway itself**
  (`StaticFiles` mount on `/`), anchored to `services/gateway/app.py`'s own
  file location rather than the process's cwd. Simplest path for local
  dev/testing and a legitimate (if not final) production option; a real
  deployment would likely put these behind a CDN instead, which is a pure
  infra change with no code impact.
- **Playwright is a dev-only, on-demand dependency** (`pytest -m e2e`,
  same pattern as the load tests), not part of the default suite --
  a Chromium binary is a heavy, environment-specific thing to require for
  every contributor's default `pytest tests/` run.

---

## 2026-08-22 — Phase 1 (Telegram bot, spec Prompt 5) scoping decisions

- **`/deposit`, `/withdraw`, `/limits` honestly report "not available yet"**
  rather than faking a payment or limits flow. Building a working-looking
  deposit command with no real provider behind it (Phase 5-6, not built) or
  a limits command with no responsible-gambling engine behind it (Phase 7,
  not built) would be exactly the "fake/mock functionality" the CTO
  instructions explicitly forbid. `/balance`, `/history`, `/invite`,
  `/language`, `/rules`, `/support`, and the full registration flow are all
  real, reading and writing the actual ledger/DB.
- **Play button is conditional on `MINIAPP_URL` being configured.** Telegram
  requires a valid HTTPS URL for a `web_app` button; there is no Mini App
  yet (Phase 4). Rather than ship a button that would error or point
  nowhere, `/play` and the main menu button fall back to an honest "not
  open yet" message until `MINIAPP_URL` is set.
- **Referral codes are just the referrer's own numeric user_id** (`ref_
  {telegram_id}`), not a separately generated/looked-up code — already
  unique, no new table needed. Only the `referred_by` link and a plain
  count are implemented; the `referrals` table's reward/qualifying-deposit
  tracking (spec section 4.5) depends on deposits existing, so it's
  deferred to whichever phase builds the promotions engine.
- **`/change_username` takes its argument inline** (`/change_username New
  Name`) rather than using aiogram's FSM (a "send the command, then reply
  with the name" conversation). Avoids pulling in FSM storage machinery for
  one simple field; can be upgraded to a real conversation later if product
  wants a friendlier flow.
- **Structural, AST-based test enforcing "no hardcoded string in any
  handler"** (`tests/unit/test_bot_no_hardcoded_strings.py`), not just
  reviewer discipline. It walks the whole `handlers.py` AST for string
  literals with real alphabetic content and fails on anything not
  specifically exempted (a `t(...)` call's translation key, a
  `Command(...)`/`Router(...)` protocol identifier, a `row["key"]`
  Record-subscript, a ledger account "kind" enum value, or an f-string's
  structural fragments used only for URL building). Every exemption was
  added only after the checker actually flagged that real, verified-safe
  case against the live file — none were guessed in advance. The checker
  caught one real bug while being built: `outcome = "won" if ... else "—"`
  was hardcoded English text passed straight into a `t(...)` format kwarg,
  bypassing i18n entirely; fixed by adding `history.outcome_won` /
  `history.outcome_other` translation keys.
- **aiogram's `Router` can only attach to one `Dispatcher` for its
  lifetime.** `services/bot/handlers.py`'s `router` is the normal aiogram
  module-level-singleton pattern, correct for production (one long-lived
  Dispatcher), but it meant test code building a fresh `Dispatcher` per
  test function crashed on the second test ("router already attached").
  Fixed by sharing one session-scoped `Dispatcher`/`Bot`/fake-session
  across all of `test_bot_handlers.py`, matching how the bot actually runs.
- **Bot API testing uses a fake `aiogram.client.session.base.BaseSession`
  subclass** that records `SendMessage` calls and returns a synthetic
  `Message` instead of touching the network — no real bot token or
  Telegram connectivity needed. Combined with `Dispatcher.feed_update()`
  fed synthetic `Update` objects, this exercises the real Router/handler
  code end to end, which is what let the spec's own Prompt 5 test list
  (contact-mismatch rejection, typed-number rejection, duplicate
  `update_id`) be proven directly rather than approximated.
- **Amharic/English translations are a first-pass draft**, not
  native-speaker-reviewed — except the handful of strings taken verbatim
  from the spec itself (`register.prompt`, `register.use_button`,
  `wallet.insufficient`), which are presumably already vetted. Flagging
  this explicitly rather than implying production-ready translation
  quality; a native speaker should review `services/bot/locales/am.json`
  before this ships for real users.
- **Docker containers had stopped mid-session** (host uptime showed an
  11-minute restart partway through this phase) — not a code issue,
  just `docker compose up -d postgres redis` again, data intact via the
  named volume. Noted because it produced a wall of unrelated-looking
  connection-refused test failures that could otherwise look like a real
  regression.
- **Load-test variance reconfirmed, more dramatically.** Immediately after
  the container restart above (host uptime 11 minutes, other unrelated
  Docker workloads on the same shared 4-core machine also mid-restart),
  the 1000-socket fan-out test measured p99 = 482-528ms across repeated
  runs -- well over budget. Waiting and rerunning once the host settled
  brought it straight back to 147-190ms, matching every other measurement
  taken during this project. Real, environment-driven variance on shared
  infrastructure, not a regression in the (unchanged) fan-out code --
  exactly why this test is `@pytest.mark.load` and excluded from the
  default run rather than a hard CI gate.

---

## 2026-08-22 — Phase 3 (realtime gateway, spec Prompt 4) scoping decisions

- **Gateway↔engine command channel: Redis Streams, one per room.** Prompt 3
  deliberately left this open (engine tested as a direct Python API). Built
  it now as `services/engine/commands.py`: the gateway `XADD`s a command
  with a correlation id and awaits a reply on a per-request pubsub channel;
  `RoundEngine._serve_commands()` is the sole consumer of its own room's
  stream for as long as it holds the room lock, so no consumer group is
  needed the way the (not-yet-built) payout queue will need one. A command
  to a room with no live owner times out cleanly (`CommandTimeout`) rather
  than hanging.
- **`state_sync` and `rooms` are served straight from Postgres**, not
  through the engine. Postgres is already the durable source of truth
  (`round_engine.py` writes every transition there before publishing
  anything), so reconnection works correctly even mid-crash-recovery, with
  no dependency on a live engine existing right now. Only the four
  money/state-moving actions (`join`/`drop_card`/`claim`/`set_auto`) go
  over the command channel.
- **Backpressure is queue-depth-based, not byte-size-based.** The spec's
  "64 KB send buffer" language assumes access to the raw socket's write
  buffer, which Starlette/ASGI doesn't expose at the application level. Each
  connection gets a bounded `asyncio.Queue` (`fanout.MAX_QUEUE_SIZE = 100`)
  instead; when it's full, pending droppable messages (`lobby_tick`, `call`)
  are dropped and a `state_sync` is queued next, matching the spec's actual
  behavioral intent (a slow client falls back to a full resync rather than
  blocking the room) via a different but equivalent signal.
- **`round_end`'s per-winner shape carries over from Phase 2** (see that
  section below) — a list of `{user_id, card_no, pattern, amount}` rather
  than one top-level `pattern`/`cells` pair, since ties can have co-winners
  on different patterns.
- **Two protocol additions beyond the spec's literal table**, both noted
  here rather than silently invented: a `rooms` reply (`{"t": "rooms",
  "rooms": [...]}`) for the `rooms` request — the spec names the request
  but not its reply shape — and a generic `{"t": "ack", "for": ..., "ok":
  ..., "reason": ...}` for `take_card`/`drop_card`/`set_auto`, since the
  spec only names a distinct reply (`claim_result`) for `claim`. Clients
  need *some* way to know their own action's own result distinctly from the
  room's broadcast of it.
- **`take_card`'s client-supplied `idem` field is accepted but unused.**
  `RoundEngine.join()` already derives its own idempotency key from
  `(round_id, user_id)`, which is a stronger guarantee than a client-chosen
  token (immune to a client reusing or mis-generating one) — the field is
  read from the frame for protocol compatibility and otherwise ignored.
- **1000-socket fan-out test is marked `@pytest.mark.load`, excluded from
  the default `pytest tests/` run.** Measured p99 in isolation: ~130–175ms
  (well under the spec's 300ms budget), repeatedly. Run as the 423rd test
  in one long-lived pytest process (everything else's accumulated GC
  pressure and event-loop bookkeeping sharing the same 4 cores as the test
  itself), the same scenario measured p99 = 318ms — confirmed by rerunning
  it standalone immediately afterward, which came back at 167ms, proving
  the regression was process-level contention, not architecture. This is
  exactly why real load tests run as their own dedicated process rather
  than interleaved with a correctness suite; `pytest -m load` runs it
  separately for a clean reading. Numbers are what this single 4-core dev
  machine produced, not a production claim — Phase 8's real load testing
  runs against a real deployed topology with load generators on separate
  hardware from the server, which is the only way to responsibly measure
  the spec's 10,000-concurrent target.
- **`get_or_create_user_by_telegram_id` lazily creates a user row on first
  Mini App connect.** In the target architecture the bot (Phase 1, not yet
  built) creates the user during registration before the Mini App is ever
  opened. This bridges the gap until then and becomes a pure no-op read
  once Phase 1 exists.
- **Fixed a real bug found while writing gateway tests**: `ledger.balance()`
  and `ledger.available()` returned an unscaled `Decimal("0")` (displays as
  `"0"`) for an account with no history yet, instead of `Decimal("0.00")`.
  Both now `.quantize(Decimal("0.01"))` before returning — money always
  renders at 2 decimal places, full stop.

---

## 2026-08-22 — Phase 2 (game engine, spec Prompt 3) scoping decisions

- **Round status: `voided` covers every refund-only outcome.** The spec's
  `rounds.status` enum (`lobby|running|settling|done|voided`) doesn't
  distinguish "ran to completion with no winner" from "never really
  happened." Using `voided` uniformly for lobby-underfill, 75-calls-exhausted-
  no-winner, *and* crash recovery means all three share one function
  (`services/engine/refunds.py::refund_round`) instead of three near-copies
  of the same ledger logic. `done` is reserved exclusively for a round that
  actually paid a winner.
- **Stakes debit `user_cash` only.** The spec's `available()` balance
  (`user_cash + user_bonus - user_locked`) implies bonus funds can stake, but
  there's no wagering-requirement tracking built yet (no prompt in the pack
  covers bonuses specifically — it's mentioned under Payments §8.5, Prompt
  7/8 territory). Staking from bonus without anything to track wagering
  progress would be a half-feature, so `join()` only ever touches
  `user_cash`; revisit when the bonus/promotion engine is built.
- **`_publish_room()` implements the room-broadcast events only**
  (`lobby_tick`, `round_start`, `card_taken`, `call`, `round_end`) — not the
  per-user `claim_result` / `balance` messages from spec §6.3. Those are
  responses to a specific connection's action, which is Prompt 4's (the
  gateway's) concept to own once it exists; `claim()`'s return value already
  carries everything a caller needs. Publishing to `user:{id}` for a balance
  push with nothing subscribed yet would be speculative.
- **`round_end`'s `winners` is a list of `{user_id, card_no, pattern,
  amount}`**, not the single top-level `pattern`/`cells` fields the spec's
  table sketches (written assuming one winner). A tied round can have
  co-winners on different patterns; per-winner fields are the honest
  representation.
- **The gateway↔engine command transport is out of scope for Prompt 3.**
  `RoundEngine` exposes `join`/`drop_card`/`claim` as a plain async Python
  API, matching how Prompt 3's own test list exercises it (direct
  simulation, not over a network). How a WebSocket message reaches a
  same-room engine running in a different process (Redis Stream per room is
  the natural fit, matching the payout-queue pattern the spec already uses
  elsewhere) is Prompt 4's problem to solve.
- **`max_players` has a narrow TOCTOU race for non-default room sizes.** The
  check happens in memory before the atomic DB insert. The hard invariant —
  no duplicate card, no double-charge — is still fully enforced by the
  `(round_id, card_no)` primary key regardless, since the card pool caps at
  100 and most rooms will set `max_players = 100` anyway. Noted rather than
  fixed with a distributed counter, which felt like solving a problem this
  phase doesn't actually have yet.
- **`RoomLock`'s TTL and refresh interval are constructor-configurable**
  (defaults still 15s / 5s per spec) purely so the test suite can prove
  expiry/refresh behavior in ~1.5s instead of 15s+, without changing
  production timing.
- Discovered `services/__init__.py` was missing (Phase 0 created every
  `services/<name>/__init__.py` but not the parent package marker) while
  wiring engine imports — mypy caught it immediately as a duplicate-module
  error. Added.

---

## 2026-08-22 — `idea.md` contains three layered documents; adopted the "Jo Bingo" spec pack as authoritative

`idea.md` turned out to be three documents concatenated, not one:

1. A generic "Bengo" CTO-prompt (lines 1–2175) — a requirements checklist,
   suggesting a generic Node/Go + React stack.
2. A second, more detailed pass of the same checklist, "Bengo — Extreme
   Production Build" (lines 2176–4775).
3. The **"Jo Bingo" spec pack** (lines 4776–6298) — concrete and internally
   consistent: exact SQL schema, exact economics (35 players × 20 ETB stake
   → 700 pot → 560 derash at a 20% house cut), exact WebSocket protocol,
   exact provider list (Chapa/SantimPay/ArifPay), exact stack (Python 3.12,
   FastAPI, aiogram 3, PostgreSQL 15, Redis 7, asyncpg, Alembic, pytest), and
   its own 9-prompt build sequence with test requirements per prompt.

**Decision:** the Jo Bingo spec pack is the authoritative blueprint (stack,
schema, economics, protocol). The two generic sections are treated as the
requirement checklist Jo Bingo must satisfy — compliance, security,
observability etc. get layered on as the corresponding Jo Bingo phase is
built, not invented ahead of schedule. Confirmed with the user before
writing any code.

## 2026-08-22 — Credentials not collected yet

User has a Telegram bot token and a SantimPay/ArifPay key already, but
neither is needed until Phase 1 (bot) or Phase 5–6 (payments). Not collected
during Phase 0 — `.env.example` documents the variable names so `config.py`
has a stable shape now; real values go directly into a local `.env` (never
committed) when those phases start.

## 2026-08-22 — Delivery is phase by phase

Per the spec's own instruction ("one prompt per session... stop after each
numbered prompt for review") and the sheer size of the system (10 build
phases, ~16 weeks by the spec's own estimate), work proceeds one phase at a
time, each reviewed before the next starts. Phase 0 (this pass) covers the
spec's Prompt 1 (foundations + ledger) and Prompt 2 (pure bingo logic).

## 2026-08-22 — `account_balances` denormalizes `kind` from `accounts`

The spec's own SQL sketch (§4.2) doesn't put `kind` on `account_balances`,
but a real non-negative-balance guarantee for `user_cash` / `user_bonus` /
`user_locked` needs a CHECK constraint, and Postgres CHECK constraints can't
reference another table. Copying `kind` onto `account_balances` at row
creation (immutable thereafter) makes `CHECK (kind NOT IN (...) OR balance
>= 0)` a real, table-local, database-enforced constraint instead of
something only `ledger.post()`'s row-lock logic protects. Belt and
suspenders: the row lock is still what makes concurrent debits correct; the
CHECK is the backstop if anything ever writes to `account_balances` outside
`post()`.

## 2026-08-22 — Partial unique index for system accounts

`accounts` has `UNIQUE (user_id, kind, currency)`, but Postgres treats
`NULL` as distinct from `NULL` in a unique constraint — so that constraint
alone would let two `house_revenue` rows (both `user_id = NULL`) coexist.
Added `CREATE UNIQUE INDEX ... ON accounts (kind, currency) WHERE user_id IS
NULL` so system accounts (`house_revenue`, `pot_escrow`, etc.) stay
singletons the same way user accounts do.

## 2026-08-22 — Postgres/Redis moved off default ports in docker-compose

This machine already runs another project's Postgres (5432) and Redis
(6379). `deploy/docker-compose.yml` maps Jo Bingo's containers to host ports
5433 and 6380 instead; `packages/core/config.py` and `.env.example` default
to those. Purely a local-dev accommodation, has no bearing on production
deployment.

## Pre-existing repo state noted, not touched

This directory already had a `.git` folder with `origin` pointing to
`https://github.com/Nebyudejenie/game.git` before this session started (no
commits yet). Left as-is — not part of this work, and remotes/commits are
outside what was asked for.

## Observed: an "Initial commit" appeared and was pushed to origin/main mid-session, not made by the assistant

While Phase 0 files were being written, `git log` came to show a commit
("Initial commit", author `Nebyu Dejenie <nebyudejenie1@gmail.com>` — the
account's own configured git identity, matching `origin`) containing that
work, and `refs/remotes/origin/main` recorded `update by push` at the same
timestamp. The assistant issued no `git add`, `git commit`, or `git push` in
this session (project instructions require an explicit ask before
committing, and none was given) — most likely explanation is the user
staging/committing/pushing via VS Code's Source Control panel while working
alongside this session. Content pushed matches what was verified with a
green test suite, so noted here rather than acted on.
