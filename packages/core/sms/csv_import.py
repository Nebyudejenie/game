"""CSV bulk-SMS import: parse, normalize, validate a CSV upload into a
real, inspectable `sms_import_jobs`/`sms_import_rows` record -- then, once
an operator explicitly confirms, turn its valid rows into ordinary
`sms_campaigns`/`sms_messages` rows through the exact same insert shape
`campaigns.start_campaign()` already uses. This module never creates a
second queue, a second message table, or a second suppression check --
see `campaigns.py`'s own `validate_campaign()`/`start_campaign()`, which
branch on `Campaign.import_job_id` to call back into this module instead
of the audience-filter path, and nowhere else.

Pipeline (matches the production directive's own explicit ordering):

    upload bytes -> decode/parse -> normalize -> validate -> deduplicate
    -> suppression check -> persist sms_import_rows (preview) -> operator
    confirms -> create sms_campaigns row -> start_campaign() resolves
    recipients from sms_import_rows -> sms_messages -> existing pull
    protocol takes over unchanged.

Two supported CSV shapes (the directive's own "minimum format" and its
template-driven alternative):

  - PHONE_MESSAGE: `phone_number,message` -- every row supplies its own
    exact message text. No template/body_override needed on the campaign
    at all (see the campaigns_check CHECK constraint's own
    `import_job_id IS NOT NULL` branch, migration 198d7fa10f43).
  - PHONE_ONLY: `phone_number` -- every row is sent through a shared
    template or body_override the campaign is created with, exactly like
    an audience-filter campaign's own per-recipient rendering.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Iterator, Literal

from packages.core.ledger import AsyncpgConnection
from packages.core.phone import normalize_ethiopian_phone
from packages.core.sms import templates

ImportFormat = Literal["phone_message", "phone_only"]
RowStatus = Literal["valid", "invalid", "duplicate", "suppressed"]

# --- limits (production directive Section 20: "do not invent arbitrary
# limits without documenting them") ------------------------------------

# A hard ceiling on the raw upload, checked before any decoding/parsing
# is attempted. 10 MB comfortably covers tens of thousands of short CSV
# rows; a genuinely larger single file needs the background-job/
# resumable-progress tier the directive's Section 10 describes for
# 1,000,000+ rows, which this pass does not build or claim to support --
# see docs/SMS_CONTROL_PLANE.md's "what's deliberately not built yet"
# for the honest scope line.
MAX_CSV_SIZE_BYTES = 10 * 1024 * 1024

# A hard ceiling on row count, independent of byte size -- a
# maliciously short-line file could otherwise pack far more rows into
# the same byte budget than a real campaign ever would. Proven at this
# exact size by test_sms_csv_import.py's own load test; a real need for
# more is a real signal to build the background-job tier next, not to
# quietly raise this number.
MAX_CSV_ROWS = 50_000

# How many rows this module ever holds/writes per round trip -- bounds
# both memory and any single transaction's lock duration regardless of
# the CSV's total size, without needing a background worker at this
# scale tier.
IMPORT_BATCH_SIZE = 500

# A generous cap on a phone_message row's own message length, in GSM-7
# segments (packages/core/sms/templates.py::count_segments) -- long
# enough for a real multi-part promotional message, short enough that
# one CSV row can never turn into an unbounded number of billed SMS
# segments by accident or abuse.
MAX_MESSAGE_SEGMENTS = 10

_REQUIRED_COLUMNS: dict[ImportFormat, frozenset[str]] = {
    "phone_message": frozenset({"phone_number", "message"}),
    "phone_only": frozenset({"phone_number"}),
}


class CsvImportError(Exception):
    """A file-level problem (wrong columns, unreadable encoding, too
    large, too many rows) -- rejected before a single row is persisted,
    distinct from a *row*-level validation failure (which is recorded,
    not raised, so the operator sees it in the preview's error table).
    """


@dataclass(frozen=True)
class ParsedRow:
    row_number: int  # 1-based, matching the CSV's own data rows (header excluded)
    raw_phone: str
    normalized_phone: str | None
    message_body: str | None
    status: RowStatus
    error_reason: str | None


def _decode_csv_text(raw_bytes: bytes) -> str:
    if len(raw_bytes) > MAX_CSV_SIZE_BYTES:
        raise CsvImportError(
            f"file is {len(raw_bytes):,} bytes, over the {MAX_CSV_SIZE_BYTES:,} byte limit"
        )
    try:
        # utf-8-sig transparently strips a BOM if present (Excel's own
        # default CSV export on Windows writes one) -- without this, the
        # very first header cell silently fails to match "phone_number".
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError(f"file is not valid UTF-8 text: {exc}") from exc


@dataclass(frozen=True)
class _ParsedHeader:
    reader: csv.DictReader[str]
    unsupported_columns: list[str]


def _parse_header(text: str, *, format: ImportFormat) -> _ParsedHeader:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvImportError("file is empty -- no header row found")
    # Column names are matched case-insensitively (a header of
    # "Phone_Number" or "PHONE_NUMBER" -- a real, common spreadsheet
    # -export variation -- must still match "phone_number").
    actual_columns = {name.strip().lower() for name in reader.fieldnames if name}
    required = _REQUIRED_COLUMNS[format]
    missing = required - actual_columns
    if missing:
        raise CsvImportError(
            f"missing required column(s) for {format!r} format: {sorted(missing)} "
            f"(found: {sorted(actual_columns)})"
        )
    return _ParsedHeader(reader=reader, unsupported_columns=sorted(actual_columns - required))


def _iter_validated_rows(
    reader: csv.DictReader[str],
    *,
    format: ImportFormat,
    existing_suppressed_phones: frozenset[str],
) -> Iterator[ParsedRow]:
    """A streaming generator over an already header-validated reader --
    callers write each yielded row to sms_import_rows in
    IMPORT_BATCH_SIZE-sized batches (see create_import_job below) rather
    than ever holding every row's ParsedRow in memory at once. A problem
    with one specific row is always a yielded 'invalid' ParsedRow, never
    an exception -- one bad row must never abort the whole import; the
    one exception is exceeding MAX_CSV_ROWS, a file-level problem no
    per-row status can express.
    """
    seen_phones: set[str] = set()
    row_number = 0
    for raw_row in reader:
        row_number += 1
        if row_number > MAX_CSV_ROWS:
            raise CsvImportError(
                f"file has more than {MAX_CSV_ROWS:,} data rows -- split it into smaller imports"
            )
        normalized_row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        raw_phone = normalized_row.get("phone_number", "")
        message_body = normalized_row.get("message") if format == "phone_message" else None

        if not raw_phone:
            yield ParsedRow(row_number, raw_phone, None, message_body, "invalid", "blank/missing phone number")
            continue

        normalized = normalize_ethiopian_phone(raw_phone)
        if normalized is None:
            yield ParsedRow(row_number, raw_phone, None, message_body, "invalid", "not a recognizable Ethiopian phone number")
            continue

        if format == "phone_message":
            if not message_body:
                yield ParsedRow(row_number, raw_phone, normalized, message_body, "invalid", "blank/missing message")
                continue
            segments = templates.count_segments(message_body)
            if segments > MAX_MESSAGE_SEGMENTS:
                yield ParsedRow(
                    row_number, raw_phone, normalized, message_body, "invalid",
                    f"message is {segments} SMS segments, over the {MAX_MESSAGE_SEGMENTS}-segment limit",
                )
                continue

        if normalized in seen_phones:
            yield ParsedRow(row_number, raw_phone, normalized, message_body, "duplicate", "duplicate phone number within this file")
            continue
        seen_phones.add(normalized)

        if normalized in existing_suppressed_phones:
            yield ParsedRow(row_number, raw_phone, normalized, message_body, "suppressed", "phone number is on the suppression list")
            continue

        yield ParsedRow(row_number, raw_phone, normalized, message_body, "valid", None)

    if row_number == 0:
        raise CsvImportError("file has a header row but no data rows")


def validate_csv_text(
    raw_bytes: bytes, *, format: ImportFormat, suppressed_phones: frozenset[str] = frozenset()
) -> Iterator[ParsedRow]:
    """A DB-free entry point over parsing/normalization/validation alone
    -- create_import_job below is the real, DB-backed path that persists
    the result; this exists so that logic can be unit-tested in
    isolation, without a real suppressions table to query against.
    """
    text = _decode_csv_text(raw_bytes)
    header = _parse_header(text, format=format)
    return _iter_validated_rows(header.reader, format=format, existing_suppressed_phones=suppressed_phones)


@dataclass(frozen=True)
class ImportSummary:
    job_id: int
    format: ImportFormat
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    suppressed_rows: int
    unsupported_columns: list[str]


async def _load_suppressed_phones(conn: AsyncpgConnection, *, tenant_id: int) -> frozenset[str]:
    """Loaded once per import, not per row -- suppression lists are
    admin-curated and expected to stay small relative to a bulk-SMS
    audience (thousands, not millions); if that assumption is ever
    wrong, this is the one query to revisit, not every row's own check.
    """
    rows = await conn.fetch("SELECT phone_e164 FROM sms_suppressions WHERE tenant_id = $1", tenant_id)
    return frozenset(r["phone_e164"] for r in rows)


async def create_import_job(
    conn: AsyncpgConnection,
    *,
    tenant_id: int,
    admin_id: int,
    raw_bytes: bytes,
    original_filename: str | None,
    format: ImportFormat,
    idempotency_key: str,
) -> ImportSummary:
    """Parses, validates, and persists one CSV upload. Idempotent on
    (tenant_id, idempotency_key): a retried upload with the identical
    key (double-click, browser retry, page refresh re-submitting a
    cached form) returns the already-computed summary for the existing
    job instead of re-parsing and re-persisting the file a second time
    -- Section 11's own "never accidentally create two full campaigns
    because the operator refreshed the browser" applies just as much to
    the *import* step as to campaign creation itself.
    """
    existing = await conn.fetchrow(
        "SELECT id FROM sms_import_jobs WHERE tenant_id = $1 AND idempotency_key = $2",
        tenant_id, idempotency_key,
    )
    if existing is not None:
        return await get_import_summary(conn, job_id=existing["id"], tenant_id=tenant_id)

    # Header-level validation happens eagerly, before any DB row exists
    # for this upload -- a CSV with the wrong columns entirely (picked
    # the wrong format, uploaded the wrong file) never creates a job row
    # at all, rather than creating one this function would immediately
    # have to mark 'failed'.
    text = _decode_csv_text(raw_bytes)
    header = _parse_header(text, format=format)

    suppressed = await _load_suppressed_phones(conn, tenant_id=tenant_id)
    reader = _iter_validated_rows(header.reader, format=format, existing_suppressed_phones=suppressed)

    job_row = await conn.fetchrow(
        """
        INSERT INTO sms_import_jobs (tenant_id, format, original_filename, idempotency_key, created_by_admin_id)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        tenant_id, format, original_filename, idempotency_key, admin_id,
    )
    assert job_row is not None
    job_id = job_row["id"]

    counts = {"valid": 0, "invalid": 0, "duplicate": 0, "suppressed": 0}
    try:
        batch: list[ParsedRow] = []

        async def flush() -> None:
            if not batch:
                return
            await conn.executemany(
                """
                INSERT INTO sms_import_rows
                    (import_job_id, row_number, raw_phone, normalized_phone, message_body, status, error_reason)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                [
                    (job_id, r.row_number, r.raw_phone, r.normalized_phone, r.message_body, r.status, r.error_reason)
                    for r in batch
                ],
            )
            batch.clear()

        for parsed in reader:
            counts[parsed.status] += 1
            batch.append(parsed)
            if len(batch) >= IMPORT_BATCH_SIZE:
                await flush()
        await flush()
    except CsvImportError as exc:
        await conn.execute(
            "UPDATE sms_import_jobs SET status = 'failed', error_summary = $2, completed_at = now() WHERE id = $1",
            job_id, str(exc),
        )
        raise

    total = sum(counts.values())
    await conn.execute(
        """
        UPDATE sms_import_jobs
        SET status = 'completed', total_rows = $2, valid_rows = $3, invalid_rows = $4,
            duplicate_rows = $5, suppressed_rows = $6, completed_at = now()
        WHERE id = $1
        """,
        job_id, total, counts["valid"], counts["invalid"], counts["duplicate"], counts["suppressed"],
    )
    return ImportSummary(
        job_id=job_id, format=format, total_rows=total,
        valid_rows=counts["valid"], invalid_rows=counts["invalid"],
        duplicate_rows=counts["duplicate"], suppressed_rows=counts["suppressed"],
        unsupported_columns=header.unsupported_columns,
    )


async def get_import_summary(conn: AsyncpgConnection, *, job_id: int, tenant_id: int | None = None) -> ImportSummary:
    """tenant_id is optional only for internal callers that already
    resolved/locked the campaign row it's reached through (campaigns.py's
    own _resolvable_import_recipient_count and _enqueue_import_messages,
    which already operate on a Campaign whose tenant_id was itself
    fetched from the database) -- every admin-facing route (services/sms/
    app.py) must always pass the caller's real tenant_id, or this raises
    exactly like a real cross-tenant lookup should: 'no such import job'
    (Section 19's "cross-tenant access, IDOR" -- a job id guessed or
    enumerated from another tenant must 404, not 200 with someone else's
    data).
    """
    if tenant_id is not None:
        row = await conn.fetchrow(
            """
            SELECT id, format, total_rows, valid_rows, invalid_rows, duplicate_rows, suppressed_rows
            FROM sms_import_jobs WHERE id = $1 AND tenant_id = $2
            """,
            job_id, tenant_id,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT id, format, total_rows, valid_rows, invalid_rows, duplicate_rows, suppressed_rows
            FROM sms_import_jobs WHERE id = $1
            """,
            job_id,
        )
    if row is None:
        raise CsvImportError(f"no such import job: {job_id}")
    return ImportSummary(
        job_id=row["id"], format=row["format"], total_rows=row["total_rows"],
        valid_rows=row["valid_rows"], invalid_rows=row["invalid_rows"],
        duplicate_rows=row["duplicate_rows"], suppressed_rows=row["suppressed_rows"],
        unsupported_columns=[],
    )


async def list_import_rows(
    conn: AsyncpgConnection,
    *,
    job_id: int,
    tenant_id: int,
    status: RowStatus | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, object]]:
    """The preview screen's own error table -- paginated, since a large
    import can legitimately have thousands of invalid rows and the UI
    must never try to render all of them onto one page at once.
    tenant_id is required (unlike get_import_summary's internal-caller
    exception above) because this has no internal, already-tenant-scoped
    caller today -- every caller is the admin-facing route, which must
    always prove the job it's reading actually belongs to its own
    tenant, not just that some row with this id exists.
    """
    job = await get_import_summary(conn, job_id=job_id, tenant_id=tenant_id)  # raises if cross-tenant/missing
    if status is not None:
        rows = await conn.fetch(
            """
            SELECT row_number, raw_phone, normalized_phone, message_body, status, error_reason
            FROM sms_import_rows WHERE import_job_id = $1 AND status = $2
            ORDER BY row_number LIMIT $3 OFFSET $4
            """,
            job.job_id, status, limit, offset,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT row_number, raw_phone, normalized_phone, message_body, status, error_reason
            FROM sms_import_rows WHERE import_job_id = $1
            ORDER BY row_number LIMIT $2 OFFSET $3
            """,
            job.job_id, limit, offset,
        )
    return [dict(r) for r in rows]


async def resolve_import_recipients_batch(
    conn: AsyncpgConnection, *, job_id: int, after_row_number: int, limit: int = IMPORT_BATCH_SIZE
) -> list[ParsedRow]:
    """One page of this import's *valid* rows, ordered by row_number --
    the CSV-sourced equivalent of audience.resolve_recipients(), read in
    bounded pages so start_campaign() (packages/core/sms/campaigns.py)
    never holds an entire large import's rows in memory at once, however
    many thousands of rows it has.
    """
    rows = await conn.fetch(
        """
        SELECT row_number, raw_phone, normalized_phone, message_body
        FROM sms_import_rows
        WHERE import_job_id = $1 AND status = 'valid' AND row_number > $2
        ORDER BY row_number LIMIT $3
        """,
        job_id, after_row_number, limit,
    )
    return [
        ParsedRow(
            row_number=r["row_number"], raw_phone=r["raw_phone"], normalized_phone=r["normalized_phone"],
            message_body=r["message_body"], status="valid", error_reason=None,
        )
        for r in rows
    ]
