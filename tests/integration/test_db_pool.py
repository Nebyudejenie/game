"""Real proof that a genuinely exhausted Postgres pool fails fast instead
of hanging forever -- packages/core/db_pool.py's own fix for the class of
bug packages/core/redis_conn.py already fixed for Redis (asyncpg's
Pool.acquire(timeout=None) waits forever by default, and create_pool()
has no way to set a pool-level default). A real, tiny (max_size=1) pool
against the actual dev-compose Postgres, not a mock -- the same
"saturate a small pool with held connections, assert a bounded failure"
technique the audit finding behind this fix itself proposed.
"""

import time

import asyncpg
import pytest

from packages.core import db_pool
from packages.core.config import get_settings


async def test_exhausted_pool_acquire_fails_fast_by_default(monkeypatch):
    # A short synthetic timeout (not the real 10s default) keeps this
    # test itself fast while proving the exact mechanism under test: a
    # caller that passes no explicit timeout still gets one applied,
    # rather than waiting forever.
    monkeypatch.setattr(db_pool, "ACQUIRE_TIMEOUT_SECONDS", 1.0)
    settings = get_settings()
    pool = await db_pool.create_pool(dsn=settings.database_url, min_size=1, max_size=1)
    try:
        async with pool.acquire():
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                async with pool.acquire():
                    pass
            elapsed = time.monotonic() - start
            # A real ceiling, not "it eventually raised something" --
            # bounded close to the configured timeout, nowhere near a hang.
            assert elapsed < 5.0
    finally:
        await pool.close()


async def test_an_explicit_timeout_still_overrides_the_default(monkeypatch):
    # A long default (would make this test itself slow if it were ever
    # actually honored) proves the caller's own explicit timeout wins,
    # not just that *some* default exists.
    monkeypatch.setattr(db_pool, "ACQUIRE_TIMEOUT_SECONDS", 30.0)
    settings = get_settings()
    pool = await db_pool.create_pool(dsn=settings.database_url, min_size=1, max_size=1)
    try:
        async with pool.acquire():
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                async with pool.acquire(timeout=0.5):
                    pass
            elapsed = time.monotonic() - start
            assert elapsed < 2.0
    finally:
        await pool.close()


async def test_pool_fetch_and_fetchval_inherit_the_same_default_timeout(monkeypatch):
    # Pool.fetch()/fetchval()/etc. call self.acquire() internally without
    # the caller ever touching acquire() directly -- confirms the fix
    # protects those call sites too, not just explicit `async with
    # pool.acquire()` blocks.
    monkeypatch.setattr(db_pool, "ACQUIRE_TIMEOUT_SECONDS", 1.0)
    settings = get_settings()
    pool = await db_pool.create_pool(dsn=settings.database_url, min_size=1, max_size=1)
    try:
        async with pool.acquire():
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                await pool.fetchval("SELECT 1")
            elapsed = time.monotonic() - start
            assert elapsed < 5.0
    finally:
        await pool.close()


async def test_pool_created_via_db_pool_is_a_real_asyncpg_pool_instance():
    # Every existing `pool: asyncpg.Pool` type annotation across the
    # codebase must keep working unchanged -- this is a genuine Pool
    # subclass, not a duck-typed facade (class reassignment and instance
    # monkeypatching were both real dead ends -- asyncpg.pool.Pool uses
    # __slots__ with no __dict__ -- see db_pool.py's own module docstring).
    settings = get_settings()
    pool = await db_pool.create_pool(dsn=settings.database_url, min_size=1, max_size=2)
    try:
        assert isinstance(pool, asyncpg.Pool)
        assert await pool.fetchval("SELECT 1") == 1
    finally:
        await pool.close()
