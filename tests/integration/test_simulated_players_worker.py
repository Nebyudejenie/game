"""services/engine/simulated_players_worker.py: the bot-runner's own
scheduling/joining decisions, layered on top of the real RoundEngine
command channel already proven correct by test_simulated_players.py's
own join()/claim() tests. Every test here drives a real RoundEngine
consuming real commands.send_command() traffic -- never a mocked engine
or a stubbed send_command.

Reuses bot_factory/_delete_bot_completely from test_simulated_players.py
(the exact same "shared, never-reset dev database has a real 10-bot cap"
cleanup discipline applies here too), matching this codebase's own
precedent of importing test helpers across integration test modules (see
test_emergency_room_stop.py importing _auth_headers from test_admin_app.py).
"""

import asyncio
from decimal import Decimal

import pytest

from services.admin import simulated_players_queries as spq
from services.engine.round_engine import RoundEngine, load_room_config
from services.engine.simulated_players_worker import SimulatedPlayersWorker
from tests.integration.conftest import create_room
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_simulated_players import bot_factory  # noqa: F401


async def _wait_for_round_status(pool, room_id: int, status: str, timeout: float = 10) -> None:
    async def _poll() -> None:
        while True:
            row = await pool.fetchrow(
                "SELECT status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
            )
            if row is not None and row["status"] == status:
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _set_deterministic_strategy(
    pool, *, admin_id: int, user_id: int, join_probability_pct: int,
    strategy: str = "active", pinned_room_id: int | None = None,
) -> None:
    # room_specific + pinned_room_id, whenever a test provides one, is what
    # keeps _choose_room's real list_rooms() scan from ever considering any
    # *other* is_active=true room in this suite's shared, never-truncated
    # dev database -- including ones an earlier test run's engine.stop()
    # left forever parked mid-lobby with no live engine left to actually
    # answer a send_command() against it.
    await spq.set_strategy_admin(
        pool, admin_id=admin_id, user_id=user_id, strategy=strategy,
        join_probability_pct=join_probability_pct, max_cards_per_join=1,
        schedule_mode="room_specific" if pinned_room_id is not None else "always_on",
        schedule_window_start_minute=None, schedule_window_end_minute=None,
        pinned_room_id=pinned_room_id,
        reason="test", ip_address=None,
    )


@pytest.fixture
async def globally_enabled(pool):
    admin_id, *_ = await create_test_admin(pool)
    await spq.update_settings_admin(
        pool, admin_id=admin_id, enabled=True, max_concurrent_bots=10, reason="test enable", ip_address=None
    )
    yield admin_id
    await spq.update_settings_admin(
        pool, admin_id=admin_id, enabled=False, max_concurrent_bots=10, reason="test cleanup", ip_address=None
    )


async def test_tick_is_a_full_noop_when_globally_disabled(pool, redis, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    bot_id = await bot_factory(admin_id)
    await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=bot_id, reason="t", ip_address=None)
    # simulated_players_settings.enabled is false by default -- this test
    # never touches the globally_enabled fixture, proving the shipped
    # default alone is enough to keep an idle-but-started bot inert.
    settings = await spq.get_settings_admin(pool)
    assert settings["enabled"] is False

    worker = SimulatedPlayersWorker(pool, redis)
    await worker.tick()

    row = await pool.fetchrow("SELECT status, current_room_id FROM simulated_players WHERE user_id = $1", bot_id)
    assert row["status"] == "idle"
    assert row["current_room_id"] is None


async def test_idle_bot_joins_an_open_lobby_via_a_real_tick(
    pool, redis, card_pool, conn, bot_factory, globally_enabled
):
    admin_id = globally_enabled
    bot_id = await bot_factory(admin_id)
    await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=bot_id, reason="t", ip_address=None)

    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=10,
        lobby_seconds=30, call_interval_ms=20, is_active=True,
    )
    await _set_deterministic_strategy(
        pool, admin_id=admin_id, user_id=bot_id, join_probability_pct=100, pinned_room_id=room_id
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        await _wait_for_round_status(pool, room_id, "lobby")

        worker = SimulatedPlayersWorker(pool, redis)
        await worker.tick()

        row = await pool.fetchrow(
            "SELECT status, current_room_id FROM simulated_players WHERE user_id = $1", bot_id
        )
        assert row["status"] == "playing"
        assert row["current_room_id"] == room_id

        entries = await pool.fetchval(
            "SELECT count(*) FROM round_entries re JOIN rounds r ON r.id = re.round_id "
            "WHERE r.room_id = $1 AND re.user_id = $2",
            room_id, bot_id,
        )
        assert entries == 1
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=5)
        await conn.execute("UPDATE rooms SET is_active = false WHERE id = $1", room_id)


async def test_bot_never_joins_when_probability_is_zero(
    pool, redis, card_pool, conn, bot_factory, globally_enabled
):
    admin_id = globally_enabled
    bot_id = await bot_factory(admin_id)
    await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=bot_id, reason="t", ip_address=None)

    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=10,
        lobby_seconds=30, call_interval_ms=20, is_active=True,
    )
    await _set_deterministic_strategy(
        pool, admin_id=admin_id, user_id=bot_id, join_probability_pct=0, pinned_room_id=room_id
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        await _wait_for_round_status(pool, room_id, "lobby")

        worker = SimulatedPlayersWorker(pool, redis)
        await worker.tick()

        row = await pool.fetchrow(
            "SELECT status, current_room_id FROM simulated_players WHERE user_id = $1", bot_id
        )
        assert row["status"] == "idle"
        assert row["current_room_id"] is None
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=5)
        await conn.execute("UPDATE rooms SET is_active = false WHERE id = $1", room_id)


async def test_paused_bot_is_never_touched_by_tick(pool, redis, conn, bot_factory, globally_enabled):
    admin_id = globally_enabled
    bot_id = await bot_factory(admin_id)
    await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=bot_id, reason="t", ip_address=None)
    await spq.pause_simulated_player_admin(pool, admin_id=admin_id, user_id=bot_id, reason="t", ip_address=None)

    worker = SimulatedPlayersWorker(pool, redis)
    await worker.tick()  # must not raise or touch a paused bot's row

    row = await pool.fetchrow("SELECT status, current_room_id FROM simulated_players WHERE user_id = $1", bot_id)
    assert row["status"] == "paused"
    assert row["current_room_id"] is None


async def test_bot_rejoins_the_next_round_lobby_at_its_own_room(
    pool, redis, card_pool, conn, bot_factory, globally_enabled
):
    # Rather than waiting out a full real round end-to-end (this room's
    # own real settlement timing), this drives the worker's continuation
    # branch directly against a hand-built "already playing, new lobby
    # just opened" state -- the exact state a real just-finished round
    # leaves a parked bot in.
    admin_id = globally_enabled
    bot_id = await bot_factory(admin_id)
    await _set_deterministic_strategy(pool, admin_id=admin_id, user_id=bot_id, join_probability_pct=100)

    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=10,
        lobby_seconds=30, call_interval_ms=20, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        await _wait_for_round_status(pool, room_id, "lobby")
        await pool.execute(
            "UPDATE simulated_players SET status = 'playing', current_room_id = $2 WHERE user_id = $1",
            bot_id, room_id,
        )

        worker = SimulatedPlayersWorker(pool, redis)
        await worker._continue_or_leave_room(bot_id, room_id, max_cards_per_join=1)

        entries = await pool.fetchval(
            "SELECT count(*) FROM round_entries re JOIN rounds r ON r.id = re.round_id "
            "WHERE r.room_id = $1 AND re.user_id = $2",
            room_id, bot_id,
        )
        assert entries == 1
        row = await pool.fetchrow("SELECT status FROM simulated_players WHERE user_id = $1", bot_id)
        assert row["status"] == "playing"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=5)
        await conn.execute("UPDATE rooms SET is_active = false WHERE id = $1", room_id)


async def test_bot_goes_idle_when_its_room_is_deactivated(pool, redis, conn, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    bot_id = await bot_factory(admin_id)
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, is_active=False)
    await pool.execute(
        "UPDATE simulated_players SET status = 'playing', current_room_id = $2 WHERE user_id = $1",
        bot_id, room_id,
    )

    worker = SimulatedPlayersWorker(pool, redis)
    await worker._continue_or_leave_room(bot_id, room_id, max_cards_per_join=1)

    row = await pool.fetchrow("SELECT status, current_room_id FROM simulated_players WHERE user_id = $1", bot_id)
    assert row["status"] == "idle"
    assert row["current_room_id"] is None


def test_pick_room_prefers_the_quietest_lobby_for_conservative():
    from services.engine.simulated_players_worker import _pick_room

    joinable = [
        {"room_id": 1, "players": 4, "max_players": 10},
        {"room_id": 2, "players": 0, "max_players": 10},
        {"room_id": 3, "players": 7, "max_players": 10},
    ]
    assert _pick_room(joinable, "quiet")["room_id"] == 2


def test_pick_room_prefers_the_fullest_lobby_for_active():
    from services.engine.simulated_players_worker import _pick_room

    joinable = [
        {"room_id": 1, "players": 4, "max_players": 10},
        {"room_id": 2, "players": 0, "max_players": 10},
        {"room_id": 3, "players": 7, "max_players": 10},
    ]
    assert _pick_room(joinable, "full")["room_id"] == 3


def test_pick_room_returns_none_with_no_candidates():
    from services.engine.simulated_players_worker import _pick_room

    assert _pick_room([], "quiet") is None
