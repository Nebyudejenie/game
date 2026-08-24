"""Integration tests for services/payments/payout_worker.py against a real
Postgres + Redis Stream consumer group -- the exact scenarios spec Prompt 8
calls out: a crashed worker's job is redelivered and the provider ends up
called safely (our_ref is the provider's own idempotency key, so a second
call after a crash is not a double-pay), and a rejected payout returns the
exact amount to user_cash.
"""

import collections
from dataclasses import dataclass, field
from decimal import Decimal

from packages.core import ledger
from services.payments import payout_worker, withdrawals
from services.payments.provider import PayoutResult
from tests.integration.conftest import create_funded_user

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
