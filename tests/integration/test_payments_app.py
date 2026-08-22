"""HTTP-layer tests for services/payments/app.py: the one inbound surface
Chapa's own servers actually reach. Exercised as real HTTP requests against
a real running uvicorn instance (payments_server fixture), the same
discipline as test_admin_app.py and test_gateway_auth.py -- and one true
end-to-end test proving the whole chain: a real webhook HTTP request credits
the ledger and pushes a live balance_update over a real connected WebSocket.
"""

import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import websockets

from packages.core import ledger
from tests.integration.conftest import build_init_data, next_telegram_id
from tests.integration.test_gateway_gameplay import recv_until

CHAPA_SECRET = "test-chapa-secret-for-suite"  # matches conftest's CHAPA_API_KEY setdefault


def _sign(raw_body: bytes) -> dict[str, str]:
    payload_signature = hmac.new(CHAPA_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    key_signature = hmac.new(CHAPA_SECRET.encode(), CHAPA_SECRET.encode(), hashlib.sha256).hexdigest()
    return {"x-chapa-signature": payload_signature, "chapa-signature": key_signature}


async def _insert_pending_payment(pool, *, user_id: int, our_ref: str, amount: Decimal) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO payments (user_id, direction, provider, our_ref, amount, status)
        VALUES ($1, 'in', 'chapa', $2, $3, 'processing')
        RETURNING id
        """,
        user_id,
        our_ref,
        amount,
    )
    assert row is not None
    return int(row["id"])


async def test_healthz_reports_ok(payments_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{payments_server}/healthz")
    assert response.status_code == 200


async def test_webhook_missing_signature_is_rejected(payments_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/webhooks/chapa", content=b"{}", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 401


async def test_webhook_wrong_signature_is_rejected(payments_server):
    body = json.dumps({"tx_ref": "DEP-does-not-matter", "reference": "r1", "status": "success", "amount": "1.00"}).encode()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/webhooks/chapa",
            content=body,
            headers={"x-chapa-signature": "wrong", "chapa-signature": "wrong"},
        )
    assert response.status_code == 401


async def test_webhook_credits_a_real_payment_over_real_http(pool, conn, payments_server):
    telegram_id = next_telegram_id()
    user_row = await conn.fetchrow(
        "INSERT INTO users (telegram_id, display_name) VALUES ($1, $2) RETURNING id",
        telegram_id,
        f"http-webhook-{telegram_id}",
    )
    user_id = user_row["id"]
    our_ref = f"DEP-test-{telegram_id}"
    await _insert_pending_payment(pool, user_id=user_id, our_ref=our_ref, amount=Decimal("120.00"))

    body = json.dumps(
        {
            "event": "charge.success",
            "status": "success",
            "amount": "120.00",
            "tx_ref": our_ref,
            "reference": f"chapa-ref-{telegram_id}",
        }
    ).encode()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/webhooks/chapa", content=body, headers=_sign(body)
        )
    assert response.status_code == 200
    assert response.text == "credited"

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("120.00")


async def test_webhook_pushes_live_balance_update_over_websocket(
    pool, conn, payments_server, gateway_server
):
    telegram_id = next_telegram_id()
    async with websockets.connect(gateway_server) as ws:
        await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
        authed = json.loads(await ws.recv())
        assert authed["t"] == "authed"
        user_id = authed["user"]["id"]

        our_ref = f"DEP-live-{telegram_id}"
        await _insert_pending_payment(pool, user_id=user_id, our_ref=our_ref, amount=Decimal("65.00"))

        body = json.dumps(
            {
                "event": "charge.success",
                "status": "success",
                "amount": "65.00",
                "tx_ref": our_ref,
                "reference": f"chapa-ref-live-{telegram_id}",
            }
        ).encode()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{payments_server}/webhooks/chapa", content=body, headers=_sign(body)
            )
        assert response.status_code == 200

        push = await recv_until(ws, "balance_update")
        assert push["cash"] == "65.00"
