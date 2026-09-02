"""Tests for services/engine/commands.py -- the Redis Stream RPC layer that
lets a gateway process reach whichever engine process owns a room.
"""

import asyncio
from decimal import Decimal

import pytest

from services.engine import commands
from services.engine.commands import CommandTimeout
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_join_command_reaches_the_owning_engine(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        await wait_until(lambda: engine.is_lock_held(), timeout=5)
        p1 = await create_funded_user(conn)

        result = await commands.send_command(redis, room_id, "join", p1, {"card_no": 5})
        assert result.ok is True
        assert engine.player_count() == 1
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_claim_set_auto_and_drop_card_commands(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=5)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)

        join_result = await commands.send_command(
            redis, room_id, "join", p1, {"card_no": 3, "auto_mark": False}
        )
        assert join_result.ok

        auto_result = await commands.send_command(redis, room_id, "set_auto", p1, {"auto": True})
        assert auto_result.ok

        row = await pool.fetchrow(
            "SELECT auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
            engine.round_id,
            p1,
        )
        assert row["auto_mark"] is True

        # Round is still in LOBBY, not RUNNING -- claim must be cleanly
        # rejected, not silently accepted or left hanging.
        claim_result = await commands.send_command(
            redis, room_id, "claim", p1, {"card_no": 3}
        )
        assert claim_result.ok is False
        assert claim_result.reason == "round_not_running"

        drop_result = await commands.send_command(
            redis, room_id, "drop_card", p1, {"card_no": 3}
        )
        assert drop_result.ok
        assert engine.player_count() == 0
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_command_to_a_room_with_no_live_owner_times_out(redis):
    with pytest.raises(CommandTimeout):
        await commands.send_command(redis, 999999, "join", 1, {"card_no": 1}, timeout=0.5)


async def test_unknown_action_rejected_cleanly(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        await wait_until(lambda: engine.is_lock_held(), timeout=5)
        result = await commands.send_command(redis, room_id, "self_destruct", 1)
        assert result.ok is False
        assert result.reason == "unknown_action"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_a_command_handler_exception_fails_only_that_command_not_the_room(
    pool, redis, card_pool, conn, monkeypatch
):
    # Regression: a real code review pass caught that _handle_command()
    # had no exception isolation at all -- a single unexpected exception
    # anywhere inside join()/drop_card()/claim()/set_auto() (a malformed
    # payload, an edge-case bug) would propagate straight out of
    # _serve_commands()'s loop and kill this room's single long-lived
    # command consumer permanently, with no restart: every subsequent
    # command for the room would silently time out for players while the
    # round itself kept running unattended. Simulates the exception
    # directly (deterministic) rather than hunting for a specific
    # malformed payload that happens to trigger one -- what's under test
    # is the isolation property itself, not which inputs are unsafe.
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        await wait_until(lambda: engine.is_lock_held(), timeout=5)
        p1 = await create_funded_user(conn)

        real_join = engine.join
        call_count = 0

        async def flaky_join(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated bug in join()")
            return await real_join(*args, **kwargs)

        monkeypatch.setattr(engine, "join", flaky_join)

        first = await commands.send_command(redis, room_id, "join", p1, {"card_no": 5})
        assert first.ok is False
        assert first.reason == "internal_error"

        # The room's command consumer must still be alive and correct --
        # a second, real join for a different player must succeed, not
        # time out.
        p2 = await create_funded_user(conn)
        second = await commands.send_command(redis, room_id, "join", p2, {"card_no": 6})
        assert second.ok is True
        assert engine.player_count() == 1  # only p2 -- p1's join genuinely failed
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)
