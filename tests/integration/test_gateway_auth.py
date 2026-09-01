"""Gateway WebSocket handshake tests -- the same attack vectors as
test_telegram_auth.py, but proven end to end through the real running
gateway app rather than just the validation function in isolation.
"""

import json
import time

import pytest
import websockets
from structlog.testing import capture_logs

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


async def test_handshake_rejection_is_logged_server_side_with_the_real_reason(gateway_server):
    # A production incident (every real player stuck on a "session
    # expired" banner) caught that _handshake()'s rejection reason only
    # ever reached the client's own WS close frame -- nothing was ever
    # logged in this process, so there was no way to tell "one stale
    # session" apart from "every single connection failing identically
    # (e.g. a misconfigured TELEGRAM_BOT_TOKEN)" from server-side logs
    # alone. A tampered hash is bad_hash specifically -- the single most
    # likely real-world cause of every real player failing at once, since
    # a wrong bot token makes every hash check fail identically for
    # every user's otherwise-perfectly-fresh initData.
    telegram_id = next_telegram_id()
    raw = build_init_data(telegram_id)
    tampered = raw[:-4] + "0000"
    with capture_logs() as logs:
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": tampered}))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()
    assert any(
        e.get("event") == "ws_handshake_rejected" and e.get("reason") == "invalid_init_data:bad_hash"
        for e in logs
    ), logs
    # Never the raw initData or bot token -- see telegram_auth.py's own
    # module docstring for why, and packages/core/logging.py's redaction
    # list this would otherwise need to rely on.
    assert not any(tampered in str(e) for e in logs), logs


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
