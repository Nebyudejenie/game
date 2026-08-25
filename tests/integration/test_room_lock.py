"""Tests for services/engine/room_lock.py -- the single-owner election that
makes "exactly one engine writes a room's state" a real guarantee instead of
a hope.
"""

import asyncio

import pytest

from services.engine.room_lock import RoomLock


async def test_second_worker_cannot_acquire_while_first_holds(redis):
    room_id = 900001
    lock_a = RoomLock(redis, room_id, worker_id="worker-a")
    lock_b = RoomLock(redis, room_id, worker_id="worker-b")
    try:
        assert await lock_a.acquire() is True
        assert await lock_b.acquire() is False
    finally:
        await lock_a.release()
        await lock_b.release()


async def test_release_makes_the_room_available_again(redis):
    room_id = 900002
    lock_a = RoomLock(redis, room_id, worker_id="worker-a")
    lock_b = RoomLock(redis, room_id, worker_id="worker-b")
    try:
        assert await lock_a.acquire() is True
        await lock_a.release()
        assert await lock_b.acquire() is True
    finally:
        await lock_a.release()
        await lock_b.release()


async def test_a_worker_cannot_release_a_lock_it_does_not_own(redis):
    room_id = 900003
    lock_a = RoomLock(redis, room_id, worker_id="worker-a")
    lock_b = RoomLock(redis, room_id, worker_id="worker-b")
    try:
        assert await lock_a.acquire() is True
        # worker-b never held this lock -- release() must be a safe no-op,
        # not something that deletes worker-a's active lock out from
        # under it.
        await lock_b.release()
        assert await redis.get(lock_a.key) == "worker-a"
    finally:
        await lock_a.release()


async def test_refresh_loop_keeps_the_lock_alive_past_its_original_ttl(redis):
    room_id = 900004
    lock_a = RoomLock(redis, room_id, worker_id="worker-a", ttl_seconds=1, refresh_interval_seconds=0.3)
    try:
        assert await lock_a.acquire() is True
        await asyncio.sleep(1.5)  # would have expired by now without a refresh
        assert lock_a.is_held() is True
        assert await redis.get(lock_a.key) == "worker-a"
    finally:
        await lock_a.release()


async def test_lock_expires_and_becomes_available_if_never_refreshed(redis):
    room_id = 900005
    # ttl shorter than the refresh interval simulates a crashed worker:
    # acquire() fires once, the refresh loop's first sleep never completes
    # before the ttl itself lapses.
    lock_a = RoomLock(redis, room_id, worker_id="worker-a", ttl_seconds=1, refresh_interval_seconds=10)
    lock_b = RoomLock(redis, room_id, worker_id="worker-b")
    try:
        assert await lock_a.acquire() is True
        await asyncio.sleep(1.5)
        assert await redis.get(lock_a.key) is None
        assert await lock_b.acquire() is True
    finally:
        await lock_a.release()
        await lock_b.release()


async def test_a_redis_error_during_refresh_relinquishes_ownership_rather_than_sticking(
    redis, monkeypatch
):
    # Regression: a real code review pass caught a genuine split-brain
    # risk -- an unhandled Redis error during refresh (a transient blip,
    # not even a full outage) used to kill the refresh task *before*
    # self._held = False ran, so is_held() reported True forever, even
    # after the real Redis TTL key expired on schedule and a second
    # engine legitimately acquired the same room. Confirmed directly here
    # rather than assumed: a real exception raised from the actual
    # eval() call this lock depends on, not a hypothetical.
    room_id = 900006
    lock_a = RoomLock(redis, room_id, worker_id="worker-a", ttl_seconds=5, refresh_interval_seconds=0.2)
    try:
        assert await lock_a.acquire() is True

        real_eval = redis.eval
        call_count = 0

        async def flaky_eval(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("simulated Redis blip")
            return await real_eval(*args, **kwargs)

        monkeypatch.setattr(redis, "eval", flaky_eval)

        await asyncio.sleep(0.4)  # >= one refresh interval, hits the flaky eval

        assert lock_a.is_held() is False
    finally:
        await lock_a.release()


async def test_a_redis_error_during_release_still_clears_held(redis, monkeypatch):
    room_id = 900007
    lock_a = RoomLock(redis, room_id, worker_id="worker-a")
    assert await lock_a.acquire() is True

    async def failing_eval(*args, **kwargs):
        raise ConnectionError("simulated Redis blip")

    monkeypatch.setattr(redis, "eval", failing_eval)

    with pytest.raises(ConnectionError):
        await lock_a.release()
    # Even though the DEL itself failed, is_held() must not stay stuck
    # True -- we're releasing either way, and the real key will still
    # expire via its own TTL regardless.
    assert lock_a.is_held() is False
