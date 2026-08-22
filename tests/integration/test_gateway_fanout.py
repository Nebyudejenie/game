"""Fan-out scale tests -- spec's own instruction is to report actual
measured numbers rather than assume an architecture works at 10k just
because it's designed to. This is that measurement, at the scale this
single-process test environment can actually drive: real WebSocket clients,
real Redis pub/sub, real per-connection queues.

"Message serialized exactly once" isn't separately instrumented here: it's
structural: round_engine.py's `_publish_room` calls `json.dumps` exactly
once per event and hands Redis one string; every gateway replica's
FanoutHub receives that same string and forwards it byte-for-byte to every
local socket (fanout.py's `ConnectionQueue.offer` / the writer loop's
`send_text(raw)`) -- there is no per-connection re-serialization anywhere
in that path to instrument a count for.
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


async def _connect_and_join(url: str, room_id: int, telegram_id: int):
    ws = await websockets.connect(url, open_timeout=10, close_timeout=2)
    await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
    await ws.recv()  # authed
    await ws.send(json.dumps({"t": "join", "room_id": room_id}))
    await ws.recv()  # state_sync
    return ws


async def test_many_sockets_receive_a_call_within_budget(gateway_server, redis, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    n = 1000

    telegram_ids = [next_telegram_id() for _ in range(n)]
    sockets = await asyncio.gather(
        *(_connect_and_join(gateway_server, room_id, tid) for tid in telegram_ids)
    )

    try:
        publish_time = time.monotonic()
        await redis.publish(
            f"room:{room_id}",
            json.dumps({"t": "call", "round_id": 1, "index": 1, "number": 40, "letter": "N"}),
        )

        async def receive_latency(ws) -> float:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                message = json.loads(raw)
                if message.get("t") == "call":
                    return time.monotonic() - publish_time

        latencies = await asyncio.gather(*(receive_latency(ws) for ws in sockets))
    finally:
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)

    latencies_ms = sorted(latency * 1000 for latency in latencies)
    p50 = latencies_ms[len(latencies_ms) // 2]
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    p99 = latencies_ms[int(len(latencies_ms) * 0.99)]
    worst = latencies_ms[-1]

    print(
        f"\n[fanout n={n}] p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms "
        f"worst={worst:.1f}ms mean={mean(latencies_ms):.1f}ms"
    )

    assert p99 < 300, f"p99 call-to-render latency was {p99:.1f}ms; spec budget is 300ms"


async def test_stalled_reader_does_not_delay_other_sockets(gateway_server, redis, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    n = 50

    telegram_ids = [next_telegram_id() for _ in range(n)]
    sockets = await asyncio.gather(
        *(_connect_and_join(gateway_server, room_id, tid) for tid in telegram_ids)
    )
    stalled, healthy = sockets[0], sockets[1:]

    try:
        # Flood enough messages to overflow the stalled connection's bounded
        # queue (fanout.MAX_QUEUE_SIZE) -- it never calls recv() through any
        # of this.
        for i in range(150):
            await redis.publish(
                f"room:{room_id}",
                json.dumps(
                    {"t": "call", "round_id": 1, "index": i, "number": (i % 75) + 1, "letter": "N"}
                ),
            )

        publish_time = time.monotonic()
        await redis.publish(
            f"room:{room_id}",
            json.dumps(
                {"t": "round_end", "round_id": 1, "winners": [], "derash": "0.00", "server_seed": "ab"}
            ),
        )

        async def receive_round_end(ws) -> float:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                message = json.loads(raw)
                if message.get("t") == "round_end":
                    return time.monotonic() - publish_time

        latencies = await asyncio.gather(*(receive_round_end(ws) for ws in healthy))
    finally:
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)

    worst_ms = max(latencies) * 1000
    print(f"\n[stalled-reader] {len(healthy)} healthy sockets, worst delivery {worst_ms:.1f}ms")
    assert worst_ms < 300, f"a stalled reader delayed healthy sockets by {worst_ms:.1f}ms"
