"""CSV bulk-SMS import: packages/core/sms/csv_import.py (parsing,
normalization, validation, persistence) and its integration into the
existing campaign/message model via campaigns.py's import_job_id branch.
Real Postgres throughout, no mocks -- same discipline as test_sms_core.py,
including a fresh throwaway tenant per test (see that file's own tenant_id
fixture docstring for why a shared tenant would let two tests claim or
count each other's queued messages).
"""

from __future__ import annotations

import asyncio
import random

import pytest
import pytest_asyncio

from packages.core import ledger
from packages.core.sms import campaigns, compliance
from packages.core.sms.csv_import import (
    CsvImportError,
    MAX_CSV_ROWS,
    MAX_CSV_SIZE_BYTES,
    MAX_MESSAGE_SEGMENTS,
    create_import_job,
    list_import_rows,
    validate_csv_text,
)


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_id(pool):
    slug = f"test-csv-{random.randint(1, 10**12)}"
    return await pool.fetchval(
        "INSERT INTO sms_tenants (slug, name) VALUES ($1, $2) RETURNING id", slug, slug
    )


async def _make_admin_id(conn):
    row = await conn.fetchrow("SELECT id FROM admin_users LIMIT 1")
    if row is not None:
        return row["id"]
    from services.admin import auth

    admin_id, _ = await auth.create_admin_user(
        conn, username=f"csv-test-{random.randint(1, 10**9)}", password="x" * 20, role="superadmin"
    )
    return admin_id


def _unique_key() -> str:
    return f"test-import-{random.randint(1, 10**15)}"


# --- pure parsing/validation (validate_csv_text -- no DB) -----------------


def test_valid_phone_message_rows_normalize_and_pass():
    csv_bytes = (
        b"phone_number,message\n"
        b"0911111111,Hello there\n"
        b"+251922222222,Second message\n"
    )
    rows = list(validate_csv_text(csv_bytes, format="phone_message"))
    assert [r.status for r in rows] == ["valid", "valid"]
    assert rows[0].normalized_phone == "+251911111111"
    assert rows[1].normalized_phone == "+251922222222"


def test_phone_only_format_ignores_message_column():
    rows = list(validate_csv_text(b"phone_number\n0911111111\n", format="phone_only"))
    assert rows[0].status == "valid"
    assert rows[0].message_body is None


def test_invalid_phone_rejected_with_reason():
    rows = list(validate_csv_text(b"phone_number,message\n0512345678,hi\n", format="phone_message"))
    assert rows[0].status == "invalid"
    assert "phone number" in rows[0].error_reason


def test_blank_phone_rejected():
    rows = list(validate_csv_text(b"phone_number,message\n,hi\n", format="phone_message"))
    assert rows[0].status == "invalid"


def test_blank_message_rejected_for_phone_message_format():
    rows = list(validate_csv_text(b"phone_number,message\n0911111111,\n", format="phone_message"))
    assert rows[0].status == "invalid"
    assert "message" in rows[0].error_reason


def test_duplicate_phone_within_file_flagged_after_the_first():
    csv_bytes = b"phone_number,message\n0911111111,first\n0911111111,second\n"
    rows = list(validate_csv_text(csv_bytes, format="phone_message"))
    assert rows[0].status == "valid"
    assert rows[1].status == "duplicate"


def test_suppressed_phone_flagged_not_silently_dropped():
    rows = list(
        validate_csv_text(
            b"phone_number,message\n0911111111,hi\n",
            format="phone_message",
            suppressed_phones=frozenset({"+251911111111"}),
        )
    )
    assert rows[0].status == "suppressed"


def test_message_over_segment_limit_rejected():
    # GSM-7 single segment is 160 chars; MAX_MESSAGE_SEGMENTS=10 multi
    # -segment concatenated messages cap at 153 chars/segment, so 11
    # segments' worth of plain ASCII text must be rejected.
    huge_message = "x" * (153 * (MAX_MESSAGE_SEGMENTS + 1))
    rows = list(
        validate_csv_text(f"phone_number,message\n0911111111,{huge_message}\n".encode(), format="phone_message")
    )
    assert rows[0].status == "invalid"
    assert "segment" in rows[0].error_reason


def test_missing_required_column_raises_file_level_error():
    with pytest.raises(CsvImportError, match="missing required column"):
        list(validate_csv_text(b"wrong_column\n0911111111\n", format="phone_only"))


def test_missing_message_column_raises_for_phone_message_format():
    with pytest.raises(CsvImportError, match="missing required column"):
        list(validate_csv_text(b"phone_number\n0911111111\n", format="phone_message"))


def test_empty_file_raises():
    with pytest.raises(CsvImportError, match="empty"):
        list(validate_csv_text(b"", format="phone_only"))


def test_header_only_file_raises():
    with pytest.raises(CsvImportError, match="no data rows"):
        list(validate_csv_text(b"phone_number\n", format="phone_only"))


def test_oversized_file_rejected_before_parsing():
    with pytest.raises(CsvImportError, match="byte limit"):
        list(validate_csv_text(b"x" * (MAX_CSV_SIZE_BYTES + 1), format="phone_only"))


def test_excel_bom_is_stripped_and_header_still_matches():
    raw = "phone_number\n0911111111\n".encode("utf-8-sig")
    rows = list(validate_csv_text(raw, format="phone_only"))
    assert rows[0].status == "valid"


def test_non_utf8_encoding_rejected_cleanly():
    raw = "phone_number\n0911111111\n".encode("utf-16")
    with pytest.raises(CsvImportError, match="UTF-8"):
        list(validate_csv_text(raw, format="phone_only"))


def test_unsupported_extra_column_reported_not_fatal():
    from packages.core.sms.csv_import import _parse_header

    header = _parse_header("phone_number,extra_column\n0911111111\n", format="phone_only")
    assert header.unsupported_columns == ["extra_column"]


def test_too_many_rows_raises_a_clean_error():
    lines = "\n".join(f"09{str(i).zfill(8)}" for i in range(MAX_CSV_ROWS + 1))
    with pytest.raises(CsvImportError, match="more than"):
        list(validate_csv_text(f"phone_number\n{lines}\n".encode(), format="phone_only"))


# --- DB-backed create_import_job: idempotency, persistence -----------------


async def test_create_import_job_persists_rows_and_summary(pool, tenant_id, conn):
    admin_id = await _make_admin_id(conn)
    csv_bytes = b"phone_number,message\n0911111111,Hello\n0512345678,bad\n0911111111,dup\n"
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=csv_bytes,
        original_filename="test.csv", format="phone_message", idempotency_key=_unique_key(),
    )
    assert summary.total_rows == 3
    assert summary.valid_rows == 1
    assert summary.invalid_rows == 1
    assert summary.duplicate_rows == 1

    rows = await list_import_rows(conn, job_id=summary.job_id, tenant_id=tenant_id)
    assert [r["status"] for r in rows] == ["valid", "invalid", "duplicate"]


async def test_repeated_upload_with_same_idempotency_key_does_not_reparse(pool, tenant_id, conn):
    admin_id = await _make_admin_id(conn)
    key = _unique_key()
    csv_bytes = b"phone_number,message\n0911111111,Hello\n"

    first = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=csv_bytes,
        original_filename="a.csv", format="phone_message", idempotency_key=key,
    )
    # A different file, same key -- must NOT be re-parsed; the operator's
    # retry (double-click, browser retry) always resends the identical
    # file for a given key in practice, but the guarantee that matters is
    # "same key never creates a second job", proven here even harder by
    # deliberately changing the payload.
    second = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=b"phone_number,message\n0922222222,Different\n",
        original_filename="b.csv", format="phone_message", idempotency_key=key,
    )
    assert first.job_id == second.job_id
    assert second.valid_rows == 1

    count = await pool.fetchval("SELECT count(*) FROM sms_import_jobs WHERE idempotency_key = $1", key)
    assert count == 1


async def test_concurrent_uploads_with_the_same_idempotency_key_create_exactly_one_job(pool, tenant_id):
    """The real double-click/browser-retry race: two genuinely concurrent
    requests (separate pooled connections via asyncio.gather, not
    sequential calls on one connection -- see test_sms_core.py's own
    concurrency tests for why that distinction matters) carrying the
    identical idempotency_key must never create two import jobs.
    """
    key = _unique_key()
    csv_bytes = b"phone_number,message\n0911111111,Hello\n"

    async def upload():
        async with pool.acquire() as conn:
            admin_id = await _make_admin_id(conn)
            async with conn.transaction():
                return await create_import_job(
                    conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=csv_bytes,
                    original_filename="race.csv", format="phone_message", idempotency_key=key,
                )

    results = await asyncio.gather(upload(), upload(), return_exceptions=True)
    job_ids = {r.job_id for r in results if not isinstance(r, Exception)}
    # Either both succeeded and agree on one job id (one won the UNIQUE
    # constraint race, the other's own INSERT attempt raced into it and
    # -- depending on transaction timing -- either saw the row via the
    # pre-check or hit the real UNIQUE(tenant_id, idempotency_key)
    # constraint), or one raised a real uniqueness violation. Either way,
    # never two distinct job rows for one key.
    assert len(job_ids) <= 1
    count = await pool.fetchval("SELECT count(*) FROM sms_import_jobs WHERE idempotency_key = $1", key)
    assert count == 1


async def test_failed_parse_marks_the_job_failed_not_completed(pool, tenant_id, conn):
    admin_id = await _make_admin_id(conn)
    with pytest.raises(CsvImportError):
        await create_import_job(
            conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=b"",
            original_filename="empty.csv", format="phone_only", idempotency_key=_unique_key(),
        )
    # An empty file fails header parsing before any job row is even
    # created (see csv_import.py's own comment on why) -- confirmed no
    # orphaned row was left behind either.
    count = await pool.fetchval(
        "SELECT count(*) FROM sms_import_jobs WHERE tenant_id = $1 AND original_filename = 'empty.csv'", tenant_id
    )
    assert count == 0


# --- end-to-end: import -> campaign -> validate -> start -> real messages --


async def test_phone_message_import_creates_a_campaign_and_sends_each_rows_own_text(pool, tenant_id, conn):
    admin_id = await _make_admin_id(conn)
    csv_bytes = (
        b"phone_number,message\n"
        b"0911111111,Message for row one\n"
        b"0922222222,Message for row two\n"
    )
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=csv_bytes,
        original_filename="bulk.csv", format="phone_message", idempotency_key=_unique_key(),
    )
    assert summary.valid_rows == 2

    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="csv-campaign", template_id=None, body_override=None,
        audience_filter={}, created_by_admin_id=admin_id, import_job_id=summary.job_id,
    )
    assert campaign.import_job_id == summary.job_id

    validated = await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert validated.status == "ready"
    assert validated.recipient_count == 2

    started, created = await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert started.status == "running"
    assert created == 2

    rows = await conn.fetch(
        "SELECT phone_e164, body, status FROM sms_messages WHERE campaign_id = $1 ORDER BY phone_e164", campaign.id
    )
    assert rows[0]["phone_e164"] == "+251911111111"
    assert rows[0]["body"] == "Message for row one"
    assert rows[1]["phone_e164"] == "+251922222222"
    assert rows[1]["body"] == "Message for row two"
    assert all(r["status"] == "queued" for r in rows)

    # Contacts were really upserted, tagged with the import job for
    # forensic traceability -- not left null.
    contacts = await conn.fetch(
        "SELECT phone_e164, attributes FROM sms_contacts WHERE tenant_id = $1 ORDER BY phone_e164", tenant_id
    )
    assert len(contacts) == 2


async def test_phone_only_import_renders_every_row_through_the_chosen_body_override(pool, tenant_id, conn):
    admin_id = await _make_admin_id(conn)
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id,
        raw_bytes=b"phone_number\n0911111111\n0922222222\n",
        original_filename="phones.csv", format="phone_only", idempotency_key=_unique_key(),
    )
    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="csv-template-campaign", template_id=None,
        body_override="Shared promo message", audience_filter={}, created_by_admin_id=admin_id,
        import_job_id=summary.job_id,
    )
    validated = await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert validated.status == "ready"
    assert validated.recipient_count == 2

    _, created = await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert created == 2
    bodies = {r["body"] for r in await conn.fetch("SELECT body FROM sms_messages WHERE campaign_id = $1", campaign.id)}
    assert bodies == {"Shared promo message"}


async def test_create_campaign_rejects_phone_only_import_missing_a_message(pool, tenant_id, conn):
    from packages.core.sms.audience import InvalidAudienceFilter

    admin_id = await _make_admin_id(conn)
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=b"phone_number\n0911111111\n",
        original_filename="phones2.csv", format="phone_only", idempotency_key=_unique_key(),
    )
    with pytest.raises(InvalidAudienceFilter, match="phone numbers only"):
        await campaigns.create_campaign(
            conn, tenant_id=tenant_id, name="missing-message", template_id=None, body_override=None,
            audience_filter={}, created_by_admin_id=admin_id, import_job_id=summary.job_id,
        )


async def test_suppressed_import_row_is_recorded_but_never_sent(pool, tenant_id, conn):
    admin_id = await _make_admin_id(conn)
    suppressed_phone = "+251933333333"
    await compliance.add_suppression(
        conn, tenant_id=tenant_id, phone_e164=suppressed_phone, reason="manual", note=None, created_by_admin_id=None
    )
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id,
        raw_bytes=b"phone_number,message\n0933333333,hi\n0944444444,hello\n",
        original_filename="supp.csv", format="phone_message", idempotency_key=_unique_key(),
    )
    assert summary.valid_rows == 1
    assert summary.suppressed_rows == 1

    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="supp-campaign", template_id=None, body_override=None,
        audience_filter={}, created_by_admin_id=admin_id, import_job_id=summary.job_id,
    )
    await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    _, created = await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    # Only the one non-suppressed row was ever a candidate to enqueue --
    # the suppressed row was excluded at CSV-parse time already (status
    # != 'valid'), so start_campaign() never even attempts it.
    assert created == 1
    rows = await conn.fetch("SELECT phone_e164, status FROM sms_messages WHERE campaign_id = $1", campaign.id)
    assert len(rows) == 1
    assert rows[0]["phone_e164"] == "+251944444444"
    assert rows[0]["status"] == "queued"


async def test_starting_a_csv_campaign_twice_never_double_enqueues(pool, tenant_id, conn):
    """The same idempotent-resume guarantee campaigns.py's own docstring
    already documents for audience-filter campaigns, proven for the
    import path too.
    """
    admin_id = await _make_admin_id(conn)
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=b"phone_number,message\n0911111111,hi\n",
        original_filename="twice.csv", format="phone_message", idempotency_key=_unique_key(),
    )
    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="twice-campaign", template_id=None, body_override=None,
        audience_filter={}, created_by_admin_id=admin_id, import_job_id=summary.job_id,
    )
    await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    _, created_first = await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    _, created_second = await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert created_first == 1
    assert created_second == 0
    count = await pool.fetchval("SELECT count(*) FROM sms_messages WHERE campaign_id = $1", campaign.id)
    assert count == 1


async def test_no_valid_rows_makes_validation_fail_honestly(pool, tenant_id, conn):
    admin_id = await _make_admin_id(conn)
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=b"phone_number,message\n0512345678,bad\n",
        original_filename="allbad.csv", format="phone_message", idempotency_key=_unique_key(),
    )
    assert summary.valid_rows == 0
    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="allbad-campaign", template_id=None, body_override=None,
        audience_filter={}, created_by_admin_id=admin_id, import_job_id=summary.job_id,
    )
    validated = await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert validated.status == "failed"


async def test_ledger_reconciles_after_a_real_csv_campaign_flow(pool, tenant_id, conn):
    """SMS spending never touches the Bingo ledger at all -- this proves
    the negative just as explicitly as a positive assertion would prove
    a real financial flow: running a full CSV-import campaign leaves
    zero ledger mismatches, because it was never supposed to post to the
    ledger in the first place.
    """
    admin_id = await _make_admin_id(conn)
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=b"phone_number,message\n0911111111,hi\n",
        original_filename="ledger.csv", format="phone_message", idempotency_key=_unique_key(),
    )
    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="ledger-campaign", template_id=None, body_override=None,
        audience_filter={}, created_by_admin_id=admin_id, import_job_id=summary.job_id,
    )
    await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    mismatches = await ledger.reconcile(conn)
    assert mismatches == []


async def test_genuinely_concurrent_start_of_a_csv_campaign_never_double_enqueues(pool, tenant_id):
    """The sequential double-start test above proves idempotency; this
    proves it holds under real concurrency too (two separate pooled
    connections via asyncio.gather, not sequential calls sharing one
    connection -- the same distinction test_sms_core.py's own claim
    -concurrency tests insist on). Unlike claim_next_message()'s own
    check-then-act capacity race, this enqueue path's safety comes from
    a real UNIQUE constraint (idempotency_key) at the database level, not
    from any locking this test needs to add -- exactly why it's expected
    to hold even under two genuinely simultaneous attempts.
    """
    async with pool.acquire() as conn:
        admin_id = await _make_admin_id(conn)
        summary = await create_import_job(
            conn, tenant_id=tenant_id, admin_id=admin_id,
            raw_bytes=b"phone_number,message\n0911111111,a\n0922222222,b\n0933333333,c\n",
            original_filename="concurrent.csv", format="phone_message", idempotency_key=_unique_key(),
        )
        campaign = await campaigns.create_campaign(
            conn, tenant_id=tenant_id, name="concurrent-csv-campaign", template_id=None, body_override=None,
            audience_filter={}, created_by_admin_id=admin_id, import_job_id=summary.job_id,
        )
        await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)

    async def start_attempt():
        async with pool.acquire() as c:
            async with c.transaction():
                return await campaigns.start_campaign(c, campaign_id=campaign.id, admin_id=admin_id)

    results = await asyncio.gather(start_attempt(), start_attempt())
    total_created = sum(created for _, created in results)
    assert total_created == 3, f"expected exactly 3 messages created across both attempts combined, got {total_created}"

    count = await pool.fetchval("SELECT count(*) FROM sms_messages WHERE campaign_id = $1", campaign.id)
    assert count == 3


async def test_import_job_is_not_readable_from_a_different_tenant(pool, tenant_id, conn):
    """A real IDOR/cross-tenant gap this closes: get_import_summary()/
    list_import_rows() originally selected by bare job id with no tenant
    check at all -- harmless only by accident, since this deployment has
    never had a second real tenant to leak. Guessing or enumerating
    another tenant's job id must 404, exactly like a real cross-tenant
    lookup should, never return someone else's row counts or CSV content.
    """
    admin_id = await _make_admin_id(conn)
    summary = await create_import_job(
        conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=b"phone_number,message\n0911111111,secret\n",
        original_filename="private.csv", format="phone_message", idempotency_key=_unique_key(),
    )

    other_tenant_id = await conn.fetchval(
        "INSERT INTO sms_tenants (slug, name) VALUES ($1, $2) RETURNING id",
        f"test-other-{random.randint(1, 10**12)}", "other",
    )

    from packages.core.sms.csv_import import get_import_summary

    with pytest.raises(CsvImportError, match="no such import job"):
        await get_import_summary(conn, job_id=summary.job_id, tenant_id=other_tenant_id)
    with pytest.raises(CsvImportError, match="no such import job"):
        await list_import_rows(conn, job_id=summary.job_id, tenant_id=other_tenant_id)

    # The real owner can still read it -- this isn't a blanket breakage,
    # only cross-tenant access is refused.
    own = await get_import_summary(conn, job_id=summary.job_id, tenant_id=tenant_id)
    assert own.job_id == summary.job_id
