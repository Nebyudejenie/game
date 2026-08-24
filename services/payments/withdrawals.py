"""Withdrawal domain logic (spec section 8.3, Prompt 8).

"The single most important step" (the spec's own words): the moment a
withdrawal is requested, funds move user_cash -> user_locked in the same
transaction that creates the payments row. There is no window in which a
player can both request a withdrawal and stake the same birr -- the ledger's
own row-locked, InsufficientFunds-raising post() is what actually
guarantees that, the same mechanism a stake and any other debit already
rely on. bonus funds are structurally excluded: a withdrawal only ever
debits user_cash, never user_bonus.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import ledger
from services.payments.provider import PaymentProvider

logger = structlog.get_logger()

PAYOUT_STREAM = "payouts"

# The one payout method every provider in the spec's table (Chapa,
# SantimPay, ArifPay) covers -- the bot's /withdraw command doesn't yet
# collect a method choice, so this is the default until it does.
DEFAULT_METHOD_KIND = "telebirr"

# WithdrawalIntent.status values -- named here so callers (services/bot
# /handlers.py in particular) compare against a constant instead of a bare
# string literal, which would otherwise trip that file's AST-based
# no-hardcoded-strings check the same way a raw domain string anywhere else
# in it would.
STATUS_APPROVED = "approved"
STATUS_REVIEW = "review"


class WithdrawalRejected(Exception):
    """Base class -- see services/payments/deposits.py's DepositRejected
    for why this is a class hierarchy rather than one exception with a
    string `.reason` field.
    """


class BelowMinimumWithdrawal(WithdrawalRejected):
    pass


class InsufficientAvailableBalance(WithdrawalRejected):
    pass


class KycLevelTooLow(WithdrawalRejected):
    pass


class RecentReversibleDeposit(WithdrawalRejected):
    pass


class UnknownWithdrawer(WithdrawalRejected):
    pass


@dataclass(frozen=True)
class WithdrawalIntent:
    payment_id: int
    our_ref: str
    status: str  # 'approved' (queued for payout) | 'review' (admin queue)


async def request_withdrawal(
    pool: asyncpg.Pool,
    redis: Redis,
    provider: PaymentProvider,
    *,
    user_id: int,
    amount: Decimal,
    method_kind: str,
    account_ref: str,
    holder_name: str,
    min_withdraw: Decimal,
    auto_approve_limit: Decimal,
    kyc_threshold: Decimal,
    chargeback_window_minutes: int,
    min_account_age_hours: float = 24.0,
) -> WithdrawalIntent:
    if amount < min_withdraw:
        raise BelowMinimumWithdrawal(f"amount {amount} is below the minimum {min_withdraw}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT kyc_level, created_at FROM users WHERE id = $1 FOR UPDATE", user_id
            )
            if user is None:
                raise UnknownWithdrawer(f"user {user_id} does not exist")

            if amount > kyc_threshold and user["kyc_level"] < 2:
                raise KycLevelTooLow(
                    f"user {user_id} has kyc_level {user['kyc_level']}, needs >= 2 for amount {amount}"
                )

            recent_deposit = await conn.fetchval(
                """
                SELECT 1 FROM payments
                WHERE user_id = $1 AND direction = 'in' AND status = 'succeeded'
                  AND created_at > now() - make_interval(mins => $2)
                LIMIT 1
                """,
                user_id,
                chargeback_window_minutes,
            )
            if recent_deposit:
                raise RecentReversibleDeposit(
                    f"user {user_id} has a succeeded deposit within the last "
                    f"{chargeback_window_minutes} minutes"
                )

            method_row = await conn.fetchrow(
                """
                INSERT INTO payment_methods (user_id, kind, account_ref, holder_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, kind, account_ref) DO UPDATE SET holder_name = EXCLUDED.holder_name
                RETURNING id
                """,
                user_id,
                method_kind,
                account_ref,
                holder_name,
            )
            assert method_row is not None
            method_id: int = method_row["id"]

            ref_row = await conn.fetchrow(
                "SELECT 'WD-' || extract(year from now())::text || '-' || "
                "lpad(nextval('payment_ref_seq')::text, 6, '0') AS our_ref"
            )
            assert ref_row is not None
            our_ref: str = ref_row["our_ref"]

            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            locked = await ledger.get_or_create_account(conn, user_id, "user_locked")
            try:
                txn = await ledger.post(
                    conn,
                    "withdrawal",
                    [ledger.Entry(cash.id, -amount), ledger.Entry(locked.id, amount)],
                    idempotency_key=our_ref,
                )
            except ledger.InsufficientFunds as exc:
                raise InsufficientAvailableBalance(
                    f"user {user_id} does not have {amount} of available cash"
                ) from exc

            lifetime_in = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM payments "
                "WHERE user_id = $1 AND direction = 'in' AND status = 'succeeded'",
                user_id,
            )
            lifetime_out = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM payments "
                "WHERE user_id = $1 AND direction = 'out' AND status = 'succeeded'",
                user_id,
            )
            account_age = datetime.now(UTC) - user["created_at"]
            auto_ok = (
                amount <= auto_approve_limit
                and account_age.total_seconds() > min_account_age_hours * 3600
                and lifetime_in >= lifetime_out
            )
            status = STATUS_APPROVED if auto_ok else STATUS_REVIEW

            payment_row = await conn.fetchrow(
                """
                INSERT INTO payments
                    (user_id, direction, provider, our_ref, amount, status, method_id, ledger_txn_id)
                VALUES ($1, 'out', $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                user_id,
                provider.name,
                our_ref,
                amount,
                status,
                method_id,
                txn.id,
            )
            assert payment_row is not None
            payment_id: int = payment_row["id"]

    if status == STATUS_APPROVED:
        await enqueue_payout(redis, our_ref=our_ref, payment_id=payment_id)

    return WithdrawalIntent(payment_id=payment_id, our_ref=our_ref, status=status)


async def enqueue_payout(redis: Redis, *, our_ref: str, payment_id: int) -> None:
    await redis.xadd(PAYOUT_STREAM, {"our_ref": our_ref, "payment_id": str(payment_id)})
