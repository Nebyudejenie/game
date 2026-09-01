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
import contextlib
import signal
import uuid

import asyncpg
from redis.asyncio import Redis

from packages.core import metrics
from packages.core.bingo import Grid
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.logging import configure_logging
from packages.core.redis_conn import get_redis
from packages.core.tracing import configure_tracing
from services.engine.recovery import recover_orphaned_rounds
from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config

METRICS_PORT = 8004

# How often a live worker re-scans for newly-activated rooms. Deliberately
# not tied to anything room-specific (lobby_seconds etc.) -- this is
# purely "notice a room an admin just turned on," not part of any single
# room's own timing.
CLAIM_POLL_INTERVAL_SECONDS = 30


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
        """Claims every currently-active room this worker doesn't already
        own a live engine for. Safe to call repeatedly -- a real production
        entrypoint calls this on a timer, not just once at startup, since a
        room admin-created *after* startup would otherwise never get an
        engine at all. claim_room() itself has no such guard (it
        unconditionally overwrites self._engines/self._tasks), so calling
        it twice for a room this worker already owns would silently orphan
        the running task -- still executing, but with no reference left to
        stop it on shutdown -- while a redundant second engine raced it for
        a lock it can only lose. Skips anything still genuinely running;
        reclaims anything whose task already finished (lock never won, or
        the room went terminal and the engine returned on its own).
        """
        rows = await self._pool.fetch("SELECT id FROM rooms WHERE is_active = true")
        for row in rows:
            room_id = row["id"]
            existing_task = self._tasks.get(room_id)
            if existing_task is not None and not existing_task.done():
                continue
            await self.claim_room(room_id)

    def engine_for(self, room_id: int) -> RoundEngine | None:
        return self._engines.get(room_id)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(engine.stop() for engine in self._engines.values())
        )
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._engines.clear()
        self._tasks.clear()


def main() -> None:
    """Real production entrypoint: recovers any round a previous, now-dead
    worker left mid-flight, claims every currently-active room, then keeps
    re-scanning for newly-activated ones (run_active_rooms()'s own
    docstring explains why claim_room() alone isn't enough for that) until
    the container runtime sends SIGTERM/SIGINT, at which point every owned
    room's engine gets a real chance to stop cleanly rather than just
    vanishing mid-round.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing("engine-worker", settings.otel_exporter_endpoint)

    async def _run() -> None:
        pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=20)
        redis = get_redis()
        worker = EngineWorker(pool, redis)
        metrics_runner = await metrics.start_metrics_server(METRICS_PORT)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        try:
            await worker.start()
            while not stop_event.is_set():
                await worker.run_active_rooms()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=CLAIM_POLL_INTERVAL_SECONDS)
        finally:
            await worker.shutdown()
            await metrics_runner.cleanup()
            await redis.aclose()
            await pool.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
