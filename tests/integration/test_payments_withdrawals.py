"""Integration tests for services/payments/withdrawals.py against a real
Postgres + Redis. The one behavior the spec calls "the single most
important step" gets the most direct test: the moment a withdrawal is
requested, funds move out of user_cash in the same transaction, so there is
no window in which a later stake can spend money that's already earmarked
for payout.
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core import ledger
from services.engine.round_engine import RoundEngine, load_room_config
from services.payments import payout_worker, withdrawals
from services.payments.provider import PayoutResult
from tests.integration.conftest import create_funded_user, create_room, create_user, recv_balance_update

MIN_WITHDRAW = Decimal("50.00")
AUTO_APPROVE_LIMIT = Decimal("2000.00")
KYC_THRESHOLD = Decimal("5000.00")
CHARGEBACK_MINUTES = 30


class _NullProvider:
    name = "chapa"

    async def create_checkout(self, **kwargs):
        raise NotImplementedError

    def verify_webhook(self, headers, raw_body):
        raise NotImplementedError

    async def fetch_status(self, our_ref):
        raise NotImplementedError

    async def create_payout(self, **kwargs):
        raise NotImplementedError


class _AlwaysSucceedsProvider(_NullProvider):
    async def create_payout(self, *, method, amount, our_ref):
        return PayoutResult(provider_ref=f"chapa-{our_ref}", status="succeeded", raw_response={})


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _locked(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_locked")
    return await ledger.balance(conn, account.id)


async def _request(pool, redis, conn, user_id, amount, **overrides):
    kwargs = dict(
        user_id=user_id,
        amount=amount,
        method_kind="telebirr",
        account_ref="0911223344",
        holder_name="Test Holder",
        min_withdraw=MIN_WITHDRAW,
        auto_approve_limit=AUTO_APPROVE_LIMIT,
        kyc_threshold=KYC_THRESHOLD,
        chargeback_window_minutes=CHARGEBACK_MINUTES,
        min_account_age_hours=0,
    )
    kwargs.update(overrides)
    return await withdrawals.request_withdrawal(pool, redis, _NullProvider(), **kwargs)


async def test_below_minimum_withdrawal_rejected(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    with pytest.raises(withdrawals.BelowMinimumWithdrawal):
        await _request(pool, redis, conn, user_id, Decimal("10.00"))


async def test_bonus_funds_cannot_be_withdrawn(pool, redis, conn):
    user_id = await create_user(conn)
    bonus = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    provider_account = await ledger.get_or_create_account(conn, None, "provider_settlement")
    await ledger.post(
        conn,
        "bonus_grant",
        [ledger.Entry(provider_account.id, Decimal("-500.00")), ledger.Entry(bonus.id, Decimal("500.00"))],
        idempotency_key=f"test-bonus-{user_id}",
    )
    with pytest.raises(withdrawals.InsufficientAvailableBalance):
        await _request(pool, redis, conn, user_id, Decimal("100.00"))


async def test_insufficient_cash_balance_rejected(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("60.00"))
    with pytest.raises(withdrawals.InsufficientAvailableBalance):
        await _request(pool, redis, conn, user_id, Decimal("100.00"))
    assert await _cash(conn, user_id) == Decimal("60.00")


async def test_kyc_required_above_threshold(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("10000.00"))
    with pytest.raises(withdrawals.KycLevelTooLow):
        await _request(pool, redis, conn, user_id, Decimal("6000.00"))


async def test_kyc_verified_user_can_withdraw_above_threshold(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("10000.00"))
    await conn.execute("UPDATE users SET kyc_level = 2 WHERE id = $1", user_id)
    intent = await _request(pool, redis, conn, user_id, Decimal("6000.00"))
    assert intent.our_ref.startswith("WD-")


async def test_recent_deposit_blocks_withdrawal(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    our_ref = f"DEP-test-recent-{user_id}"
    await conn.execute(
        "INSERT INTO payments (user_id, direction, provider, our_ref, amount, status) "
        "VALUES ($1, 'in', 'chapa', $2, $3, 'succeeded')",
        user_id,
        our_ref,
        Decimal("200.00"),
    )
    with pytest.raises(withdrawals.RecentReversibleDeposit):
        await _request(pool, redis, conn, user_id, Decimal("100.00"))


async def test_small_amount_auto_approved_and_enqueued(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    intent = await _request(pool, redis, conn, user_id, Decimal("100.00"))
    assert intent.status == withdrawals.STATUS_APPROVED

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "approved"
    assert await _cash(conn, user_id) == Decimal("900.00")


async def test_request_withdrawal_pushes_a_live_balance_update(pool, redis, conn):
    # A code review pass caught that only services/payments/deposits.py
    # ever pushed a live balance_update -- requesting a withdrawal locks
    # real funds out of user_cash but never told a connected player's UI
    # its balance had changed.
    user_id = await create_funded_user(conn, Decimal("1000.00"))

    async def _do_request() -> None:
        intent = await _request(pool, redis, conn, user_id, Decimal("100.00"))
        assert intent.status == withdrawals.STATUS_APPROVED

    push = await recv_balance_update(redis, user_id, _do_request)
    assert push["cash"] == "900.00"
    assert push["locked"] == "100.00"


async def test_sweep_re_enqueues_an_approved_payout_that_never_got_queued(pool, redis, conn, monkeypatch):
    # Regression: a real code review pass caught that enqueue_payout()
    # (the Redis XADD) runs *after* request_withdrawal()'s own DB
    # transaction commits, not inside it -- a crash or a Redis blip in
    # that narrow window leaves a withdrawal stuck at status='approved'
    # forever: funds already locked out of user_cash, but nothing ever
    # queued to actually pay them out. Simulates the crash directly by
    # no-op'ing enqueue_payout for this one call, rather than trying to
    # actually crash a process mid-request -- scoped to just this one
    # call (monkeypatch.context()) since sweep_stuck_approved_payouts()
    # itself calls the very same module-level enqueue_payout() to do its
    # actual job, and a patch left in place would silently no-op that
    # too, making the rest of this test check nothing real.
    # This session's shared dev database accumulates real "approved"
    # withdrawal rows across every prior test run, so assertions here
    # check membership for *this* test's own payment_id, not exact
    # counts -- an ambient old row from hours ago legitimately matching
    # the same sweep query is a real cross-test-pollution risk this
    # session has hit before (reconcile_job's idempotency keys, rate_
    # limit TTLs, LTV leaderboard ranking), not a hypothetical one.
    with monkeypatch.context() as m:
        m.setattr(withdrawals, "enqueue_payout", lambda *a, **kw: asyncio.sleep(0))
        user_id = await create_funded_user(conn, Decimal("1000.00"))
        intent = await _request(pool, redis, conn, user_id, Decimal("100.00"))
    assert intent.status == withdrawals.STATUS_APPROVED

    # Not yet old enough -- the sweep must not touch a withdrawal that's
    # simply mid-flight through the normal enqueue path.
    swept_too_soon = await withdrawals.sweep_stuck_approved_payouts(pool, redis, older_than_seconds=3600)
    assert intent.payment_id not in swept_too_soon

    await conn.execute(
        "UPDATE payments SET updated_at = now() - interval '2 hours' WHERE id = $1", intent.payment_id
    )

    swept = await withdrawals.sweep_stuck_approved_payouts(pool, redis, older_than_seconds=3600)
    assert intent.payment_id in swept

    # And it's a real, processable entry -- not just a stream write that
    # looks right but doesn't actually let the payout worker do anything
    # with it. Calling process_one() directly with this payment's own
    # our_ref (rather than process_next(), which would read whatever
    # stream entry happens to be next -- possibly an unrelated ambient
    # one from the same shared stream) proves this specific re-enqueued
    # payout is genuinely processable, unambiguously.
    provider = _AlwaysSucceedsProvider()
    outcome = await payout_worker.process_one(
        pool, redis, provider, msg_id="0-0", our_ref=intent.our_ref
    )
    assert outcome == "succeeded"
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "succeeded"
    assert await _locked(conn, user_id) == Decimal("0.00")


async def test_amount_above_auto_approve_limit_goes_to_review(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("10000.00"))
    intent = await _request(pool, redis, conn, user_id, Decimal("3000.00"))
    assert intent.status == withdrawals.STATUS_REVIEW

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "review"
    # Funds are still locked immediately, review or not -- only the payout
    # dispatch is deferred, never the fund lock.
    assert await _locked(conn, user_id) == Decimal("3000.00")


async def test_withdrawal_locks_funds_immediately_so_a_later_stake_fails(pool, redis, conn, card_pool):
    room_id = await create_room(conn, stake=Decimal("100.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    user_id = await create_funded_user(conn, Decimal("100.00"))

    intent = await _request(pool, redis, conn, user_id, Decimal("100.00"))
    assert intent.status == withdrawals.STATUS_APPROVED
    assert await _cash(conn, user_id) == Decimal("0.00")

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        result = await engine.join(user_id, 1)
        assert result.ok is False
        assert result.reason == "insufficient_funds"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_concurrent_withdrawal_and_stake_never_both_succeed(pool, redis, conn, card_pool):
    room_id = await create_room(conn, stake=Decimal("50.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    user_id = await create_funded_user(conn, Decimal("50.00"))
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        withdrawal_outcome = {}

        async def _do_withdrawal():
            try:
                await _request(pool, redis, conn, user_id, Decimal("50.00"))
                withdrawal_outcome["ok"] = True
            except withdrawals.WithdrawalRejected:
                withdrawal_outcome["ok"] = False

        join_result, _ = await asyncio.gather(engine.join(user_id, 1), _do_withdrawal())

        # Exactly one of the two debits against the same 50.00 succeeded --
        # never both, regardless of which one won the race.
        assert (join_result.ok, withdrawal_outcome["ok"]).count(True) == 1
        assert await _cash(conn, user_id) == Decimal("0.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)
