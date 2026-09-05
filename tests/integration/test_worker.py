"""Smoke tests for services/engine/worker.py -- claiming rooms and running
crash recovery on startup. The state-machine correctness itself is covered
exhaustively in test_round_engine.py and test_recovery.py; this just proves
the worker wiring actually drives a real RoundEngine end to end.
"""

import asyncio
import contextlib
from decimal import Decimal

from packages.core import ledger
from services.engine import worker as worker_module
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
    #
    # run_active_rooms() claims *every* is_active=true row with no limit --
    # unlike every other test here, which only ever touches the one room it
    # creates, this test is directly sensitive to how many such rows exist
    # in the whole (shared, never-truncated-between-runs) database. Root-
    # caused via a live reproduction: this dev DB had accumulated 560
    # is_active=true rooms from unrelated tests over a long session, so a
    # single run_active_rooms() call here span up 560 real engines/locks/
    # stream-readers concurrently and blew straight through the Redis pool's
    # connection cap -- the exact MaxConnectionsError/TimeoutError this test
    # kept flaking with, confirmed by instrumenting the real pool and seeing
    # room-lock refresh failures for rooms this test never created. Neutralize
    # that shared-DB noise the same way other tests already defend against
    # it (see the campaigns _INERT_AUDIENCE / unique_username() precedent)
    # instead of assuming the table starts empty.
    await conn.execute("UPDATE rooms SET is_active = false WHERE is_active = true")
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, is_active=True)
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


async def test_run_active_rooms_caps_new_claims_per_poll(pool, redis, conn):
    # Launch-readiness audit: proves the real MAX_NEW_CLAIMS_PER_POLL cap
    # (worker.py) actually limits how many rooms one run_active_rooms()
    # call claims, using the real production constant -- not a smaller
    # stand-in -- so this is a direct proof of the real deployed behavior,
    # not an approximation of it. Same defensive cleanup as the test
    # above: this dev database is shared and never truncated.
    await conn.execute("UPDATE rooms SET is_active = false WHERE is_active = true")
    room_count = worker_module.MAX_NEW_CLAIMS_PER_POLL + 5
    room_ids = [
        await create_room(conn, stake=Decimal("10.00"), min_players=2, is_active=True)
        for _ in range(room_count)
    ]
    worker = EngineWorker(pool, redis, worker_id="test-worker-cap")
    await worker.start()
    try:
        await worker.run_active_rooms()
        assert len(worker._tasks) == worker_module.MAX_NEW_CLAIMS_PER_POLL  # noqa: SLF001

        # The remainder aren't lost -- the next poll (30s later in real
        # production) picks up exactly the ones left over.
        await worker.run_active_rooms()
        assert len(worker._tasks) == room_count  # noqa: SLF001
        assert set(worker._tasks) == set(room_ids)  # noqa: SLF001
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


async def test_run_active_rooms_recovers_a_room_that_dies_mid_session(pool, redis, conn):
    # The real gap a code-review pass caught: recover_orphaned_rounds()
    # was only ever wired to run once, at worker.start() (see the test
    # above). A room whose live engine task dies *mid-session* -- any
    # unhandled exception, packages/core/db_pool.py's new bounded
    # pool.acquire() timeout under sustained load being one concrete new
    # way that can now happen where it previously would have just hung
    # instead -- releases its room lock in RoundEngine.run_forever()'s own
    # finally block, then gets silently reclaimed by this exact
    # run_active_rooms() poll with a brand-new RoundEngine whose __init__
    # hardcodes self._status = "idle" with no DB read at all. Without
    # recovery running here too, the old round's real player stakes would
    # sit orphaned in pot_escrow with no refund until the entire process
    # eventually restarts. This proves the fix: one run_active_rooms()
    # call both refunds the abandoned round AND reclaims the room with a
    # working fresh engine, no restart needed.
    #
    # Real regression caught by this exact test after MAX_NEW_CLAIMS_PER_
    # POLL was introduced (see worker.py): run_active_rooms() now caps how
    # many *new* rooms it claims per call, and this shared, never-
    # truncated-between-runs dev database can easily have 50+ other
    # is_active=true rooms left behind by other tests within the same
    # full-suite run -- enough to exhaust that cap before ever reaching
    # this test's own room, since the underlying query has no ORDER BY.
    # Same defensive cleanup as test_run_active_rooms_is_safe_to_call_
    # repeatedly and test_run_active_rooms_caps_new_claims_per_poll above.
    await conn.execute("UPDATE rooms SET is_active = false WHERE is_active = true")
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=50, is_active=True
    )
    worker = EngineWorker(pool, redis, worker_id="test-worker-midsession")
    await worker.start()
    try:
        first_engine = await worker.claim_room(room_id)
        await wait_until(lambda: first_engine.is_lock_held(), timeout=5)

        p1 = await create_funded_user(conn, Decimal("50.00"))
        p2 = await create_funded_user(conn, Decimal("50.00"))
        assert (await first_engine.join(p1, 1)).ok
        assert (await first_engine.join(p2, 2)).ok
        await wait_until(lambda: first_engine.status == "running", timeout=5)
        round_id = first_engine.round_id
        assert round_id is not None

        # Simulate the real failure mode directly: the engine task dies
        # unexpectedly (any uncaught exception, including a pool.acquire()
        # timeout) rather than a graceful stop() -- cancel it and delete
        # the lock key, the same crash-simulation technique test_recovery
        # .py's own tests already establish, reproducing the state a real
        # crash leaves behind regardless of which code path caused it.
        worker._tasks[room_id].cancel()  # noqa: SLF001
        with contextlib.suppress(asyncio.CancelledError):
            await worker._tasks[room_id]  # noqa: SLF001
        await redis.delete(f"room:lock:{room_id}")

        stuck = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert stuck["status"] == "running"

        await worker.run_active_rooms()

        voided = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert voided["status"] == "voided"
        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")

        # The room itself is working again -- reclaimed with a fresh
        # engine in the very same call, no restart required.
        second_engine = worker.engine_for(room_id)
        assert second_engine is not first_engine
        await wait_until(lambda: second_engine.is_lock_held(), timeout=5)
    finally:
        await worker.shutdown()
