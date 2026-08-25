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
        self._last_refreshed_at = 0.0

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
            self._last_refreshed_at = asyncio.get_running_loop().time()
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
                # owned it.
                #
                # A second code review pass caught that the fix above then
                # over-corrected: relinquishing on the *very first* failed
                # eval treated one recoverable blip (a single bad
                # round-trip, well within the TTL) identically to a real
                # outage. A failed eval doesn't touch the Redis key itself
                # (only a DEL removes it), so this worker still
                # legitimately owns the room right up until the key's own
                # TTL -- but relinquishing early made it voluntarily
                # abandon a room it still owns, and because the key was
                # never deleted, no *other* engine could take over either
                # until that orphaned key expired on its own. Every player
                # in the room would stall for up to ttl_seconds over a
                # blip that would have cleared by the very next scheduled
                # refresh.
                #
                # Retrying is only safe while we can still be certain the
                # real Redis-side TTL hasn't lapsed: self._last_refreshed_at
                # is the last point ownership was actually confirmed and
                # that TTL reset, so elapsed time since then is an exact
                # lower bound on how much of it is left. A margin of one
                # full refresh interval below ttl_seconds keeps this
                # conservative -- by the time this gives up, the real key
                # (barring clock skew) still has that much time left,
                # never claiming ownership past a point Redis itself might
                # already disagree with.
                elapsed = asyncio.get_running_loop().time() - self._last_refreshed_at
                retry_margin = self._ttl_seconds - self._refresh_interval_seconds
                if elapsed < retry_margin:
                    logger.warning(
                        "room_lock_refresh_failed_retrying",
                        room_id=self._room_id,
                        elapsed_seconds=elapsed,
                        exc_info=True,
                    )
                    continue
                logger.warning("room_lock_refresh_failed", room_id=self._room_id, exc_info=True)
                self._held = False
                return
            if not refreshed:
                self._held = False
                return
            self._last_refreshed_at = asyncio.get_running_loop().time()

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
