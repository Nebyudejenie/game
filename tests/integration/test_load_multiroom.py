"""Multi-room fan-out at scale (spec Prompt 10 / section 10.3's "10,000
concurrent sockets, 200 rooms" scenario) -- a different code path from
test_gateway_fanout.py's single-room 1000-socket test: many separate
`room:{id}` pub/sub channels dispatched through one shared psubscribe
connection (services/gateway/fanout.py's FanoutHub), not one channel fanned
out to many sockets.

Honest scope note (see DECISIONS.md): this drives real WebSocket clients
and a real gateway from the *same* process/event loop on a 4-core, ~8GB
dev sandbox -- load generator and system-under-test share CPU, unlike a
real distributed load rig. Numbers here are real measurements at the scale
this environment can actually sustain, not the spec's literal 10k/200-room
target; that gap is reported, not hidden.
"""

import asyncio
import json
import time
from decimal import Decimal
from statistics import mean

import pytest
import websockets

from tests.integration.conftest import build_init_data, create_room, next_telegram_id

pytestmark = pytest.mark.load

ROOM_COUNT = 100
SOCKETS_PER_ROOM = 10
TOTAL_SOCKETS = ROOM_COUNT * SOCKETS_PER_ROOM


async def _connect_and_join(url: str, room_id: int, telegram_id: int):
    ws = await websockets.connect(url, open_timeout=20, close_timeout=2)
    await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
    await ws.recv()  # authed
    await ws.send(json.dumps({"t": "join", "room_id": room_id}))
    await ws.recv()  # state_sync
    return ws


async def test_many_rooms_each_receive_a_call_within_budget(gateway_server, redis, conn):
    room_ids = [
        await create_room(conn, stake=Decimal("10.00"), min_players=2) for _ in range(ROOM_COUNT)
    ]

    connect_started = time.monotonic()
    sockets_by_room: list[list] = []
    for room_id in room_ids:
        telegram_ids = [next_telegram_id() for _ in range(SOCKETS_PER_ROOM)]
        room_sockets = await asyncio.gather(
            *(_connect_and_join(gateway_server, room_id, tid) for tid in telegram_ids)
        )
        sockets_by_room.append(list(room_sockets))
    connect_elapsed = time.monotonic() - connect_started

    all_sockets = [ws for room_sockets in sockets_by_room for ws in room_sockets]
    assert len(all_sockets) == TOTAL_SOCKETS

    try:
        # Sequential, not asyncio.gather: real publishes come from many
        # separate RoundEngine processes, each with its own single Redis
        # connection doing one publish -- firing genuinely concurrent
        # commands through this one shared test client just exhausts its
        # connection pool, an artifact of the test client, not the system.
        # A publish is sub-millisecond, so this is still effectively
        # simultaneous from the receiving sockets' perspective.
        publish_time = time.monotonic()
        for room_id in room_ids:
            await redis.publish(
                f"room:{room_id}",
                json.dumps({"t": "call", "round_id": 1, "index": 1, "number": 40, "letter": "N"}),
            )

        async def receive_latency(ws) -> float:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                message = json.loads(raw)
                if message.get("t") == "call":
                    return time.monotonic() - publish_time

        latencies = await asyncio.gather(*(receive_latency(ws) for ws in all_sockets))
    finally:
        await asyncio.gather(*(ws.close() for ws in all_sockets), return_exceptions=True)

    latencies_ms = sorted(latency * 1000 for latency in latencies)
    p50 = latencies_ms[len(latencies_ms) // 2]
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    p99 = latencies_ms[int(len(latencies_ms) * 0.99)]
    worst = latencies_ms[-1]

    print(
        f"\n[multiroom rooms={ROOM_COUNT} sockets/room={SOCKETS_PER_ROOM} "
        f"total={TOTAL_SOCKETS}] connect_time={connect_elapsed:.1f}s "
        f"p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms worst={worst:.1f}ms "
        f"mean={mean(latencies_ms):.1f}ms"
    )

    assert p99 < 300, f"p99 call-to-render latency was {p99:.1f}ms across {TOTAL_SOCKETS} sockets in {ROOM_COUNT} rooms; spec budget is 300ms"
