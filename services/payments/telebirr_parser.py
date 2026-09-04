"""Telebirr SMS parser (CTO directive sections 94/95/121) -- pure, no I/O.

Built and tested against real Telebirr "money received" SMS text (two
samples the user provided 2026-09-04), not a guessed format. Ethio
Telecom's own template, verbatim (line breaks preserved, names/numbers
are the real sample's own):

    Dear Nebyu
    You have received ETB 10.00 from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. Your transaction number is DI41FHSD4J. Your current E-Money Account balance is ETB 252.12.
    Thank you for using telebirr
    Ethio telecom

Only this "money received" P2P-transfer template is confirmed real. A
fee/VAT-bearing template and an Amharic-language template have NOT been
seen in a real sample -- parse_telebirr_sms() fails closed (returns
ParseFailure) on anything that doesn't match this confirmed shape, per
spec section 94: "if required payment evidence cannot be parsed
confidently, do not make the payment available... false acceptance is
more dangerous than false rejection." Extending to a new real template
means adding a new alternative extraction path here once a real sample of
it exists -- never a guess at what one might look like.

The greeting line ("Dear Nebyu") names the ACCOUNT HOLDER WE control, not
the payer -- Telebirr's own "received" template never restates the
recipient's own phone number, so this greeting name is the only
recipient-identity signal available at all. Matching it against a
configured, trusted name is the caller's job (telebirr_ingest.py), not
this module's -- this module only extracts it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

# Bumped whenever extraction logic changes -- stored per payment_evidence
# row (see migrations/versions/9c1f4d7a2b3e) so a later re-parse of
# historical evidence is never ambiguous about which logic produced it.
PARSER_VERSION = "telebirr-received-v1"

# Same tz value/library services/admin/queries.py's own ETHIOPIA_TZ
# already uses -- not imported from there to avoid a payments-service ->
# admin-service dependency; this codebase already repeats the
# 'Africa/Addis_Ababa' literal per-file (responsible_gaming.py, admin/
# queries.py, payments/deposits.py all do), so a local constant here
# matches the established convention rather than fighting it.
_ADDIS_TZ = ZoneInfo("Africa/Addis_Ababa")

# "You have received ETB 10.00 from ..." -- anchored on "received" rather
# than a bare "ETB <number>" search, because the same message also states
# the POST-transaction account balance as "... balance is ETB 252.12" and
# a position-only (first match wins) search would be one template
# reordering away from silently reading the wrong number as the amount.
_AMOUNT_RE = re.compile(r"received\s+ETB\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)

# "from DAWIT WERKALEMAHU(2519****6294)" / "from Kertina Gizachew(...)" --
# real samples show both all-caps and mixed-case names, so casing is never
# assumed. Name is free text up to the opening paren; a hyphen/apostrophe/
# period is allowed since a real Ethiopian name could plausibly include
# one (e.g. an initial), even though neither real sample does. Phone is a
# fixed masked shape: 4 digits, 4 literal asterisks, 4 digits.
_SENDER_RE = re.compile(
    r"from\s+([A-Za-z][A-Za-z.\-' ]*?)\s*\((\d{4}\*{4}\d{4})\)",
    re.IGNORECASE,
)

# "on 04/09/2026 10:27:23" -- DD/MM/YYYY, confirmed against the real
# sample's own send date (04/09/2026 = 4 September 2026), 24h clock.
_DATETIME_RE = re.compile(r"\bon\s+([0-9]{2})/([0-9]{2})/([0-9]{4})\s+([0-9]{2}):([0-9]{2}):([0-9]{2})")

# "Your transaction number is DI41FHSD4J." -- Telebirr's own reference,
# the canonical identity (sections 93/121). Length is bounded (6-20), not
# pinned to exactly 10, in case it varies by transaction type -- both real
# samples are 10 uppercase alphanumeric characters.
_REFERENCE_RE = re.compile(r"transaction number is\s+([A-Za-z0-9]{6,20})", re.IGNORECASE)

# "Dear Nebyu" -- see module docstring on why this is the only recipient
# signal this template carries. Stops at end-of-line since _normalize_
# whitespace() below processes one line at a time.
_GREETING_RE = re.compile(r"^\s*Dear\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class ParsedEvidence:
    external_reference: str
    raw_reference: str
    amount: Decimal
    payer_name: str
    payer_phone: str
    recipient_name: str
    transaction_at: datetime


@dataclass(frozen=True)
class ParseFailure:
    reason: str


def _normalize_whitespace(raw: str) -> str:
    """Collapses runs of any Unicode whitespace (regular spaces, non-
    breaking spaces, tabs, the double-space the real samples already show
    before "on") to a single space -- one line at a time, so a genuine
    newline between fields (the greeting line vs. the body) still
    terminates a line-anchored match instead of being swallowed. NFKC
    first so a lookalike Unicode character (e.g. a fullwidth digit) can't
    slip a required field past the regexes below unnoticed.
    """
    lines = unicodedata.normalize("NFKC", raw).splitlines()
    return "\n".join(re.sub(r"\s+", " ", line, flags=re.UNICODE).strip() for line in lines)


def parse_telebirr_sms(raw: str) -> ParsedEvidence | ParseFailure:
    text = _normalize_whitespace(raw)

    amount_match = _AMOUNT_RE.search(text)
    if amount_match is None:
        return ParseFailure("amount_not_found")
    try:
        amount = Decimal(amount_match.group(1))
    except InvalidOperation:
        return ParseFailure("amount_not_parseable")
    if amount <= 0:
        return ParseFailure("amount_not_positive")

    sender_match = _SENDER_RE.search(text)
    if sender_match is None:
        return ParseFailure("sender_not_found")
    payer_name = sender_match.group(1).strip()
    if not payer_name:
        return ParseFailure("sender_name_empty")
    payer_phone = sender_match.group(2)

    reference_match = _REFERENCE_RE.search(text)
    if reference_match is None:
        return ParseFailure("reference_not_found")
    raw_reference = reference_match.group(1)
    external_reference = raw_reference.strip().upper()

    greeting_match = _GREETING_RE.search(text)
    if greeting_match is None:
        return ParseFailure("recipient_greeting_not_found")
    recipient_name = greeting_match.group(1).strip()
    if not recipient_name:
        return ParseFailure("recipient_greeting_empty")

    datetime_match = _DATETIME_RE.search(text)
    if datetime_match is None:
        return ParseFailure("transaction_datetime_not_found")
    day, month, year, hour, minute, second = (int(g) for g in datetime_match.groups())
    try:
        transaction_at = datetime(year, month, day, hour, minute, second, tzinfo=_ADDIS_TZ)
    except ValueError:
        return ParseFailure("transaction_datetime_invalid")

    return ParsedEvidence(
        external_reference=external_reference,
        raw_reference=raw_reference,
        amount=amount,
        payer_name=payer_name,
        payer_phone=payer_phone,
        recipient_name=recipient_name,
        transaction_at=transaction_at,
    )
