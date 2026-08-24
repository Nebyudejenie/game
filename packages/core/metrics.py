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

from prometheus_client import Counter, Gauge, Histogram

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
