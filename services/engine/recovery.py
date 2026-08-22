"""Crash recovery. Never restart a game blindly after a crash: recover the
authoritative state from Postgres and decide the round can't continue.

Run once when an engine worker starts (services/engine/worker.py calls this
before claiming any room locks), so a round an earlier, now-dead worker
process left mid-flight gets voided and refunded rather than sitting stuck
forever or -- worse -- being silently picked up and resumed with a state
that has drifted from actual player balances.
"""

from __future__ import annotations

import asyncpg
from redis.asyncio import Redis

from services.engine.refunds import refund_round

NON_TERMINAL_STATUSES = ("lobby", "running", "settling")


async def recover_orphaned_rounds(pool: asyncpg.Pool, redis: Redis) -> list[int]:
    """Refunds every round left in a non-terminal status whose room lock is
    not currently held by any live engine. Returns the recovered round ids.
    """
    stuck = await pool.fetch(
        "SELECT id, room_id FROM rounds WHERE status = ANY($1::text[])",
        list(NON_TERMINAL_STATUSES),
    )

    recovered: list[int] = []
    for row in stuck:
        lock_key = f"room:lock:{row['room_id']}"
        if await redis.get(lock_key) is not None:
            continue  # a live engine still owns this room -- not orphaned
        if await refund_round(pool, row["id"], reason="crash_recovery"):
            recovered.append(row["id"])
    return recovered
