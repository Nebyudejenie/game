"""Telebirr SMS ingestion (CTO directive sections 91/93/114/115/116) -- the
one real pipeline: SOURCE -> PROVIDER ADAPTER -> NORMALIZED PAYMENT INPUT
(telebirr_parser.ParsedEvidence) -> RECIPIENT VALIDATION -> PAYMENT_EVIDENCE
ROW. services/payments/app.py's MacroDroid route and services/bot/
handlers.py's payment-agent handler are both thin adapters (sections
114/115) that only authenticate their own channel and call
ingest_sms_evidence() -- neither contains any parsing or acceptance logic
of its own, so there is exactly one place these rules can ever drift.

Idempotent by construction (section 93): external_reference is the
canonical identity (a real Telebirr reference can never legitimately
repeat), evidence_hash is the extra guard for a byte-identical resubmission
of the exact same message. A resubmission under the SAME reference but a
DIFFERENT message body is never silently accepted as an update -- section
91's ownership rule ("the system must never silently transfer ownership")
applies here to evidence identity itself, not just to who redeems it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

import asyncpg
import structlog

from packages.core import metrics
from packages.core.ledger import AsyncpgConnection
from services.payments.telebirr_parser import PARSER_VERSION, ParseFailure, parse_telebirr_sms

logger = structlog.get_logger()

IngestSource = Literal["macrodroid", "telegram_agent"]
SOURCE_MACRODROID: IngestSource = "macrodroid"
SOURCE_TELEGRAM_AGENT: IngestSource = "telegram_agent"

IngestStatus = Literal[
    "ingested_available",  # parsed clean, recipient matched -> a new AVAILABLE row
    "ingested_rejected",  # parsed, but recipient didn't match (or parse failed) -> a new REJECTED row
    "duplicate",  # exact same message already ingested -- idempotent no-op
    "conflicting_duplicate",  # same reference, different message body -- flagged, not accepted
    "unparseable",  # no reference could be extracted at all -- nothing persisted (see below)
]
# Named so callers (services/bot/handlers.py in particular, whose AST is
# scanned by tests/unit/test_bot_no_hardcoded_strings.py) never need a bare
# string literal to branch on an outcome -- these are internal status
# identifiers, not user-facing text, but the checker can't tell the
# difference between a literal used for that and one used for a message,
# so referencing a Name/Attribute instead of a Constant is what actually
# keeps the check meaningful.
STATUS_INGESTED_AVAILABLE: IngestStatus = "ingested_available"
STATUS_INGESTED_REJECTED: IngestStatus = "ingested_rejected"
STATUS_DUPLICATE: IngestStatus = "duplicate"
STATUS_CONFLICTING_DUPLICATE: IngestStatus = "conflicting_duplicate"
STATUS_UNPARSEABLE: IngestStatus = "unparseable"


@dataclass(frozen=True)
class IngestOutcome:
    status: IngestStatus
    evidence_id: int | None
    external_reference: str | None
    reason: str | None


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mask_ethiopian_phone(raw: str) -> str | None:
    """Reduces an admin-entered phone (any of "0911000000",
    "+251911000000", "251911000000", "911000000") to the exact masked
    shape Telebirr's own SMS uses ("2519****0000"), so a configured
    destination's plain account_ref can be compared against a "transferred"
    template's recipient_phone (telebirr_parser.py) without asking an
    admin to enter the masked form directly. Returns None for anything
    that doesn't reduce to a plausible 9-digit Ethiopian mobile number --
    fail-closed, never a guess at what the real number might be.
    """
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("251") and len(digits) == 12:
        local = digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        local = digits[1:]
    elif len(digits) == 9:
        local = digits
    else:
        return None
    return f"251{local[0]}****{local[-4:]}"


async def _find_matching_recipient(
    conn: AsyncpgConnection, *, recipient_name: str, recipient_phone: str | None, at: datetime
) -> bool:
    """Fail-closed recipient check (sections 92/94/13). Name match is
    always required, case-insensitively and exactly against manual_
    payment_destinations.account_name -- a fuzzy/contains match would let
    an unrelated similarly-named recipient slip through, which is exactly
    the false-acceptance risk section 94 forbids. An admin configuring a
    telebirr destination should set account_name to precisely what
    Telebirr's own SMS calls the account holder (the "Dear {name}"
    greeting for a "received" template, the "to {name}" recipient for a
    "transferred" one -- both resolve to this same field).

    When the parsed evidence also carries a recipient_phone (only the
    "transferred" template ever does -- "received" never restates the
    recipient's own number), that phone must ALSO match the configured
    destination's account_ref (masked via _mask_ethiopian_phone above) --
    a strictly stronger check than name alone, closing the gap where two
    different real accounts might coincidentally share a display name.
    """
    rows = await conn.fetch(
        """
        SELECT account_ref FROM manual_payment_destinations
        WHERE method_kind = 'telebirr' AND is_active
          AND lower(trim(account_name)) = lower(trim($1))
          AND (effective_from IS NULL OR effective_from <= $2)
          AND (effective_until IS NULL OR effective_until >= $2)
        """,
        recipient_name,
        at,
    )
    if not rows:
        return False
    if recipient_phone is None:
        return True
    return any(_mask_ethiopian_phone(row["account_ref"]) == recipient_phone for row in rows)


async def ingest_sms_evidence(
    pool: asyncpg.Pool,
    *,
    raw_sms: str,
    source: IngestSource,
    source_ref: str,
) -> IngestOutcome:
    # A thin wrapper so the outcome metric is incremented exactly once
    # regardless of which of _ingest_sms_evidence_impl's several return
    # points fired, rather than needing a matching increment call kept in
    # sync at each one individually.
    outcome = await _ingest_sms_evidence_impl(
        pool, raw_sms=raw_sms, source=source, source_ref=source_ref
    )
    metrics.telebirr_ingestion_total.labels(outcome=outcome.status).inc()
    return outcome


async def _ingest_sms_evidence_impl(
    pool: asyncpg.Pool,
    *,
    raw_sms: str,
    source: IngestSource,
    source_ref: str,
) -> IngestOutcome:
    parsed = parse_telebirr_sms(raw_sms)
    evidence_hash = _sha256_hex(raw_sms)

    if isinstance(parsed, ParseFailure):
        # No reference could be extracted at all -- there is nothing
        # canonical to dedupe against and nothing an admin could ever
        # search for, so this is logged (the audit trail lives in the
        # structured log, not a DB row) and returned as a real, non-
        # persisted outcome rather than fabricating a placeholder
        # reference to satisfy the schema.
        logger.warning(
            "telebirr_ingest_unparseable",
            source=source,
            source_ref=source_ref,
            reason=parsed.reason,
            evidence_hash=evidence_hash,
        )
        metrics.telebirr_parser_failures_total.labels(reason=parsed.reason).inc()
        return IngestOutcome(
            status=STATUS_UNPARSEABLE, evidence_id=None, external_reference=None, reason=parsed.reason
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            recipient_ok = await _find_matching_recipient(
                conn,
                recipient_name=parsed.recipient_name,
                recipient_phone=parsed.recipient_phone,
                at=parsed.transaction_at,
            )
            initial_status = "available" if recipient_ok else "rejected"
            reject_reason = None if recipient_ok else "recipient_not_recognized"

            inserted = await conn.fetchrow(
                """
                INSERT INTO payment_evidence
                    (source, source_ref, raw_sms, evidence_hash, external_reference, raw_reference,
                     amount, fee, vat, payer_name, payer_phone, recipient_name, recipient_phone,
                     receipt_url, direction, transaction_at, status, reject_reason, parser_version)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
                ON CONFLICT (external_reference) DO NOTHING
                RETURNING id
                """,
                source,
                source_ref,
                raw_sms,
                evidence_hash,
                parsed.external_reference,
                parsed.raw_reference,
                parsed.amount,
                parsed.fee,
                parsed.vat,
                parsed.payer_name,
                parsed.payer_phone,
                parsed.recipient_name,
                parsed.recipient_phone,
                parsed.receipt_url,
                parsed.direction,
                parsed.transaction_at,
                initial_status,
                reject_reason,
                PARSER_VERSION,
            )

            if inserted is not None:
                logger.info(
                    "telebirr_ingest_new_evidence",
                    evidence_id=inserted["id"],
                    external_reference=parsed.external_reference,
                    status=initial_status,
                    source=source,
                )
                return IngestOutcome(
                    status=STATUS_INGESTED_AVAILABLE if recipient_ok else STATUS_INGESTED_REJECTED,
                    evidence_id=inserted["id"],
                    external_reference=parsed.external_reference,
                    reason=reject_reason,
                )

            # A row for this reference already exists -- idempotent
            # ingestion (section 93). Never re-insert, never silently
            # re-evaluate it against config that may have changed since
            # (e.g. a recipient added after the first, rejected attempt) --
            # only an authorized admin resolution (a later phase) may move
            # a rejected/disputed row forward.
            existing = await conn.fetchrow(
                "SELECT id, evidence_hash, status FROM payment_evidence "
                "WHERE external_reference = $1 FOR UPDATE",
                parsed.external_reference,
            )
            assert existing is not None

            if existing["evidence_hash"] == evidence_hash:
                return IngestOutcome(
                    status=STATUS_DUPLICATE,
                    evidence_id=existing["id"],
                    external_reference=parsed.external_reference,
                    reason=None,
                )

            # Same reference, different message body -- never silently
            # overwritten (section 91 applied to evidence identity). A
            # redeemed row stays redeemed regardless (no transition out of
            # it exists); anything else gets flagged disputed so a human
            # has to look at it.
            logger.warning(
                "telebirr_ingest_conflicting_duplicate",
                evidence_id=existing["id"],
                external_reference=parsed.external_reference,
                existing_status=existing["status"],
            )
            if existing["status"] not in ("redeemed", "disputed"):
                await conn.execute(
                    "UPDATE payment_evidence SET status = 'disputed', "
                    "reject_reason = 'conflicting_resubmission', updated_at = now() WHERE id = $1",
                    existing["id"],
                )
            return IngestOutcome(
                status=STATUS_CONFLICTING_DUPLICATE,
                evidence_id=existing["id"],
                external_reference=parsed.external_reference,
                reason="conflicting_resubmission",
            )
