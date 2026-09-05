"""Bonus & Referral admin operations: rule CRUD, manual grants, the
referral-history/liability lists, and a referral-specific fraud-signal
query in the same on-demand, human-review-first style
services/admin/queries.py's shared_payout_account_clusters/
repeat_room_pairings already use for the Risk screen. Money movement
itself always goes through packages/core/bonuses.py -- this module never
posts a ledger entry directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core.bonuses import BonusNotFound, grant_bonus, revoke_bonus
from services.admin import audit

_TRIGGER_TYPES = frozenset({"referral_reward", "welcome_bonus", "deposit_match", "manual_grant"})
_REWARD_TYPES = frozenset({"flat", "percentage"})
_RULE_EDITABLE_FIELDS = frozenset({
    "name", "reward_amount", "reward_percentage", "reward_cap", "min_qualifying_deposit",
    "wagering_multiplier", "expiry_days", "max_grants_per_user", "is_active", "starts_at", "ends_at",
})


class InvalidBonusRule(ValueError):
    pass


def _validate_reward_shape(reward_type: str, reward_amount: Decimal | None, reward_percentage: Decimal | None) -> None:
    if reward_type not in _REWARD_TYPES:
        raise InvalidBonusRule(f"unknown reward_type: {reward_type!r}")
    if reward_type == "flat" and reward_amount is None:
        raise InvalidBonusRule("reward_amount is required for a flat reward")
    if reward_type == "percentage" and reward_percentage is None:
        raise InvalidBonusRule("reward_percentage is required for a percentage reward")


async def create_bonus_rule_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    name: str,
    trigger_type: str,
    reward_type: str,
    reward_amount: Decimal | None,
    reward_percentage: Decimal | None,
    reward_cap: Decimal | None,
    min_qualifying_deposit: Decimal,
    wagering_multiplier: Decimal,
    expiry_days: int | None,
    max_grants_per_user: int,
    ip_address: str | None,
) -> int:
    if trigger_type not in _TRIGGER_TYPES:
        raise InvalidBonusRule(f"unknown trigger_type: {trigger_type!r}")
    _validate_reward_shape(reward_type, reward_amount, reward_percentage)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO bonus_rules
                    (name, trigger_type, reward_type, reward_amount, reward_percentage, reward_cap,
                     min_qualifying_deposit, wagering_multiplier, expiry_days, max_grants_per_user,
                     created_by_admin_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
                """,
                name,
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
            await audit.record(
                conn,
                admin_id=admin_id,
                action="bonus_rules.create",
                target_type="bonus_rule",
                target_id=str(row["id"]),
                after={"name": name, "trigger_type": trigger_type, "reward_type": reward_type},
                ip_address=ip_address,
            )
            return int(row["id"])


async def update_bonus_rule_admin(
    pool: asyncpg.Pool, *, admin_id: int, rule_id: int, changes: dict[str, Any], ip_address: str | None
) -> bool:
    unknown = set(changes) - _RULE_EDITABLE_FIELDS
    if unknown:
        raise InvalidBonusRule(f"not an editable bonus rule field: {unknown}")
    if not changes:
        return False

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow("SELECT * FROM bonus_rules WHERE id = $1 FOR UPDATE", rule_id)
            if before is None:
                return False

            # A partial reward-shape edit (e.g. only reward_percentage
            # changing) still has to satisfy chk_bonus_rules_reward_shape
            # against the *resulting* row, not just the fields present in
            # this one call -- checked here so a bad edit gets a clean
            # 422 instead of a raw DB constraint violation surfacing as a
            # 500.
            reward_type = changes.get("reward_type", before["reward_type"])
            reward_amount = changes.get("reward_amount", before["reward_amount"])
            reward_percentage = changes.get("reward_percentage", before["reward_percentage"])
            _validate_reward_shape(reward_type, reward_amount, reward_percentage)

            set_clauses = []
            values: list[Any] = []
            for i, (field, value) in enumerate(changes.items(), start=1):
                set_clauses.append(f"{field} = ${i}")
                values.append(value)
            values.append(rule_id)
            await conn.execute(
                f"UPDATE bonus_rules SET {', '.join(set_clauses)}, updated_by_admin_id = ${len(values) + 1}, "
                f"updated_at = now() WHERE id = ${len(values)}",
                *values,
                admin_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="bonus_rules.update",
                target_type="bonus_rule",
                target_id=str(rule_id),
                before={k: before[k] for k in changes},
                after=changes,
                ip_address=ip_address,
            )
            return True


async def list_bonus_rules_admin(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch("SELECT * FROM bonus_rules ORDER BY id DESC")
    return [dict(r) for r in rows]


async def grant_manual_bonus_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    amount: Decimal,
    wagering_multiplier: Decimal,
    expiry_days: int | None,
    reason: str,
    ip_address: str | None,
) -> int:
    """A superadmin/finance-initiated grant with no triggering deposit --
    e.g. a goodwill credit or a manually-approved promotion. Reuses
    packages/core/bonuses.py::grant_bonus() directly, the same primitive
    every automatic trigger uses, just with granted_by_admin_id set and
    no rule_id.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(days=expiry_days) if expiry_days else None

    async with pool.acquire() as conn:
        async with conn.transaction():
            bonus = await grant_bonus(
                conn,
                user_id=user_id,
                idempotency_key=f"manual-grant-{admin_id}-{user_id}-{datetime.now(timezone.utc).timestamp()}",
                amount=amount,
                wagering_required=(amount * wagering_multiplier).quantize(Decimal("0.01")),
                expires_at=expires_at,
                granted_by_admin_id=admin_id,
                reason=reason,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="bonuses.manual_grant",
                target_type="bonus",
                target_id=str(bonus.id),
                after={"user_id": user_id, "amount": str(amount)},
                reason=reason,
                ip_address=ip_address,
            )
            return bonus.id


async def list_bonuses_admin(
    pool: asyncpg.Pool, *, user_id: int | None = None, status: str | None = None,
    limit: int = 100, offset: int = 0,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []

    def _p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if user_id is not None:
        clauses.append(f"b.user_id = {_p(user_id)}")
    if status is not None:
        clauses.append(f"b.status = {_p(status)}")
    where = " AND ".join(clauses) if clauses else "true"
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"""
        SELECT b.id, b.user_id, u.display_name, b.rule_id, r.name AS rule_name,
               b.referral_of_user_id, b.amount, b.wagering_required, b.wagering_progress,
               b.status, b.expires_at, b.converted_at, b.reason, b.created_at
        FROM bonuses b
        JOIN users u ON u.id = b.user_id
        LEFT JOIN bonus_rules r ON r.id = b.rule_id
        WHERE {where}
        ORDER BY b.id DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def revoke_bonus_admin(
    pool: asyncpg.Pool, *, admin_id: int, bonus_id: int, reason: str, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                revoked = await revoke_bonus(conn, bonus_id=bonus_id)
            except BonusNotFound:
                return False
            if revoked:
                await audit.record(
                    conn,
                    admin_id=admin_id,
                    action="bonuses.revoke",
                    target_type="bonus",
                    target_id=str(bonus_id),
                    reason=reason,
                    ip_address=ip_address,
                )
            return revoked


async def referral_funnel_admin(pool: asyncpg.Pool) -> dict[str, Any]:
    """Invited -> registered -> made a qualifying deposit -> rewarded.
    "Invited" only counts registrations that actually carry a referrer
    (users.referred_by), the same column services/bot/registration.py's
    own register_from_contact() already sets -- there's no separate
    "link clicked" tracking anywhere in this codebase, so the funnel
    starts from the first stage that's real, verifiable data.
    """
    registered = await pool.fetchval("SELECT count(*) FROM users WHERE referred_by IS NOT NULL")
    deposited = await pool.fetchval(
        "SELECT count(DISTINCT u.id) FROM users u JOIN payments p ON p.user_id = u.id "
        "WHERE u.referred_by IS NOT NULL AND p.status = 'succeeded'"
    )
    rewarded = await pool.fetchval("SELECT count(*) FROM bonuses WHERE referral_of_user_id IS NOT NULL")
    liability = await pool.fetchval(
        "SELECT COALESCE(SUM(amount - wagering_progress), 0) FROM bonuses WHERE status = 'active'"
    )
    top_referrers = await pool.fetch(
        """
        SELECT u.id AS user_id, u.display_name, count(*) AS referral_count,
               COALESCE(SUM(b.amount), 0) AS total_rewarded
        FROM bonuses b
        JOIN users u ON u.id = b.user_id
        WHERE b.referral_of_user_id IS NOT NULL
        GROUP BY u.id, u.display_name
        ORDER BY referral_count DESC
        LIMIT 20
        """
    )
    return {
        "registered_via_referral": registered,
        "referees_who_deposited": deposited,
        "referrals_rewarded": rewarded,
        "outstanding_bonus_liability": str(liability),
        "top_referrers": [dict(r) for r in top_referrers],
    }


async def referral_fraud_candidates_admin(pool: asyncpg.Pool) -> dict[str, Any]:
    """Referral-specific extension of services/admin/queries.py's own
    shared_payout_account_clusters/repeat_room_pairings philosophy: a
    live, on-demand query surfacing candidates for a human admin to
    review, never an automatic block. Two signals, both drawn from data
    this codebase already has: a referrer/referee pair sharing a payout
    account (spec 8.4's own account-linking rule, applied to referral
    pairs specifically), and one referrer with an unusually high referral
    count in the trailing 24 hours.
    """
    shared_account_pairs = await pool.fetch(
        """
        SELECT DISTINCT ru.id AS referrer_id, ru.display_name AS referrer_name,
               eu.id AS referee_id, eu.display_name AS referee_name,
               pm.kind, pm.account_ref
        FROM users eu
        JOIN users ru ON ru.id = eu.referred_by
        JOIN payment_methods pm ON pm.user_id = ru.id
        JOIN payment_methods pm2 ON pm2.user_id = eu.id AND pm2.kind = pm.kind AND pm2.account_ref = pm.account_ref
        """
    )
    burst_referrers = await pool.fetch(
        """
        SELECT ru.id AS referrer_id, ru.display_name AS referrer_name, count(*) AS referrals_last_24h
        FROM users eu
        JOIN users ru ON ru.id = eu.referred_by
        WHERE eu.created_at >= now() - interval '24 hours'
        GROUP BY ru.id, ru.display_name
        HAVING count(*) >= 5
        ORDER BY referrals_last_24h DESC
        """
    )
    return {
        "shared_payout_account_pairs": [dict(r) for r in shared_account_pairs],
        "burst_referrers_last_24h": [dict(r) for r in burst_referrers],
    }
