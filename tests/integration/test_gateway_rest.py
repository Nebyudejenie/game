"""Tests for the gateway's REST endpoints (/api/me, /api/history) -- the
Mini App wallet screen's data source. Same auth boundary as the WebSocket
handshake: `Authorization: tma <initData>`, validated the same way.
"""

from decimal import Decimal

import httpx

from tests.integration.conftest import build_init_data, fund_user, next_telegram_id


def http_base(gateway_server: str) -> str:
    return gateway_server.replace("ws://", "http://").replace("/ws", "")


async def test_api_me_requires_authorization_header(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base(gateway_server)}/api/me")
    assert response.status_code == 401


async def test_api_me_rejects_tampered_init_data(gateway_server):
    telegram_id = next_telegram_id()
    raw = build_init_data(telegram_id)
    tampered = raw[:-4] + "0000"
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {tampered}"}
        )
    assert response.status_code == 401


async def test_api_me_returns_real_balance(gateway_server, pool, conn):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 200
    assert response.json() == {"cash": "0.00", "bonus": "0.00", "locked": "0.00"}

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("42.50"))

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.json()["cash"] == "42.50"


async def test_api_history_empty_for_new_user(gateway_server):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/history", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 200
    assert response.json() == []
