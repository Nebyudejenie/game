"""Payout worker (spec section 8.3 steps 5-8, Prompt 8): consumes the
'payouts' Redis Stream with a real consumer group so more than one worker
replica can safely share the queue, and so a crashed worker's in-flight job
gets redelivered rather than lost -- to *any* live consumer, not only a
replacement process that happens to reuse the exact same consumer name
(XAUTOCLAIM in process_next()/run_forever() reclaims a stale entry from
whichever consumer originally owned it once it's been idle too long; a
real gap a code review pass caught, since the original crash-recovery
check only ever looked at the *current* consumer's own pending list).

Exactly-once semantics for the *provider* call are the provider's job, not
ours: our_ref is passed as Chapa's own idempotency reference (spec:
"the provider is called exactly once (via our_ref idempotency)"), so it is
safe -- not just tolerated -- for this worker to call create_payout() again
after a crash-and-redeliver. What this module guarantees on its own side is
that a payment already in a terminal state (succeeded/failed) is never
touched twice, and that the ledger settlement itself is idempotent via
ledger.post()'s idempotency_key.

A provider result of "processing" (Chapa merely *accepted* the transfer
request, with no confirmation it actually completed) is deliberately never
treated as settled -- see process_one()'s own comment on that branch. This
codebase has no payout webhook route and no status-polling fallback for
outbound transfers (unlike services/payments/deposits.py's own
poll_pending_deposits() for inbound ones), so there is currently no way to
learn a "processing" transfer later actually failed; wrongly marking it
succeeded here would be a silent, permanent, unrecoverable loss with no
signal anywhere. A payment left at status='processing' is a real,
currently-unresolved gap in this module's coverage, not a bug being
papered over -- see withdrawals.list_stuck_processing_payouts() for the
operator-visibility this converts it into, and DECISIONS.md for why a full
automated fix remains blocked on Chapa's transfer-status response
vocabulary rather than guessed at.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import asyncpg
import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from packages.core import ledger, tracing
from packages.core.notifications import notify_user
from services.payments.provider import PaymentProvider
from services.payments.withdrawals import PAYOUT_STREAM

logger = structlog.get_logger()
_tracer = tracing.get_tracer(__name__)

GROUP = "payout-workers"
_PENDING_STATUSES = ("approved", "processing")
# How long a stream entry can sit unacked in another consumer's PEL before
# XAUTOCLAIM will steal it -- long enough that a normal in-flight job (a
# real Chapa call, httpx timeout=15.0 elsewhere in this module) is never
# prematurely reclaimed out from under a consumer that's still actually
# working on it; matches this codebase's other "how long before we
# consider something possibly crashed" thresholds (services/payments/
# deposits.py's poll_pending_deposits(), withdrawals.py's
# sweep_stuck_approved_payouts(), both default to a similar order of
# magnitude).
CLAIM_STALE_AFTER_MS = 60_000


async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(PAYOUT_STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def process_one(
    pool: asyncpg.Pool, redis: Redis, provider: PaymentProvider, *, msg_id: str, our_ref: str
) -> str:
    """Returns 'succeeded' | 'failed' | 'processing' | 'skipped'. Always
    acks -- the only way a job is left unacked (and therefore
    redelivered) is this process dying mid-call, which is exactly the
    crash-recovery case the spec asks to be tested.
    """
    with _tracer.start_as_current_span(
        "payout.dispatch", attributes={"our_ref": our_ref}
    ) as span:
        async with pool.acquire() as conn:
            async with conn.transaction():
                payment = await conn.fetchrow(
                    "SELECT id, user_id, amount, method_id, status FROM payments "
                    "WHERE our_ref = $1 FOR UPDATE",
                    our_ref,
                )
                if payment is None or payment["status"] not in _PENDING_STATUSES:
                    await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
                    span.set_attribute("payout.outcome", "skipped")
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
            with _tracer.start_as_current_span("payout.provider_call") as provider_span:
                provider_span.set_attribute("provider.name", provider.name)
                result = await provider.create_payout(method=method_payload, amount=amount, our_ref=our_ref)
        except Exception as exc:
            logger.error("payout_provider_error", our_ref=our_ref, error=str(exc))
            await _reverse(pool, payment_id=payment_id, user_id=user_id, amount=amount, our_ref=our_ref, reason=str(exc))
            await ledger.publish_balance_update(pool, redis, user_id)
            await notify_user(pool, redis, user_id=user_id, key="notify.withdrawal_failed", amount=str(amount))
            await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
            span.set_attribute("payout.outcome", "failed")
            return "failed"

        if result.status == "succeeded":
            await _settle_success(
                pool,
                payment_id=payment_id,
                user_id=user_id,
                amount=amount,
                our_ref=our_ref,
                provider_ref=result.provider_ref,
            )
            await ledger.publish_balance_update(pool, redis, user_id)
            await notify_user(pool, redis, user_id=user_id, key="notify.withdrawal_succeeded", amount=str(amount))
            await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
            span.set_attribute("payout.outcome", "succeeded")
            return "succeeded"

        if result.status == "processing":
            # A code review pass caught this treating a merely-accepted
            # transfer as fully settled -- moving locked funds to
            # provider_settlement and telling the player they'd been
            # paid, on nothing more than Chapa having *acknowledged* the
            # request. There is no payout webhook route and no polling
            # fallback for outbound transfers (unlike deposits.py's
            # poll_pending_deposits()), so a transfer Chapa later actually
            # rejects (a bad account number, say) was never reconciled:
            # silent, permanent, unrecoverable player money loss with no
            # signal anywhere it had happened. Left deliberately
            # unresolved instead: the payment stays at status='processing'
            # (already set above), locked funds stay exactly where they
            # already were, and neither a success nor a failure
            # notification goes out -- both would be a real claim about
            # an outcome this worker does not actually know yet. Still
            # records provider_ref, though -- an admin resolving this
            # manually (see services.admin.queries
            # .list_stuck_processing_payouts(), the operator-visibility
            # half of this) needs Chapa's own reference to look the
            # transfer up with them at all. A full fix (automated
            # payout-status polling against Chapa's real transfer-
            # verification response) remains blocked on the same
            # Chapa status-vocabulary gap documented elsewhere in this
            # codebase's history: the exact response shape for
            # GET /v1/transfers/verify/<tx_ref> could not be confirmed
            # from Chapa's docs, and guessing a status mapping here is
            # exactly the kind of money-safety risk not worth taking.
            await pool.execute(
                "UPDATE payments SET provider_ref = $2, raw_response = $3, updated_at = now() "
                "WHERE id = $1",
                payment_id,
                result.provider_ref,
                json.dumps(result.raw_response, default=str),
            )
            await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
            span.set_attribute("payout.outcome", "processing")
            return "processing"

        await _reverse(
            pool, payment_id=payment_id, user_id=user_id, amount=amount, our_ref=our_ref,
            reason=f"provider reported status={result.status}",
        )
        await ledger.publish_balance_update(pool, redis, user_id)
        await notify_user(pool, redis, user_id=user_id, key="notify.withdrawal_failed", amount=str(amount))
        await redis.xack(PAYOUT_STREAM, GROUP, msg_id)
        span.set_attribute("payout.outcome", "failed")
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


async def _claim_stale_entries(
    redis: Redis, consumer_name: str, *, count: int, min_idle_time: int
) -> list[tuple[str, dict[str, str]]]:
    """XAUTOCLAIMs entries idle longer than min_idle_time ms from *any*
    consumer in the group, not just consumer_name's own pending list --
    the cross-consumer half of crash recovery process_next()'s own "this
    consumer's own pending entries first" step can't provide on its own.
    Confirmed directly against this codebase's real Redis version, not
    assumed from docs: XAUTOCLAIM returns
    [next_cursor, [(id, fields), ...], [deleted_ids]] -- the middle
    element is already in the exact (msg_id, fields) shape _flatten()
    produces, so no reshaping is needed here.
    """
    _cursor, claimed, _deleted = await redis.xautoclaim(
        PAYOUT_STREAM,
        GROUP,
        consumer_name,
        min_idle_time=min_idle_time,
        start_id="0-0",
        count=count,
    )
    return list(claimed)


async def process_next(
    pool: asyncpg.Pool,
    redis: Redis,
    provider: PaymentProvider,
    *,
    consumer_name: str = "worker-1",
    claim_stale_after_ms: int = CLAIM_STALE_AFTER_MS,
) -> str | None:
    """Processes at most one job, in order: this consumer's own still-
    pending entries (crash recovery under the same consumer name), then a
    stale entry claimed from a *different*, possibly-dead consumer (cross
    -consumer crash recovery), then a genuinely new one. Returns the
    outcome, or None if the stream was empty. Built as a single-shot step
    so tests can drive it deterministically instead of racing a
    background loop.
    """
    await ensure_group(redis)

    pending = await redis.xreadgroup(GROUP, consumer_name, {PAYOUT_STREAM: "0"}, count=1)
    entries = _flatten(pending)
    if not entries:
        entries = await _claim_stale_entries(
            redis, consumer_name, count=1, min_idle_time=claim_stale_after_ms
        )
    if not entries:
        fresh = await redis.xreadgroup(GROUP, consumer_name, {PAYOUT_STREAM: ">"}, count=1)
        entries = _flatten(fresh)
    if not entries:
        return None

    msg_id, fields = entries[0]
    return await process_one(pool, redis, provider, msg_id=msg_id, our_ref=fields["our_ref"])


async def run_forever(
    pool: asyncpg.Pool,
    redis: Redis,
    provider: PaymentProvider,
    *,
    consumer_name: str = "worker-1",
    claim_stale_after_ms: int = CLAIM_STALE_AFTER_MS,
) -> None:
    await ensure_group(redis)
    while True:
        pending = await redis.xreadgroup(GROUP, consumer_name, {PAYOUT_STREAM: "0"}, count=10)
        entries = _flatten(pending)
        if not entries:
            entries = await _claim_stale_entries(
                redis, consumer_name, count=10, min_idle_time=claim_stale_after_ms
            )
        if not entries:
            fresh = await redis.xreadgroup(
                GROUP, consumer_name, {PAYOUT_STREAM: ">"}, count=10, block=5000
            )
            entries = _flatten(fresh)
        for msg_id, fields in entries:
            await process_one(pool, redis, provider, msg_id=msg_id, our_ref=fields["our_ref"])
