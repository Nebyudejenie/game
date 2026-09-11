"""Tests for the automated Android/MacroDroid Telebirr ingestion path's
per-device authentication and health tracking (services/payments/
device_registry.py, migrations/versions/e3a7c9f01b2d).

The one thing every test in this file ultimately has to prove is that
this is a *second door into the same room*, not a second room: whatever
comes in through a registered device's own bearer token converges on the
exact same services/payments/telebirr_ingest.py::ingest_sms_evidence()
pipeline, the exact same payment_evidence table, and the exact same
telebirr_sms provider-availability kill switch at redemption -- proven
here against the real HTTP routes (services/payments/app.py,
services/admin/app.py, services/gateway/app.py), not by calling internal
functions directly, the same discipline test_telebirr_ingest.py's own
docstring already establishes for the legacy shared-token route.
"""

import asyncio
import itertools
import random

import httpx

from services.admin import queries as admin_queries
from services.payments.telebirr_ingest import STATUS_DUPLICATE, ingest_sms_evidence
from tests.integration.conftest import build_init_data, next_telegram_id
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_gateway_rest import http_base

_ref_counter = itertools.count(random.randint(3 * 10**7, 4 * 10**7))
_device_counter = itertools.count(random.randint(1, 10**6))


def _next_reference() -> str:
    return f"DI{next(_ref_counter):08d}"


def _next_device_id() -> str:
    return f"test-device-{next(_device_counter)}"


def _build_sms(reference: str, *, amount: str = "10.00", recipient: str) -> str:
    return (
        f"Dear {recipient} \n"
        f"You have received ETB {amount} from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )


async def _register_device(pool) -> dict:
    admin_id, *_ = await create_test_admin(pool)
    return await admin_queries.create_ingestion_device_admin(
        pool,
        admin_id=admin_id,
        device_id=_next_device_id(),
        device_name="Test phone",
        ip_address=None,
    )


async def _post_ingest(payments_server: str, *, token: str, raw_sms: str, device_id: str = "unused"):
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"{payments_server}/internal/telebirr/ingest",
            json={"raw_sms": raw_sms, "device_id": device_id},
            headers={"Authorization": f"Bearer {token}"},
        )


# --- happy path + health counters ------------------------------------------


async def test_device_token_ingests_a_real_sms_over_real_http(pool, conn, payments_server):
    device = await _register_device(pool)
    recipient = f"DeviceRecip{_next_reference()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    reference = _next_reference()

    response = await _post_ingest(
        payments_server, token=device["token"], raw_sms=_build_sms(reference, recipient=recipient)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ingested_available"
    assert body["external_reference"] == reference
    assert body["device_name"] == "Test phone"

    # source_ref is the *registered* device_id, never the client-supplied
    # body field -- proven by having sent a different, wrong device_id in
    # the JSON body above ("unused") and confirming it was ignored.
    row = await conn.fetchrow(
        "SELECT source, source_ref FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row["source"] == "macrodroid"
    assert row["source_ref"] == device["device_id"]

    health = await conn.fetchrow(
        "SELECT success_count, last_seen_at, last_success_at FROM ingestion_devices WHERE id = $1",
        device["id"],
    )
    assert health["success_count"] == 1
    assert health["last_seen_at"] is not None
    assert health["last_success_at"] is not None


async def test_device_duplicate_submission_increments_duplicate_count_not_success(pool, conn, payments_server):
    device = await _register_device(pool)
    recipient = f"DupRecip{_next_reference()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    sms = _build_sms(_next_reference(), recipient=recipient)

    first = await _post_ingest(payments_server, token=device["token"], raw_sms=sms)
    second = await _post_ingest(payments_server, token=device["token"], raw_sms=sms)
    assert first.json()["status"] == "ingested_available"
    assert second.json()["status"] == STATUS_DUPLICATE

    health = await conn.fetchrow(
        "SELECT success_count, duplicate_count FROM ingestion_devices WHERE id = $1", device["id"]
    )
    assert health["success_count"] == 1
    assert health["duplicate_count"] == 1


async def test_device_unparseable_sms_increments_failure_count_and_creates_no_evidence(pool, conn, payments_server):
    device = await _register_device(pool)
    before = await conn.fetchval("SELECT count(*) FROM payment_evidence")

    response = await _post_ingest(payments_server, token=device["token"], raw_sms="Your OTP is 483920.")
    assert response.json()["status"] == "unparseable"

    after = await conn.fetchval("SELECT count(*) FROM payment_evidence")
    assert after == before

    health = await conn.fetchrow(
        "SELECT failure_count, last_error_reason FROM ingestion_devices WHERE id = $1", device["id"]
    )
    assert health["failure_count"] == 1
    assert health["last_error_reason"] == "unrecognized_template"


# --- authentication ---------------------------------------------------------


async def test_unknown_bearer_token_is_rejected(payments_server):
    response = await _post_ingest(
        payments_server, token="not-a-real-token-at-all", raw_sms=_build_sms(_next_reference(), recipient="X")
    )
    assert response.status_code == 401


async def test_revoked_device_token_is_rejected_and_counted(pool, conn, payments_server):
    device = await _register_device(pool)
    admin_id, *_ = await create_test_admin(pool)
    revoked = await admin_queries.set_ingestion_device_status_admin(
        pool, admin_id=admin_id, device_pk=device["id"], status="revoked", ip_address=None
    )
    assert revoked is True

    response = await _post_ingest(
        payments_server, token=device["token"], raw_sms=_build_sms(_next_reference(), recipient="X")
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "device revoked"

    auth_failures = await conn.fetchval(
        "SELECT auth_failure_count FROM ingestion_devices WHERE id = $1", device["id"]
    )
    assert auth_failures == 1


async def test_rotated_token_invalidates_old_and_activates_new(pool, conn, payments_server):
    device = await _register_device(pool)
    old_token = device["token"]

    admin_id, *_ = await create_test_admin(pool)
    rotated = await admin_queries.rotate_ingestion_device_token_admin(
        pool, admin_id=admin_id, device_pk=device["id"], ip_address=None
    )
    assert rotated is not None
    assert rotated["token"] != old_token

    old_response = await _post_ingest(
        payments_server, token=old_token, raw_sms=_build_sms(_next_reference(), recipient="X")
    )
    assert old_response.status_code == 401

    recipient = f"RotatedRecip{_next_reference()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    new_response = await _post_ingest(
        payments_server, token=rotated["token"], raw_sms=_build_sms(_next_reference(), recipient=recipient)
    )
    assert new_response.status_code == 200
    assert new_response.json()["status"] == "ingested_available"


async def test_legacy_shared_token_still_works_once_devices_exist(pool, conn, payments_server):
    # A real regression check: registering per-device credentials must
    # never break an already-deployed phone still using the original
    # single shared MACRODROID_INGEST_TOKEN (conftest.py sets this to
    # "test-macrodroid-token-for-suite" for the whole suite).
    await _register_device(pool)  # some other device now exists
    recipient = f"LegacyRecip{_next_reference()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    reference = _next_reference()

    response = await _post_ingest(
        payments_server,
        token="test-macrodroid-token-for-suite",
        raw_sms=_build_sms(reference, recipient=recipient),
        device_id="legacy-phone",
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ingested_available"
    row = await conn.fetchrow(
        "SELECT source_ref FROM payment_evidence WHERE external_reference = $1", reference
    )
    # The legacy path still trusts the client-supplied device_id label --
    # unchanged behavior, exactly as before this feature existed.
    assert row["source_ref"] == "legacy-phone"


# --- one canonical pipeline regardless of source ---------------------------


async def test_manual_and_automated_duplicate_converge_on_one_evidence_row(pool, conn, payments_server):
    device = await _register_device(pool)
    recipient = f"ConvergeRecip{_next_reference()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    reference = _next_reference()
    sms = _build_sms(reference, recipient=recipient)

    # The exact same real SMS arrives through the Telegram payment-agent
    # path first (a direct call, exactly how services/bot/handlers.py's
    # on_agent_sms invokes the canonical pipeline)...
    manual_outcome = await ingest_sms_evidence(
        pool, raw_sms=sms, source="telegram_agent", source_ref="12345"
    )
    assert manual_outcome.status == "ingested_available"

    # ...and again through the new authenticated Android device path.
    device_response = await _post_ingest(payments_server, token=device["token"], raw_sms=sms)
    assert device_response.json()["status"] == STATUS_DUPLICATE
    assert device_response.json()["evidence_id"] == manual_outcome.evidence_id

    count = await conn.fetchval(
        "SELECT count(*) FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert count == 1


async def test_redemption_kill_switch_blocks_evidence_from_the_device_path_too(pool, conn, gateway_server, payments_server):
    # telebirr_sms ships disabled by default (migration 9c1f4d7a2b3e) --
    # test_gateway_telebirr.py's own test_redeem_endpoint_is_disabled_by_
    # default already proves this for evidence ingested directly. This
    # proves the exact same gate holds when the evidence instead came
    # from a brand-new, per-device-authenticated Android submission --
    # the kill switch lives at the redemption boundary
    # (services/gateway/app.py), which has no idea which ingestion
    # adapter produced any given payment_evidence row, by design.
    device = await _register_device(pool)
    recipient = f"KillSwitchRecip{_next_reference()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    reference = _next_reference()
    ingest_response = await _post_ingest(
        payments_server, token=device["token"], raw_sms=_build_sms(reference, recipient=recipient)
    )
    assert ingest_response.json()["status"] == "ingested_available"

    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/wallet/deposits/telebirr/redeem",
            headers={"Authorization": f"tma {init_data}"},
            json={"reference": reference},
        )
    assert response.status_code == 503

    row = await conn.fetchrow(
        "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row["status"] == "available"  # never touched -- redemption never even ran


async def test_two_identical_concurrent_requests_never_double_credit_evidence(pool, conn, payments_server):
    # Simulates MacroDroid firing twice near-simultaneously (a real device
    # retry racing the original request) -- external_reference's UNIQUE
    # constraint plus telebirr_ingest.py's own "SELECT ... FOR UPDATE"
    # fallback is what actually makes this safe; this proves it holds
    # over two real concurrent HTTP requests, not just two sequential
    # calls to the same Python function in the same process.
    device = await _register_device(pool)
    recipient = f"ConcurrentRecip{_next_reference()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    reference = _next_reference()
    sms = _build_sms(reference, recipient=recipient)

    responses = await asyncio.gather(
        _post_ingest(payments_server, token=device["token"], raw_sms=sms),
        _post_ingest(payments_server, token=device["token"], raw_sms=sms),
    )
    statuses = sorted(r.json()["status"] for r in responses)
    assert statuses == ["duplicate", "ingested_available"]

    count = await conn.fetchval(
        "SELECT count(*) FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert count == 1


# --- admin console -----------------------------------------------------------


async def test_create_device_returns_a_token_exactly_once(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    device_id = _next_device_id()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/ingestion-devices",
            headers=headers,
            json={"device_id": device_id, "device_name": "Console-created phone"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] == device_id
    assert len(body["token"]) > 20


async def test_duplicate_device_id_is_rejected(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    device_id = _next_device_id()
    async with httpx.AsyncClient() as client:
        first = await client.post(
            f"{admin_server}/ingestion-devices", headers=headers,
            json={"device_id": device_id, "device_name": "First"},
        )
        second = await client.post(
            f"{admin_server}/ingestion-devices", headers=headers,
            json={"device_id": device_id, "device_name": "Second"},
        )
    assert first.status_code == 200
    assert second.status_code == 422


async def test_support_role_can_view_devices_but_not_create_one(admin_server, pool):
    device = await _register_device(pool)
    support_headers = await _auth_headers(admin_server, pool, role="support")

    async with httpx.AsyncClient() as client:
        list_response = await client.get(f"{admin_server}/ingestion-devices", headers=support_headers)
        create_response = await client.post(
            f"{admin_server}/ingestion-devices", headers=support_headers,
            json={"device_id": _next_device_id(), "device_name": "Should be forbidden"},
        )
    assert list_response.status_code == 200
    assert any(d["id"] == device["id"] for d in list_response.json())
    assert create_response.status_code == 403


async def test_list_shows_awaiting_first_ingestion_before_any_success(admin_server, pool):
    device = await _register_device(pool)
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/ingestion-devices", headers=headers)
    row = next(d for d in response.json() if d["id"] == device["id"])
    assert row["health"] == "awaiting_first_ingestion"


async def test_revoke_then_reactivate_via_admin_api(admin_server, pool):
    device = await _register_device(pool)
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        revoke = await client.patch(
            f"{admin_server}/ingestion-devices/{device['id']}", headers=headers, json={"status": "revoked"}
        )
        reactivate = await client.patch(
            f"{admin_server}/ingestion-devices/{device['id']}", headers=headers, json={"status": "active"}
        )
    assert revoke.status_code == 200
    assert reactivate.status_code == 200
