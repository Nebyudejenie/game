"""Crash-safety: kill an engine mid-round, restart, and prove nobody's
money is lost. This is the scenario the spec calls out explicitly --
"Losing a round is acceptable; losing money is not."
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core import ledger
from services.engine import recovery
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_crash_mid_round_then_recovery_refunds_everyone(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=50)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    p1 = await create_funded_user(conn, Decimal("100.00"))
    p2 = await create_funded_user(conn, Decimal("100.00"))
    assert (await engine.join(p1, 1)).ok
    assert (await engine.join(p2, 2)).ok

    await wait_until(lambda: engine.status == "running", timeout=5)
    round_id = engine.round_id
    assert round_id is not None

    # Simulate a hard crash: the worker process is just gone, no graceful
    # shutdown code gets to run. Cancelling the task and then forcing the
    # lock key gone reproduces the state a real crash leaves behind,
    # regardless of whether our own cleanup code happens to run during
    # cancellation -- either way the round is stuck in 'running' with no
    # live owner, which is exactly what recovery.py has to detect.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await redis.delete(f"room:lock:{room_id}")

    stuck = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
    assert stuck["status"] == "running"

    recovered = await recovery.recover_orphaned_rounds(pool, redis)
    assert round_id in recovered

    voided = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
    assert voided["status"] == "voided"

    cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
    cash2 = await ledger.get_or_create_account(conn, p2, "user_cash")
    assert await ledger.balance(conn, cash1.id) == Decimal("100.00")
    assert await ledger.balance(conn, cash2.id) == Decimal("100.00")

    mismatches = await ledger.reconcile(conn)
    assert mismatches == []

    # Idempotency: recovering an already-voided round must be a no-op, not
    # a second refund.
    recovered_again = await recovery.recover_orphaned_rounds(pool, redis)
    assert round_id not in recovered_again
    assert await ledger.balance(conn, cash1.id) == Decimal("100.00")


async def test_recovery_leaves_a_still_owned_room_alone(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=50)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        await engine.join(p1, 1)
        await engine.join(p2, 2)
        await wait_until(lambda: engine.status == "running", timeout=5)
        round_id = engine.round_id

        # This engine is alive and still refreshing its lock -- recovery
        # must not touch its round out from under it.
        recovered = await recovery.recover_orphaned_rounds(pool, redis)
        assert round_id not in recovered

        still_running = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert still_running["status"] == "running"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)
