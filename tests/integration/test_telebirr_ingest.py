"""Integration tests for services/payments/telebirr_ingest.py -- real
Postgres, and the real MacroDroid HTTP route (services/payments/app.py)
exercised as genuine HTTP requests, the same discipline test_payments_app.py
already uses for the Chapa webhook.

payment_evidence.external_reference is UNIQUE at the database level and
this suite has no per-test transaction rollback (same as every other
integration test file here) -- _next_reference() mints a fresh reference
per test the same way conftest.py's next_telegram_id() does, so tests
never collide with each other's rows.
"""

import itertools
import random

import httpx

from services.payments.telebirr_ingest import (
    STATUS_CONFLICTING_DUPLICATE,
    STATUS_DUPLICATE,
    STATUS_INGESTED_AVAILABLE,
    STATUS_INGESTED_REJECTED,
    ingest_sms_evidence,
)

_ref_counter = itertools.count(random.randint(10**7, 2 * 10**7))


def _next_reference() -> str:
    return f"DI{next(_ref_counter):08d}"


def _build_sms(
    reference: str,
    *,
    amount: str = "10.00",
    payer: str = "DAWIT WERKALEMAHU",
    payer_phone: str = "2519****6294",
    recipient: str = "Nebyu",
) -> str:
    # The exact real Telebirr "money received" template (see services/
    # payments/telebirr_parser.py's own module docstring) -- only the
    # fields under test vary here.
    return (
        f"Dear {recipient} \n"
        f"You have received ETB {amount} from {payer}({payer_phone})  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )


async def _add_recognized_recipient(conn, *, account_name: str = "Nebyu") -> None:
    await conn.execute(
        """
        INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active)
        VALUES ('telebirr', '0911000000', $1, true)
        """,
        account_name,
    )


async def test_ingestion_with_no_recognized_recipient_is_rejected(pool, conn):
    # The user's own product decision: recipient config ships empty, so
    # every real SMS fails closed until an admin configures one -- this is
    # the default state this whole feature ships in. Uses its own
    # dedicated recipient name so it can never accidentally match a row
    # another test in this file inserted.
    reference = _next_reference()
    sms = _build_sms(reference, recipient=f"NoOneConfigured{reference}")
    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device-1")
    assert outcome.status == STATUS_INGESTED_REJECTED
    assert outcome.reason == "recipient_not_recognized"
    assert outcome.external_reference == reference

    row = await conn.fetchrow(
        "SELECT status, amount FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is not None
    assert row["status"] == "rejected"


async def test_ingestion_with_a_recognized_recipient_becomes_available(pool, conn):
    recipient = f"Recognized{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_sms(reference, amount="100.00", payer="Kertina Gizachew", recipient=recipient)

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device-1")
    assert outcome.status == STATUS_INGESTED_AVAILABLE
    assert outcome.reason is None
    assert outcome.external_reference == reference

    row = await conn.fetchrow(
        "SELECT status, amount, payer_name FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is not None
    assert row["status"] == "available"
    assert str(row["amount"]) == "100.00"
    assert row["payer_name"] == "Kertina Gizachew"


async def test_identical_resubmission_is_idempotent_not_a_second_row(pool, conn):
    recipient = f"Idempotent{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_sms(reference, recipient=recipient)

    first = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="device-a")
    second = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="device-a")
    assert first.status == STATUS_INGESTED_AVAILABLE
    assert second.status == STATUS_DUPLICATE
    assert second.evidence_id == first.evidence_id

    count = await conn.fetchval(
        "SELECT count(*) FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert count == 1


async def test_same_reference_different_text_is_flagged_disputed_not_overwritten(pool, conn):
    recipient = f"Disputed{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    original = _build_sms(reference, amount="10.00", recipient=recipient)

    first = await ingest_sms_evidence(pool, raw_sms=original, source="macrodroid", source_ref="device-a")
    assert first.status == STATUS_INGESTED_AVAILABLE

    # Same reference, but the amount is different -- a forged/tampered
    # resubmission attempt, not a legitimate retry.
    tampered = _build_sms(reference, amount="999.00", recipient=recipient)
    second = await ingest_sms_evidence(pool, raw_sms=tampered, source="macrodroid", source_ref="device-b")
    assert second.status == STATUS_CONFLICTING_DUPLICATE
    assert second.evidence_id == first.evidence_id

    row = await conn.fetchrow("SELECT status, amount FROM payment_evidence WHERE id = $1", first.evidence_id)
    assert row is not None
    assert row["status"] == "disputed"
    # The ORIGINAL amount is preserved -- the tampered resubmission never
    # overwrote it, it only flagged the row for human review.
    assert str(row["amount"]) == "10.00"


async def test_unparseable_message_persists_no_row(pool, conn):
    before = await conn.fetchval("SELECT count(*) FROM payment_evidence")
    outcome = await ingest_sms_evidence(
        pool, raw_sms="Your OTP is 483920.", source="macrodroid", source_ref="device-a"
    )
    assert outcome.status == "unparseable"
    assert outcome.evidence_id is None

    after = await conn.fetchval("SELECT count(*) FROM payment_evidence")
    assert after == before


# --- real HTTP: the MacroDroid ingestion route -----------------------------


async def test_macrodroid_route_rejects_missing_bearer_token(payments_server):
    sms = _build_sms(_next_reference())
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/internal/telebirr/ingest",
            json={"raw_sms": sms, "device_id": "phone-1"},
        )
    assert response.status_code == 401


async def test_macrodroid_route_rejects_wrong_bearer_token(payments_server):
    sms = _build_sms(_next_reference())
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/internal/telebirr/ingest",
            json={"raw_sms": sms, "device_id": "phone-1"},
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert response.status_code == 401


async def test_macrodroid_route_ingests_a_real_sms_over_real_http(payments_server, conn):
    recipient = f"HttpRoute{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_sms(reference, amount="100.00", payer="Kertina Gizachew", recipient=recipient)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/internal/telebirr/ingest",
            json={"raw_sms": sms, "device_id": "phone-1"},
            headers={"Authorization": "Bearer test-macrodroid-token-for-suite"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == STATUS_INGESTED_AVAILABLE
    assert body["external_reference"] == reference

    row = await conn.fetchrow(
        "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is not None
    assert row["status"] == "available"
