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
