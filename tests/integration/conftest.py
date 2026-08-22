import itertools
import random

import asyncpg
import pytest_asyncio

from packages.core.config import get_settings

# Every test that needs a fresh user picks the next id off this counter,
# seeded randomly so re-runs of the suite never collide with leftover rows
# from a previous run against the same database.
_telegram_id_counter = itertools.count(random.randint(10**9, 2 * 10**9))


def next_telegram_id() -> int:
    return next(_telegram_id_counter)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pool():
    settings = get_settings()
    p = await asyncpg.create_pool(dsn=settings.database_url, min_size=5, max_size=50)
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="session")
async def conn(pool):
    async with pool.acquire() as connection:
        yield connection


async def create_user(conn: asyncpg.Connection) -> int:
    telegram_id = next_telegram_id()
    row = await conn.fetchrow(
        """
        INSERT INTO users (telegram_id, display_name)
        VALUES ($1, $2)
        RETURNING id
        """,
        telegram_id,
        f"test-user-{telegram_id}",
    )
    return row["id"]
