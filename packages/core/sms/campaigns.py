"""Campaign lifecycle state machine. Every transition goes through
_transition(), which enforces the FROM-status set and records a real row
in sms_campaign_events -- "mandatory transition validation and recording"
taken literally, not left as an implicit side effect of an UPDATE.

v1 lifecycle (a real subset of the directive's full state list -- see
DECISIONS.md for what's deferred):

    draft --validate--> ready | failed
    ready --schedule--> scheduled
    ready|scheduled --start--> running        (messages enqueued here)
    running --pause--> paused --resume--> running
    draft|ready|scheduled|running|paused --cancel--> cancelled
    running --(reconciliation sweep, all messages terminal)--> completed | completed_with_errors

No stage here ever mutates round_engine.py, the ledger, or anything
Bingo-specific -- this package has no import of any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core.ledger import AsyncpgConnection
from packages.core.sms import audience, csv_import, templates
from packages.core.sms.audience import InvalidAudienceFilter, validate_audience_filter

CampaignStatus = str


class InvalidTransition(Exception):
    def __init__(self, campaign_id: int, from_status: str, allowed: frozenset[str]) -> None:
        self.campaign_id = campaign_id
        self.from_status = from_status
        super().__init__(
            f"campaign {campaign_id} is {from_status!r}, not one of {sorted(allowed)}"
        )


class CampaignNotFound(Exception):
    pass


@dataclass(frozen=True)
class Campaign:
    id: int
    tenant_id: int
    name: str
    template_id: int | None
    body_override: str | None
    status: CampaignStatus
    audience_filter: dict[str, Any]
    recipient_count: int | None
    scheduled_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_by_admin_id: int
    required_fleet_group: str | None
    # Set only for a CSV bulk-import-sourced campaign (services/sms/
    # csv_import.py) -- recipients then come from that job's own
    # sms_import_rows, never from audience_filter, which stays the
    # default {} for these campaigns. See validate_campaign()/
    # start_campaign()'s own branch on this field.
    import_job_id: int | None


_CAMPAIGN_COLUMNS = (
    "id, tenant_id, name, template_id, body_override, status, audience_filter, "
    "recipient_count, scheduled_at, started_at, completed_at, created_by_admin_id, "
    "required_fleet_group, import_job_id"
)


def _row_to_campaign(row: asyncpg.Record) -> Campaign:
    import json

    audience_filter = row["audience_filter"]
    if isinstance(audience_filter, str):
        audience_filter = json.loads(audience_filter)
    return Campaign(
        id=row["id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        template_id=row["template_id"],
        body_override=row["body_override"],
        status=row["status"],
        audience_filter=audience_filter,
        recipient_count=row["recipient_count"],
        scheduled_at=row["scheduled_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        created_by_admin_id=row["created_by_admin_id"],
        required_fleet_group=row["required_fleet_group"],
        import_job_id=row["import_job_id"],
    )


_CAMPAIGN_SELECT = f"SELECT {_CAMPAIGN_COLUMNS} FROM sms_campaigns WHERE id = $1"


async def get_campaign(conn: AsyncpgConnection, *, campaign_id: int) -> Campaign:
    row = await conn.fetchrow(_CAMPAIGN_SELECT, campaign_id)
    if row is None:
        raise CampaignNotFound(str(campaign_id))
    return _row_to_campaign(row)


async def list_campaigns(conn: AsyncpgConnection, *, tenant_id: int) -> list[Campaign]:
    rows = await conn.fetch(
        f"SELECT {_CAMPAIGN_COLUMNS} FROM sms_campaigns WHERE tenant_id = $1 ORDER BY id DESC",
        tenant_id,
    )
    return [_row_to_campaign(row) for row in rows]


async def create_campaign(
    conn: AsyncpgConnection,
    *,
    tenant_id: int,
    name: str,
    template_id: int | None,
    body_override: str | None,
    audience_filter: dict[str, Any],
    created_by_admin_id: int,
    required_fleet_group: str | None = None,
    import_job_id: int | None = None,
) -> Campaign:
    validate_audience_filter(audience_filter)
    if import_job_id is not None:
        # A phone_message-format import supplies its own per-recipient
        # body -- no template/override needed at all. A phone_only
        # import still needs one, exactly like any audience-filter
        # campaign, since its rows carry nothing but a phone number.
        import_format = await conn.fetchval(
            "SELECT format FROM sms_import_jobs WHERE id = $1 AND tenant_id = $2",
            import_job_id, tenant_id,
        )
        if import_format is None:
            raise InvalidAudienceFilter(f"no such import job: {import_job_id}")
        if import_format == "phone_only" and template_id is None and body_override is None:
            raise InvalidAudienceFilter(
                "this import has phone numbers only -- select a template or enter a message"
            )
    elif template_id is None and body_override is None:
        raise InvalidAudienceFilter("either template_id or body_override is required")
    import json

    row = await conn.fetchrow(
        f"""
        INSERT INTO sms_campaigns
            (tenant_id, name, template_id, body_override, audience_filter, created_by_admin_id,
             required_fleet_group, import_job_id)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
        RETURNING {_CAMPAIGN_COLUMNS}
        """,
        tenant_id,
        name,
        template_id,
        body_override,
        json.dumps(audience_filter),
        created_by_admin_id,
        required_fleet_group,
        import_job_id,
    )
    assert row is not None
    campaign = _row_to_campaign(row)
    await _record_event(conn, campaign_id=campaign.id, from_status=None, to_status="draft", admin_id=created_by_admin_id, reason=None)
    if import_job_id is not None:
        await conn.execute("UPDATE sms_import_jobs SET campaign_id = $1 WHERE id = $2", campaign.id, import_job_id)
    return campaign


async def _record_event(
    conn: AsyncpgConnection,
    *,
    campaign_id: int,
    from_status: str | None,
    to_status: str,
    admin_id: int | None,
    reason: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO sms_campaign_events (campaign_id, from_status, to_status, admin_id, reason)
        VALUES ($1, $2, $3, $4, $5)
        """,
        campaign_id,
        from_status,
        to_status,
        admin_id,
        reason,
    )


async def _transition(
    conn: AsyncpgConnection,
    *,
    campaign_id: int,
    allowed_from: frozenset[str],
    to_status: str,
    admin_id: int | None,
    reason: str | None,
    extra_set_sql: str = "",
    extra_args: tuple[object, ...] = (),
) -> Campaign:
    row = await conn.fetchrow(
        "SELECT status FROM sms_campaigns WHERE id = $1 FOR UPDATE", campaign_id
    )
    if row is None:
        raise CampaignNotFound(str(campaign_id))
    from_status = row["status"]
    if from_status not in allowed_from:
        raise InvalidTransition(campaign_id, from_status, allowed_from)
    await conn.execute(
        f"UPDATE sms_campaigns SET status = $1, updated_at = now() {extra_set_sql} WHERE id = $2",
        to_status,
        campaign_id,
        *extra_args,
    )
    await _record_event(
        conn, campaign_id=campaign_id, from_status=from_status, to_status=to_status, admin_id=admin_id, reason=reason
    )
    return await get_campaign(conn, campaign_id=campaign_id)


def _effective_body(campaign: Campaign, template_body: str | None) -> str:
    if campaign.body_override is not None:
        return campaign.body_override
    assert template_body is not None
    return template_body


async def _resolvable_import_recipient_count(conn: AsyncpgConnection, *, campaign: Campaign) -> int:
    """The CSV-import equivalent of the audience-filter loop below.
    phone_message rows carry their own already-validated final body, so
    every 'valid' row is resolvable by construction -- no per-row render
    needed. phone_only rows all share the identical variable set (CSV
    import supplies no per-contact attributes), so the template/override
    either renders for all of them or none -- one render proves it,
    exactly as cheap as checking one recipient, never one query per row.
    """
    assert campaign.import_job_id is not None
    summary = await csv_import.get_import_summary(conn, job_id=campaign.import_job_id)
    if summary.format == "phone_message":
        return summary.valid_rows
    template_body = None
    if campaign.template_id is not None:
        template_row = await conn.fetchrow(
            "SELECT body FROM sms_templates WHERE id = $1 AND tenant_id = $2",
            campaign.template_id, campaign.tenant_id,
        )
        if template_row is None:
            return 0
        template_body = template_row["body"]
    body = _effective_body(campaign, template_body)
    try:
        templates.render_template(body, {"display_name": ""})
    except templates.MissingVariables:
        return 0
    return summary.valid_rows


async def validate_campaign(conn: AsyncpgConnection, *, campaign_id: int, admin_id: int) -> Campaign:
    """draft|failed -> ready (recipient_count > 0) or failed (0 resolvable
    recipients, or a template variable a resolved recipient can't
    satisfy). Real recipient resolution, not an estimate -- start_campaign
    re-resolves at enqueue time using the identical query, so a campaign
    never claims a preview count it can't actually deliver against
    (modulo audience drift between validate and start, which start's own
    fresh resolution handles correctly either way).
    """
    campaign = await _transition(
        conn, campaign_id=campaign_id, allowed_from=frozenset({"draft", "failed"}),
        to_status="validating", admin_id=admin_id, reason=None,
    )

    if campaign.import_job_id is not None:
        resolvable = await _resolvable_import_recipient_count(conn, campaign=campaign)
    else:
        template_body = None
        if campaign.template_id is not None:
            template_row = await conn.fetchrow(
                "SELECT body FROM sms_templates WHERE id = $1 AND tenant_id = $2",
                campaign.template_id, campaign.tenant_id,
            )
            if template_row is None:
                return await _transition(
                    conn, campaign_id=campaign_id, allowed_from=frozenset({"validating"}),
                    to_status="failed", admin_id=admin_id, reason="template not found",
                )
            template_body = template_row["body"]

        recipients = await audience.resolve_recipients(
            conn, tenant_id=campaign.tenant_id, audience_filter=campaign.audience_filter
        )
        resolvable = 0
        for recipient in recipients:
            body = _effective_body(campaign, template_body)
            variables = {"display_name": recipient.display_name or ""}
            try:
                templates.render_template(body, variables)
            except templates.MissingVariables:
                continue
            resolvable += 1

    if resolvable == 0:
        return await _transition(
            conn, campaign_id=campaign_id, allowed_from=frozenset({"validating"}),
            to_status="failed", admin_id=admin_id, reason="no resolvable recipients",
        )
    return await _transition(
        conn, campaign_id=campaign_id, allowed_from=frozenset({"validating"}),
        to_status="ready", admin_id=admin_id, reason=None,
        extra_set_sql=", recipient_count = $3", extra_args=(resolvable,),
    )


async def schedule_campaign(
    conn: AsyncpgConnection, *, campaign_id: int, admin_id: int, scheduled_at: datetime
) -> Campaign:
    return await _transition(
        conn, campaign_id=campaign_id, allowed_from=frozenset({"ready"}),
        to_status="scheduled", admin_id=admin_id, reason=None,
        extra_set_sql=", scheduled_at = $3", extra_args=(scheduled_at,),
    )


async def _get_or_create_import_contact(
    conn: AsyncpgConnection, *, tenant_id: int, phone_e164: str, import_job_id: int
) -> int:
    """The CSV-import equivalent of admin_queries.create_contact_admin's
    own upsert shape, at this package's own (audit-agnostic) layer --
    tags the contact with the import job it came from, purely for
    forensic traceability (Section 15: "investigate a message" should be
    able to show where its recipient actually came from), never read by
    audience filtering itself.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO sms_contacts (tenant_id, phone_e164, attributes)
        VALUES ($1, $2, jsonb_build_object('imported_via_job', $3::bigint))
        ON CONFLICT (tenant_id, phone_e164) DO UPDATE SET updated_at = now()
        RETURNING id
        """,
        tenant_id, phone_e164, import_job_id,
    )
    assert row is not None
    return int(row["id"])


async def _enqueue_import_messages(conn: AsyncpgConnection, *, campaign: Campaign) -> int:
    """The CSV-import equivalent of the audience-filter enqueue loop
    below -- reads sms_import_rows in bounded pages (csv_import.py's own
    IMPORT_BATCH_SIZE) so a large import is never pulled entirely into
    memory here, however many thousands of valid rows it has.
    """
    assert campaign.import_job_id is not None
    summary = await csv_import.get_import_summary(conn, job_id=campaign.import_job_id)
    template_body = None
    if campaign.template_id is not None:
        template_row = await conn.fetchrow("SELECT body FROM sms_templates WHERE id = $1", campaign.template_id)
        assert template_row is not None
        template_body = template_row["body"]

    created = 0
    after_row_number = 0
    while True:
        batch = await csv_import.resolve_import_recipients_batch(
            conn, job_id=campaign.import_job_id, after_row_number=after_row_number
        )
        if not batch:
            break
        after_row_number = batch[-1].row_number

        for row in batch:
            assert row.normalized_phone is not None
            if summary.format == "phone_message":
                assert row.message_body is not None
                rendered = row.message_body
            else:
                body_template = _effective_body(campaign, template_body)
                try:
                    rendered = templates.render_template(body_template, {"display_name": ""})
                except templates.MissingVariables:
                    continue
            # Re-checked here, not just at CSV-parse time -- the same
            # "audience drift between validate/parse and start" reason
            # the audience-filter enqueue loop below re-checks
            # suppression itself, applied to CSV rows too (a phone could
            # be suppressed after this file was uploaded but before the
            # campaign was actually started).
            suppressed = await audience.is_suppressed(
                conn, tenant_id=campaign.tenant_id, phone_e164=row.normalized_phone
            )
            contact_id = await _get_or_create_import_contact(
                conn, tenant_id=campaign.tenant_id, phone_e164=row.normalized_phone,
                import_job_id=campaign.import_job_id,
            )
            segment_count = templates.count_segments(rendered)
            idempotency_key = f"campaign:{campaign.id}:{contact_id}"
            status = "suppressed" if suppressed else "queued"
            result = await conn.execute(
                """
                INSERT INTO sms_messages
                    (tenant_id, campaign_id, contact_id, phone_e164, body, segment_count, status, idempotency_key)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                campaign.tenant_id, campaign.id, contact_id, row.normalized_phone,
                rendered, segment_count, status, idempotency_key,
            )
            if result == "INSERT 0 1":
                created += 1
    return created


async def start_campaign(conn: AsyncpgConnection, *, campaign_id: int, admin_id: int) -> tuple[Campaign, int]:
    """Enqueues real sms_messages rows, one per resolvable recipient,
    idempotently: re-calling this after a partial failure (e.g. the
    process died mid-enqueue, or an operator retries after a timeout)
    can never create a duplicate message for the same recipient, because
    idempotency_key is a real UNIQUE constraint and every insert here is
    ON CONFLICT DO NOTHING. Returns (campaign, messages_created).

    A campaign already 'running' is a legitimate resume, not an error --
    "resume without duplication" (the directive's own pause/cancel
    semantics section) applies just as much to recovering from a crash
    mid-enqueue as to an explicit pause/resume cycle.
    """
    row = await conn.fetchrow("SELECT status FROM sms_campaigns WHERE id = $1 FOR UPDATE", campaign_id)
    if row is None:
        raise CampaignNotFound(str(campaign_id))
    from_status = row["status"]
    if from_status == "running":
        campaign = await get_campaign(conn, campaign_id=campaign_id)
    elif from_status in ("ready", "scheduled"):
        await conn.execute(
            "UPDATE sms_campaigns SET status = 'running', started_at = now(), updated_at = now() WHERE id = $1",
            campaign_id,
        )
        await _record_event(
            conn, campaign_id=campaign_id, from_status=from_status, to_status="running",
            admin_id=admin_id, reason=None,
        )
        campaign = await get_campaign(conn, campaign_id=campaign_id)
    else:
        raise InvalidTransition(campaign_id, from_status, frozenset({"ready", "scheduled"}))

    if campaign.import_job_id is not None:
        created = await _enqueue_import_messages(conn, campaign=campaign)
        return campaign, created

    template_body = None
    if campaign.template_id is not None:
        template_row = await conn.fetchrow(
            "SELECT body FROM sms_templates WHERE id = $1", campaign.template_id
        )
        assert template_row is not None
        template_body = template_row["body"]

    recipients = await audience.resolve_recipients(
        conn, tenant_id=campaign.tenant_id, audience_filter=campaign.audience_filter
    )
    created = 0
    for recipient in recipients:
        body_template = _effective_body(campaign, template_body)
        variables = {"display_name": recipient.display_name or ""}
        try:
            rendered = templates.render_template(body_template, variables)
        except templates.MissingVariables:
            continue
        segment_count = templates.count_segments(rendered)
        idempotency_key = f"campaign:{campaign.id}:{recipient.contact_id}"
        suppressed = await audience.is_suppressed(
            conn, tenant_id=campaign.tenant_id, phone_e164=recipient.phone_e164
        )
        status = "suppressed" if suppressed else "queued"
        result = await conn.execute(
            """
            INSERT INTO sms_messages
                (tenant_id, campaign_id, contact_id, phone_e164, body, segment_count, status, idempotency_key)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            campaign.tenant_id, campaign.id, recipient.contact_id, recipient.phone_e164,
            rendered, segment_count, status, idempotency_key,
        )
        if result == "INSERT 0 1":
            created += 1
    return campaign, created


async def pause_campaign(conn: AsyncpgConnection, *, campaign_id: int, admin_id: int) -> Campaign:
    return await _transition(
        conn, campaign_id=campaign_id, allowed_from=frozenset({"running"}),
        to_status="paused", admin_id=admin_id, reason=None,
    )


async def resume_campaign(conn: AsyncpgConnection, *, campaign_id: int, admin_id: int) -> Campaign:
    return await _transition(
        conn, campaign_id=campaign_id, allowed_from=frozenset({"paused"}),
        to_status="running", admin_id=admin_id, reason=None,
    )


async def cancel_campaign(conn: AsyncpgConnection, *, campaign_id: int, admin_id: int, reason: str) -> Campaign:
    """Only messages not yet claimed by a node move to 'cancelled' -- an
    already-assigned/sending message is left alone to resolve through the
    normal report-result/reconciliation path, since (per the directive)
    you cannot un-send an SMS that may already be in flight to a carrier.
    """
    campaign = await _transition(
        conn, campaign_id=campaign_id,
        allowed_from=frozenset({"draft", "ready", "scheduled", "running", "paused"}),
        to_status="cancelled", admin_id=admin_id, reason=reason,
    )
    await conn.execute(
        "UPDATE sms_messages SET status = 'cancelled', updated_at = now() WHERE campaign_id = $1 AND status = 'queued'",
        campaign_id,
    )
    return campaign


async def maybe_complete_campaign(conn: AsyncpgConnection, *, campaign_id: int) -> Campaign | None:
    """Called by the reconciliation sweep after processing timeouts --
    system-driven, not an admin action (admin_id is None on this event).
    A no-op (returns None) unless the campaign is 'running' and every one
    of its messages has actually reached a terminal state.
    """
    row = await conn.fetchrow("SELECT status FROM sms_campaigns WHERE id = $1 FOR UPDATE", campaign_id)
    if row is None or row["status"] != "running":
        return None
    counts = await conn.fetch(
        "SELECT status, count(*) AS n FROM sms_messages WHERE campaign_id = $1 GROUP BY status",
        campaign_id,
    )
    by_status: dict[str, int] = {r["status"]: r["n"] for r in counts}
    non_terminal = {"queued", "assigned", "sending", "unknown"}
    if any(by_status.get(s, 0) > 0 for s in non_terminal):
        return None
    has_errors = by_status.get("failed", 0) > 0 or by_status.get("dead_letter", 0) > 0
    to_status = "completed_with_errors" if has_errors else "completed"
    return await _transition(
        conn, campaign_id=campaign_id, allowed_from=frozenset({"running"}),
        to_status=to_status, admin_id=None, reason=None,
        extra_set_sql=", completed_at = now()",
    )
