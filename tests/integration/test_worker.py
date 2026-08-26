"""Smoke tests for services/engine/worker.py -- claiming rooms and running
crash recovery on startup. The state-machine correctness itself is covered
exhaustively in test_round_engine.py and test_recovery.py; this just proves
the worker wiring actually drives a real RoundEngine end to end.
"""

import asyncio
from decimal import Decimal

from packages.core import ledger
from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config
from services.engine.worker import EngineWorker
from tests.integration.conftest import create_funded_user, create_room


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_worker_claims_and_runs_a_room(pool, redis, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=10)
    worker = EngineWorker(pool, redis, worker_id="test-worker-1")
    await worker.start()
    engine = await worker.claim_room(room_id)
    try:
        await wait_until(lambda: engine.is_lock_held(), timeout=5)

        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)

        round_row = await pool.fetchrow(
            "SELECT status, pot FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1",
            room_id,
        )
        assert round_row["status"] in ("done", "voided")
        assert round_row["pot"] == Decimal("20.00")
    finally:
        await worker.shutdown()


async def test_run_active_rooms_is_safe_to_call_repeatedly(pool, redis, conn):
    # A real production entrypoint calls this on a timer, not just once at
    # startup, so a room admin-created after startup still gets an engine.
    # claim_room() itself has no guard against being called twice for the
    # same room -- calling it again for a room this worker already owns
    # would silently orphan the running task (still executing, but with no
    # reference left to stop it on shutdown) while a redundant second
    # engine raced it for a lock it could only ever lose.
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    worker = EngineWorker(pool, redis, worker_id="test-worker-repoll")
    await worker.start()
    try:
        await worker.run_active_rooms()
        first_task = worker._tasks[room_id]  # noqa: SLF001
        first_engine = worker.engine_for(room_id)

        await worker.run_active_rooms()
        second_task = worker._tasks[room_id]  # noqa: SLF001

        assert second_task is first_task, "an already-running room's engine got replaced"
        assert worker.engine_for(room_id) is first_engine
    finally:
        await worker.shutdown()


async def test_worker_start_recovers_orphaned_rounds(pool, redis, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=50)
    room = await load_room_config(pool, room_id)
    card_pool = await load_card_pool(pool)
    orphan_engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(orphan_engine.run_forever())

    p1 = await create_funded_user(conn, Decimal("50.00"))
    p2 = await create_funded_user(conn, Decimal("50.00"))
    assert (await orphan_engine.join(p1, 1)).ok
    assert (await orphan_engine.join(p2, 2)).ok

    await wait_until(lambda: orphan_engine.status == "running", timeout=5)
    round_id = orphan_engine.round_id

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await redis.delete(f"room:lock:{room_id}")

    worker = EngineWorker(pool, redis, worker_id="test-worker-2")
    recovered = await worker.start()
    try:
        assert round_id in recovered
        row = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert row["status"] == "voided"

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")
    finally:
        await worker.shutdown()
