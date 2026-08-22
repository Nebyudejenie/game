"""Gateway WebSocket handshake tests -- the same attack vectors as
test_telegram_auth.py, but proven end to end through the real running
gateway app rather than just the validation function in isolation.
"""

import json
import time

import pytest
import websockets

from tests.integration.conftest import build_init_data, next_telegram_id


async def test_valid_auth_is_accepted(gateway_server):
    telegram_id = next_telegram_id()
    async with websockets.connect(gateway_server) as ws:
        await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
        reply = json.loads(await ws.recv())
        assert reply["t"] == "authed"
        assert reply["user"]["id"] > 0
        assert "server_time" in reply


async def test_tampered_hash_is_rejected(gateway_server):
    telegram_id = next_telegram_id()
    raw = build_init_data(telegram_id)
    tampered = raw[:-4] + "0000"  # corrupt the trailing hash characters
    async with websockets.connect(gateway_server) as ws:
        await ws.send(json.dumps({"t": "auth", "init_data": tampered}))
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await ws.recv()
        assert exc_info.value.rcvd is not None
        assert exc_info.value.rcvd.code == 4003


async def test_stale_auth_date_is_rejected(gateway_server):
    telegram_id = next_telegram_id()
    stale = int(time.time()) - (25 * 60 * 60)
    raw = build_init_data(telegram_id, auth_date=stale)
    async with websockets.connect(gateway_server) as ws:
        await ws.send(json.dumps({"t": "auth", "init_data": raw}))
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await ws.recv()
        assert exc_info.value.rcvd.code == 4003


async def test_non_auth_first_frame_is_rejected(gateway_server):
    async with websockets.connect(gateway_server) as ws:
        await ws.send(json.dumps({"t": "ping", "ts": 1}))
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await ws.recv()
        assert exc_info.value.rcvd.code == 4000


async def test_healthz_reports_ok(gateway_server):
    import httpx

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base}/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
