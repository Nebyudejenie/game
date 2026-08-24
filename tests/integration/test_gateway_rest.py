"""Tests for the gateway's REST endpoints (/api/me, /api/history,
/api/deposit, /api/withdraw) -- the Mini App wallet screen's data source
and, for the latter two, its deposit/withdraw tabs. Same auth boundary as
the WebSocket handshake: `Authorization: tma <initData>`, validated the
same way.
"""

from decimal import Decimal

import httpx
import pytest

from services.gateway.app import app as gateway_app
from tests.integration.conftest import build_init_data, fund_user, next_telegram_id
from tests.integration.test_payments_deposits import FakePaymentProvider
from tests.integration.test_payout_worker import FakePayoutProvider


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


@pytest.fixture
def fake_chapa():
    """/api/deposit and /api/withdraw read their provider from
    app.state.chapa (a real ChapaProvider by default, set once at gateway
    startup) -- swap in a fake for the duration of one test, same pattern
    test_admin_app.py uses for app.state.ip_allowlist.
    """
    original = gateway_app.state.chapa
    fake = FakePaymentProvider()
    gateway_app.state.chapa = fake
    try:
        yield fake
    finally:
        gateway_app.state.chapa = original


async def test_api_deposit_requires_auth(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit", json={"amount": "100"}
        )
    assert response.status_code == 401


async def test_api_deposit_rejects_invalid_amount(gateway_server):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "not-a-number"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_amount"


async def test_api_deposit_rejects_below_minimum(gateway_server, pool, conn):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await conn.execute(
        "UPDATE users SET phone_e164 = $2 WHERE id = $1",
        user_row["id"],
        f"+2519{telegram_id % 100_000_000:08d}",
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "1"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "below_minimum"


async def test_api_deposit_requires_phone_on_file(gateway_server, fake_chapa):
    # get_or_create_user_by_telegram_id() (the gateway's own auth path)
    # creates a user row with no phone_e164 -- this is exactly that user,
    # who has never gone through the bot's phone-share registration.
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "100"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "phone_required"


async def test_api_deposit_blocked_when_self_excluded(gateway_server, pool, conn, fake_chapa):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await conn.execute(
        "UPDATE users SET phone_e164 = $2, status = 'self_excluded' WHERE id = $1",
        user_row["id"],
        f"+2519{telegram_id % 100_000_000:08d}",
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "100"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "self_excluded"


async def test_api_deposit_succeeds_and_returns_a_checkout_url(gateway_server, pool, conn, fake_chapa):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await conn.execute(
        "UPDATE users SET phone_e164 = $2 WHERE id = $1",
        user_row["id"],
        f"+2519{telegram_id % 100_000_000:08d}",
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "150"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checkout_url"] == f"https://pay.test/{body['our_ref']}"
    assert body["our_ref"].startswith("DEP-")

    row = await pool.fetchrow("SELECT status, amount FROM payments WHERE our_ref = $1", body["our_ref"])
    assert row["status"] == "processing"
    assert row["amount"] == Decimal("150.00")


@pytest.fixture
def fake_chapa_payout():
    original = gateway_app.state.chapa
    fake = FakePayoutProvider()
    gateway_app.state.chapa = fake
    try:
        yield fake
    finally:
        gateway_app.state.chapa = original


async def test_api_withdraw_requires_auth(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/withdraw",
            json={"amount": "100", "account_ref": "0911223344", "holder_name": "Test"},
        )
    assert response.status_code == 401


async def test_api_withdraw_rejects_insufficient_balance(gateway_server, fake_chapa_payout):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/withdraw",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "1000", "account_ref": "0911223344", "holder_name": "Test Holder"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "insufficient_balance"


async def test_api_withdraw_succeeds_and_locks_funds(gateway_server, pool, conn, fake_chapa_payout):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("500.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/withdraw",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "200", "account_ref": "0911223344", "holder_name": "Test Holder"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["our_ref"].startswith("WD-")
    assert body["status"] in ("approved", "review")
