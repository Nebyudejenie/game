"""Manual deposit request creation -- the player-facing half of the P1
manual-payment fallback (Chapa unavailable/not configured/not yet
approved for a market). Admin-side approve/reject lives in
services/admin/queries.py, following the exact row-lock + status-guard
shape as the existing approve_withdrawal_admin/reject_withdrawal_admin.

A manual deposit skips the pending->processing checkout dance a real
(automatic) deposit goes through -- there's no checkout step, so this
goes straight from nothing to status='review' in one insert: exactly the
state an admin's review queue already knows how to work from, matching
the product directive's "PENDING REVIEW".

Manual withdrawals have no equivalent module here -- they reuse
services/payments/withdrawals.py's request_withdrawal() directly (with
provider=ManualProvider(), force_review=True), since withdrawal's
existing validation/fund-locking is already provider-agnostic and needs
no new entry point at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import tracing
from services.payments.deposits import (
    DepositRejected,
    _check_deposit_eligibility,
    _check_deposit_rate_limit_and_minimum,
)

logger = structlog.get_logger()
_tracer = tracing.get_tracer(__name__)


class UnknownManualDestination(DepositRejected):
    """manual_destination_id doesn't exist, or points at a destination an
    admin has since deactivated -- same DepositRejected hierarchy as the
    automatic path's rejection reasons, so callers (gateway/bot) can
    catch "any deposit request was rejected" generically without caring
    which rail it was.
    """


@dataclass(frozen=True)
class ManualDepositIntent:
    payment_id: int
    our_ref: str


async def create_manual_deposit_request(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    user_id: int,
    amount: Decimal,
    manual_destination_id: int,
    external_reference: str,
    receipt_telegram_file_id: str | None,
    min_deposit: Decimal,
    daily_cap: Decimal,
) -> ManualDepositIntent:
    with _tracer.start_as_current_span(
        "deposit.create_manual_request", attributes={"user_id": user_id, "amount": str(amount)}
    ) as span:
        # Exact same gates an automatic deposit request runs -- see
        # deposits.py's own docstring on these two functions for why
        # they're shared rather than duplicated.
        await _check_deposit_rate_limit_and_minimum(
            redis, user_id=user_id, amount=amount, min_deposit=min_deposit
        )

        async with pool.acquire() as conn:
            async with conn.transaction():
                await _check_deposit_eligibility(
                    conn, user_id=user_id, amount=amount, daily_cap=daily_cap
                )

                destination = await conn.fetchval(
                    "SELECT id FROM manual_payment_destinations WHERE id = $1 AND is_active",
                    manual_destination_id,
                )
                if destination is None:
                    raise UnknownManualDestination(
                        f"manual_destination_id {manual_destination_id} is not a real, active destination"
                    )

                ref_row = await conn.fetchrow(
                    "SELECT 'DEP-' || extract(year from now())::text || '-' || "
                    "lpad(nextval('payment_ref_seq')::text, 6, '0') AS our_ref"
                )
                assert ref_row is not None
                our_ref: str = ref_row["our_ref"]
                span.set_attribute("deposit.our_ref", our_ref)

                # Straight to 'review' -- no checkout, nothing to be
                # 'pending'/'processing' about. Duplicate-external-
                # reference detection is deliberately NOT done here (see
                # services/admin/queries.py's list_pending_manual_deposits):
                # a stored flag at insert time would never un-flag once
                # the earlier conflicting request got rejected, so it's
                # computed live at admin-list-query time instead. This
                # never blocks a submission.
                payment_row = await conn.fetchrow(
                    """
                    INSERT INTO payments
                        (user_id, direction, provider, our_ref, amount, status,
                         manual_destination_id, provider_ref, receipt_telegram_file_id)
                    VALUES ($1, 'in', 'manual', $2, $3, 'review', $4, $5, $6)
                    RETURNING id
                    """,
                    user_id,
                    our_ref,
                    amount,
                    manual_destination_id,
                    external_reference,
                    receipt_telegram_file_id,
                )
                assert payment_row is not None
                payment_id: int = payment_row["id"]

        return ManualDepositIntent(payment_id=payment_id, our_ref=our_ref)


async def attach_receipt_to_latest_pending_deposit(
    pool: asyncpg.Pool, *, user_id: int, telegram_file_id: str
) -> int | None:
    """Correlates an incoming Telegram photo message to the player's own
    most recent manual deposit still awaiting review with no receipt
    attached yet -- the whole mechanism the bot's photo handler needs, no
    conversational state required. Returns the payment_id attached to, or
    None if there was nothing pending to attach it to (the bot handler
    uses that to reply honestly instead of silently dropping the photo).
    """
    row = await pool.fetchrow(
        """
        UPDATE payments SET receipt_telegram_file_id = $2, updated_at = now()
        WHERE id = (
            SELECT id FROM payments
            WHERE user_id = $1 AND direction = 'in' AND provider = 'manual'
              AND status = 'review' AND receipt_telegram_file_id IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        )
        RETURNING id
        """,
        user_id,
        telegram_file_id,
    )
    return row["id"] if row is not None else None
