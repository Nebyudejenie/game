"""Integration tests for packages/core/referrals.py: the referral/welcome
bonus trigger, its fraud guards, and -- for the real end-to-end proof --
the actual wired-in hook inside services/payments/deposits.py's real
webhook-confirmation path, not just the standalone function in isolation.
"""

import asyncio
import json
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import ledger
from packages.core.referrals import maybe_grant_referral_bonus, maybe_grant_welcome_bonus
from services.payments import deposits
from tests.integration.conftest import create_user
from tests.integration.test_payments_deposits import FakePaymentProvider, _cash_balance, _webhook

MIN_DEPOSIT = Decimal("10.00")
DAILY_CAP = Decimal("50000.00")


async def _link_referral(conn: asyncpg.Connection, *, referrer_id: int, referee_id: int) -> None:
    await conn.execute("UPDATE users SET referred_by = $1 WHERE id = $2", referrer_id, referee_id)


async def _create_rule(
    conn: asyncpg.Connection,
    admin_id: int,
    *,
    trigger_type: str = "referral_reward",
    reward_type: str = "flat",
    reward_amount: Decimal | None = Decimal("10.00"),
    reward_percentage: Decimal | None = None,
    reward_cap: Decimal | None = None,
    min_qualifying_deposit: Decimal = Decimal("20.00"),
    wagering_multiplier: Decimal = Decimal("3"),
    expiry_days: int | None = None,
    max_grants_per_user: int = 1,
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO bonus_rules
            (name, trigger_type, reward_type, reward_amount, reward_percentage, reward_cap,
             min_qualifying_deposit, wagering_multiplier, expiry_days, max_grants_per_user,
             created_by_admin_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        f"test-rule-{uuid.uuid4()}",
        trigger_type,
        reward_type,
        reward_amount,
        reward_percentage,
        reward_cap,
        min_qualifying_deposit,
        wagering_multiplier,
        expiry_days,
        max_grants_per_user,
        admin_id,
    )
    assert row is not None
    return row["id"]


async def _bonus_balance(conn: asyncpg.Connection, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    return await ledger.balance(conn, account.id)


@pytest.fixture
async def admin_id(pool):
    from tests.integration.test_admin_auth import create_test_admin

    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    return admin_id


async def test_referral_bonus_grants_the_referrer_on_a_qualifying_deposit(conn, admin_id):
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await _create_rule(conn, admin_id, reward_amount=Decimal("10.00"), min_qualifying_deposit=Decimal("20.00"))

    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("50.00"))

    assert await _bonus_balance(conn, referrer_id) == Decimal("10.00")
    row = await conn.fetchrow(
        "SELECT wagering_required, status FROM bonuses WHERE referral_of_user_id = $1", referee_id
    )
    assert row["status"] == "active"
    assert row["wagering_required"] == Decimal("30.00")  # 10.00 * 3x multiplier


async def test_below_minimum_deposit_does_not_grant(conn, admin_id):
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await _create_rule(conn, admin_id, min_qualifying_deposit=Decimal("20.00"))

    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("5.00"))
    assert await _bonus_balance(conn, referrer_id) == Decimal("0.00")


async def test_no_referrer_is_a_silent_no_op(conn, admin_id):
    referee_id = await create_user(conn)  # referred_by left null
    await _create_rule(conn, admin_id, min_qualifying_deposit=Decimal("0"))
    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("100.00"))
    # No exception, and nothing was created for anyone.
    count = await conn.fetchval("SELECT count(*) FROM bonuses WHERE referral_of_user_id = $1", referee_id)
    assert count == 0


async def test_no_active_rule_is_a_silent_no_op(conn):
    # bonus_rules is real, persisted, shared state across this whole test
    # file (and any earlier run against this same dev database) -- an
    # earlier test's own still-active rule must not leak into this one's
    # "there is no active rule" premise.
    await conn.execute("UPDATE bonus_rules SET is_active = false WHERE trigger_type = 'referral_reward'")
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("1000.00"))
    assert await _bonus_balance(conn, referrer_id) == Decimal("0.00")


async def test_an_inactive_rule_is_ignored(conn, admin_id):
    await conn.execute("UPDATE bonus_rules SET is_active = false WHERE trigger_type = 'referral_reward'")
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    rule_id = await _create_rule(conn, admin_id, min_qualifying_deposit=Decimal("0"))
    await conn.execute("UPDATE bonus_rules SET is_active = false WHERE id = $1", rule_id)

    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("100.00"))
    assert await _bonus_balance(conn, referrer_id) == Decimal("0.00")


async def test_self_referral_never_pays_out(conn, admin_id):
    user_id = await create_user(conn)
    await conn.execute("UPDATE users SET referred_by = $1 WHERE id = $1", user_id)
    await _create_rule(conn, admin_id, min_qualifying_deposit=Decimal("0"))

    await maybe_grant_referral_bonus(conn, user_id=user_id, deposit_amount=Decimal("100.00"))
    assert await _bonus_balance(conn, user_id) == Decimal("0.00")


async def test_referrer_and_referee_sharing_a_payout_account_blocks_the_reward(conn, admin_id):
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await _create_rule(conn, admin_id, min_qualifying_deposit=Decimal("0"))

    # Same withdrawal-destination account registered under both users --
    # the exact clustering signal services/admin/queries.py's own
    # shared_payout_account_clusters already surfaces platform-wide.
    await conn.execute(
        "INSERT INTO payment_methods (user_id, kind, account_ref, holder_name) "
        "VALUES ($1, 'telebirr', '0911000000', 'Shared Holder')",
        referrer_id,
    )
    await conn.execute(
        "INSERT INTO payment_methods (user_id, kind, account_ref, holder_name) "
        "VALUES ($1, 'telebirr', '0911000000', 'Shared Holder')",
        referee_id,
    )

    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("100.00"))
    assert await _bonus_balance(conn, referrer_id) == Decimal("0.00")


async def test_a_referee_can_only_ever_trigger_one_reward(conn, admin_id):
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await _create_rule(conn, admin_id, reward_amount=Decimal("10.00"), min_qualifying_deposit=Decimal("0"))

    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("100.00"))
    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("100.00"))

    assert await _bonus_balance(conn, referrer_id) == Decimal("10.00")  # not 20 -- granted once


async def test_max_grants_per_user_caps_the_referrer_not_the_referee(conn, admin_id, pool):
    referrer_id = await create_user(conn)
    await _create_rule(
        conn, admin_id, reward_amount=Decimal("10.00"), min_qualifying_deposit=Decimal("0"),
        max_grants_per_user=1,
    )

    referee_a = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_a)
    await maybe_grant_referral_bonus(conn, user_id=referee_a, deposit_amount=Decimal("100.00"))

    referee_b = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_b)
    await maybe_grant_referral_bonus(conn, user_id=referee_b, deposit_amount=Decimal("100.00"))

    # Two distinct, legitimate referees -- but the rule's own cap (1 per
    # referrer) means only the first one actually paid out.
    assert await _bonus_balance(conn, referrer_id) == Decimal("10.00")


async def test_percentage_reward_respects_its_own_cap(conn, admin_id):
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await _create_rule(
        conn, admin_id, reward_type="percentage", reward_amount=None, reward_percentage=Decimal("10.00"),
        reward_cap=Decimal("15.00"), min_qualifying_deposit=Decimal("0"),
    )

    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("1000.00"))
    # 10% of 1000 = 100, but the rule caps it at 15.
    assert await _bonus_balance(conn, referrer_id) == Decimal("15.00")


async def test_welcome_bonus_fires_independently_of_any_referral(conn, admin_id):
    user_id = await create_user(conn)  # no referred_by at all
    await _create_rule(
        conn, admin_id, trigger_type="welcome_bonus", reward_amount=Decimal("5.00"),
        min_qualifying_deposit=Decimal("0"),
    )

    await maybe_grant_welcome_bonus(conn, user_id=user_id, deposit_amount=Decimal("50.00"))
    assert await _bonus_balance(conn, user_id) == Decimal("5.00")


async def test_welcome_bonus_does_not_repeat_beyond_its_own_cap(conn, admin_id):
    user_id = await create_user(conn)
    await _create_rule(
        conn, admin_id, trigger_type="welcome_bonus", reward_amount=Decimal("5.00"),
        min_qualifying_deposit=Decimal("0"), max_grants_per_user=1,
    )
    await maybe_grant_welcome_bonus(conn, user_id=user_id, deposit_amount=Decimal("50.00"))
    await maybe_grant_welcome_bonus(conn, user_id=user_id, deposit_amount=Decimal("50.00"))
    assert await _bonus_balance(conn, user_id) == Decimal("5.00")  # not 10 -- granted once


async def test_concurrent_referral_grants_for_the_same_referee_settle_exactly_once(pool, conn, admin_id):
    """The real race this feature must survive: two of the three deposit-
    confirmation paths (or two racing retries of the same one) both
    reaching maybe_grant_referral_bonus for the same referee at once.
    Mirrors this codebase's own established concurrency-test shape (e.g.
    test_round_engine.py's double-claim race test).
    """
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await _create_rule(conn, admin_id, reward_amount=Decimal("10.00"), min_qualifying_deposit=Decimal("0"))

    async def attempt() -> None:
        async with pool.acquire() as race_conn:
            await maybe_grant_referral_bonus(race_conn, user_id=referee_id, deposit_amount=Decimal("100.00"))

    await asyncio.gather(*(attempt() for _ in range(10)))

    assert await _bonus_balance(conn, referrer_id) == Decimal("10.00")  # exactly once, not 10x
    count = await conn.fetchval("SELECT count(*) FROM bonuses WHERE referral_of_user_id = $1", referee_id)
    assert count == 1


# --- real end-to-end: the actual wired-in hook inside deposits.py --------


async def test_real_webhook_confirmation_grants_the_referral_bonus(pool, redis, conn, admin_id):
    provider = FakePaymentProvider()
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await _link_referral(conn, referrer_id=referrer_id, referee_id=referee_id)
    await _create_rule(conn, admin_id, reward_amount=Decimal("10.00"), min_qualifying_deposit=Decimal("20.00"))

    intent = await deposits.create_deposit_intent(
        pool, redis, provider, user_id=referee_id, amount=Decimal("200.00"),
        phone_e164="+251911000001", return_url="https://app.test/return",
        callback_url="https://payments.test/webhooks/chapa", min_deposit=MIN_DEPOSIT, daily_cap=DAILY_CAP,
    )
    headers, body = _webhook(
        event_id=f"evt-{intent.our_ref}", our_ref=intent.our_ref, status="succeeded", amount="200.00"
    )
    outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)

    assert outcome == "credited"
    assert await _cash_balance(conn, referee_id) == Decimal("200.00")  # the referee's own deposit, unaffected
    assert await _bonus_balance(conn, referrer_id) == Decimal("10.00")  # the referrer's real reward
