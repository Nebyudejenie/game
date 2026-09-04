"""Telebirr SMS-evidence reconciliation (CTO directive sections 124-127) --
mirrors services/payments/deposits.py's own reconcile()/
run_provider_reconciliation() shape: a pure query-based check plus a real,
wired periodic sweep (services/payments/payout_worker.py runs it hourly,
the same process/timer run_provider_reconciliation() already uses).

Section 126's "one transaction_reference -> more than one payment record"
half is enforced by a database UNIQUE constraint on payment_evidence.
external_reference (migrations/versions/9c1f4d7a2b3e_telebirr_sms_
evidence.py) -- structurally impossible, not something a runtime check
also needs to verify. The other half -- one payments row credited by more
than one evidence row, or a payments row with no evidence behind it at
all -- has no equivalent database constraint (a payment_evidence row's own
payment_id FK doesn't stop two different rows from pointing at the same
payment), so that is what find_evidence_source_mismatches() below
actually checks, even though the only code path that writes either column
(services/payments/telebirr_redemption.py's redeem_evidence(), one
transaction, one evidence row, one payments row) can't currently produce
a mismatch. Section 126's own ask is a standing detection query, not a
one-time proof that today's code is correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import structlog

from packages.core import metrics

logger = structlog.get_logger()


@dataclass(frozen=True)
class EvidenceSourceMismatch:
    payment_id: int
    our_ref: str
    evidence_count: int


async def find_evidence_source_mismatches(pool: asyncpg.Pool) -> list[EvidenceSourceMismatch]:
    """Every telebirr_sms payments row must be linked from EXACTLY one
    payment_evidence row: zero means a wallet credit with no evidence
    behind it (section 125, "no payment without source"); more than one
    means the same payment was referenced by multiple evidence rows (
    section 126, "no payment credited twice").
    """
    rows = await pool.fetch(
        """
        SELECT p.id, p.our_ref,
               (SELECT count(*) FROM payment_evidence e WHERE e.payment_id = p.id) AS evidence_count
        FROM payments p
        WHERE p.provider = 'telebirr_sms'
          AND (SELECT count(*) FROM payment_evidence e WHERE e.payment_id = p.id) != 1
        """
    )
    return [
        EvidenceSourceMismatch(payment_id=r["id"], our_ref=r["our_ref"], evidence_count=r["evidence_count"])
        for r in rows
    ]


@dataclass(frozen=True)
class EvidenceStatusBreakdown:
    status: str
    count: int
    total_amount: Decimal


async def evidence_status_breakdown(pool: asyncpg.Pool) -> list[EvidenceStatusBreakdown]:
    """Section 127, "no payment lost": every imported payment's lifecycle
    must be explainable as a count+sum per status. Exposed to the admin
    console as a report and to Prometheus as a gauge per status (see
    run_telebirr_reconciliation() below) -- not itself a "mismatch," just
    the standing account of where every row currently is.
    """
    rows = await pool.fetch(
        "SELECT status, count(*) AS count, COALESCE(sum(amount), 0) AS total_amount "
        "FROM payment_evidence GROUP BY status ORDER BY status"
    )
    return [
        EvidenceStatusBreakdown(status=r["status"], count=r["count"], total_amount=r["total_amount"])
        for r in rows
    ]


async def run_telebirr_reconciliation(pool: asyncpg.Pool) -> list[EvidenceSourceMismatch]:
    mismatches = await find_evidence_source_mismatches(pool)
    metrics.telebirr_evidence_reconciliation_mismatch_count.set(len(mismatches))
    if mismatches:
        logger.error(
            "telebirr_evidence_reconciliation_mismatch",
            mismatch_count=len(mismatches),
            mismatches=[
                {"payment_id": m.payment_id, "our_ref": m.our_ref, "evidence_count": m.evidence_count}
                for m in mismatches
            ],
        )

    breakdown = await evidence_status_breakdown(pool)
    for row in breakdown:
        metrics.telebirr_evidence_by_status.labels(status=row.status).set(row.count)

    return mismatches
