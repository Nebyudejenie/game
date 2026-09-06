# Telegram Bot Performance

Command latency diagnosis, instrumentation architecture, and honest
before/after measurements. Every number in this document was produced by
a real script or a real test against the real local Postgres/Redis —
none are estimated. Where a real number could not be produced from this
environment (no real Telegram bot token, no production traffic), that is
stated explicitly rather than filled in with a plausible-looking guess.

## 1. Webhook architecture (already correct before this pass)

Confirmed by reading aiogram's own installed source, not assumed:
`services/bot/app.py` registers the webhook via `SimpleRequestHandler`,
whose default (`handle_in_background=True`, aiogram's own default for
this specific handler class) means `_handle_request_background()` does:

```python
asyncio.create_task(self._background_feed_update(...))
return web.json_response({})
```

Telegram is acknowledged with an empty `200 {}` the instant the update is
handed off, fully decoupled from how long the matched handler actually
takes. **This means the webhook itself was never the bottleneck** — the
bottleneck, if any, lives inside handler execution time, which is what
the rest of this document instruments and measures.

Update-id deduplication (`services/bot/dedup.py::claim_update`, a Redis
`SET NX EX 600`) already runs as an outer middleware
(`_dedup_middleware`) before every handler, for every update type, so a
retried Telegram delivery is a no-op rather than a duplicate side effect.

## 2. Instrumentation added this pass

Every hook below was attached at exactly one place — no handler in
`services/bot/handlers.py` was touched to add any of this:

| What | Mechanism | File |
|---|---|---|
| Per-command total latency, success/error counts | `dp.message.middleware(perf.perf_middleware)` — an inner middleware that only runs once aiogram has matched a specific handler, labeled by that handler's own real function name | `services/bot/perf.py` |
| Dispatch delay (dedup + routing overhead, before handler start) | Timestamp stashed in `_dedup_middleware`, read back in `perf_middleware` | `services/bot/app.py`, `services/bot/perf.py` |
| Per-command DB time | `asyncpg.Connection.add_query_logger()` (asyncpg ≥0.29's own built-in query-timing hook), attached once per physical connection via `create_pool(init=...)` | `services/bot/perf.py`, `packages/core/db_pool.py` |
| Per-command Redis time | A single `Redis.execute_command()` override (every redis-py call funnels through it) | `services/bot/perf.py`, `packages/core/redis_conn.py` |
| Telegram API response time | Timed directly around `Notifier`'s own sole `bot.send_message()` call site | `services/bot/notifier.py` |
| Webhook backlog | Live `getWebhookInfo()` call, admin-facing | `services/admin/telegram_diagnostics.py`, `GET /telegram/webhook-health` |

Attribution across DB/Redis/command metrics uses one
`contextvars.ContextVar` (`services/bot/perf._current_command`), set for
the duration of each handler's execution. Contextvars snapshot per
asyncio task, and aiogram processes each webhook delivery in its own
task, so concurrent commands from different users never cross-attribute
each other's query time — verified directly
(`tests/integration/test_perf_db_redis.py`), not assumed from how
contextvars are documented to behave.

`create_pool()` and `get_redis()` both gained a new, optional,
default-`None`/default-plain parameter (`init=`, `redis_class=`) — every
other caller (gateway, engine, payments, admin) is unaffected; only
`services/bot/app.py` opts in.

## 3. Real measurements taken this pass

Two genuine N+1/duplicate-query patterns were found and fixed by reading
the actual command handlers, not by waiting for a profiler to point at
them (this session's own scripted benchmarks, run against the real local
Postgres, `/tmp/.../bench_balance.py` and `bench_language_and_user.py`):

| Fix | Before | After |
|---|---|---|
| `/balance`: 3×`get_or_create_account()` + 3×`balance()` → `ledger.user_balance_snapshot()` (one query) | p50 7.24ms / p95 13.29ms / p99 14.72ms | p50 1.57ms / p95 2.28ms / p99 2.73ms |
| ~11 command handlers: `_language_for()` + `get_registered_user()` (2 queries, same row) → `get_user_and_language()` (1 query) | p50 3.44ms / p95 8.00ms / p99 11.45ms | p50 1.75ms / p95 3.30ms / p99 4.43ms |

Both are **DB round-trip time only** — the isolated cost of the specific
query pattern that changed — not the full end-to-end command latency
(dispatch + handler + Telegram API response). Full per-command latency
histograms (`telegram_command_latency_seconds`) are now recorded in
production for every command; see the Grafana dashboard section below
for how to read real P50/P95/P99 once traffic exists.

## 4. Engineering targets (not yet claims)

| Class | Target | Measured (real production traffic) |
|---|---|---|
| FAST (`/balance`, `/rules`, `/support`, `/language`, `/limits` on invalid input) | P50 < 300ms, P95 < 800ms, P99 < 1500ms | **Not yet measured** — no real Telegram bot token or production traffic reaches this sandbox. `telegram_command_latency_seconds` is live and will report real numbers the moment the bot serves real traffic. |
| NORMAL (`/deposit`, `/withdraw` — involve a payment provider call) | P95 < 1500ms | Not yet measured, same reason |

This is a deliberate decision, not an oversight: Section 86 of the
parent directive ("do not fabricate performance numbers; do not claim a
command is fast until measured") is honored here by reporting real
isolated-query benchmarks (section 3 above) as what they are, and
explicitly not inventing a P50/P95/P99 for a load pattern this sandbox
cannot produce. **Action for whoever deploys this**: point Grafana at
the dashboard below a few hours after a real deploy and read the actual
numbers off `telegram_command_latency_seconds`.

## 5. Reading real numbers once deployed

`deploy/grafana/dashboards/jo-bingo.json` gained 5 new panels (ids
11–15): updates/sec (received vs. deduplicated), webhook pending-updates
gauge (thresholds at 50/200, matching `services/admin/
telegram_diagnostics.py`'s own constants), per-handler command latency
P50/P95/P99, per-handler error rate, and a combined dispatch/DB/Redis/
Telegram-API breakdown panel — reusing this codebase's existing Grafana
dashboard (the same one `gateway_command_ack_seconds` and
`engine_claim_validation_seconds` already live on), not a second
monitoring system.

Example query for a specific command's P95:

```promql
histogram_quantile(0.95, sum(rate(telegram_command_latency_seconds_bucket{handler="cmd_balance"}[5m])) by (le))
```

## 6. Admin-facing "is Telegram healthy right now" (non-Grafana)

`GET /telegram/webhook-health` (RBAC: `telegram:view_health`, granted to
every admin role — same breadth as `dashboard:view`) makes a live
`getWebhookInfo()` call and reports `pending_update_count`,
`last_error_date`/`last_error_message`, `ip_address`, `max_connections`,
and a derived `status` (`healthy`/`warning`/`critical` at the same
50/200 thresholds as Grafana). Surfaced in the Admin Dashboard's new
"Telegram" screen (`web/admin/js/screens/telegram_health.js`).

**Known real limitation, stated rather than papered over**: Telegram's
`getWebhookInfo` does not report how long the oldest pending update has
been waiting — only a raw count. There is no way to compute an exact
"oldest pending update age" from Telegram's own API. The closest real
proxy is comparing `telegram_updates_received_total`'s rate against
`telegram_commands_total`'s rate on Grafana (a growing gap between them,
sustained, means a stuck backlog; a brief gap that closes is normal
jitter) — noted in the admin screen's own copy so nobody mistakes the
absence of an exact age figure for an oversight.

## 7. Real finding: cold connection-pool warm-up, and a real fix

A one-off diagnostic script (a bare `asyncpg` pool, no bot code involved
at all, run directly against this environment's real local Postgres — not
committed as a test) first measured a startling p50/p99 of ~1.8 seconds
for 50 concurrent queries through a cold pool, versus ~22-25ms for the
same 50 concurrent queries through a pool already pre-warmed to 50
open connections:

| Scenario | 50 concurrent `SELECT` queries |
|---|---|
| Pool cold (min_size=2, must open ~45 new connections at once) | p50 ≈ 1752ms, p99 ≈ 1796ms |
| Pool pre-warmed to 50 connections already open | p50 ≈ 22ms, p99 ≈ 25ms |

This proved the delay was **connection establishment, not query
execution or handler logic**. The bot's own pool (`services/bot/app.py`)
was configured with `min_size=2` — meaning almost any real concurrent
burst bigger than 2 paid this same connection-establishment tax.
**Fixed**: raised to `min_size=5` (`max_size` unchanged at 10), verified
directly to bring a realistic 5-concurrent burst down to 3–5ms per query
(pool already warm at that concurrency). Not raised further without real
production telemetry to justify it — this is a bounded, low-risk change
(a handful of idle connections, negligible against Postgres's total
connection budget across every other service's own separately-sized
pool), not a capacity-planning decision this pass has evidence to make
beyond "2 was clearly too low, 5 handles a realistic burst."

**A second, related, self-inflicted finding while committing a real test
of this**: an initial version of `tests/integration/
test_bot_handlers.py::test_15_concurrent_balance_commands_all_succeed_with_measured_latency`
used 50 concurrent commands (matching the diagnostic script above) rather
than 15. Because that test reuses this test suite's own session-scoped
`pool` fixture — the same one every other test file's `gateway_server`/
`admin_server`/`payments_server` fixtures independently hold their own
separately-sized pools alongside — forcing it to suddenly open up to 50
new physical connections at once pushed the *combined* total across every
concurrently-active session-scoped pool past this Postgres instance's own
real `max_connections=100` ceiling, producing genuine
`TooManyConnectionsError: sorry, too many clients already` failures that
cascaded into dozens of unrelated tests across the full suite. Caught by
this pass's own required full-suite regression run (not shipped
unnoticed), root-caused, and fixed by lowering the test's own concurrency
to 15 — still enough to prove no crash, no lost reply, and a real latency
distribution, without competing for the same shared, finite connection
budget every other concurrently-running test file also depends on. Kept
here as a concrete instance of exactly the shared-resource risk §9 argues
against attempting at 500-1000 concurrency in this environment.

## 8. Connection reuse and retry classification (verified already correct)

Checked directly against this project's installed aiogram version's own
source, not assumed:

- **HTTP client reuse**: `Bot` uses aiogram's default `AiohttpSession`,
  which lazily creates exactly one `aiohttp.ClientSession` (backed by a
  `TCPConnector` with `limit=100` and a 1-hour DNS cache) for the whole
  process lifetime — `services/bot/app.py::build_bot()` builds one `Bot`
  once, in `main()`, never per-command. No new TCP/TLS connection per
  outgoing Telegram request.
- **Retry classification**: `Notifier._run()` already distinguishes
  `TelegramRetryAfter` (retryable — backs off for exactly
  `exc.retry_after` seconds, capped at 5 attempts) from
  `TelegramForbiddenError` (non-retryable — the user blocked the bot,
  nothing to retry) from any other exception (treated as non-retryable —
  most causes, like malformed HTML, would never succeed on retry; logged
  and dropped rather than retried or crashing the worker).
- **DB/Redis pool reuse**: `packages/core/db_pool.py`/`redis_conn.py`
  each build one pool per process at startup; no handler anywhere
  acquires a connection outside the existing `pool.fetchrow()`/
  `pool.execute()` idiom, and this pass's own `init=`/`redis_class=`
  additions attach hooks to that same one pool/client rather than
  building a second one.

No code changes were needed for any of the three — this section exists
so the parent directive's own Sections 13–15 aren't silently treated as
"not investigated" just because nothing changed.

## 9. What could not be verified from this sandbox

- No real Telegram bot token exists here (confirmed: `[ -f .env ]` is
  false), so `getWebhookInfo`/webhook health cannot be exercised against
  a real production webhook subscription — only against a faked
  `Bot.get_webhook_info()` in tests (`tests/integration/
  test_telegram_diagnostics_admin.py`,
  `tests/integration/test_admin_console_e2e.py`).
- Real production traffic patterns (concurrent users, real network
  latency to Telegram's API, real payment provider response times)
  cannot be reproduced here — the P50/P95/P99 engineering targets above
  are targets to measure against post-deploy, not numbers this pass
  claims to have hit.
- Controlled load testing at the scale the parent directive names (100+,
  500+, 1000+ concurrent) was not attempted against this shared
  environment: this session has already found and documented a real
  incident (`DECISIONS.md`) where an uncontrolled test load left ~3000+
  stale rows in this exact shared dev database, and the admin `/rooms`
  screen (unrelated to this pass's own changes) was independently found
  mid-way through this pass's own regression testing to already carry
  5,379 accumulated room rows from this session's cumulative testing —
  real evidence that this shared database is not a safe place to run a
  large synthetic load test without first isolating or resetting it.
  Small, targeted concurrency tests (a handful of simulated concurrent
  commands) are safe and already exist; a genuine 500–1000 concurrent
  Telegram-update load test needs a dedicated, disposable environment
  this sandbox is not.
