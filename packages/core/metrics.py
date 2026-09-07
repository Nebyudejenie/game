"""Prometheus metrics (spec section 10.4): "concurrent connections, rooms
active, calls/sec, call-to-ack p50/p95/p99, claim validation time, ledger
txn/sec, deposit success rate, payout queue depth, house revenue live."

One shared module so every service (gateway, engine, payments) registers
against the same metric names on the default registry. In production each
service is its own process with its own registry -- Prometheus scrapes
every instance's own `/metrics` and aggregates with sum()/rate(), the
standard multi-service pattern. Within a single test process this module
is simply imported once, so every fixture's app instance shares the one
set of counters -- exactly what a real test asserting "this action
incremented that counter" needs.

**"Call-to-ack" is read here as the gateway's own command round-trip**
(join/drop_card/set_auto/claim: client sends a command, server replies
"ack"/"claim_result"), not the bingo number call broadcast -- that's the
concrete "call ... ack" pair the WS protocol already names
(`services/gateway/connection.py`'s own `{"t": "ack", ...}` frame), and
"claim validation time" is already called out as its own separate metric
right next to it in the spec, which only makes sense if "call-to-ack"
covers the other three actions too. Documented as an interpretation call,
not a literal spec quote, in DECISIONS.md.
"""

from __future__ import annotations

from aiohttp import web
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# --- gateway -----------------------------------------------------------

gateway_connections = Gauge(
    "gateway_connections", "Currently connected WebSocket clients"
)

gateway_command_ack_seconds = Histogram(
    "gateway_command_ack_seconds",
    "Time from receiving a client command to sending its ack/claim_result",
    ["action"],
)

# --- engine ----------------------------------------------------------------

engine_rooms_active = Gauge(
    "engine_rooms_active", "Rooms currently in lobby, running, or settling"
)

engine_calls_total = Counter(
    "engine_calls_total", "Bingo numbers called across all rounds"
)

engine_claim_validation_seconds = Histogram(
    "engine_claim_validation_seconds", "Time to validate one claim() call"
)

engine_rounds_voided_total = Counter(
    "engine_rounds_voided_total",
    "Rounds voided and refunded (underfilled lobby, exhausted draw, crash recovery)",
)

# --- ledger ------------------------------------------------------------------

ledger_transactions_total = Counter(
    "ledger_transactions_total",
    "Ledger transactions actually written (idempotent replays are not counted again)",
    ["kind"],
)

# --- payments ------------------------------------------------------------

deposit_outcomes_total = Counter(
    "deposit_outcomes_total", "Deposit crediting outcomes", ["outcome"]
)

payout_queue_depth = Gauge(
    "payout_queue_depth", "Pending entries in the payout Redis stream"
)

house_revenue_total = Gauge(
    "house_revenue_total", "Live house_revenue account balance (ETB)"
)

payment_reconciliation_mismatch_count = Gauge(
    "payment_reconciliation_mismatch_count",
    "Deposits whose provider status/amount disagreed with our own payments row on the last reconciliation pass",
)

ledger_reconciliation_sweep_mismatch_count = Gauge(
    "ledger_reconciliation_sweep_mismatch_count",
    "Accounts whose cached balance disagreed with their ledger entries on the last in-process reconciliation sweep",
)
# Distinct from ledger_reconciliation_mismatch_count above: that one lives
# on reconcile_registry and is pushed to a Pushgateway by the standalone
# packages/core/reconcile_job.py CLI (never actually scheduled anywhere --
# see that module's own docstring). This one is on the default registry so
# payout_worker.py's existing /metrics endpoint (already scraped by the
# "payout-worker" Prometheus job, same as payment_reconciliation_mismatch_
# count right above) serves it for free from a sweep that *is* actually
# running, reusing this codebase's existing periodic-sweep architecture
# instead of standing up a new scheduler.

telebirr_redemption_outcomes_total = Counter(
    "telebirr_redemption_outcomes_total",
    "Telebirr SMS-evidence redemption attempts by outcome",
    ["outcome"],
)

telebirr_ingestion_total = Counter(
    "telebirr_ingestion_total",
    "Telebirr SMS ingestion attempts by outcome (ingested_available, ingested_rejected, "
    "duplicate, conflicting_duplicate, unparseable)",
    ["outcome"],
)

telebirr_parser_failures_total = Counter(
    "telebirr_parser_failures_total",
    "Telebirr SMS messages that failed to parse, by reason",
    ["reason"],
)

telebirr_evidence_reconciliation_mismatch_count = Gauge(
    "telebirr_evidence_reconciliation_mismatch_count",
    "Telebirr payments rows whose linked payment_evidence count is not exactly 1 "
    "on the last reconciliation pass",
)

telebirr_evidence_by_status = Gauge(
    "telebirr_evidence_by_status",
    "Current payment_evidence row count by status",
    ["status"],
)

notification_campaign_deliveries_total = Counter(
    "notification_campaign_deliveries_total",
    "Notification Center campaign deliveries by terminal outcome "
    "(delivered, blocked, gave_up, failed)",
    ["outcome"],
)

# --- bot / Telegram (command latency diagnosis pass) --------------------
#
# Every one of these is populated from a single choke point each, the same
# discipline as gateway_command_ack_seconds above:
#   - telegram_updates_received_total/telegram_updates_deduplicated_total:
#     services/bot/app.py's own _dedup_middleware, which every update of
#     every type already passes through (spec section 5's dedup gate).
#   - telegram_commands_total/_success_total/_error_total/_latency_seconds:
#     services/bot/perf.py's perf_middleware, an inner middleware on the
#     message observer -- runs only once routing has matched a specific
#     handler, labeled by that handler's own function name (cmd_balance,
#     on_menu_text, ...) so a label always names a real function in
#     handlers.py, never an invented category.
#   - telegram_db_duration_seconds: asyncpg's own Connection.add_query_logger
#     hook (asyncpg >= 0.29), attached once per physical connection via
#     create_pool()'s `init` callback -- not a wrapper around every
#     pool.fetchrow()/execute() call site, so no handler code changes at
#     all were needed to get real per-query timing.
#   - telegram_redis_duration_seconds: a single Redis.execute_command()
#     override (services/bot/perf.py's _TimedRedis) -- every redis-py
#     command funnels through that one method.
#   - telegram_api_duration_seconds: services/bot/notifier.py's own
#     Notifier._run(), the sole call site of bot.send_message() in this
#     codebase.
# Both the DB and Redis histograms are labeled by the same "handler" name
# as the command metrics, via a single contextvars.ContextVar
# (services/bot/perf.py's _current_command) set for the duration of each
# handler's own execution -- correctly isolated per concurrent update
# since aiogram processes each webhook delivery in its own asyncio task,
# and contextvars snapshot per-task.
telegram_updates_received_total = Counter(
    "telegram_updates_received_total", "Telegram updates delivered to the webhook, before dedup"
)

telegram_updates_deduplicated_total = Counter(
    "telegram_updates_deduplicated_total",
    "Updates dropped by dedup.py as an already-seen update_id (retried delivery, not a new event)",
)

telegram_commands_total = Counter(
    "telegram_commands_total", "Bot handler invocations, by handler function name", ["handler"]
)

telegram_command_success_total = Counter(
    "telegram_command_success_total",
    "Bot handler invocations that returned without raising",
    ["handler"],
)

telegram_command_error_total = Counter(
    "telegram_command_error_total", "Bot handler invocations that raised", ["handler"]
)

# Telegram Command Center (Phase 2): a player still attempting a command
# an admin has disabled via the bot_commands registry -- counted
# separately from telegram_commands_total/_success_total/_error_total
# (services/bot/command_registry.py's own gate middleware short-circuits
# before perf_middleware even starts timing), so an admin disabling a
# command doesn't silently pollute that command's own latency/error
# stats, while still giving real visibility into "how many people hit
# this while it was off".
telegram_command_blocked_total = Counter(
    "telegram_command_blocked_total",
    "Command invocations rejected because an admin disabled that command",
    ["handler"],
)

# Phase 3: distinct from telegram_command_blocked_total above (an admin's
# own deliberate choice) -- this is a specific user hitting their own
# configured cooldown_seconds or rate_limit_per_minute, split by which of
# the two ("cooldown" or "rate_limit") to tell them apart on Grafana.
telegram_command_rate_limited_total = Counter(
    "telegram_command_rate_limited_total",
    "Command invocations rejected by the per-user cooldown or rate limit",
    ["handler", "limit_type"],
)

_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)

telegram_command_latency_seconds = Histogram(
    "telegram_command_latency_seconds",
    "Wall-clock time from the start of handler execution to its completion, by handler name -- "
    "the 'total command latency' figure engineering targets (P50/P95/P99) are measured against",
    ["handler"],
    buckets=_LATENCY_BUCKETS,
)

telegram_dispatch_delay_seconds = Histogram(
    "telegram_dispatch_delay_seconds",
    "Time from _dedup_middleware (immediately after webhook receipt) to the matched handler "
    "actually starting -- routing/filter-check overhead, not network delay",
    buckets=_LATENCY_BUCKETS,
)

telegram_db_duration_seconds = Histogram(
    "telegram_db_duration_seconds",
    "Time spent inside individual DB queries while handling one command, by handler name",
    ["handler"],
    buckets=_LATENCY_BUCKETS,
)

telegram_redis_duration_seconds = Histogram(
    "telegram_redis_duration_seconds",
    "Time spent inside individual Redis commands while handling one command, by handler name",
    ["handler"],
    buckets=_LATENCY_BUCKETS,
)

telegram_api_duration_seconds = Histogram(
    "telegram_api_duration_seconds",
    "Time spent inside a single bot.send_message() call (Notifier's own outbound send, "
    "excluding queueing/backoff wait time) -- isolates Telegram's own response time from "
    "this application's processing time",
    buckets=_LATENCY_BUCKETS,
)

telegram_webhook_pending_updates = Gauge(
    "telegram_webhook_pending_updates",
    "Telegram's own reported pending_update_count from the last getWebhookInfo poll",
)

telegram_webhook_last_error_unixtime = Gauge(
    "telegram_webhook_last_error_unixtime",
    "Unix timestamp of Telegram's own last_error_date from the last getWebhookInfo poll, "
    "0 if none reported",
)

# --- reconcile_job -----------------------------------------------------

# A one-shot batch job (packages/core/reconcile_job.py), not a scraped
# long-running process -- pushed to a Prometheus Pushgateway instead of
# exposed on a /metrics endpoint, the standard pattern for batch jobs. Its
# own registry, separate from the default one every other metric in this
# module registers to, so pushing it never drags along an unrelated
# snapshot of whatever else happens to share this process (in reconcile_job
# itself, nothing else does; in a test process, everything else would).
reconcile_registry = CollectorRegistry()
ledger_reconciliation_mismatch_count = Gauge(
    "ledger_reconciliation_mismatch_count",
    "Accounts whose cached balance disagreed with their ledger entries on the last reconciliation run",
    registry=reconcile_registry,
)

# --- sms control plane -------------------------------------------------

sms_messages_created_total = Counter(
    "sms_messages_created_total",
    "SMS messages created, by initial status (queued or suppressed)",
    ["status"],
)

sms_delivery_attempts_total = Counter(
    "sms_delivery_attempts_total",
    "SMS delivery attempts reported by a node, by outcome",
    ["outcome"],
)

sms_messages_reconciled_total = Counter(
    "sms_messages_reconciled_total",
    "Messages the timeout-based reconciliation sweep moved out of an in-flight state",
    ["to_status"],
)

sms_queue_depth = Gauge(
    "sms_queue_depth",
    "Messages currently queued, by priority",
    ["priority"],
)

sms_node_health_score = Gauge(
    "sms_node_health_score",
    "Last-computed 0-100 health score per active delivery node",
    ["node_id", "node_name"],
)

# --- bare /metrics server, for the two long-running processes with no
# other HTTP surface of their own (engine worker, payout worker) --------

# A real, pre-existing gap this closes: services/engine/round_engine.py
# and services/payments/payout_worker.py already record real metrics
# (engine_calls_total, engine_rooms_active, and friends) against this
# module's own default registry, but nothing ever served them anywhere in
# production -- gateway/admin/payments/bot each define their own
# framework-native /metrics route, but the engine worker and payout
# worker are plain background loops with no HTTP surface at all. One
# tiny, shared aiohttp app (not a full FastAPI app, to avoid pulling in a
# second web framework for one endpoint) rather than duplicating this
# same handful of lines in both entrypoints.
async def start_metrics_server(port: int) -> web.AppRunner:
    """Starts a bare /metrics-only HTTP server in the background. Returns
    the AppRunner so the caller can `await runner.cleanup()` on shutdown.
    """
    app = web.Application()

    async def _metrics(_request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), content_type="text/plain", charset="utf-8")

    app.router.add_get("/metrics", _metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    return runner
