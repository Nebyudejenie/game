"""Real-Postgres/real-Redis tests for services/bot/perf.py's DB and Redis
timing instrumentation: asyncpg's own Connection.add_query_logger hook and
a Redis.execute_command() override, both attributing elapsed time to
whichever command name is set on perf._current_command at the moment the
query/command actually runs -- exactly the mechanism services/bot/app.py
wires into the bot's own pool/redis clients, exercised here directly
against the real test database/Redis rather than mocked.
"""

import asyncio

from packages.core import metrics
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.redis_conn import get_redis
from services.bot import perf


async def test_a_query_made_while_a_command_is_set_is_attributed_to_that_handler():
    settings = get_settings()
    pool = await create_pool(
        dsn=settings.database_url, min_size=1, max_size=1, init=perf.init_connection_for_query_timing
    )
    try:
        before = metrics.telegram_db_duration_seconds.labels(handler="test_db_attribution")._sum.get()
        token = perf._current_command.set("test_db_attribution")  # noqa: SLF001
        try:
            await pool.fetchval("SELECT 1")
        finally:
            perf._current_command.reset(token)  # noqa: SLF001

        # add_query_logger's own callback is scheduled via loop.call_soon(),
        # not run synchronously inside fetchval() -- a bare sleep(0) yields
        # control back to the loop long enough for that one already-queued
        # callback to actually execute before the assertion below.
        await asyncio.sleep(0)

        after = metrics.telegram_db_duration_seconds.labels(handler="test_db_attribution")._sum.get()
        assert after > before, "the real query's elapsed time was never recorded"
    finally:
        await pool.close()


async def test_a_query_made_with_no_command_set_is_not_attributed_to_any_handler():
    settings = get_settings()
    pool = await create_pool(
        dsn=settings.database_url, min_size=1, max_size=1, init=perf.init_connection_for_query_timing
    )
    try:
        assert perf._current_command.get() is None  # noqa: SLF001 -- the default outside any handler

        before = metrics.telegram_db_duration_seconds.labels(handler="unattributed_probe")._sum.get()
        await pool.fetchval("SELECT 1")
        await asyncio.sleep(0)
        after = metrics.telegram_db_duration_seconds.labels(handler="unattributed_probe")._sum.get()

        # A label that was never set() to is simply never observed --
        # _on_query()'s own early return for command is None, confirmed by
        # this staying exactly zero rather than picking up a stray value.
        assert after == before == 0.0
    finally:
        await pool.close()


async def test_a_redis_command_made_while_a_command_is_set_is_attributed_to_that_handler():
    redis = get_redis(redis_class=perf.TimedRedis)
    try:
        before = metrics.telegram_redis_duration_seconds.labels(handler="test_redis_attribution")._sum.get()
        token = perf._current_command.set("test_redis_attribution")  # noqa: SLF001
        try:
            await redis.ping()
        finally:
            perf._current_command.reset(token)  # noqa: SLF001

        after = metrics.telegram_redis_duration_seconds.labels(handler="test_redis_attribution")._sum.get()
        assert after > before, "the real Redis command's elapsed time was never recorded"
    finally:
        await redis.aclose()


async def test_a_redis_command_made_with_no_command_set_is_not_attributed_to_any_handler():
    redis = get_redis(redis_class=perf.TimedRedis)
    try:
        assert perf._current_command.get() is None  # noqa: SLF001

        before = metrics.telegram_redis_duration_seconds.labels(handler="unattributed_redis_probe")._sum.get()
        await redis.ping()
        after = metrics.telegram_redis_duration_seconds.labels(handler="unattributed_redis_probe")._sum.get()

        assert after == before == 0.0
    finally:
        await redis.aclose()
