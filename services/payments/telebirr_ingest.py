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


def _name_matches_whole_word(haystack: str, needle: str) -> bool:
    """True if `needle` appears in `haystack` as a whole word (word-
    boundary matched), not merely as a substring -- e.g. "nebyu" is found
    in "nebyu dejenie" but not in "nebyuworks". Both arguments are
    expected already lower-cased/trimmed by the caller.
    """
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


async def _find_matching_recipient(
    conn: AsyncpgConnection, *, recipient_name: str, recipient_phone: str | None, at: datetime
) -> bool:
    """Fail-closed recipient check (sections 92/94/13).

    "received" template (recipient_phone is always None -- it never
    restates the recipient's own number): name match is required,
    case/whitespace-insensitively and EXACTLY against manual_payment_
    destinations.account_name -- a fuzzy/contains match would let an
    unrelated similarly-named recipient slip through (section 94's own
    false-acceptance risk), and there is no second signal to fall back on
    if the name is wrong.

    "transferred" template (always carries a recipient_phone): Telebirr's
    own template states the PAYER's full registered name for the "to
    {name}" recipient field (e.g. "Nebyu Dejenie"), which routinely
    differs from the shorter nickname an admin realistically configures
    as account_name (e.g. "Nebyu", matching the "received" template's own
    shorter "Dear {name}" greeting for the same real person) -- a real,
    genuine payment was rejected live over exactly this naming difference
    on 2026-09-14, despite the phone matching exactly. account_name is
    accepted here as either an exact match OR a whole-word match within
    the SMS's full name (never a bare substring check, which could
    false-match part of an unrelated longer word) -- but the phone must
    STILL also match the configured destination's account_ref (masked via
    _mask_ethiopian_phone above) regardless of which way the name
    matched: this keeps "transferred" a genuine two-factor check
    (name-ish + exact phone), it just no longer requires the name half to
    be byte-identical to Telebirr's own full-name rendering.
    """
    rows = await conn.fetch(
        """
        SELECT account_ref, account_name FROM manual_payment_destinations
        WHERE method_kind = 'telebirr' AND is_active
          AND (effective_from IS NULL OR effective_from <= $1)
          AND (effective_until IS NULL OR effective_until >= $1)
        """,
        at,
    )
    normalized_recipient_name = recipient_name.strip().lower()
    for row in rows:
        configured_name = row["account_name"].strip().lower()
        if recipient_phone is None:
            if configured_name == normalized_recipient_name:
                return True
            continue
        name_matches = configured_name == normalized_recipient_name or _name_matches_whole_word(
            normalized_recipient_name, configured_name
        )
        phone_matches = _mask_ethiopian_phone(row["account_ref"]) == recipient_phone
        if name_matches and phone_matches:
            return True
    return False


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

            # Two real concurrent submissions of the byte-identical message
            # (a MacroDroid retry racing the original request, or the same
            # SMS arriving through two different ingestion sources at once)
            # can both reach this INSERT before either commits. ON CONFLICT
            # only names external_reference, but evidence_hash carries its
            # own separate UNIQUE constraint -- Postgres raises a real,
            # unhandled UniqueViolationError on THAT constraint before it
            # ever gets to apply the named conflict target, so this must be
            # caught too, not just the named-constraint path. Isolated in
            # its own nested transaction (asyncpg promotes this to a real
            # SAVEPOINT since a transaction is already open) so the failed
            # INSERT can be rolled back without aborting the whole
            # surrounding transaction the fallback lookup below still needs
            # to run in.
            try:
                async with conn.transaction():
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
            except asyncpg.UniqueViolationError:
                # A real SHA-256 collision on evidence_hash can only mean
                # the exact same message was already inserted by whichever
                # concurrent request won the race -- which necessarily
                # carries the exact same external_reference, so the
                # fallback lookup below (keyed on external_reference) finds
                # it correctly regardless of which unique constraint fired.
                inserted = None

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
