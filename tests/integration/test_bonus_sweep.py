"""Integration tests for services/payments/bonus_sweep.py against real
Postgres + Redis: a real sweep tick converting a fully-wagered bonus,
leaving a partially-wagered one alone, and expiring one past its own
deadline that was never wagered off.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from packages.core import ledger
from packages.core.bonuses import grant_bonus
from services.payments.bonus_sweep import sweep_bonus_wagering
from tests.integration.conftest import create_user, recv_balance_update


def key() -> str:
    return f"test-sweep-{uuid.uuid4()}"


async def _stake(conn, user_id: int, amount: Decimal) -> None:
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, -amount), ledger.Entry(pot.id, amount)], idempotency_key=key()
    )


async def _fund_cash(conn, user_id: int, amount: Decimal) -> None:
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    await ledger.post(
        conn, "deposit", [ledger.Entry(provider.id, -amount), ledger.Entry(cash.id, amount)],
        idempotency_key=key(),
    )


async def test_sweep_converts_a_fully_wagered_bonus(pool, conn, redis):
    user_id = await create_user(conn)
    await _fund_cash(conn, user_id, Decimal("100.00"))
    grant = await grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("10.00"),
        wagering_required=Decimal("30.00"),
    )
    await _stake(conn, user_id, Decimal("30.00"))  # exactly meets the requirement

    async def trigger() -> None:
        await sweep_bonus_wagering(pool, redis)

    payload = await recv_balance_update(redis, user_id, trigger)
    assert payload["bonus"] == "0.00"
    assert payload["cash"] != "0.00"

    row = await conn.fetchrow("SELECT status, wagering_progress FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "converted"
    assert row["wagering_progress"] == Decimal("30.00")

    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("0.00")
    # 100 funded - 30 staked + 10 converted bonus = 80
    assert await ledger.balance(conn, cash_account.id) == Decimal("80.00")


async def test_sweep_leaves_a_partially_wagered_bonus_active(pool, conn, redis):
    user_id = await create_user(conn)
    await _fund_cash(conn, user_id, Decimal("100.00"))
    grant = await grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("10.00"),
        wagering_required=Decimal("30.00"),
    )
    await _stake(conn, user_id, Decimal("10.00"))  # short of the 30.00 requirement

    await sweep_bonus_wagering(pool, redis)

    row = await conn.fetchrow("SELECT status, wagering_progress FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "active"
    assert row["wagering_progress"] == Decimal("10.00")

    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("10.00")  # untouched


async def test_sweep_expires_an_unwagered_bonus_past_its_deadline(pool, conn, redis):
    user_id = await create_user(conn)
    grant = await grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("5.00"),
        wagering_required=Decimal("15.00"), expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    async def trigger() -> None:
        await sweep_bonus_wagering(pool, redis)

    payload = await recv_balance_update(redis, user_id, trigger)
    assert payload["bonus"] == "0.00"

    row = await conn.fetchrow("SELECT status FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "expired"

    promo = await ledger.get_or_create_account(conn, None, "promo_expense")
    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("0.00")
    # Reversed back to promo_expense, not silently vanished from the ledger.
    reversal = await conn.fetchval(
        "SELECT count(*) FROM ledger_entries le JOIN ledger_transactions lt ON lt.id = le.transaction_id "
        "WHERE le.account_id = $1 AND lt.kind = 'bonus_grant' AND le.amount > 0",
        promo.id,
    )
    assert reversal >= 1


async def test_sweep_does_not_expire_a_bonus_with_no_expiry_set(pool, conn, redis):
    user_id = await create_user(conn)
    grant = await grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("5.00"),
        wagering_required=Decimal("15.00"), expires_at=None,
    )
    await sweep_bonus_wagering(pool, redis)
    row = await conn.fetchrow("SELECT status FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "active"


async def test_sweep_ignores_bonuses_that_are_not_active(pool, conn, redis):
    user_id = await create_user(conn)
    grant = await grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("5.00"),
        wagering_required=Decimal("0.00"),  # would otherwise convert immediately
    )
    await conn.execute("UPDATE bonuses SET status = 'revoked' WHERE id = $1", grant.id)

    await sweep_bonus_wagering(pool, redis)

    row = await conn.fetchrow("SELECT status FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "revoked"  # the sweep never touched it
