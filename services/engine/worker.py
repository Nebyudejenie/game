"""Engine worker process.

On startup, recovers any round a previous (now-dead) worker left mid-flight,
then claims and runs rooms -- one asyncio task per room, per spec's "one
Game Engine process owns any given room at a time" rule (section 2.3).
Room ownership itself is arbitrated by Redis (room_lock.py), not by this
class, so nothing here needs to coordinate directly with other workers:
every worker can attempt to claim every active room, and Redis decides who
actually gets each one.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
from redis.asyncio import Redis

from packages.core.bingo import Grid
from services.engine.recovery import recover_orphaned_rounds
from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config


class EngineWorker:
    def __init__(
        self, pool: asyncpg.Pool, redis: Redis, *, worker_id: str | None = None
    ) -> None:
        self._pool = pool
        self._redis = redis
        self._worker_id = worker_id or str(uuid.uuid4())
        self._card_pool: dict[int, Grid] | None = None
        self._engines: dict[int, RoundEngine] = {}
        self._tasks: dict[int, asyncio.Task[bool]] = {}

    async def start(self) -> list[int]:
        """Runs crash recovery, then loads the card pool. Call once before
        claiming any rooms. Returns the round ids that were recovered.
        """
        recovered = await recover_orphaned_rounds(self._pool, self._redis)
        self._card_pool = await load_card_pool(self._pool)
        return recovered

    async def claim_room(self, room_id: int) -> RoundEngine:
        """Starts an engine task attempting to own room_id. Whether it
        actually wins ownership is decided by Redis and only known a short
        while later -- poll the returned engine's is_lock_held().
        """
        if self._card_pool is None:
            raise RuntimeError("call start() before claiming rooms")

        room = await load_room_config(self._pool, room_id)
        engine = RoundEngine(
            self._pool, self._redis, room, self._card_pool, worker_id=self._worker_id
        )
        self._engines[room_id] = engine
        self._tasks[room_id] = asyncio.create_task(engine.run_forever())
        return engine

    async def run_active_rooms(self) -> None:
        rows = await self._pool.fetch("SELECT id FROM rooms WHERE is_active = true")
        for row in rows:
            await self.claim_room(row["id"])

    def engine_for(self, room_id: int) -> RoundEngine | None:
        return self._engines.get(room_id)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(engine.stop() for engine in self._engines.values())
        )
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._engines.clear()
        self._tasks.clear()
