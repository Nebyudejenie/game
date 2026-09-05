"""Integration tests for packages/core/bonuses.py against real Postgres:
grant/convert/expire/revoke and wagering-progress, in isolation, before
anything (a referral trigger, an admin manual-grant route, a sweep
worker) ever calls these for real.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core import bonuses, ledger
from tests.integration.conftest import create_user


def key() -> str:
    return f"test-bonus-{uuid.uuid4()}"


async def test_grant_bonus_credits_user_bonus_and_debits_promo_expense(conn):
    user_id = await create_user(conn)
    promo = await ledger.get_or_create_account(conn, None, "promo_expense")
    promo_before = await ledger.balance(conn, promo.id)

    grant = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("10.00"),
        wagering_required=Decimal("30.00"),
    )

    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("10.00")
    assert await ledger.balance(conn, promo.id) == promo_before - Decimal("10.00")
    assert grant.status == "active"
    assert grant.amount == Decimal("10.00")


async def test_grant_bonus_never_touches_user_cash(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    cash_before = await ledger.balance(conn, cash.id)

    await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("15.00"),
        wagering_required=Decimal("45.00"),
    )

    assert await ledger.balance(conn, cash.id) == cash_before


async def test_grant_bonus_is_idempotent_on_a_repeated_key(conn):
    user_id = await create_user(conn)
    idem = key()
    first = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=idem, amount=Decimal("5.00"),
        wagering_required=Decimal("15.00"),
    )
    second = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=idem, amount=Decimal("5.00"),
        wagering_required=Decimal("15.00"),
    )
    assert first.id == second.id

    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("5.00")  # not 10 -- credited once


async def test_convert_bonus_to_cash_moves_the_full_amount(conn):
    user_id = await create_user(conn)
    grant = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("20.00"),
        wagering_required=Decimal("60.00"),
    )

    converted = await bonuses.convert_bonus_to_cash(conn, bonus_id=grant.id)
    assert converted is True

    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("0.00")
    assert await ledger.balance(conn, cash_account.id) == Decimal("20.00")

    row = await conn.fetchrow("SELECT status, convert_txn_id FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "converted"
    assert row["convert_txn_id"] is not None


async def test_convert_bonus_to_cash_is_a_no_op_the_second_time(conn):
    user_id = await create_user(conn)
    grant = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("8.00"),
        wagering_required=Decimal("24.00"),
    )
    assert await bonuses.convert_bonus_to_cash(conn, bonus_id=grant.id) is True
    assert await bonuses.convert_bonus_to_cash(conn, bonus_id=grant.id) is False  # already converted

    cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash_account.id) == Decimal("8.00")  # not double-credited


async def test_convert_bonus_to_cash_raises_for_an_unknown_id(conn):
    with pytest.raises(bonuses.BonusNotFound):
        await bonuses.convert_bonus_to_cash(conn, bonus_id=999_999_999)


async def test_expire_bonus_reverses_the_grant_not_just_flips_a_flag(conn):
    user_id = await create_user(conn)
    promo = await ledger.get_or_create_account(conn, None, "promo_expense")
    promo_before = await ledger.balance(conn, promo.id)

    grant = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("12.00"),
        wagering_required=Decimal("36.00"),
    )
    expired = await bonuses.expire_bonus(conn, bonus_id=grant.id)
    assert expired is True

    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("0.00")
    assert await ledger.balance(conn, promo.id) == promo_before  # reversed back to where it started

    row = await conn.fetchrow("SELECT status FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "expired"


async def test_expire_bonus_is_a_no_op_once_already_converted(conn):
    user_id = await create_user(conn)
    grant = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("6.00"),
        wagering_required=Decimal("18.00"),
    )
    await bonuses.convert_bonus_to_cash(conn, bonus_id=grant.id)
    assert await bonuses.expire_bonus(conn, bonus_id=grant.id) is False

    cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash_account.id) == Decimal("6.00")  # untouched by the no-op


async def test_revoke_bonus_reverses_like_expire_but_records_a_distinct_status(conn):
    user_id = await create_user(conn)
    grant = await bonuses.grant_bonus(
        conn, user_id=user_id, idempotency_key=key(), amount=Decimal("9.00"),
        wagering_required=Decimal("27.00"),
    )
    assert await bonuses.revoke_bonus(conn, bonus_id=grant.id) is True

    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("0.00")
    row = await conn.fetchrow("SELECT status FROM bonuses WHERE id = $1", grant.id)
    assert row["status"] == "revoked"


async def test_only_one_referral_reward_can_ever_exist_for_the_same_referee(conn):
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await bonuses.grant_bonus(
        conn, user_id=referrer_id, idempotency_key=key(), amount=Decimal("10.00"),
        wagering_required=Decimal("30.00"), referral_of_user_id=referee_id,
    )
    with pytest.raises(Exception):  # the DB's own ux_bonuses_referral_once partial unique index
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO bonuses (user_id, amount, wagering_required, referral_of_user_id) "
                "VALUES ($1, 5.00, 15.00, $2)",
                referrer_id,
                referee_id,
            )


async def test_wagering_progress_sums_real_cash_stakes_since_a_given_time(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    # Fund the user's cash first -- a real stake can never take a user_cash
    # balance negative (ledger.post()'s own InsufficientFunds guard).
    await ledger.post(
        conn, "deposit", [ledger.Entry(pot.id, -Decimal("100.00")), ledger.Entry(cash.id, Decimal("100.00"))],
        idempotency_key=key(),
    )

    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, -Decimal("10.00")), ledger.Entry(pot.id, Decimal("10.00"))],
        idempotency_key=key(),
    )
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, -Decimal("15.00")), ledger.Entry(pot.id, Decimal("15.00"))],
        idempotency_key=key(),
    )

    progress = await bonuses.wagering_progress_for_user_since(conn, user_id=user_id, since=since)
    assert progress == Decimal("25.00")


async def test_wagering_progress_ignores_stakes_before_the_cutoff(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    await ledger.post(
        conn, "deposit", [ledger.Entry(pot.id, -Decimal("50.00")), ledger.Entry(cash.id, Decimal("50.00"))],
        idempotency_key=key(),
    )
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, -Decimal("20.00")), ledger.Entry(pot.id, Decimal("20.00"))],
        idempotency_key=key(),
    )

    since = datetime.now(timezone.utc) + timedelta(hours=1)  # cutoff is in the future
    progress = await bonuses.wagering_progress_for_user_since(conn, user_id=user_id, since=since)
    assert progress == Decimal("0.00")


async def test_wagering_progress_ignores_non_stake_transactions(conn):
    user_id = await create_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    await ledger.post(
        conn, "deposit", [ledger.Entry(pot.id, -Decimal("40.00")), ledger.Entry(cash.id, Decimal("40.00"))],
        idempotency_key=key(),
    )
    # A deposit moves cash but is not wagering -- must not count.
    progress = await bonuses.wagering_progress_for_user_since(conn, user_id=user_id, since=since)
    assert progress == Decimal("0.00")
