"""Integration tests for packages/core/ledger.py against a real Postgres.

These are the tests that matter most in the whole codebase: a green run
here is the actual proof that double-spend and duplicate-payout are
structurally impossible, not just documented as forbidden.
"""

import asyncio
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import ledger
from packages.core.ledger import Entry, InsufficientFunds
from tests.integration.conftest import create_user


def key() -> str:
    return f"test-{uuid.uuid4()}"


async def test_get_or_create_account_is_idempotent(conn, pool):
    user_id = await create_user(conn)
    a1 = await ledger.get_or_create_account(conn, user_id, "user_cash")
    a2 = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert a1.id == a2.id


async def test_get_or_create_system_account_is_singleton(conn, pool):
    a1 = await ledger.get_or_create_account(conn, None, "house_revenue")
    a2 = await ledger.get_or_create_account(conn, None, "house_revenue")
    assert a1.id == a2.id
    assert a1.user_id is None


async def test_post_basic_deposit_credits_user_cash(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")

    await ledger.post(
        conn,
        "deposit",
        [
            Entry(provider.id, Decimal("-200.00")),
            Entry(cash.id, Decimal("200.00")),
        ],
        idempotency_key=key(),
    )

    assert await ledger.balance(conn, cash.id) == Decimal("200.00")
    assert await ledger.available(conn, user_id) == Decimal("200.00")


async def test_settlement_matches_reference_economics(conn):
    """35 players x 20 ETB stake = 700 pot; 20% house cut -> 560 derash,
    140 house revenue. Straight from the spec's own worked example.
    """
    winner_id = await create_user(conn)
    winner_cash = await ledger.get_or_create_account(conn, winner_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    house = await ledger.get_or_create_account(conn, None, "house_revenue")

    # Fund the pot as if 35 players had already staked into it.
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    await ledger.post(
        conn,
        "adjustment",
        [Entry(provider.id, Decimal("-700.00")), Entry(pot.id, Decimal("700.00"))],
        idempotency_key=key(),
    )
    pot_balance_before = await ledger.balance(conn, pot.id)

    await ledger.post(
        conn,
        "payout",
        [
            Entry(pot.id, Decimal("-700.00")),
            Entry(winner_cash.id, Decimal("560.00")),
            Entry(house.id, Decimal("140.00")),
        ],
        idempotency_key=key(),
    )

    assert await ledger.balance(conn, pot.id) == pot_balance_before - Decimal("700.00")
    assert await ledger.balance(conn, winner_cash.id) == Decimal("560.00")


async def test_insufficient_funds_raised_and_nothing_written(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")

    with pytest.raises(InsufficientFunds):
        await ledger.post(
            conn,
            "stake",
            [Entry(cash.id, Decimal("-10.00")), Entry(pot.id, Decimal("10.00"))],
            idempotency_key=key(),
        )

    assert await ledger.balance(conn, cash.id) == Decimal("0")


async def test_same_idempotency_key_twice_returns_existing_transaction(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    idem = key()

    entries = [Entry(provider.id, Decimal("-50.00")), Entry(cash.id, Decimal("50.00"))]
    t1 = await ledger.post(conn, "deposit", entries, idempotency_key=idem)
    t2 = await ledger.post(conn, "deposit", entries, idempotency_key=idem)

    assert t1.id == t2.id
    assert await ledger.balance(conn, cash.id) == Decimal("50.00")
    count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", idem
    )
    assert count == 1


async def test_sum_to_zero_violation_rejected_by_database(conn):
    """Bypass ledger.post() entirely and write directly via SQL -- the
    database itself, via the deferred constraint trigger, must be what
    rejects this, not a Python-side check that a rogue script could skip.
    """
    house = await ledger.get_or_create_account(conn, None, "house_float")
    txn_id = await conn.fetchval(
        "INSERT INTO ledger_transactions (kind, idempotency_key, created_by) "
        "VALUES ('adjustment', $1, 'test') RETURNING id",
        key(),
    )

    with pytest.raises(asyncpg.exceptions.PostgresError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO ledger_entries (transaction_id, account_id, amount) "
                "VALUES ($1, $2, $3)",
                txn_id,
                house.id,
                Decimal("-20.00"),  # lone entry, sums to -20, must be rejected
            )


async def test_concurrent_stakes_exactly_half_succeed(pool):
    """100 concurrent 10-ETB stakes against a 500-ETB balance -> exactly 50
    succeed, 50 raise InsufficientFunds, final balance is exactly 0.
    """
    async with pool.acquire() as setup_conn:
        user_id = await create_user(setup_conn)
        cash = await ledger.get_or_create_account(setup_conn, user_id, "user_cash")
        pot = await ledger.get_or_create_account(setup_conn, None, "pot_escrow")
        provider = await ledger.get_or_create_account(
            setup_conn, None, "provider_settlement"
        )
        await ledger.post(
            setup_conn,
            "deposit",
            [
                Entry(provider.id, Decimal("-500.00")),
                Entry(cash.id, Decimal("500.00")),
            ],
            idempotency_key=key(),
        )

    async def one_stake(i: int) -> str:
        async with pool.acquire() as c:
            try:
                await ledger.post(
                    c,
                    "stake",
                    [
                        Entry(cash.id, Decimal("-10.00")),
                        Entry(pot.id, Decimal("10.00")),
                    ],
                    idempotency_key=key(),
                )
                return "ok"
            except InsufficientFunds:
                return "insufficient"

    results = await asyncio.gather(*(one_stake(i) for i in range(100)))

    assert results.count("ok") == 50
    assert results.count("insufficient") == 50

    async with pool.acquire() as check_conn:
        assert await ledger.balance(check_conn, cash.id) == Decimal("0")


async def test_same_idempotency_key_fired_concurrently_100_times(pool):
    async with pool.acquire() as setup_conn:
        user_id = await create_user(setup_conn)
        cash = await ledger.get_or_create_account(setup_conn, user_id, "user_cash")
        provider = await ledger.get_or_create_account(
            setup_conn, None, "provider_settlement"
        )

    idem = key()
    entries = [Entry(provider.id, Decimal("-30.00")), Entry(cash.id, Decimal("30.00"))]

    async def one_post() -> int:
        async with pool.acquire() as c:
            txn = await ledger.post(c, "deposit", entries, idempotency_key=idem)
            return txn.id

    txn_ids = await asyncio.gather(*(one_post() for _ in range(100)))

    assert len(set(txn_ids)) == 1

    async with pool.acquire() as check_conn:
        assert await ledger.balance(check_conn, cash.id) == Decimal("30.00")
        count = await check_conn.fetchval(
            "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1",
            idem,
        )
        assert count == 1


async def test_reconcile_matches_cache_after_varied_transactions(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    bonus = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    promo = await ledger.get_or_create_account(conn, None, "promo_expense")

    await ledger.post(
        conn,
        "deposit",
        [Entry(provider.id, Decimal("-100.00")), Entry(cash.id, Decimal("100.00"))],
        idempotency_key=key(),
    )
    await ledger.post(
        conn,
        "bonus_grant",
        [Entry(promo.id, Decimal("-25.00")), Entry(bonus.id, Decimal("25.00"))],
        idempotency_key=key(),
    )
    await ledger.post(
        conn,
        "withdrawal",
        [Entry(cash.id, Decimal("-40.00")), Entry(provider.id, Decimal("40.00"))],
        idempotency_key=key(),
    )

    mismatches = await ledger.reconcile(conn)
    assert mismatches == []
