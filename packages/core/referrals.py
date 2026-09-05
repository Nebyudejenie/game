"""Referral and welcome-bonus reward triggers, called from inside the
same transaction as a deposit's own success (see services/payments/
deposits.py::_apply_confirmed_status, services/admin/queries.py::
approve_manual_deposit_admin, services/payments/telebirr_redemption.py::
redeem_evidence -- the three places a deposit becomes 'succeeded' today).

Every check here is a silent return, never a raised exception: a fraud
signal or a missing rule must never fail the deposit itself, which is
real player money already committed by the time this runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg

from packages.core.bonuses import grant_bonus
from packages.core.ledger import AsyncpgConnection

_CENT = Decimal("0.01")


def _compute_reward_amount(rule: asyncpg.Record, deposit_amount: Decimal) -> Decimal:
    amount: Decimal
    if rule["reward_type"] == "flat":
        amount = rule["reward_amount"]
    else:
        amount = (deposit_amount * rule["reward_percentage"] / Decimal("100")).quantize(_CENT)
        if rule["reward_cap"] is not None:
            amount = min(amount, rule["reward_cap"])
    return amount


async def _active_rule(conn: AsyncpgConnection, trigger_type: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, reward_type, reward_amount, reward_percentage, reward_cap,
               min_qualifying_deposit, wagering_multiplier, expiry_days, max_grants_per_user
        FROM bonus_rules
        WHERE trigger_type = $1 AND is_active = true
          AND (starts_at IS NULL OR starts_at <= now())
          AND (ends_at IS NULL OR ends_at >= now())
        ORDER BY id DESC LIMIT 1
        """,
        trigger_type,
    )


async def _shares_a_payout_account(conn: AsyncpgConnection, user_a: int, user_b: int) -> bool:
    """Same signal services/admin/queries.py::shared_payout_account_clusters
    already surfaces platform-wide for the Risk screen, applied narrowly
    to one referrer/referee pair before a reward is ever paid out --
    spec 8.4's own anti-fraud table lists this exact pattern.
    """
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM payment_methods a
            JOIN payment_methods b ON a.kind = b.kind AND a.account_ref = b.account_ref
            WHERE a.user_id = $1 AND b.user_id = $2
        )
        """,
        user_a,
        user_b,
    )
    return bool(exists)


def _expiry_from(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


async def maybe_grant_referral_bonus(
    conn: AsyncpgConnection, *, user_id: int, deposit_amount: Decimal
) -> None:
    """`user_id` is the referee who just made a successful deposit. Grants
    the *referrer* a reward on the referee's first deposit that meets the
    active rule's minimum -- not necessarily their literal first deposit
    ever, since an earlier deposit might not have met the threshold.
    """
    referrer_id = await conn.fetchval("SELECT referred_by FROM users WHERE id = $1", user_id)
    if referrer_id is None:
        return
    if referrer_id == user_id:
        # Structurally shouldn't happen given how services/bot/registration
        # .py::register_from_contact resolves referred_by, but checked
        # explicitly and defensively -- a self-referral must never pay out.
        return

    already_rewarded = await conn.fetchval(
        "SELECT 1 FROM bonuses WHERE referral_of_user_id = $1", user_id
    )
    if already_rewarded:
        return

    rule = await _active_rule(conn, "referral_reward")
    if rule is None or deposit_amount < rule["min_qualifying_deposit"]:
        return

    if await _shares_a_payout_account(conn, referrer_id, user_id):
        return

    grants_to_referrer = await conn.fetchval(
        "SELECT count(*) FROM bonuses WHERE user_id = $1 AND rule_id = $2", referrer_id, rule["id"]
    )
    if grants_to_referrer >= rule["max_grants_per_user"]:
        return

    reward_amount = _compute_reward_amount(rule, deposit_amount)
    if reward_amount <= 0:
        return

    try:
        await grant_bonus(
            conn,
            user_id=referrer_id,
            idempotency_key=f"referral-{referrer_id}-{user_id}",
            amount=reward_amount,
            wagering_required=(reward_amount * rule["wagering_multiplier"]).quantize(_CENT),
            rule_id=rule["id"],
            referral_of_user_id=user_id,
            expires_at=_expiry_from(rule["expiry_days"]),
            reason=f"Referral reward for inviting user {user_id}",
        )
    except asyncpg.exceptions.UniqueViolationError:
        # ux_bonuses_referral_once caught a genuine race with another
        # concurrent grant attempt for this same referee -- someone else
        # already won it, not an error.
        return


async def maybe_grant_welcome_bonus(
    conn: AsyncpgConnection, *, user_id: int, deposit_amount: Decimal
) -> None:
    """Independent of referrals entirely -- fires for *any* user's first
    deposit that meets an active welcome_bonus rule's minimum, referred
    or not, per the same trigger-type-agnostic rule engine.
    """
    rule = await _active_rule(conn, "welcome_bonus")
    if rule is None or deposit_amount < rule["min_qualifying_deposit"]:
        return

    grants_so_far = await conn.fetchval(
        "SELECT count(*) FROM bonuses WHERE user_id = $1 AND rule_id = $2", user_id, rule["id"]
    )
    if grants_so_far >= rule["max_grants_per_user"]:
        return

    reward_amount = _compute_reward_amount(rule, deposit_amount)
    if reward_amount <= 0:
        return

    await grant_bonus(
        conn,
        user_id=user_id,
        # grants_so_far in the key means a genuinely new grant (the
        # common max_grants_per_user=1 "welcome" case, and any later one
        # for a repeatable rule) always gets its own idempotency key,
        # while a real retry of the *same* attempt still recomputes the
        # same count and lands on the same key as before.
        idempotency_key=f"welcome-{user_id}-{rule['id']}-{grants_so_far}",
        amount=reward_amount,
        wagering_required=(reward_amount * rule["wagering_multiplier"]).quantize(_CENT),
        rule_id=rule["id"],
        expires_at=_expiry_from(rule["expiry_days"]),
        reason="Welcome bonus on first qualifying deposit",
    )
