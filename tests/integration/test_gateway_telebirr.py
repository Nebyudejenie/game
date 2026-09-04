"""HTTP tests for the gateway's Telebirr SMS-evidence redemption endpoint
(POST /api/wallet/deposits/telebirr/redeem) -- the one player-facing
surface for services/payments/telebirr_redemption.py. Same auth boundary
and availability-gating discipline as test_gateway_rest.py's existing
/api/deposit* tests.
"""

import itertools
import random

import httpx

from services.admin import queries as admin_queries
from services.payments.telebirr_ingest import ingest_sms_evidence
from tests.integration.conftest import build_init_data, next_telegram_id
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_gateway_rest import http_base

_ref_counter = itertools.count(random.randint(4 * 10**7, 5 * 10**7))


def _next_reference() -> str:
    return f"DI{next(_ref_counter):08d}"


def _build_sms(reference: str, *, amount: str = "10.00", recipient: str) -> str:
    return (
        f"Dear {recipient} \n"
        f"You have received ETB {amount} from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )


async def _make_available_evidence(pool, conn, *, amount: str = "10.00") -> str:
    reference = _next_reference()
    recipient = f"GwRecipient{reference}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    outcome = await ingest_sms_evidence(
        pool, raw_sms=_build_sms(reference, amount=amount, recipient=recipient),
        source="macrodroid", source_ref="test-device",
    )
    assert outcome.status == "ingested_available"
    return reference


async def test_redeem_endpoint_is_disabled_by_default(gateway_server):
    # telebirr_sms ships seeded disabled in payment_provider_availability
    # (migration 9c1f4d7a2b3e) -- no admin toggle needed for this test.
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/wallet/deposits/telebirr/redeem",
            headers={"Authorization": f"tma {init_data}"},
            json={"reference": "DI41FHSD4J"},
        )
    assert response.status_code == 503


async def test_redeem_endpoint_requires_authorization(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/wallet/deposits/telebirr/redeem",
            json={"reference": "DI41FHSD4J"},
        )
    assert response.status_code == 401


async def test_redeem_endpoint_credits_a_real_reference_over_real_http(pool, conn, gateway_server):
    admin_id, *_ = await create_test_admin(pool)
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="telebirr_sms", direction="in", enabled=True,
        reason="test: enable telebirr redemption", ip_address=None,
    )
    try:
        reference = await _make_available_evidence(pool, conn, amount="35.00")
        telegram_id = next_telegram_id()
        init_data = build_init_data(telegram_id)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{http_base(gateway_server)}/api/wallet/deposits/telebirr/redeem",
                headers={"Authorization": f"tma {init_data}"},
                json={"reference": reference},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["amount"] == "35.00"

        user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        row = await pool.fetchrow(
            "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
        )
        assert row["status"] == "redeemed"
        assert await pool.fetchval(
            "SELECT redeemed_by_user_id FROM payment_evidence WHERE external_reference = $1", reference
        ) == user_id
    finally:
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="telebirr_sms", direction="in", enabled=False,
            reason="test cleanup", ip_address=None,
        )


async def test_redeem_endpoint_returns_a_machine_readable_code_for_an_unknown_reference(
    pool, conn, gateway_server
):
    admin_id, *_ = await create_test_admin(pool)
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="telebirr_sms", direction="in", enabled=True,
        reason="test: enable telebirr redemption", ip_address=None,
    )
    try:
        telegram_id = next_telegram_id()
        init_data = build_init_data(telegram_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{http_base(gateway_server)}/api/wallet/deposits/telebirr/redeem",
                headers={"Authorization": f"tma {init_data}"},
                json={"reference": "DOESNOTEXIST99"},
            )
        assert response.status_code == 422
        assert response.json()["detail"] == "payment_not_found"
    finally:
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="telebirr_sms", direction="in", enabled=False,
            reason="test cleanup", ip_address=None,
        )
