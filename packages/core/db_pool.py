"""Shared Postgres pool factory.

Mirrors packages/core/redis_conn.py's own fix for the identical class of
bug: asyncpg's own `Pool.acquire(timeout=None)` waits forever by default,
and `asyncpg.create_pool()` has no way to set a *default* acquire timeout
-- `timeout` is a per-call argument, not a pool-level setting. A genuinely
exhausted pool (every connection checked out, under sustained load or a
slow-query pile-up) would hang every subsequent caller indefinitely
instead of failing fast: the same "a hang isn't graceful degradation, it's
an outage this client itself manufactures" problem redis_conn.py already
documents and fixes for Redis.

The fix here is a thin `Pool` subclass overriding just `acquire()` to
supply a default timeout when the caller doesn't pass their own.
`Pool.fetch()`/`fetchval()`/`fetchrow()`/`execute()`/`executemany()` all
call `self.acquire()` internally (confirmed by reading this project's
installed asyncpg version's own pool.py, not assumed) -- so overriding
just this one method protects every call site in the codebase, both the
many places that do `async with pool.acquire() as conn:` directly and the
many more that call `pool.fetch()`/`fetchval()`/etc. without an explicit
acquire(), with zero changes needed at any of them.

Two things ruled out first, both real dead ends (`asyncpg.pool.Pool` uses
`__slots__` with no `__dict__`): reassigning `pool.__class__` on an
already-constructed vanilla Pool raises `TypeError: object layout
differs`, and binding a replacement method directly onto an instance
raises `AttributeError` for the same reason. So this constructs the
subclass directly rather than wrapping `asyncpg.create_pool()`'s own
return value -- which means duplicating the handful of `Pool.__init__`
defaults `create_pool()` itself normally supplies (nothing this codebase's
own call sites use beyond `dsn`/`min_size`/`max_size` today).
"""

from __future__ import annotations

import asyncpg

# Matches packages/core/redis_conn.py's own SOCKET_TIMEOUT_SECONDS -- same
# order of magnitude, same reasoning: long enough that a real acquire
# under brief, ordinary load never trips it (a healthy pool hands out a
# free connection in microseconds regardless of this ceiling), short
# enough that a genuinely exhausted pool fails in seconds, not never.
ACQUIRE_TIMEOUT_SECONDS = 10.0


class _BoundedPool(asyncpg.Pool):
    def acquire(self, *, timeout: float | None = None) -> asyncpg.pool.PoolAcquireContext:
        return super().acquire(timeout=ACQUIRE_TIMEOUT_SECONDS if timeout is None else timeout)


async def create_pool(dsn: str, *, min_size: int, max_size: int) -> asyncpg.Pool:
    pool = _BoundedPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        max_queries=50000,
        max_inactive_connection_lifetime=300.0,
        connect=None,
        setup=None,
        init=None,
        reset=None,
        loop=None,
        connection_class=asyncpg.Connection,
        record_class=asyncpg.Record,
    )
    await pool
    return pool
