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


async def test_stuck_round_is_still_recovered_after_the_room_gets_a_newer_round(
    pool, redis, card_pool, conn
):
    # Regression: a real code review pass caught that the original check
    # ("is the room's lock held by *anyone*?") treated a live engine
    # owning the room's *current* round as proof the *old, stuck* round
    # was also still owned. A room only ever runs one round at a time --
    # once a newer round exists for the same room, any older non-terminal
    # round is unambiguously abandoned, regardless of the room's current
    # lock state. Simulates the real scenario: round 1 crashes (lock
    # deleted, exactly like the test above), then a second, genuinely
    # live engine claims the same room and starts round 2 -- the room's
    # lock is legitimately held again, but by round 2, not round 1.
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=50)
    room = await load_room_config(pool, room_id)

    engine1 = RoundEngine(pool, redis, room, card_pool)
    task1 = asyncio.create_task(engine1.run_forever())
    p1 = await create_funded_user(conn, Decimal("100.00"))
    p2 = await create_funded_user(conn, Decimal("100.00"))
    assert (await engine1.join(p1, 1)).ok
    assert (await engine1.join(p2, 2)).ok
    await wait_until(lambda: engine1.status == "running", timeout=5)
    stuck_round_id = engine1.round_id
    assert stuck_round_id is not None

    # Simulate engine1's hard crash, same technique as the test above.
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1
    await redis.delete(f"room:lock:{room_id}")

    # A second, genuinely live engine claims the same room and starts a
    # fresh round -- the room's lock is now legitimately held again, but
    # for round 2, not the stuck round 1.
    engine2 = RoundEngine(pool, redis, room, card_pool)
    task2 = asyncio.create_task(engine2.run_forever())
    try:
        p3 = await create_funded_user(conn, Decimal("100.00"))
        p4 = await create_funded_user(conn, Decimal("100.00"))
        assert (await engine2.join(p3, 3)).ok
        assert (await engine2.join(p4, 4)).ok
        await wait_until(lambda: engine2.status == "running", timeout=5)
        live_round_id = engine2.round_id
        assert live_round_id is not None
        assert live_round_id != stuck_round_id

        stuck_before = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", stuck_round_id)
        assert stuck_before["status"] == "running"  # still stuck, not yet recovered

        recovered = await recovery.recover_orphaned_rounds(pool, redis)
        # The actual bug, made concrete: without the fix, stuck_round_id
        # is silently skipped forever here because *a* lock is held for
        # the room -- just not for this round.
        assert stuck_round_id in recovered
        assert live_round_id not in recovered  # engine2's real round must be left alone

        stuck_after = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", stuck_round_id)
        assert stuck_after["status"] == "voided"
        live_after = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", live_round_id)
        assert live_after["status"] == "running"

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        cash2 = await ledger.get_or_create_account(conn, p2, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("100.00")
        assert await ledger.balance(conn, cash2.id) == Decimal("100.00")

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine2.stop()
        await asyncio.wait_for(task2, timeout=15)


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
