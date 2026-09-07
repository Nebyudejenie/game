"""Real system health checks (services/admin/system_health.py) -- every
check is a genuine live probe, verified here by breaking each dependency
for real (a bad DSN, a wrong port) rather than mocking a status string.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis

from services.admin import system_health
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import create_room, next_telegram_id
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin


async def test_database_check_is_green_against_the_real_pool(pool):
    result = await system_health.check_database(pool)
    assert result.status == "green"


async def test_database_check_is_red_against_a_broken_connection():
    bad_pool = await asyncpg.create_pool(
        dsn="postgresql://jobingo:jobingo@127.0.0.1:1/jobingo", min_size=0, max_size=1
    )
    try:
        result = await system_health.check_database(bad_pool)
        assert result.status == "red"
        assert "SELECT 1 failed" in result.why
    finally:
        await bad_pool.close()


async def test_redis_check_is_green_against_the_real_client(redis):
    result = await system_health.check_redis(redis)
    assert result.status == "green"


async def test_redis_check_is_red_against_an_unreachable_client():
    bad_redis = Redis(host="127.0.0.1", port=1, socket_connect_timeout=1)
    try:
        result = await system_health.check_redis(bad_redis)
        assert result.status == "red"
    finally:
        await bad_redis.aclose()


async def test_telegram_check_is_unknown_when_no_token_configured():
    result = await system_health.check_telegram("")
    assert result.status == "unknown"


async def test_telegram_check_is_green_with_a_healthy_fake_webhook(monkeypatch):
    from aiogram import Bot
    from aiogram.types import WebhookInfo

    async def fake_get_webhook_info(self: Bot) -> WebhookInfo:
        return WebhookInfo(
            url="https://bot.test/webhook", has_custom_certificate=False, pending_update_count=0,
            ip_address=None, last_error_date=None, last_error_message=None,
            last_synchronization_error_date=None, max_connections=40, allowed_updates=None,
        )

    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)
    result = await system_health.check_telegram("123456:FAKE-TEST-TOKEN")
    assert result.status == "green"


async def test_telegram_check_is_red_with_a_large_pending_backlog(monkeypatch):
    from aiogram import Bot
    from aiogram.types import WebhookInfo

    async def fake_get_webhook_info(self: Bot) -> WebhookInfo:
        return WebhookInfo(
            url="https://bot.test/webhook", has_custom_certificate=False, pending_update_count=999,
            ip_address=None, last_error_date=None, last_error_message=None,
            last_synchronization_error_date=None, max_connections=40, allowed_updates=None,
        )

    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)
    result = await system_health.check_telegram("123456:FAKE-TEST-TOKEN")
    assert result.status == "red"


async def test_bingo_check_matches_a_real_stuck_round_count(pool):
    # Not asserting "green" unconditionally -- this exact check (run
    # directly against this shared, long-lived dev database while
    # writing it) found 6 real rounds abandoned in 'lobby' status by past
    # test runs (started_at IS NULL, never actually started), which is
    # the check correctly doing its job, not a bug in the check or a
    # flaky test. Verifying the check's own query is internally
    # consistent -- reporting "green" if and only if a direct,
    # independent count of the same condition is also zero -- is what's
    # actually true regardless of this database's current real state.
    from datetime import timedelta

    from services.engine.recovery import NON_TERMINAL_STATUSES

    real_count = await pool.fetchval(
        """
        SELECT count(*) FROM rounds r
        WHERE r.status = ANY($1::text[])
          AND COALESCE(r.started_at, now() - interval '1 hour') < now() - $2::interval
        """,
        list(NON_TERMINAL_STATUSES),
        timedelta(minutes=30),
    )
    result = await system_health.check_bingo(pool)
    if real_count == 0:
        assert result.status == "green"
    else:
        assert result.status == "red"
        assert str(real_count) in result.why or "round #" in result.why


async def test_bingo_check_is_red_with_a_real_stuck_round(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=200)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        from tests.integration.conftest import create_funded_user

        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok

        async def wait_until_running() -> None:
            for _ in range(50):
                if engine.status == "running":
                    return
                await asyncio.sleep(0.1)
            raise AssertionError("round never reached running")

        await wait_until_running()
        round_id = engine.round_id
        assert round_id is not None

        # Backdate started_at past the threshold -- a real, durable DB
        # state, not a mocked "stuck" flag -- to prove the check's own
        # query actually finds it.
        await conn.execute(
            "UPDATE rounds SET started_at = now() - $1::interval WHERE id = $2",
            timedelta(minutes=31),
            round_id,
        )

        result = await system_health.check_bingo(pool)
        assert result.status == "red"
        assert str(round_id) in result.why or "round #" in result.why
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


# --- endpoint, real HTTP -------------------------------------------------


async def test_system_health_endpoint_returns_all_checks_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/system-health", headers=headers)
    assert response.status_code == 200
    names = {c["name"] for c in response.json()}
    assert names == {"database", "redis", "telegram", "bingo"}


async def test_system_health_endpoint_requires_authentication(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/system-health")
    assert response.status_code in (401, 403)
