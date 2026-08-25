"""Single-owner election for a room, via Redis.

Exactly one Game Engine process owns any given room at any time (spec
section 2.3) -- that single owner is the only writer of room state, which is
what lets the whole game loop run lock-free in memory instead of hitting
Postgres on every tick. Ownership is a Redis key with a TTL, refreshed
periodically; a worker that stops refreshing (crash, network partition) loses
ownership automatically within LOCK_TTL_SECONDS, and refresh/release both use
compare-and-clear Lua scripts so a worker can never refresh or release a lock
it no longer actually owns (classic Redlock safety property).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger()

LOCK_TTL_SECONDS = 15
REFRESH_INTERVAL_SECONDS = 5

# Both scripts only act if the stored value still matches our worker_id --
# otherwise someone else already claimed this room after our lock expired,
# and touching their lock (extending or deleting it) would be a correctness
# bug, not just a nicety.
_REFRESH_IF_OWNER = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""

_DELETE_IF_OWNER = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class RoomLock:
    def __init__(
        self,
        redis: Redis,
        room_id: int,
        worker_id: str | None = None,
        *,
        ttl_seconds: int = LOCK_TTL_SECONDS,
        refresh_interval_seconds: float = REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._redis = redis
        self._room_id = room_id
        self._worker_id = worker_id or str(uuid.uuid4())
        self._key = f"room:lock:{room_id}"
        self._ttl_seconds = ttl_seconds
        self._refresh_interval_seconds = refresh_interval_seconds
        self._refresh_task: asyncio.Task[None] | None = None
        self._held = False

    @property
    def key(self) -> str:
        return self._key

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def is_held(self) -> bool:
        """Best-effort local view -- true until we know we've lost the lock
        (never refreshed, or a refresh found someone else owns it). Callers
        on a hot path should trust this rather than round-tripping to Redis
        on every check; it's updated at least every REFRESH_INTERVAL_SECONDS.
        """
        return self._held

    async def acquire(self) -> bool:
        ok = await self._redis.set(
            self._key, self._worker_id, nx=True, ex=self._ttl_seconds
        )
        self._held = bool(ok)
        if self._held:
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        return self._held

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval_seconds)
            try:
                refreshed = await self._redis.eval(
                    _REFRESH_IF_OWNER, 1, self._key, self._worker_id, self._ttl_seconds
                )
            except Exception:
                # A code review pass caught a real split-brain risk here:
                # an unhandled Redis error (a transient network blip, not
                # even a full outage) used to kill this task *before*
                # self._held = False ran, so is_held() reported True
                # forever -- even after the real Redis TTL key expired on
                # schedule and a second engine legitimately acquired the
                # same room, both engines would then believe they alone
                # owned it. Treated identically to "someone else already
                # holds this lock": relinquish immediately. This module's
                # own docstring already frames losing the lock as the
                # *safe* outcome of a refresh failure -- a real Redis
                # error is just one more reason refreshing can fail, not
                # a special case that should leave ownership ambiguous.
                logger.warning("room_lock_refresh_failed", room_id=self._room_id, exc_info=True)
                self._held = False
                return
            if not refreshed:
                self._held = False
                return

    async def release(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        try:
            await self._redis.eval(_DELETE_IF_OWNER, 1, self._key, self._worker_id)
        finally:
            # Same reasoning as _refresh_loop: a Redis error deleting the
            # key must not leave self._held stuck True -- we're releasing
            # either way, and the real key will still expire via its own
            # TTL even if this DEL never lands.
            self._held = False
