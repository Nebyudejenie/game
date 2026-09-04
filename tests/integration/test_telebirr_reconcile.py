"""Tests for services/payments/telebirr_reconcile.py -- the detection
queries CTO directive sections 125-127 explicitly ask for. The "credited
twice" / "no source" scenarios can't currently happen through the real
application code (redeem_evidence() is the only writer of both payments.
provider='telebirr_sms' rows and payment_evidence.payment_id, one
transaction, one of each) -- these tests construct the anomalous state
directly to prove the detection query itself works, the same reasoning
services/payments/deposits.py's own reconcile() tests use pure inputs
rather than only ever exercising it through a real webhook.
"""

import itertools
import random
from decimal import Decimal

from services.payments.telebirr_ingest import ingest_sms_evidence
from services.payments.telebirr_reconcile import (
    evidence_status_breakdown,
    find_evidence_source_mismatches,
    run_telebirr_reconciliation,
)
from services.payments.telebirr_redemption import redeem_evidence
from tests.integration.conftest import create_funded_user

_ref_counter = itertools.count(random.randint(5 * 10**7, 6 * 10**7))


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


async def test_a_healthy_redemption_produces_no_mismatch(pool, redis, conn):
    reference = _next_reference()
    recipient = f"Reconcile{reference}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    outcome = await ingest_sms_evidence(
        pool, raw_sms=_build_sms(reference, recipient=recipient), source="macrodroid", source_ref="reconcile-test"
    )
    assert outcome.status == "ingested_available"
    user_id = await create_funded_user(conn, Decimal("0.00"))
    redemption = await redeem_evidence(
        pool, redis, user_id=user_id, reference=reference, daily_cap=Decimal("50000.00")
    )
    assert redemption.code == "PAYMENT_REDEEMED"

    mismatches = await find_evidence_source_mismatches(pool)
    assert not any(m.payment_id == redemption.payment_id for m in mismatches)


async def test_a_payment_with_no_linked_evidence_is_detected(pool, conn):
    # Directly fabricates the "no source" anomaly (section 125) -- a
    # telebirr_sms payments row that redeem_evidence() never actually
    # created, since that's the only real writer and it always links one.
    user_id = await create_funded_user(conn, Decimal("0.00"))
    row = await conn.fetchrow(
        "INSERT INTO payments (user_id, direction, provider, our_ref, amount, status) "
        "VALUES ($1, 'in', 'telebirr_sms', $2, 15.00, 'succeeded') RETURNING id",
        user_id,
        f"DEP-RECONCILE-{_next_reference()}",
    )
    payment_id = row["id"]

    mismatches = await find_evidence_source_mismatches(pool)
    matching = [m for m in mismatches if m.payment_id == payment_id]
    assert len(matching) == 1
    assert matching[0].evidence_count == 0


async def test_a_payment_credited_by_two_evidence_rows_is_detected(pool, conn):
    # Directly fabricates the "credited twice" anomaly (section 126) -- two
    # payment_evidence rows both claiming the same payment_id, which
    # redeem_evidence()'s own row-lock + status-check makes impossible in
    # practice (the second attempt on any reference always sees the first
    # one's committed 'redeemed' status).
    user_id = await create_funded_user(conn, Decimal("0.00"))
    payment_row = await conn.fetchrow(
        "INSERT INTO payments (user_id, direction, provider, our_ref, amount, status) "
        "VALUES ($1, 'in', 'telebirr_sms', $2, 30.00, 'succeeded') RETURNING id",
        user_id,
        f"DEP-RECONCILE-{_next_reference()}",
    )
    payment_id = payment_row["id"]

    for _ in range(2):
        reference = _next_reference()
        await conn.execute(
            """
            INSERT INTO payment_evidence
                (source, source_ref, raw_sms, evidence_hash, external_reference, raw_reference,
                 amount, recipient_name, status, parser_version, redeemed_by_user_id, redeemed_at, payment_id)
            VALUES ('macrodroid', 'test', 'test', $1, $2, $2, 30.00, 'Test', 'redeemed', 'test-v1', $3, now(), $4)
            """,
            f"hash-{reference}",
            reference,
            user_id,
            payment_id,
        )

    mismatches = await find_evidence_source_mismatches(pool)
    matching = [m for m in mismatches if m.payment_id == payment_id]
    assert len(matching) == 1
    assert matching[0].evidence_count == 2


async def test_status_breakdown_accounts_for_every_row(pool, conn):
    reference = _next_reference()
    await ingest_sms_evidence(
        pool, raw_sms=_build_sms(reference, recipient=f"Breakdown{reference}"),
        source="macrodroid", source_ref="reconcile-test",
    )

    breakdown = await evidence_status_breakdown(pool)
    total_counted = sum(row.count for row in breakdown)
    real_total = await conn.fetchval("SELECT count(*) FROM payment_evidence")
    assert total_counted == real_total

    rejected_row = next(row for row in breakdown if row.status == "rejected")
    assert rejected_row.count >= 1


async def test_run_telebirr_reconciliation_sets_the_mismatch_gauge(pool, conn):
    from packages.core import metrics

    user_id = await create_funded_user(conn, Decimal("0.00"))
    await conn.execute(
        "INSERT INTO payments (user_id, direction, provider, our_ref, amount, status) "
        "VALUES ($1, 'in', 'telebirr_sms', $2, 5.00, 'succeeded')",
        user_id,
        f"DEP-RECONCILE-{_next_reference()}",
    )

    mismatches = await run_telebirr_reconciliation(pool)
    assert len(mismatches) >= 1
    assert metrics.telebirr_evidence_reconciliation_mismatch_count._value.get() == len(mismatches)  # noqa: SLF001
