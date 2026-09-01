"""Deposit domain logic (spec section 8.2, Prompt 7).

The webhook and the polling fallback share one crediting path
(_apply_confirmed_status) so "a webhook arriving after a successful poll is
a no-op" is a structural guarantee, not a coincidence of two separately
written code paths agreeing by luck: both dedupe on (provider, event_id),
both lock the same payments row before checking its status, and both credit
through packages.core.ledger with idempotency_key = our_ref -- the same
belt-and-suspenders idempotency the round engine's settlement uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import ledger, metrics, rate_limit, responsible_gaming, tracing
from packages.core.ledger import AsyncpgConnection
from packages.core.notifications import notify_user
from services.payments.provider import PaymentProvider

logger = structlog.get_logger()
_tracer = tracing.get_tracer(__name__)

# Anything a webhook or a poll can report the underlying transaction is in.
_TERMINAL_FAILURE_STATUSES = ("failed", "cancelled")


class DepositRejected(Exception):
    """Base class -- catch this to handle any rejection generically. Each
    concrete reason is its own subclass (mirroring services/bot/registration
    .py's ContactMismatch/InvalidPhone/PhoneAlreadyRegistered) rather than a
    single exception with a string `.reason` field, so a caller can match on
    type and so services/bot/handlers.py's AST-based no-hardcoded-strings
    check never has to see a reason->locale-key string table.
    """


class DepositRateLimited(DepositRejected):
    pass


class BelowMinimumDeposit(DepositRejected):
    pass


class DailyDepositCapExceeded(DepositRejected):
    pass


class DepositorSelfExcluded(DepositRejected):
    pass


class DepositorBanned(DepositRejected):
    pass


class DepositorCoolingOff(DepositRejected):
    pass


class UnknownDepositor(DepositRejected):
    pass


class DepositProviderError(DepositRejected):
    pass


@dataclass(frozen=True)
class DepositIntent:
    payment_id: int
    our_ref: str
    checkout_url: str


async def _check_deposit_rate_limit_and_minimum(
    redis: Redis, *, user_id: int, amount: Decimal, min_deposit: Decimal
) -> None:
    """Raises DepositRateLimited / BelowMinimumDeposit. No DB access needed
    -- callable before a connection is even acquired, exactly where
    create_deposit_intent() already ran these two checks. Shared with
    services/payments/manual.py's create_manual_deposit_request() so a
    manual deposit is gated by the exact same rules an automatic one is,
    not a second copy that could quietly drift from this one.
    """
    # Rate-limited before anything else (spec section 9.2: "deposit
    # 5/hour") -- a cheap Redis round-trip gates every DB write and
    # provider call below it, the same rate-limit-first ordering
    # services/gateway/connection.py's own _run_action() uses.
    if not await rate_limit.allow(redis, "deposit", str(user_id), **rate_limit.DEPOSIT):
        raise DepositRateLimited(f"user {user_id} exceeded the deposit rate limit")

    if amount < min_deposit:
        raise BelowMinimumDeposit(f"amount {amount} is below the minimum {min_deposit}")


async def _check_deposit_eligibility(
    conn: AsyncpgConnection, *, user_id: int, amount: Decimal, daily_cap: Decimal
) -> None:
    """Raises UnknownDepositor / DepositorSelfExcluded / DepositorBanned /
    DepositorCoolingOff / DailyDepositCapExceeded. Must be called with
    `conn` already acquired (a caller-owned transaction for the manual
    path; create_deposit_intent()'s own bare `pool.acquire()` block for
    the automatic path, unchanged from before this was extracted). See
    _check_deposit_rate_limit_and_minimum's own docstring for why this is
    split out at all.
    """
    user_status = await conn.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    if user_status is None:
        raise UnknownDepositor(f"user {user_id} does not exist")
    if user_status == "self_excluded":
        raise DepositorSelfExcluded(f"user {user_id} is self-excluded")
    if user_status == "banned":
        raise DepositorBanned(f"user {user_id} is banned")

    limits = await responsible_gaming.get_or_create_limits(conn, user_id)
    if limits.cooloff_until is not None and datetime.now(UTC) < limits.cooloff_until:
        raise DepositorCoolingOff(f"user {user_id} is cooling off until {limits.cooloff_until}")

    user_cap = responsible_gaming.effective_deposit_cap(limits)
    effective_cap = min(daily_cap, user_cap) if user_cap is not None else daily_cap

    # Ethiopian calendar day, not the Postgres session's ambient
    # (UTC-by-default) one -- see DECISIONS.md's "day-boundary timezone
    # mismatch" entry; a bare date_trunc('day', now()) resets this cap
    # three hours early/late every night against the calendar day players
    # actually experience.
    today_total = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0) FROM payments
        WHERE user_id = $1 AND direction = 'in'
          AND status IN ('pending', 'processing', 'succeeded')
          AND created_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'
        """,
        user_id,
    )
    if today_total + amount > effective_cap:
        raise DailyDepositCapExceeded(f"user {user_id} would exceed the daily cap {effective_cap}")


async def create_deposit_intent(
    pool: asyncpg.Pool,
    redis: Redis,
    provider: PaymentProvider,
    *,
    user_id: int,
    amount: Decimal,
    phone_e164: str,
    return_url: str,
    callback_url: str,
    min_deposit: Decimal,
    daily_cap: Decimal,
) -> DepositIntent:
    with _tracer.start_as_current_span(
        "deposit.create_intent", attributes={"user_id": user_id, "amount": str(amount)}
    ) as span:
        await _check_deposit_rate_limit_and_minimum(
            redis, user_id=user_id, amount=amount, min_deposit=min_deposit
        )

        async with pool.acquire() as conn:
            await _check_deposit_eligibility(conn, user_id=user_id, amount=amount, daily_cap=daily_cap)

            ref_row = await conn.fetchrow(
                "SELECT 'DEP-' || extract(year from now())::text || '-' || "
                "lpad(nextval('payment_ref_seq')::text, 6, '0') AS our_ref"
            )
            assert ref_row is not None
            our_ref: str = ref_row["our_ref"]
            span.set_attribute("deposit.our_ref", our_ref)

            payment_row = await conn.fetchrow(
                """
                INSERT INTO payments (user_id, direction, provider, our_ref, amount, status)
                VALUES ($1, 'in', $2, $3, $4, 'pending')
                RETURNING id
                """,
                user_id,
                provider.name,
                our_ref,
                amount,
            )
            assert payment_row is not None
            payment_id: int = payment_row["id"]

        try:
            with _tracer.start_as_current_span("deposit.provider_checkout") as checkout_span:
                checkout_span.set_attribute("provider.name", provider.name)
                checkout = await provider.create_checkout(
                    amount=amount,
                    user_ref=phone_e164,
                    our_ref=our_ref,
                    return_url=return_url,
                    callback_url=callback_url,
                )
        except Exception as exc:
            await pool.execute(
                "UPDATE payments SET status = 'failed', failure_reason = $2, updated_at = now() "
                "WHERE id = $1",
                payment_id,
                str(exc),
            )
            raise DepositProviderError(f"provider {provider.name} rejected checkout creation") from exc

        await pool.execute(
            "UPDATE payments SET status = 'processing', provider_ref = $2, raw_response = $3, "
            "updated_at = now() WHERE id = $1",
            payment_id,
            checkout.provider_ref,
            json.dumps(checkout.raw_response, default=str),
        )

        return DepositIntent(payment_id=payment_id, our_ref=our_ref, checkout_url=checkout.checkout_url)


async def _apply_confirmed_status(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    our_ref: str,
    event_id: str,
    provider_name: str,
    status: str,
    amount: Decimal | None,
    provider_ref: str,
    raw: dict[str, object],
) -> str:
    """Returns 'credited' | 'duplicate' | 'amount_mismatch' | 'not_found' |
    'not_succeeded' | 'pending'. Never raises for a business-logic outcome
    -- only a genuine bug (a broken query, a dead connection) escapes as
    an exception.

    metrics.deposit_outcomes_total only counts 'credited' /
    'not_succeeded' / 'amount_mismatch' -- the three real terminal outcomes
    "deposit success rate" (spec section 10.4) means. 'not_found' isn't a
    real deposit attempt on our side, 'duplicate' is a replay of an
    outcome already counted once, and 'pending' (a status
    poll_pending_deposits() can genuinely see mid-flight, e.g. Chapa's
    ChapaProvider.fetch_status() 404 case) is not terminal either -- it
    must never increment the metric or update the payment row, since the
    same deposit can still go on to 'credited' later. Counting it as
    'not_succeeded' would double-count one real deposit across two
    outcome labels and understate the success rate the Grafana dashboard
    shows (a real bug this docstring's own claim above used to
    contradict, caught by a code review pass, not a test).
    """
    user_id: int | None = None

    with _tracer.start_as_current_span(
        "deposit.apply_confirmed_status", attributes={"our_ref": our_ref, "status": status}
    ) as span:
        async with pool.acquire() as conn:
            async with conn.transaction():
                payment = await conn.fetchrow(
                    "SELECT id, user_id, amount, status FROM payments WHERE our_ref = $1 FOR UPDATE",
                    our_ref,
                )
                if payment is None:
                    logger.warning("payment_webhook_unknown_ref", our_ref=our_ref, provider=provider_name)
                    span.set_attribute("deposit.outcome", "not_found")
                    return "not_found"

                event_row = await conn.fetchrow(
                    """
                    INSERT INTO payment_events (payment_id, provider, event_id, signature_ok, payload)
                    VALUES ($1, $2, $3, true, $4)
                    ON CONFLICT (provider, event_id) DO NOTHING
                    RETURNING id
                    """,
                    payment["id"],
                    provider_name,
                    event_id,
                    json.dumps(raw, default=str),
                )
                if event_row is None or payment["status"] == "succeeded":
                    span.set_attribute("deposit.outcome", "duplicate")
                    return "duplicate"

                if status != "succeeded":
                    if status not in _TERMINAL_FAILURE_STATUSES:
                        # Genuinely still in flight (e.g. Chapa's own
                        # "pending") -- not a real outcome yet, so it must
                        # not touch the payment row or the metric. The
                        # same deposit can still go on to 'credited'.
                        span.set_attribute("deposit.outcome", "pending")
                        return "pending"
                    await conn.execute(
                        "UPDATE payments SET status = $2, updated_at = now() WHERE id = $1",
                        payment["id"],
                        status,
                    )
                    metrics.deposit_outcomes_total.labels(outcome="not_succeeded").inc()
                    span.set_attribute("deposit.outcome", "not_succeeded")
                    return "not_succeeded"

                if amount is None or amount != payment["amount"]:
                    logger.error(
                        "payment_amount_mismatch",
                        our_ref=our_ref,
                        provider_amount=str(amount),
                        our_amount=str(payment["amount"]),
                    )
                    await conn.execute(
                        "UPDATE payments SET status = 'review', updated_at = now() WHERE id = $1",
                        payment["id"],
                    )
                    metrics.deposit_outcomes_total.labels(outcome="amount_mismatch").inc()
                    span.set_attribute("deposit.outcome", "amount_mismatch")
                    return "amount_mismatch"

                provider_account = await ledger.get_or_create_account(conn, None, "provider_settlement")
                cash_account = await ledger.get_or_create_account(conn, payment["user_id"], "user_cash")
                txn = await ledger.post(
                    conn,
                    "deposit",
                    [
                        ledger.Entry(provider_account.id, -payment["amount"]),
                        ledger.Entry(cash_account.id, payment["amount"]),
                    ],
                    idempotency_key=our_ref,
                    payment_id=payment["id"],
                )
                await conn.execute(
                    "UPDATE payments SET status = 'succeeded', provider_ref = $2, ledger_txn_id = $3, "
                    "updated_at = now() WHERE id = $1",
                    payment["id"],
                    provider_ref,
                    txn.id,
                )
                user_id = payment["user_id"]
                credited_amount = payment["amount"]

        assert user_id is not None
        # Only reachable once the transaction above has actually
        # committed -- see ledger.post()'s own comment for why it can't
        # safely record this itself when called nested, which every real
        # call is. Matches this function's own already-established
        # deposit_outcomes_total placement right below.
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
        snapshot = await ledger.publish_balance_update(pool, redis, user_id)
        await notify_user(
            pool, redis, user_id=user_id, key="notify.deposit_confirmed",
            amount=str(credited_amount), balance=snapshot["cash"],
        )
        metrics.deposit_outcomes_total.labels(outcome="credited").inc()
        span.set_attribute("deposit.outcome", "credited")
        return "credited"


async def handle_webhook(
    pool: asyncpg.Pool, redis: Redis, provider: PaymentProvider, *, headers: dict[str, str], raw_body: bytes
) -> str:
    """Raises InvalidSignature (caller must return 401) before touching the
    database at all -- an unverified payload is never trusted enough to
    even look up a payment by its claimed our_ref.
    """
    event = provider.verify_webhook(headers, raw_body)
    return await _apply_confirmed_status(
        pool,
        redis,
        our_ref=event.our_ref,
        event_id=event.event_id,
        provider_name=provider.name,
        status=event.status,
        amount=event.amount,
        provider_ref=event.provider_ref,
        raw=event.raw,
    )


async def poll_pending_deposits(
    pool: asyncpg.Pool, redis: Redis, provider: PaymentProvider, *, older_than_seconds: int = 30
) -> int:
    """Status-polling fallback for a webhook that never arrives. Applies the
    exact same crediting path as handle_webhook, so it's safe to run this on
    a timer regardless of whether the webhook eventually shows up too.
    Returns how many payments this pass actually credited.
    """
    rows = await pool.fetch(
        "SELECT our_ref FROM payments WHERE direction = 'in' AND status = 'processing' "
        "AND provider = $2 AND updated_at < now() - make_interval(secs => $1)",
        older_than_seconds,
        provider.name,
    )

    credited = 0
    for row in rows:
        our_ref = row["our_ref"]
        result = await provider.fetch_status(our_ref)
        outcome = await _apply_confirmed_status(
            pool,
            redis,
            our_ref=our_ref,
            event_id=f"poll:{our_ref}:{result.status}",
            provider_name=provider.name,
            status=result.status,
            amount=result.amount,
            provider_ref=result.provider_ref or our_ref,
            raw=result.raw,
        )
        if outcome == "credited":
            credited += 1
    return credited


@dataclass(frozen=True)
class SettlementRecord:
    our_ref: str
    amount: Decimal
    status: str


@dataclass(frozen=True)
class ReconciliationMismatch:
    our_ref: str
    reason: str
    our_status: str | None
    our_amount: Decimal | None
    provider_status: str | None
    provider_amount: Decimal | None


def reconcile(
    our_payments: list[dict[str, object]], provider_report: list[SettlementRecord]
) -> list[ReconciliationMismatch]:
    """Pure comparison of our own payments rows against a provider's
    settlement report (spec: "pull the provider's settlement report, match
    on our_ref, and flag any payment where provider and ledger disagree" --
    the hourly job this runs inside just has to fetch both lists and call
    this). Kept provider-fetch-free so it's testable without a live rail,
    the same reasoning as packages.core.ledger.reconcile().
    """
    provider_by_ref = {r.our_ref: r for r in provider_report}
    mismatches: list[ReconciliationMismatch] = []

    for payment in our_payments:
        our_ref = str(payment["our_ref"])
        provider_row = provider_by_ref.pop(our_ref, None)
        our_status = str(payment["status"])
        our_amount = Decimal(str(payment["amount"]))

        if provider_row is None:
            if our_status == "succeeded":
                mismatches.append(
                    ReconciliationMismatch(
                        our_ref, "missing_from_provider_report", our_status, our_amount, None, None
                    )
                )
            continue

        provider_succeeded = provider_row.status == "succeeded"
        our_succeeded = our_status == "succeeded"
        if provider_succeeded != our_succeeded:
            mismatches.append(
                ReconciliationMismatch(
                    our_ref,
                    "status_disagreement",
                    our_status,
                    our_amount,
                    provider_row.status,
                    provider_row.amount,
                )
            )
        elif provider_succeeded and provider_row.amount != our_amount:
            mismatches.append(
                ReconciliationMismatch(
                    our_ref,
                    "amount_disagreement",
                    our_status,
                    our_amount,
                    provider_row.status,
                    provider_row.amount,
                )
            )

    for orphan_ref, orphan_row in provider_by_ref.items():
        if orphan_row.status == "succeeded":
            mismatches.append(
                ReconciliationMismatch(
                    orphan_ref, "missing_from_our_records", None, None, orphan_row.status, orphan_row.amount
                )
            )

    return mismatches


async def run_provider_reconciliation(
    pool: asyncpg.Pool, provider: PaymentProvider, *, since_hours: int = 2
) -> list[ReconciliationMismatch]:
    """The real, runnable wiring reconcile() itself was always missing --
    an architecture audit found the pure comparison above fully built and
    tested (tests/unit/test_payment_reconciliation.py) but never called
    from anywhere in production: no job, no route, no scheduled sweep. This
    is that job's real logic (payout_worker.py's main_async() runs it on a
    timer, the same "share the one already-running process" pattern as
    poll_pending_deposits() and sweep_stuck_approved_payouts()).

    Built from provider.fetch_status() -- the same real, already-documented,
    already-tested single-transaction GET /v1/transaction/verify/{tx_ref}
    poll_pending_deposits() already calls -- once per payment being
    reconciled, deliberately NOT a bulk settlement-report endpoint: Chapa's
    own docs (developer.chapa.co/docs/apis) are currently stuck in a real,
    confirmed HTTP redirect loop, so there's no way to verify one exists or
    what it would return, and guessing at an undocumented endpoint is
    exactly the kind of unverified integration this project has always
    refused to fabricate. This means the "missing_from_our_records" case
    below (a provider-side transaction we never logged at all -- both the
    webhook AND poll_pending_deposits somehow missing it) structurally
    can't be caught by this specific implementation, since it only ever
    checks our_refs we already know about; a real bulk report would be
    needed to catch that one case. Everything else reconcile() can detect
    -- a status or amount disagreement on a payment we DO have -- this
    catches for real.

    since_hours=2: this job runs hourly (this module's own reconcile()
    docstring, quoting spec) -- one full cycle of margin against a late
    run, not a re-check of every deposit ever made. An operational timing
    choice, the same category as poll_pending_deposits()'s own
    older_than_seconds=30 and sweep_stuck_approved_payouts()'s
    older_than_seconds=60 already make in this codebase, not a business
    parameter.
    """
    rows = await pool.fetch(
        "SELECT our_ref, amount, status FROM payments WHERE direction = 'in' "
        "AND provider = $1 AND updated_at > now() - make_interval(hours => $2)",
        provider.name,
        since_hours,
    )
    our_payments: list[dict[str, object]] = [dict(row) for row in rows]

    provider_report: list[SettlementRecord] = []
    for row in rows:
        result = await provider.fetch_status(row["our_ref"])
        if result.amount is None:
            # Chapa doesn't recognize this our_ref at all (its own
            # fetch_status() returns this for a 404) -- leaving it out of
            # provider_report is what lets reconcile()'s own "no provider
            # row" branch flag it correctly, but only when our own side
            # thinks it succeeded; not yet knowing about a payment we
            # ourselves still show as pending/failed isn't a mismatch.
            continue
        provider_report.append(
            SettlementRecord(our_ref=row["our_ref"], amount=result.amount, status=result.status)
        )

    mismatches = reconcile(our_payments, provider_report)
    metrics.payment_reconciliation_mismatch_count.set(len(mismatches))
    if mismatches:
        logger.error(
            "payment_reconciliation_mismatch",
            mismatch_count=len(mismatches),
            mismatches=[
                {
                    "our_ref": m.our_ref,
                    "reason": m.reason,
                    "our_status": m.our_status,
                    "provider_status": m.provider_status,
                }
                for m in mismatches
            ],
        )
    return mismatches
