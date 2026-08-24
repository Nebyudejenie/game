"""Chaos: kill an engine process mid-round with real money already staked
by many real players, and prove services/engine/recovery.py refunds every
single one of them exactly, with nothing lost and nothing double-paid.

test_worker.py already proves this mechanism works with 2 players; this is
the same mechanism under real concurrent load -- the crash-recovery path
exercised with enough simultaneous stakes that any subtle bug in "refund
everyone exactly once" (a missed row, a double ledger entry, an entry that
silently failed) would show up as a wrong total, not just a wrong count.
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core import ledger
from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config
from services.engine.worker import EngineWorker
from tests.integration.conftest import create_funded_user, create_room

pytestmark = pytest.mark.load

PLAYERS = 80
STAKE = Decimal("25.00")


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_crashed_engine_with_80_staked_players_refunds_every_single_one(pool, redis, conn):
    room_id = await create_room(
        conn, stake=STAKE, min_players=2, max_players=PLAYERS, lobby_seconds=10, call_interval_ms=200
    )
    room = await load_room_config(pool, room_id)
    card_pool = await load_card_pool(pool)
    doomed_engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(doomed_engine.run_forever())

    players = [await create_funded_user(conn, Decimal("1000.00")) for _ in range(PLAYERS)]
    results = await asyncio.gather(
        *(doomed_engine.join(user_id, i + 1) for i, user_id in enumerate(players))
    )
    assert all(r.ok for r in results), [r for r in results if not r.ok]

    await wait_until(lambda: doomed_engine.status == "running", timeout=20)
    round_id = doomed_engine.round_id
    assert round_id is not None

    pot_before_crash = await pool.fetchval("SELECT pot FROM rounds WHERE id = $1", round_id)
    assert pot_before_crash == STAKE * PLAYERS

    # The crash: no graceful shutdown, no settlement, task just dies --
    # and the room lock is never released the way a clean stop() would,
    # so a fresh worker sees it as stale/gone the same way it would after
    # a real process kill.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await redis.delete(f"room:lock:{room_id}")

    worker = EngineWorker(pool, redis, worker_id="chaos-recovery-worker")
    recovered = await worker.start()
    try:
        assert round_id in recovered

        row = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert row["status"] == "voided"

        balances = []
        for user_id in players:
            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            balances.append(await ledger.balance(conn, cash.id))

        assert all(b == Decimal("1000.00") for b in balances), (
            f"expected all {PLAYERS} players refunded to exactly 1000.00; "
            f"got {sum(1 for b in balances if b != Decimal('1000.00'))} wrong balances"
        )

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await worker.shutdown()
