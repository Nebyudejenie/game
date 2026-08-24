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

from packages.core import ledger, metrics, responsible_gaming, tracing
from packages.core.notifications import notify_user
from services.gateway.queries import user_balance_snapshot
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


async def create_deposit_intent(
    pool: asyncpg.Pool,
    provider: PaymentProvider,
    *,
    user_id: int,
    amount: Decimal,
    phone_e164: str,
    return_url: str,
    min_deposit: Decimal,
    daily_cap: Decimal,
) -> DepositIntent:
    with _tracer.start_as_current_span(
        "deposit.create_intent", attributes={"user_id": user_id, "amount": str(amount)}
    ) as span:
        if amount < min_deposit:
            raise BelowMinimumDeposit(f"amount {amount} is below the minimum {min_deposit}")

        async with pool.acquire() as conn:
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

            today_total = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount), 0) FROM payments
                WHERE user_id = $1 AND direction = 'in'
                  AND status IN ('pending', 'processing', 'succeeded')
                  AND created_at >= date_trunc('day', now())
                """,
                user_id,
            )
            if today_total + amount > effective_cap:
                raise DailyDepositCapExceeded(f"user {user_id} would exceed the daily cap {effective_cap}")

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
                    amount=amount, user_ref=phone_e164, our_ref=our_ref, return_url=return_url
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
    'not_succeeded'. Never raises for a business-logic outcome -- only a
    genuine bug (a broken query, a dead connection) escapes as an exception.

    metrics.deposit_outcomes_total only counts 'credited' /
    'not_succeeded' / 'amount_mismatch' -- the three real terminal outcomes
    "deposit success rate" (spec section 10.4) means. 'not_found' isn't a
    real deposit attempt on our side, and 'duplicate' is a replay of an
    outcome already counted once.
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
                    if status in _TERMINAL_FAILURE_STATUSES:
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
        snapshot = await _publish_balance_update(pool, redis, user_id)
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


async def _publish_balance_update(pool: asyncpg.Pool, redis: Redis, user_id: int) -> dict[str, str]:
    snapshot = await user_balance_snapshot(pool, user_id)
    await redis.publish(f"user:{user_id}", json.dumps({"t": "balance_update", **snapshot}))
    return snapshot


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
