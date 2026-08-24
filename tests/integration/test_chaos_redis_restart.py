"""Chaos: the actual Redis container goes away and comes back mid-round --
spec Prompt 10's "kill Redis" scenario, done for real rather than simulated,
since a mocked disconnect wouldn't prove anything about how the real
redis-py client and the real room-lock renewal loop behave under an actual
severed connection.

This restarts the shared docker-compose Redis container. It's reversible
(the container comes back with an empty, ready instance -- Redis here is
documented, deliberately, as holding no data anything depends on
surviving: packages/core/redis_conn.py's own docstring says "if Redis is
wiped, the platform must recover fully from Postgres"), scoped to this
machine's own dev stack, and this test file exists specifically to
exercise that guarantee for real.

Marked chaos_infra, not load: it breaks the shared session-scoped `redis`
fixture's underlying connection for every test that runs after it in the
same pytest process (confirmed -- an earlier version of this test file was
marked `load` and reliably broke test_load_rush.py's teardown when the two
ran in the same `-m load` batch, a real cross-test pollution bug, not
theoretical). Always run this file alone: `pytest -m chaos_infra`.
"""

import asyncio
import subprocess
import time
from decimal import Decimal
from pathlib import Path

import pytest

from packages.core import ledger
from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config
from services.engine.worker import EngineWorker
from tests.integration.conftest import create_funded_user, create_room

pytestmark = pytest.mark.chaos_infra

COMPOSE_FILE = Path(__file__).resolve().parent.parent.parent / "deploy" / "docker-compose.yml"


async def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _restart_redis_container() -> float:
    from redis.asyncio import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError

    from packages.core.config import get_settings

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "-f", str(COMPOSE_FILE), "restart", "redis",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode == 0

    # `docker compose restart` returning success only means the CLI command
    # finished, not that the container is accepting connections yet --
    # poll with a real PING until it genuinely is, the same discipline
    # every clean-slate rebuild this session uses for Postgres readiness.
    settings = get_settings()
    for _ in range(100):
        probe = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await probe.ping()
            await probe.aclose()
            break
        except RedisConnectionError:
            await probe.aclose()
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("redis did not become ready after restart")

    return time.monotonic() - started


async def test_redis_restart_mid_round_recovers_cleanly(pool, conn):
    from packages.core.redis_conn import get_redis

    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=2, max_players=10, lobby_seconds=5, call_interval_ms=200
    )
    room = await load_room_config(pool, room_id)
    card_pool = await load_card_pool(pool)

    engine_redis = get_redis()
    engine = RoundEngine(pool, engine_redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    players = [await create_funded_user(conn, Decimal("100.00")) for _ in range(5)]
    for i, user_id in enumerate(players):
        result = await engine.join(user_id, i + 1)
        assert result.ok, result.reason

    await wait_until(lambda: engine.status == "running", timeout=10)
    round_id = engine.round_id
    assert round_id is not None

    restart_seconds = await _restart_redis_container()
    print(f"\n[chaos redis-restart] container restart took {restart_seconds:.1f}s")

    # The engine's own connection is now broken (the container came back as
    # a brand new process) -- it can no longer renew its room lock or serve
    # commands. It should stop being the authority for this room, one way
    # or another, within a bounded time -- not spin forever pretending it
    # still owns a lock a fresh Redis has no memory of.
    await wait_until(lambda: task.done() or not engine.is_lock_held(), timeout=30)

    if not task.done():
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task
    await engine_redis.aclose()

    # Recovery: a fresh worker against the (now healthy again) Redis must
    # find this round non-terminal and safely void + refund it -- the same
    # guarantee a hard process kill already proves in
    # test_chaos_engine_crash.py, now proven across an actual Redis outage
    # in between instead of a clean-Redis engine crash.
    recovery_redis = get_redis()
    await recovery_redis.delete(f"room:lock:{room_id}")
    worker = EngineWorker(pool, recovery_redis, worker_id="chaos-redis-recovery-worker")
    recovered = await worker.start()
    try:
        row = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert row["status"] in ("voided", "done"), (
            f"round left in non-terminal status {row['status']!r} after Redis outage + recovery"
        )

        for user_id in players:
            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            assert await ledger.balance(conn, cash.id) == Decimal("100.00")

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await worker.shutdown()
        await recovery_redis.aclose()
