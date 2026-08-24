"""Payout worker (spec section 8.3 steps 5-8, Prompt 8): consumes the
'payouts' Redis Stream with a real consumer group so more than one worker
replica can safely share the queue, and so a crashed worker's in-flight job
gets redelivered rather than lost.

Exactly-once semantics for the *provider* call are the provider's job, not
ours: our_ref is passed as Chapa's own idempotency reference (spec:
"the provider is called exactly once (via our_ref idempotency)"), so it is
safe -- not just tolerated -- for this worker to call create_payout() again
after a crash-and-redeliver. What this module guarantees on its own side is
that a payment already in a terminal state (succeeded/failed) is never
touched twice, and that the ledger settlement itself is idempotent via
ledger.post()'s idempotency_key.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import asyncpg
import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from packages.core import ledger
from packages.core.notifications import notify_user
from services.payments.provider import PaymentProvider
from services.payments.withdrawals import PAYOUT_STREAM

logger = structlog.get_logger()

GROUP = "payout-workers"
_PENDING_STATUSES = ("approved", "processing")


async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(PAYOUT_STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def process_one(
    pool: asyncpg.Pool, redis: Redis, provider: PaymentProvider, *, msg_id: str, our_ref: str
) -> str:
    """Returns 'succeeded' | 'failed' | 'skipped'. Always acks -- the only
    way a job is left unacked (and therefore redelivered) is this process
    dying mid-call, which is exactly the crash-recovery case the spec asks
    to be tested.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            payment = await conn.fetchrow(
                "SELECT id, user_id, amount, method_id, status FROM payments "
                "WHERE our_ref = $1 FOR UPDATE",
                our_ref,
            )
            if payment is None or payment["status"] not in _PENDING_STATUSES:
                await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
                return "skipped"

            if payment["status"] == "approved":
                await conn.execute(
                    "UPDATE payments SET status = 'processing', updated_at = now() WHERE id = $1",
                    payment["id"],
                )

            payment_id = payment["id"]
            user_id = payment["user_id"]
            amount = payment["amount"]
            method_id = payment["method_id"]

    method = await pool.fetchrow(
        "SELECT kind, account_ref, holder_name FROM payment_methods WHERE id = $1", method_id
    )
    assert method is not None
    method_payload: dict[str, str] = {
        "kind": method["kind"],
        "account_ref": method["account_ref"],
        "holder_name": method["holder_name"],
        "bank_code": method["kind"],
    }

    try:
        result = await provider.create_payout(method=method_payload, amount=amount, our_ref=our_ref)
    except Exception as exc:
        logger.error("payout_provider_error", our_ref=our_ref, error=str(exc))
        await _reverse(pool, payment_id=payment_id, user_id=user_id, amount=amount, our_ref=our_ref, reason=str(exc))
        await notify_user(pool, redis, user_id=user_id, key="notify.withdrawal_failed", amount=str(amount))
        await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
        return "failed"

    if result.status in ("succeeded", "processing"):
        await _settle_success(
            pool,
            payment_id=payment_id,
            user_id=user_id,
            amount=amount,
            our_ref=our_ref,
            provider_ref=result.provider_ref,
        )
        await notify_user(pool, redis, user_id=user_id, key="notify.withdrawal_succeeded", amount=str(amount))
        await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
        return "succeeded"

    await _reverse(
        pool, payment_id=payment_id, user_id=user_id, amount=amount, our_ref=our_ref,
        reason=f"provider reported status={result.status}",
    )
    await notify_user(pool, redis, user_id=user_id, key="notify.withdrawal_failed", amount=str(amount))
    await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
    return "failed"


async def _settle_success(
    pool: asyncpg.Pool, *, payment_id: int, user_id: int, amount: Decimal, our_ref: str, provider_ref: str
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await ledger.get_or_create_account(conn, user_id, "user_locked")
            provider_account = await ledger.get_or_create_account(conn, None, "provider_settlement")
            txn = await ledger.post(
                conn,
                "payout",
                [ledger.Entry(locked.id, -amount), ledger.Entry(provider_account.id, amount)],
                idempotency_key=f"payout-settle-{our_ref}",
                payment_id=payment_id,
            )
            await conn.execute(
                "UPDATE payments SET status = 'succeeded', provider_ref = $2, ledger_txn_id = $3, "
                "updated_at = now() WHERE id = $1",
                payment_id,
                provider_ref,
                txn.id,
            )


async def _reverse(
    pool: asyncpg.Pool, *, payment_id: int, user_id: int, amount: Decimal, our_ref: str, reason: str
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await ledger.get_or_create_account(conn, user_id, "user_locked")
            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            await ledger.post(
                conn,
                "refund",
                [ledger.Entry(locked.id, -amount), ledger.Entry(cash.id, amount)],
                idempotency_key=f"payout-reverse-{our_ref}",
                payment_id=payment_id,
            )
            await conn.execute(
                "UPDATE payments SET status = 'failed', failure_reason = $2, updated_at = now() "
                "WHERE id = $1",
                payment_id,
                reason,
            )


def _flatten(streams: Any) -> list[tuple[str, dict[str, str]]]:
    """redis-py's xreadgroup() return shape differs by version/protocol --
    a list of (stream_name, [(id, fields), ...]) pairs on some, a dict of
    {stream_name: [(id, fields), ...]} on others. Normalize both here so
    callers never need to know which one they got.
    """
    entries: list[tuple[str, dict[str, str]]] = []
    pairs = streams.items() if isinstance(streams, dict) else streams
    for _stream_name, messages in pairs:
        for msg_id, fields in messages:
            entries.append((msg_id, fields))
    return entries


async def process_next(
    pool: asyncpg.Pool, redis: Redis, provider: PaymentProvider, *, consumer_name: str = "worker-1"
) -> str | None:
    """Processes at most one job: this consumer's own still-pending entries
    first (crash recovery under the same consumer name), then a genuinely
    new one. Returns the outcome, or None if the stream was empty. Built as
    a single-shot step so tests can drive it deterministically instead of
    racing a background loop.
    """
    await ensure_group(redis)

    pending = await redis.xreadgroup(GROUP, consumer_name, {PAYOUT_STREAM: "0"}, count=1)
    entries = _flatten(pending)
    if not entries:
        fresh = await redis.xreadgroup(GROUP, consumer_name, {PAYOUT_STREAM: ">"}, count=1)
        entries = _flatten(fresh)
    if not entries:
        return None

    msg_id, fields = entries[0]
    return await process_one(pool, redis, provider, msg_id=msg_id, our_ref=fields["our_ref"])


async def run_forever(
    pool: asyncpg.Pool, redis: Redis, provider: PaymentProvider, *, consumer_name: str = "worker-1"
) -> None:
    await ensure_group(redis)
    while True:
        pending = await redis.xreadgroup(GROUP, consumer_name, {PAYOUT_STREAM: "0"}, count=10)
        entries = _flatten(pending)
        if not entries:
            fresh = await redis.xreadgroup(
                GROUP, consumer_name, {PAYOUT_STREAM: ">"}, count=10, block=5000
            )
            entries = _flatten(fresh)
        for msg_id, fields in entries:
            await process_one(pool, redis, provider, msg_id=msg_id, our_ref=fields["our_ref"])
