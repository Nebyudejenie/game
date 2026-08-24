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
from services.payments import withdrawals
from tests.integration.conftest import create_funded_user, create_room, create_user

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
    assert await _locked(conn, user_id) == Decimal("100.00")


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
