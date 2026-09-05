"""Bonus wagering sweep: the one periodic job that ever converts a sticky
bonus grant into spendable, withdrawable cash, or expires one that was
never wagered off in time. Runs inside the existing payout_worker process
(see main_async() there) as a fifth _run_periodic_sweep() alongside the
four payments sweeps already living there -- no new deployable service.

Reads only: wagering progress comes from packages/core/bonuses.py::
wagering_progress_for_user_since(), a query against the same stake
ledger history round_engine.py's own join() already writes. Nothing this
sweep does ever touches round_engine.py, refunds.py, or any stake/payout
code path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core.bonuses import convert_bonus_to_cash, expire_bonus, wagering_progress_for_user_since
from packages.core.ledger import publish_balance_update

logger = structlog.get_logger()


async def sweep_bonus_wagering(pool: asyncpg.Pool, redis: Redis) -> None:
    rows = await pool.fetch(
        "SELECT id, user_id, wagering_required, expires_at, created_at FROM bonuses WHERE status = 'active'"
    )
    for row in rows:
        async with pool.acquire() as conn:
            progress = await wagering_progress_for_user_since(
                conn, user_id=row["user_id"], since=row["created_at"]
            )
            await conn.execute(
                "UPDATE bonuses SET wagering_progress = $2, updated_at = now() WHERE id = $1",
                row["id"],
                progress,
            )

            if progress >= row["wagering_required"]:
                converted = await convert_bonus_to_cash(conn, bonus_id=row["id"])
                if converted:
                    logger.info("bonus_wagering_requirement_met", bonus_id=row["id"], user_id=row["user_id"])
                    await publish_balance_update(pool, redis, row["user_id"])
                continue

            if row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc):
                expired = await expire_bonus(conn, bonus_id=row["id"])
                if expired:
                    logger.info("bonus_expired_unwagered", bonus_id=row["id"], user_id=row["user_id"])
                    await publish_balance_update(pool, redis, row["user_id"])
