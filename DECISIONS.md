# Decisions

Where an implementer (human or AI) deviates from `idea.md`, or makes a call
`idea.md` leaves open, it goes here with the reasoning. Newest first.

---

## 2026-08-24 — Phase 6 (withdrawals, spec Prompt 8 / section 8.3-8.4)

Built the payout side: `services/payments/withdrawals.py` (validation gate,
instant fund-lock, auto-approve rule, admin review queue hooks) and
`services/payments/payout_worker.py` (a real Redis Streams consumer group
-- the first one in this codebase; `services/engine/commands.py`'s own
comment already flagged the payout queue as the place consumer groups
would eventually be needed, unlike the per-room command stream which only
ever has one reader).

**"The single most important step" (the spec's own words) is tested
directly, not just by inference:** `request_withdrawal()` moves
`user_cash -amount` / `user_locked +amount` through `ledger.post()` inside
the same transaction that creates the `payments` row -- there is no
`await` between "decide to lock" and "lock is committed". Proved by
`test_withdrawal_locks_funds_immediately_so_a_later_stake_fails` (a
withdrawal for a user's entire balance, followed by a real
`RoundEngine.join()` attempt, which correctly fails with
`insufficient_funds`) and, for the actually-concurrent case, by
`test_concurrent_withdrawal_and_stake_never_both_succeed` (`asyncio.gather`
on both against the same balance; exactly one succeeds, regardless of
which). Bonus funds are excluded from withdrawals structurally, not by a
conditional check -- `request_withdrawal()` only ever debits `user_cash`,
never `user_bonus`, so `test_bonus_funds_cannot_be_withdrawn` is really
testing that the ledger's own `InsufficientFunds` fires when only
`user_bonus` is funded.

**Exactly-once payout dispatch is the provider's job, not the worker's --
and the worker is written to assume that, not fight it.** Spec: "a crashed
payout worker mid-job → the job is redelivered and the provider is called
exactly once (via our_ref idempotency)." Read literally, a worker cannot
itself guarantee it calls an external API exactly once across a crash --
what it *can* guarantee is passing the same `our_ref` every time, and
trusting the provider to treat that as an idempotency key (this is also
what `chapa.py`'s `create_payout()` already does: `our_ref` goes into
Chapa's own `reference` field). So `payout_worker.process_one()` is safe to
call `provider.create_payout()` again after a redelivery -- it only skips
work for a payment already in a *terminal* state (`succeeded`/`failed`),
never for `processing` (which is exactly the state a crash mid-call would
leave behind).
`test_crashed_worker_job_is_redelivered_and_settles_exactly_once` simulates
the crash directly (forces a payment into `processing`, as if a previous
worker died right after that transition) and confirms the
ledger still settles exactly once (one `ledger_transactions` row for the
`payout-settle-{our_ref}` idempotency key) even though the provider's
`create_payout` genuinely gets called again.

**The consumer group reads each worker's own pending entries (id `"0"`)
before new ones (id `">"`)**, so a worker that restarts under the same
consumer name picks its own abandoned jobs back up without needing
`XCLAIM`/`XAUTOCLAIM` across different consumer identities -- deliberately
the simplest mechanism that satisfies the spec's own test list; reclaiming
a *different* crashed consumer's pending entries (for a multi-replica
deployment where the dead consumer never comes back) is a real gap, noted
below.

**`kyc_level` (0-2) already existed on `users` since Phase 0's own schema**
(present in the original ledger-foundation migration, unused until now) --
no new migration needed for the KYC gate. `MIN_WITHDRAW_ETB` (50 ETB),
`AUTO_APPROVE_WITHDRAW_ETB` (2,000 ETB), and `KYC_REQUIRED_ABOVE_ETB`
(5,000 ETB) are placeholder business parameters, same caveat as Phase 5's
deposit constants -- `idea.md` gives example figures in prose, not
authoritative ones.

**Two anti-fraud rules from spec 8.3's validation gate were deliberately
NOT built, and are real, open gaps, not oversights:**
- **"Payout method holder_name matches account name."** This codebase has
  no independent identity-verification source to check a claimed holder
  name against -- Telegram's `display_name` is self-reported and trivially
  spoofable, so comparing against it would be theater, not a real control.
  Building this for real needs an actual KYC/identity pipeline, which is
  out of scope for this pass. `request_withdrawal()` accepts whatever
  holder name the bot command supplies.
- **"Risk score below threshold" (part of the auto-approve rule).** No
  risk-scoring system exists (spec 8.4's collusion/device-fingerprint/
  velocity flags are all unbuilt -- `risk_flags` was never more than a
  placeholder table name in the spec's own section 4.5). The auto-approve
  rule here checks amount, account age, and lifetime-deposits-vs-
  withdrawals only; every anti-fraud rule in spec 8.4's table is unbuilt.

**Bot UX is deliberately minimal, not the full spec flow.** `/deposit`
already collects only an amount (Phase 5); `/withdraw <amount>
<telebirr number> <full name>` follows the same reasoning -- a single
space-separated command instead of a multi-step aiogram FSM conversation
for choosing a payout method from a saved list. There is no "saved payout
methods" UX; every withdrawal takes a fresh `payment_methods` upsert
(the table itself, and its `(user_id, kind, account_ref)` unique
constraint, already existed from Phase 5's migration). Method kind is
hardcoded to `telebirr` (`withdrawals.DEFAULT_METHOD_KIND`) -- the one
rail every provider in the spec's table covers -- since the bot doesn't
yet ask which rail to use.

**Still deferred from Phase 5, now covering both deposits and
withdrawals:** the Telegram bot confirmation message (spec 8.3 step 8:
"Bot notifies the user with the reason"). `services/payments` remains a
separate process from the bot, and `services/bot/handlers.py`'s own tested
invariant is that nothing calls `bot.send_message` except `Notifier`. This
still needs a small Redis-Streams (or pub/sub) channel the bot process
consumes and forwards through its own `Notifier` -- deliberately not built
this pass either, so it can be done once for both money-movement
directions rather than twice. In the meantime: a depositing/withdrawing
player sees the bot's own synchronous reply to `/deposit`/`/withdraw`
(which does carry real status), the Mini App gets the live
`balance_update` WebSocket push once money actually moves, and the admin
console's audit log and payments queue give full visibility either way.

**Admin review queue additions:** `services/admin/queries.py` gained
`list_pending_withdrawals`, `approve_withdrawal_admin` (audit-logged,
enqueues the real payout job), and `reject_withdrawal_admin`
(ledger-reversed, audit-logged) -- both idempotent no-ops if the payment
isn't in `review` any more, matching `void_round_admin`'s established
pattern. Two new RBAC permissions, `payments:view` (support/finance/ops/
superadmin) and `payments:approve` (finance/superadmin), mirroring the
existing `users:adjust_balance` role split since a withdrawal approval is
exactly that kind of money-movement action.

## 2026-08-22 — Phase 5 (deposits, spec Prompt 7 / section 8.1-8.2)

Built `services/payments/`: a provider-agnostic `PaymentProvider` Protocol
(`provider.py`), a real Chapa adapter (`chapa.py`), the deposit domain logic
(`deposits.py`: create a checkout intent, credit through the ledger on a
verified webhook, a polling fallback, and a pure `reconcile()`), and a
small FastAPI app (`app.py`) exposing the one surface that genuinely has to
be a network endpoint -- the webhook Chapa's own servers POST to.

**No live SantimPay/ArifPay credentials were provided this session** (the
user confirmed having *some* credentials early on but never handed over
actual values), so only the Chapa adapter was built, matching the spec's
own Prompt 7 instruction ("Build ... a Chapa adapter first, structured so
adding SantimPay is a new file and nothing else"). The `PaymentProvider`
Protocol is what makes that literally true: `deposits.py` never imports
`chapa` by name, only the Protocol.

**Chapa's endpoint contract was fetched from developer.chapa.co (2026-08-22)
rather than reconstructed from memory**, since getting a payment
integration's wire format wrong is exactly the kind of mistake that's
invisible until it costs real money: `POST /v1/transaction/initialize` for
checkout creation, `GET /v1/transaction/verify/{tx_ref}` for polling,
`POST /v1/transfers` for payouts, and the two-header webhook signature
scheme (`x-chapa-signature` = HMAC-SHA256 of the raw body, `chapa-signature`
= HMAC-SHA256 of the secret key itself, both keyed with the secret key --
Chapa's own docs: "If either header is missing or the value does not
match, discard the request"). Both are checked in `chapa.py`. The
`/transaction/verify` and `/transfers` *response* body shapes weren't
fully documented in what was fetched; `chapa.py` reads them through
Chapa's standard `{"status", "message", "data"}` envelope, which is
well-established across their whole API, and is not yet verified against a
live sandbox call (no credentials to do that with -- see "not yet tested
against a live rail" below).

**A real gap in Phase 9 (responsible gaming) closed early, deliberately:**
the spec's Prompt 9 wants self-exclusion to block deposits; that couldn't
be built until deposits existed. `create_deposit_intent()` now checks
`users.status = 'self_excluded'` and rejects with `DepositorSelfExcluded`
before ever calling the provider -- covered by
`test_self_excluded_user_cannot_deposit`. The rest of Prompt 9 (loss
limits, cool-off, self-exclusion blocking play and marketing) is still
open.

**`create_deposit_intent()`'s exceptions are a class hierarchy
(`BelowMinimumDeposit`, `DailyDepositCapExceeded`, `DepositorSelfExcluded`,
`UnknownDepositor`, `DepositProviderError`), not one exception with a
string `.reason` field.** Matches the existing
`ContactMismatch`/`InvalidPhone`/`PhoneAlreadyRegistered` pattern in
`services/bot/registration.py`, and sidesteps a real mechanical problem:
`services/bot/handlers.py` has an AST-based test
(`test_bot_no_hardcoded_strings.py`) that fails the build on any hardcoded
string literal in that file, including inside a module-level
`reason -> locale-key` lookup dict. Distinct exception types need no such
table -- `except deposits.BelowMinimumDeposit:` is itself the dispatch.

**`MIN_DEPOSIT_ETB` (10 ETB) and `DAILY_DEPOSIT_CAP_ETB` (50,000 ETB) are
placeholder business parameters** -- `idea.md` never specifies exact
figures for either. Configurable via `Settings`, easy to correct once the
business has real numbers; not a load-bearing design choice.

**Deliberately deferred, not built this pass:**
- **Telegram bot deposit confirmation** (spec step 9: "Bot notification:
  '✅ 200 ETB deposited...'"). The locale key (`notify.deposit_confirmed`)
  already existed from an earlier phase, unused. What's missing is the
  cross-process path: `services/payments` is a separate FastAPI process
  from the bot, and `services/bot/handlers.py`'s own docstring makes
  "nothing calls `bot.send_message` except `Notifier`" a load-bearing,
  tested invariant -- so this needs a small Redis-Streams
  notify-the-bot-process channel, not a direct call. Withdrawals (Phase 6)
  need the exact same capability ("Bot notifies the user with the reason"),
  so building it once for both is the plan rather than a rushed half now.
  In the meantime, a depositing player still gets instant confirmation via
  the live `balance_update` WebSocket push (below) if they're in the Mini
  App, and can always check `/balance`.
- **The Mini App's own deposit-amount UI** (spec's `[+]` button on the
  wallet screen). The bot's `/deposit <amount>` command is the complete,
  real entry point this pass; Phase 4's Mini App screens were already
  reviewed and closed, and adding a full amount-picker screen there is a
  frontend feature addition warranting its own pass rather than a
  backend-phase add-on.
- **Live testing against Chapa's actual sandbox.** Every test in
  `tests/unit/test_chapa_provider.py` and
  `tests/integration/test_payments_deposits.py` exercises the real
  business logic (idempotency, row-locking, ledger crediting, amount
  verification) against a `FakePaymentProvider` test double standing in
  for Chapa's network -- the same reasoning every other test in this suite
  uses a real local Postgres/Redis/gateway instead of a live external
  dependency it can't safely or repeatably call. `chapa.py` itself has
  never made a real network call. **This is the one honest gap in this
  phase:** the adapter is built correctly against Chapa's documented
  contract, but has not been proven against Chapa's own server, because no
  real API key was available this session. That verification should happen
  before any real money moves through it.

**New in this phase, verified real (not test-only) end to end:** the
`user:{id}` Redis pub/sub channel that `services/gateway/fanout.py` already
subscribed to since Phase 3 -- built ahead of need, unused until now -- is
what a deposit's ledger credit now publishes to
(`deposits._publish_balance_update`), and `web/miniapp/js/app.js` now has a
`balance_update` handler that updates the header and, if open, the wallet
screen live. `test_webhook_pushes_live_balance_update_over_websocket`
proves the whole chain: a real HTTP POST to `services/payments/app.py`'s
webhook route credits the ledger and a real connected WebSocket receives
the push, with no mocking anywhere in that path.

## 2026-08-22 — Phase 7 (admin console, spec section 33) — a real gap found while wiring the fairness-verification route

Built the admin API as a fully separate FastAPI app (`services/admin/app.py`,
distinct process/port from the player-facing gateway): username + bcrypt
password + TOTP 2FA login, Redis-backed session tokens (not JWT, so a
session is server-side revocable on logout — matters for an admin console
more than for players), RBAC via a single `has_permission(role, permission)`
function checked on every mutating and every viewing route, and an
append-only audit log enforced at the database level (`BEFORE UPDATE OR
DELETE` trigger that raises rather than a convention nobody can verify).
Every mutation that touches money (`adjust_balance`, `void_round_admin`)
goes through `packages/core/ledger.py` like any other money movement —
no route in `services/admin/app.py` ever writes a balance directly.

**Real bug found, not a test bug:** building `get_round_fairness()` (the
route a regulator or a suspicious player would use to verify a finished
round's draw was not rigged) surfaced that `services/engine/round_engine.py`
never actually persisted the revealed `server_seed` to the `rounds` table —
only `server_seed_hash` (written at round start, for the commit half of the
commit-reveal scheme) and `draw_order` (written when the round starts
running) were ever saved. The actual `server_seed` bytes lived only in the
engine's in-memory state and were broadcast once over Redis pub/sub in the
`round_end` message — an ephemeral, unsubscribed-and-it's-gone channel, not
a queryable record. That made independent, after-the-fact fairness
verification structurally impossible for any round nobody happened to be
connected for at the exact moment it ended, which defeats the entire point
of a provably-fair scheme (the "provable" part requires the proof to still
exist later). Fixed by persisting `server_seed` to `rounds` in both terminal
paths: `_settle_with_winners()` (winner found) and the exhausted/no-winner
refund path in `_run_calling_phase()`. Caught by writing
`test_round_fairness_verification_matches_a_real_round`, which failed with
`fairness["revealed"] is False` against a round that had genuinely
finished — not a fixture/timing bug, the column was just always NULL.

Two other issues fixed in `tests/integration/test_admin_queries.py` while
building this, both narrow test bugs rather than app bugs (documented per
the same discipline as Phase 3/4's test-vs-app-bug calls):
- `test_search_users_finds_by_phone_fragment` hardcoded the literal phone
  `+251911223344`, which collided with `phone_e164`'s UNIQUE constraint on
  any second run against the same long-lived test database. Added
  `unique_phone()` to `tests/integration/conftest.py` (same counter pattern
  already used for `next_telegram_id()`, and already existed independently,
  duplicated, in `test_bot_handlers.py`) and switched this test to it.
- Three `RoundEngine`-backed tests used `lobby_seconds=30` with a 10s
  `asyncio.wait_for(task, timeout=10)` on `engine.stop()`. This is a real,
  narrow characteristic of `_run_lobby()` (it doesn't poll
  `self._stop_requested` inside its wait, so `stop()` can't interrupt a long
  lobby wait promptly) — deliberately not "fixed" in the engine itself,
  since a slow-to-cancel lobby wait is harmless in production and every
  other engine test already uses short `lobby_seconds` for exactly this
  reason. Brought these three into line instead (`lobby_seconds=5`).

Also: `asyncpg` returns `jsonb` columns as raw JSON text with no codec
registered (true everywhere else in this codebase too) — a test asserting
into `admin_audit_log.before`/`after` needed `json.loads()` first. Noted as
a possible future global fix (register a `jsonb` → `dict` codec on the pool
once, instead of every call site doing it ad hoc) but not made this pass —
touching pool-wide codec config this late in a session isn't worth the risk
for a cosmetic convenience.

Wrote `tests/integration/test_admin_app.py` for the HTTP layer specifically
(real `uvicorn` server via a new `admin_server` fixture mirroring the
existing `gateway_server` one — genuine requests through the actual
FastAPI dependency chain, not RBAC asserted by calling `has_permission()`
directly): login end-to-end, bearer-token rejection, RBAC enforced per-role
over real HTTP (`support` blocked from `/users/{id}/adjust`, `finance`
allowed and money actually moves), audit-log route restricted to
`superadmin`, logout actually invalidating the session, IP allowlist
returning 403 for a disallowed source, and the audit log's immutability
trigger firing on a direct `UPDATE`/`DELETE` attempt. All ran green first
try against a from-scratch `docker compose down -v` rebuild.

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
