"""Integration tests for services/payments/payout_worker.py against a real
Postgres + Redis Stream consumer group -- the exact scenarios spec Prompt 8
calls out: a crashed worker's job is redelivered and the provider ends up
called safely (our_ref is the provider's own idempotency key, so a second
call after a crash is not a double-pay), and a rejected payout returns the
exact amount to user_cash.
"""

import asyncio
import collections
import contextlib
from dataclasses import dataclass, field
from decimal import Decimal

from packages.core import ledger
from services.payments import payout_worker, withdrawals
from services.payments.provider import PayoutResult
from tests.integration.conftest import create_funded_user, recv_balance_update

# clean_payout_stream (conftest.py, autouse) clears the shared 'payouts'
# Redis Stream before every integration test, so process_next()'s "the
# next job" is unambiguous here regardless of what other test files enqueue
# onto the same real stream without consuming it.


@dataclass
class FakePayoutProvider:
    name: str = "chapa"
    # Simulates a real provider deduping on our_ref: the same our_ref
    # always gets the same outcome and is only ever actually "charged"
    # once, no matter how many times create_payout is called for it.
    outcomes: dict[str, str] = field(default_factory=dict)  # our_ref -> "succeeded" | "failed"
    call_count: dict[str, int] = field(default_factory=lambda: collections.defaultdict(int))

    async def create_checkout(self, **kwargs):
        raise NotImplementedError

    def verify_webhook(self, headers, raw_body):
        raise NotImplementedError

    async def fetch_status(self, our_ref):
        raise NotImplementedError

    async def create_payout(self, *, method, amount, our_ref):
        self.call_count[our_ref] += 1
        outcome = self.outcomes.get(our_ref, "succeeded")
        return PayoutResult(provider_ref=f"chapa-{our_ref}", status=outcome, raw_response={})


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _locked(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_locked")
    return await ledger.balance(conn, account.id)


async def _provider_settlement(conn) -> Decimal:
    account = await ledger.get_or_create_account(conn, None, "provider_settlement")
    return await ledger.balance(conn, account.id)


async def _approved_withdrawal(pool, redis, conn, user_id: int, amount: Decimal) -> str:
    intent = await withdrawals.request_withdrawal(
        pool,
        redis,
        FakePayoutProvider(),
        user_id=user_id,
        amount=amount,
        method_kind="telebirr",
        account_ref="0911223344",
        holder_name="Test Holder",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("100000.00"),
        kyc_threshold=Decimal("100000.00"),
        chargeback_window_minutes=0,
        min_account_age_hours=0,
    )
    assert intent.status == withdrawals.STATUS_APPROVED
    return intent.our_ref


async def test_successful_payout_moves_locked_to_provider_settlement(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("200.00"))

    provider = FakePayoutProvider()
    outcome = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
    assert outcome == "succeeded"

    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("300.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE our_ref = $1", our_ref)
    assert status == "succeeded"
    assert provider.call_count[our_ref] == 1


async def test_processing_status_is_not_treated_as_settled(pool, redis, conn):
    # A code review pass caught the single most severe open finding in
    # this codebase's payments pipeline: a provider result of "processing"
    # (Chapa merely *accepted* the transfer, no confirmation it actually
    # completed) used to be treated exactly like "succeeded" -- locked
    # funds moved to provider_settlement, the payment marked succeeded,
    # the player told they'd been paid. With no payout webhook route and
    # no status-polling fallback for outbound transfers, a transfer Chapa
    # later actually rejected was never reconciled: silent, permanent,
    # unrecoverable player money loss. The fix leaves it genuinely
    # unresolved -- this confirms nothing moves and nothing false gets
    # claimed, not that the underlying gap (no way to ever learn the real
    # outcome) is fully closed, which it isn't yet.
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("200.00"))
    # provider_settlement is one global account this whole session's
    # other tests have also posted real entries to -- a before/after
    # delta, not an absolute total, is what's actually being asserted.
    provider_settlement_before = await _provider_settlement(conn)

    provider = FakePayoutProvider(outcomes={our_ref: "processing"})
    outcome = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
    assert outcome == "processing"

    # Locked funds must stay exactly where they were -- neither released
    # to the player's cash (a refund, as if it failed) nor moved to
    # provider_settlement (as if it succeeded).
    assert await _locked(conn, user_id) == Decimal("200.00")
    assert await _cash(conn, user_id) == Decimal("300.00")
    assert await _provider_settlement(conn) == provider_settlement_before

    row = await conn.fetchrow(
        "SELECT status, provider_ref FROM payments WHERE our_ref = $1", our_ref
    )
    assert row["status"] == "processing"
    # provider_ref IS recorded even though nothing else is settled -- an
    # admin resolving this manually needs Chapa's own reference to look
    # the transfer up with them at all.
    assert row["provider_ref"] == f"chapa-{our_ref}"


async def test_successful_payout_pushes_a_live_balance_update(pool, redis, conn):
    # A code review pass caught that only services/payments/deposits.py
    # ever pushed a live balance_update -- a payout settling releases
    # locked funds for real but never told a connected player's UI its
    # balance had changed.
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("200.00"))

    provider = FakePayoutProvider()

    async def _process() -> None:
        outcome = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
        assert outcome == "succeeded"

    push = await recv_balance_update(redis, user_id, _process)
    assert push["cash"] == "300.00"
    assert push["locked"] == "0.00"


async def test_rejected_payout_returns_exact_amount_to_cash(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("150.00"))

    provider = FakePayoutProvider(outcomes={our_ref: "failed"})
    outcome = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
    assert outcome == "failed"

    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("500.00")  # exact amount back

    status = await conn.fetchval("SELECT status FROM payments WHERE our_ref = $1", our_ref)
    assert status == "failed"


async def test_rejected_payout_pushes_a_live_balance_update(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("150.00"))

    provider = FakePayoutProvider(outcomes={our_ref: "failed"})

    async def _process() -> None:
        outcome = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
        assert outcome == "failed"

    push = await recv_balance_update(redis, user_id, _process)
    assert push["cash"] == "500.00"
    assert push["locked"] == "0.00"


async def test_provider_exception_also_reverses_the_lock(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("150.00"))

    class ExplodingProvider(FakePayoutProvider):
        async def create_payout(self, *, method, amount, our_ref):
            raise RuntimeError("network error talking to chapa")

    outcome = await payout_worker.process_next(pool, redis, ExplodingProvider(), consumer_name="w1")
    assert outcome == "failed"
    assert await _cash(conn, user_id) == Decimal("500.00")
    assert await _locked(conn, user_id) == Decimal("0.00")


async def test_crashed_worker_job_is_redelivered_and_settles_exactly_once(pool, redis, conn):
    """Simulates a worker that died right after marking the job
    'processing' (having possibly already called the provider) -- the
    stream entry was never acked, so it's still in this consumer's pending
    list. A fresh call picks it up, calls the (idempotent-on-our_ref)
    provider again, and the ledger settles exactly once regardless.
    """
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("200.00"))

    # Simulate the crash: the payment reached 'processing' but the process
    # died before settling, and the stream message was never acked.
    await conn.execute("UPDATE payments SET status = 'processing' WHERE our_ref = $1", our_ref)

    provider = FakePayoutProvider()
    # First "redelivery" pickup after the simulated crash.
    outcome = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
    assert outcome == "succeeded"
    assert provider.call_count[our_ref] == 1

    settle_txns = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", f"payout-settle-{our_ref}"
    )
    assert settle_txns == 1
    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("300.00")


async def test_a_stale_job_left_by_a_dead_consumer_is_claimed_by_a_different_one(pool, redis, conn):
    """The crash-redelivery test above covers a replacement process that
    reuses the *same* consumer name -- process_next()'s own "this
    consumer's own pending entries" xreadgroup(..., "0") step handles that
    case on its own. This covers the other half a code review pass caught:
    a replacement process that (as is normal for a fleet -- hostname- or
    pid-derived consumer names) comes up under a *different* consumer name
    has no way to see the crashed consumer's PEL through that same-name
    read -- only XAUTOCLAIM, scanning the whole group, can hand it over.
    """
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("200.00"))

    # Simulate "worker-a" claiming the job and then dying before acking --
    # ensure_group() first, matching what process_next() itself would do,
    # since this reads directly rather than going through process_next().
    await payout_worker.ensure_group(redis)
    claimed_by_a = await redis.xreadgroup(
        payout_worker.GROUP, "worker-a", {payout_worker.PAYOUT_STREAM: ">"}, count=1
    )
    assert payout_worker._flatten(claimed_by_a)  # worker-a really did pick it up

    provider = FakePayoutProvider()
    # A *different* consumer -- claim_stale_after_ms=0 so the test doesn't
    # need to wait out the real 60s threshold to prove the mechanism works.
    outcome = await payout_worker.process_next(
        pool, redis, provider, consumer_name="worker-b", claim_stale_after_ms=0
    )
    assert outcome == "succeeded"
    assert provider.call_count[our_ref] == 1

    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("300.00")
    status = await conn.fetchval("SELECT status FROM payments WHERE our_ref = $1", our_ref)
    assert status == "succeeded"


async def test_a_second_job_for_an_already_settled_payment_is_a_safe_noop(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("200.00"))

    provider = FakePayoutProvider()
    first = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
    assert first == "succeeded"

    # A second job for the same our_ref shows up (a duplicate enqueue, or a
    # stream redelivery under a different consumer) after the payment is
    # already terminal -- must be a pure no-op, not a second payout.
    await withdrawals.enqueue_payout(redis, our_ref=our_ref, payment_id=0)
    second = await payout_worker.process_next(pool, redis, provider, consumer_name="w1")
    assert second == "skipped"

    assert provider.call_count[our_ref] == 1
    assert await _cash(conn, user_id) == Decimal("300.00")
    settle_txns = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", f"payout-settle-{our_ref}"
    )
    assert settle_txns == 1


async def test_empty_stream_returns_none(pool, redis):
    outcome = await payout_worker.process_next(pool, redis, FakePayoutProvider(), consumer_name="w-empty")
    assert outcome is None


async def test_run_forever_survives_one_message_raising_and_still_settles_the_rest(pool, redis, conn, monkeypatch):
    # The real regression a code-review pass caught: packages/core/
    # db_pool.py's new bounded pool.acquire() turns a sustained-load pool
    # exhaustion into a real TimeoutError (previously an indefinite hang)
    # -- but run_forever()'s own loop had no exception isolation at all,
    # so that exception (or any other uncaught one from inside
    # process_one()) would silently kill this fire-and-forget background
    # task for good, with nothing else in the process ever noticing.
    # Simulates the uncaught-exception case directly (monkeypatching
    # process_one, rather than actually exhausting a real pool, which
    # would take the real 10s ACQUIRE_TIMEOUT_SECONDS to manifest) and
    # proves the loop survives it, stays alive, and still settles a later
    # message in the same batch.
    user_id = await create_funded_user(conn, Decimal("500.00"))
    our_ref_bad = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("40.00"))
    our_ref_good = await _approved_withdrawal(pool, redis, conn, user_id, Decimal("30.00"))

    real_process_one = payout_worker.process_one

    async def flaky_process_one(pool, redis, provider, *, msg_id, our_ref):
        if our_ref == our_ref_bad:
            raise TimeoutError("simulated pool exhaustion")
        return await real_process_one(pool, redis, provider, msg_id=msg_id, our_ref=our_ref)

    monkeypatch.setattr(payout_worker, "process_one", flaky_process_one)

    provider = FakePayoutProvider()
    task = asyncio.create_task(
        payout_worker.run_forever(pool, redis, provider, consumer_name="w-resilience")
    )
    try:
        for _ in range(50):
            status = await conn.fetchval("SELECT status FROM payments WHERE our_ref = $1", our_ref_good)
            if status == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert not task.done(), f"run_forever() task died: {task.exception() if task.done() else None}"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # The bad message never settled (still locked, still pending -- would
    # be picked back up by this same consumer's own pending-entries
    # re-read on a real restart, the crash-redelivery guarantee this
    # module's own docstring promises).
    assert await _locked(conn, user_id) == Decimal("40.00")
    bad_status = await conn.fetchval(
        "SELECT status FROM payments WHERE our_ref = $1", our_ref_bad
    )
    assert bad_status == "approved"

    # The good message settled normally despite the other one raising.
    good_status = await conn.fetchval(
        "SELECT status FROM payments WHERE our_ref = $1", our_ref_good
    )
    assert good_status == "succeeded"
