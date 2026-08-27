"""End-to-end gameplay over a real WebSocket connection: auth, room list,
join, take_card, and a full round settling -- proving the gateway, the
Redis command channel, and the engine all actually work together, not just
each piece in isolation.
"""

import asyncio
import json
from decimal import Decimal

import websockets

from packages.core import ledger
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import (
    build_init_data,
    create_funded_user,
    create_room,
    fund_user,
    next_telegram_id,
)


async def wait_until(predicate, timeout: float = 15.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def recv_until(ws, message_type: str, *, attempts: int = 50, timeout: float = 5.0) -> dict:
    """Reads frames until one with t == message_type shows up.

    A single WebSocket carries messages from two independent paths -- the
    direct command-channel reply and this connection's own room
    broadcasts (a player sees their own card_taken/balance events the same
    as everyone else in the room) -- so a client must dispatch by message
    type rather than assume a fixed reply immediately follows a request.
    """
    for _ in range(attempts):
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if message.get("t") == message_type:
            return message
    raise AssertionError(f"never saw a '{message_type}' message after {attempts} frames")


async def test_full_gameplay_over_websocket(gateway_server, pool, redis, card_pool, conn):
    # is_active=True: the gateway's own "rooms" list command reads WHERE
    # is_active = true, unlike every other test using create_room() (see
    # its own docstring for why False is the right default there).
    room_id = await create_room(
        conn,
        stake=Decimal("10.00"),
        min_players=2,
        lobby_seconds=1,
        call_interval_ms=10,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        # A second player joins directly through the engine so the lobby
        # can actually fill -- this test is about the WebSocket path for
        # player one, not about running two browser sessions.
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            authed = json.loads(await ws.recv())
            assert authed["t"] == "authed"
            user_id = authed["user"]["id"]
            assert authed["user"]["balance"] == "0.00"

            # Deposits aren't wired up until Phase 5-6 -- fund directly
            # through the ledger, the same real path a deposit would use.
            await fund_user(conn, user_id, Decimal("100.00"))

            await ws.send(json.dumps({"t": "rooms"}))
            rooms_msg = json.loads(await ws.recv())
            assert rooms_msg["t"] == "rooms"
            assert any(r["room_id"] == room_id for r in rooms_msg["rooms"])

            await ws.send(json.dumps({"t": "join", "room_id": room_id}))
            state = json.loads(await ws.recv())
            assert state["t"] == "state_sync"
            assert state["room_id"] == room_id

            await ws.send(json.dumps({"t": "take_card", "room_id": room_id, "card_no": 1}))
            ack = await recv_until(ws, "ack")
            assert ack == {"t": "ack", "for": "take_card", "ok": True, "reason": None}

            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            assert await ledger.balance(conn, cash.id) == Decimal("90.00")

            # A live call should arrive over the socket once the round
            # starts -- proving the Redis pub/sub fan-out path, not just
            # the request/response command channel.
            await recv_until(ws, "call")

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None)

        round_row = await pool.fetchrow(
            "SELECT status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        assert round_row["status"] in ("done", "voided")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_take_card_uses_the_players_persisted_auto_mark_preference(
    gateway_server, pool, redis, card_pool, conn
):
    # Mini App spec: "AUTO toggle ... Persist the choice per user." A
    # player who turns AUTO off must get AUTO off again on their very next
    # take_card, even in a different room and a brand-new WebSocket
    # connection -- not just for the rest of the connection that set it.
    room_a = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=1,
        call_interval_ms=10, is_active=True,
    )
    room_b = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=1,
        call_interval_ms=10, is_active=True,
    )
    engine_a = RoundEngine(pool, redis, await load_room_config(pool, room_a), card_pool)
    engine_b = RoundEngine(pool, redis, await load_room_config(pool, room_b), card_pool)
    task_a = asyncio.create_task(engine_a.run_forever())
    task_b = asyncio.create_task(engine_b.run_forever())
    try:
        other_a = await create_funded_user(conn)
        assert (await engine_a.join(other_a, 2)).ok
        other_b = await create_funded_user(conn)
        assert (await engine_b.join(other_b, 2)).ok

        telegram_id = next_telegram_id()

        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            authed = json.loads(await ws.recv())
            user_id = authed["user"]["id"]
            await fund_user(conn, user_id, Decimal("100.00"))

            await ws.send(json.dumps({"t": "join", "room_id": room_a}))
            await recv_until(ws, "state_sync")
            await ws.send(json.dumps({"t": "take_card", "room_id": room_a, "card_no": 1}))
            ack = await recv_until(ws, "ack")
            assert ack == {"t": "ack", "for": "take_card", "ok": True, "reason": None}

            round_a_id = await pool.fetchval(
                "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_a
            )
            assert await pool.fetchval(
                "SELECT auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
                round_a_id,
                user_id,
            ) is True

            await ws.send(json.dumps({"t": "set_auto", "room_id": room_a, "auto": False}))
            set_auto_ack = await recv_until(ws, "ack")
            assert set_auto_ack == {"t": "ack", "for": "set_auto", "ok": True, "reason": None}

        assert await pool.fetchval(
            "SELECT auto_mark_preference FROM users WHERE id = $1", user_id
        ) is False

        # A second, independent connection, joining a different room, with
        # no set_auto sent in this session at all -- proof this is a real
        # persisted default, not state kept in the first connection's own
        # ConnectionHandler instance.
        async with websockets.connect(gateway_server) as ws2:
            await ws2.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            await ws2.recv()  # authed

            await ws2.send(json.dumps({"t": "join", "room_id": room_b}))
            await recv_until(ws2, "state_sync")
            await ws2.send(json.dumps({"t": "take_card", "room_id": room_b, "card_no": 1}))
            ack2 = await recv_until(ws2, "ack")
            assert ack2 == {"t": "ack", "for": "take_card", "ok": True, "reason": None}

        round_b_id = await pool.fetchval(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_b
        )
        assert await pool.fetchval(
            "SELECT auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
            round_b_id,
            user_id,
        ) is False
    finally:
        await engine_a.stop()
        await engine_b.stop()
        await asyncio.wait_for(task_a, timeout=15)
        await asyncio.wait_for(task_b, timeout=15)
