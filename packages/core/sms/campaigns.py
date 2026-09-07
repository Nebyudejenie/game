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
from packages.core.sms import audience, templates
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


_CAMPAIGN_COLUMNS = (
    "id, tenant_id, name, template_id, body_override, status, audience_filter, "
    "recipient_count, scheduled_at, started_at, completed_at, created_by_admin_id, required_fleet_group"
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
) -> Campaign:
    validate_audience_filter(audience_filter)
    if template_id is None and body_override is None:
        raise InvalidAudienceFilter("either template_id or body_override is required")
    import json

    row = await conn.fetchrow(
        f"""
        INSERT INTO sms_campaigns
            (tenant_id, name, template_id, body_override, audience_filter, created_by_admin_id, required_fleet_group)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        RETURNING {_CAMPAIGN_COLUMNS}
        """,
        tenant_id,
        name,
        template_id,
        body_override,
        json.dumps(audience_filter),
        created_by_admin_id,
        required_fleet_group,
    )
    assert row is not None
    campaign = _row_to_campaign(row)
    await _record_event(conn, campaign_id=campaign.id, from_status=None, to_status="draft", admin_id=created_by_admin_id, reason=None)
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
