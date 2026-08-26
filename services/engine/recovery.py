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
    """Refunds every round left in a non-terminal status that's genuinely
    orphaned. Returns the recovered round ids.

    A round counts as orphaned two ways:
    - it's no longer its room's *latest* round -- a room only ever runs
      one round at a time, so once a newer round exists for the same
      room, any older non-terminal round is unambiguously abandoned,
      regardless of whether the room's lock is currently held (it's held
      for that *newer* round, not this one); or
    - it is the latest round, but the room's lock isn't currently held by
      any live engine.

    A code review pass caught a real gap in the original, room-lock-only
    check: it treated "room lock held by *anyone*" as proof the specific
    stuck round was still owned. If that round's lock had already expired
    and a *different* engine claimed the room (starting a fresh round)
    before this sweep ran, the old stuck round was skipped forever --
    this function only runs once, at worker startup -- leaving its
    entrants' stakes in pot_escrow with no remaining path to a refund. A
    real risk in the multi-worker fleet this whole locking scheme exists
    to support.
    """
    stuck = await pool.fetch(
        "SELECT id, room_id, seq FROM rounds WHERE status = ANY($1::text[])",
        list(NON_TERMINAL_STATUSES),
    )
    if not stuck:
        return []

    # One batched lookup for every room a stuck round belongs to, not one
    # round-trip per stuck round -- a code review pass caught this as a
    # real N+1: this function runs synchronously at worker startup, before
    # any room can be claimed, so a real incident (many engines crashing
    # together, leaving rounds stuck across many rooms at once) would have
    # made recovery itself -- and therefore the whole platform coming back
    # up -- scale with however many rounds needed recovering. Scoped to
    # just the room_ids actually involved (via the same room_id column
    # ix_rounds_room_status already indexes), not every room this
    # platform has ever run, so this stays a small, targeted query
    # regardless of how large the rounds table has grown historically.
    room_ids = list({row["room_id"] for row in stuck})
    latest_seq_rows = await pool.fetch(
        "SELECT room_id, max(seq) AS max_seq FROM rounds WHERE room_id = ANY($1::bigint[]) GROUP BY room_id",
        room_ids,
    )
    latest_seq_by_room = {row["room_id"]: row["max_seq"] for row in latest_seq_rows}

    recovered: list[int] = []
    for row in stuck:
        latest_seq = latest_seq_by_room.get(row["room_id"])
        if row["seq"] == latest_seq:
            lock_key = f"room:lock:{row['room_id']}"
            if await redis.get(lock_key) is not None:
                continue  # a live engine still owns this room's current round -- not orphaned
        if await refund_round(pool, row["id"], reason="crash_recovery"):
            recovered.append(row["id"])
    return recovered
