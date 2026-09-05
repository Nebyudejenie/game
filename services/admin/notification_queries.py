"""Notification Center admin operations: templates, campaigns, delivery
queue, history, analytics. Audience resolution and delivery bookkeeping
live in packages/core/campaigns.py (shared with services/bot/
campaign_worker.py, which actually sends); this module is the admin-
facing CRUD + audit trail layer on top of it, the same split
services/admin/queries.py already uses for every other resource.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from packages.core.campaigns import count_audience
from services.admin import audit

_EDITABLE_CAMPAIGN_STATUSES = {"draft"}


class CampaignNotEditable(ValueError):
    pass


class InvalidCampaignTransition(ValueError):
    pass


# --- templates ----------------------------------------------------------


async def create_template_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    name: str,
    category: str,
    title: str,
    body: str,
    ip_address: str | None,
) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO notification_templates (name, category, title, body, created_by_admin_id) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                name,
                category,
                title,
                body,
                admin_id,
            )
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="notification_templates.create",
                target_type="notification_template",
                target_id=str(row["id"]),
                after={"name": name, "category": category},
                ip_address=ip_address,
            )
            return int(row["id"])


async def list_templates_admin(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT id, name, category, title, body, channel, is_active, created_at, updated_at "
        "FROM notification_templates ORDER BY name"
    )
    return [dict(r) for r in rows]


_TEMPLATE_EDITABLE_FIELDS = {"name", "category", "title", "body", "is_active"}


async def update_template_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    template_id: int,
    changes: dict[str, Any],
    ip_address: str | None,
) -> bool:
    unknown = set(changes) - _TEMPLATE_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not an editable template field: {unknown}")
    if not changes:
        return False
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT * FROM notification_templates WHERE id = $1", template_id
            )
            if before is None:
                return False
            set_clauses = []
            values: list[Any] = []
            for i, (field, value) in enumerate(changes.items(), start=1):
                set_clauses.append(f"{field} = ${i}")
                values.append(value)
            values.append(template_id)
            await conn.execute(
                f"UPDATE notification_templates SET {', '.join(set_clauses)}, "
                f"updated_by_admin_id = ${len(values) + 1}, updated_at = now() "
                f"WHERE id = ${len(values)}",
                *values,
                admin_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="notification_templates.update",
                target_type="notification_template",
                target_id=str(template_id),
                before={k: before[k] for k in changes},
                after=changes,
                ip_address=ip_address,
            )
            return True


# --- audience -------------------------------------------------------------


async def resolve_audience_count(
    pool: asyncpg.Pool, *, audience_filter: dict[str, Any], exclude_user_ids: list[int]
) -> int:
    return await count_audience(pool, audience_filter, exclude_user_ids)


# --- campaigns --------------------------------------------------------------


async def create_campaign_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    internal_name: str,
    title: str,
    body: str,
    audience_filter: dict[str, Any],
    exclude_user_ids: list[int],
    template_id: int | None,
    ip_address: str | None,
) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO notification_campaigns "
                "(internal_name, title, body, audience_filter, exclude_user_ids, template_id, "
                " created_by_admin_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
                internal_name,
                title,
                body,
                json.dumps(audience_filter),
                exclude_user_ids,
                template_id,
                admin_id,
            )
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="notification_campaigns.create",
                target_type="notification_campaign",
                target_id=str(row["id"]),
                after={"internal_name": internal_name, "audience_filter": audience_filter},
                ip_address=ip_address,
            )
            return int(row["id"])


_CAMPAIGN_EDITABLE_FIELDS = {
    "internal_name", "title", "body", "audience_filter", "exclude_user_ids", "template_id",
}


async def update_campaign_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    campaign_id: int,
    changes: dict[str, Any],
    ip_address: str | None,
) -> bool:
    """Edits are only ever allowed on a DRAFT -- once scheduled/queued/
    sending, the campaign's own content is what audience resolution and
    delivery already started (or will imminently start) working from;
    silently rewriting it out from under an in-flight send would let a
    recipient who already got the old text, and one who gets the new
    text after an edit, both legitimately claim "this is what it said,"
    with no single true answer. Cancel and duplicate into a fresh draft
    instead (see duplicate_campaign_admin).
    """
    unknown = set(changes) - _CAMPAIGN_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not an editable campaign field: {unknown}")
    if not changes:
        return False
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT * FROM notification_campaigns WHERE id = $1 FOR UPDATE", campaign_id
            )
            if before is None:
                return False
            if before["status"] not in _EDITABLE_CAMPAIGN_STATUSES:
                raise CampaignNotEditable(f"campaign is {before['status']}, not draft")

            set_clauses = []
            values: list[Any] = []
            for i, (field, value) in enumerate(changes.items(), start=1):
                if field == "audience_filter":
                    value = json.dumps(value)
                set_clauses.append(f"{field} = ${i}")
                values.append(value)
            values.append(campaign_id)
            await conn.execute(
                f"UPDATE notification_campaigns SET {', '.join(set_clauses)}, updated_at = now() "
                f"WHERE id = ${len(values)}",
                *values,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="notification_campaigns.update",
                target_type="notification_campaign",
                target_id=str(campaign_id),
                before={k: before[k] for k in changes},
                after=changes,
                ip_address=ip_address,
            )
            return True


async def delete_draft_campaign_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM notification_campaigns WHERE id = $1 AND status = 'draft' RETURNING id",
                campaign_id,
            )
            if row is None:
                return False
            await audit.record(
                conn,
                admin_id=admin_id,
                action="notification_campaigns.delete_draft",
                target_type="notification_campaign",
                target_id=str(campaign_id),
                ip_address=ip_address,
            )
            return True


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"queued", "scheduled"},
    "scheduled": {"cancelled", "scheduled"},  # reschedule = scheduled -> scheduled (new time)
    "queued": {"cancelled"},
}


async def _transition_campaign(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    campaign_id: int,
    to_status: str,
    scheduled_at: datetime | None,
    action: str,
    ip_address: str | None,
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM notification_campaigns WHERE id = $1 FOR UPDATE", campaign_id
            )
            if row is None:
                return False
            current = row["status"]
            allowed = _ALLOWED_TRANSITIONS.get(current, set())
            if to_status not in allowed:
                raise InvalidCampaignTransition(f"cannot move a {current!r} campaign to {to_status!r}")
            await conn.execute(
                "UPDATE notification_campaigns SET status = $2, scheduled_at = $3, updated_at = now() "
                "WHERE id = $1",
                campaign_id,
                to_status,
                scheduled_at,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action=action,
                target_type="notification_campaign",
                target_id=str(campaign_id),
                before={"status": current},
                after={"status": to_status, "scheduled_at": scheduled_at.isoformat() if scheduled_at else None},
                ip_address=ip_address,
            )
            return True


async def send_campaign_now_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None
) -> bool:
    return await _transition_campaign(
        pool, admin_id=admin_id, campaign_id=campaign_id, to_status="queued", scheduled_at=None,
        action="notification_campaigns.send_now", ip_address=ip_address,
    )


async def schedule_campaign_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, scheduled_at: datetime, ip_address: str | None
) -> bool:
    if scheduled_at.tzinfo is None:
        # Same explicit-UTC rule as services/admin/queries.py's own
        # manual-payment-destination dates fix, for the identical reason:
        # a naive datetime's encoding depends on the Python process's own
        # ambient OS timezone, not UTC and not Postgres's session
        # setting -- deterministic here regardless of that.
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    return await _transition_campaign(
        pool, admin_id=admin_id, campaign_id=campaign_id, to_status="scheduled",
        scheduled_at=scheduled_at, action="notification_campaigns.schedule", ip_address=ip_address,
    )


async def cancel_campaign_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM notification_campaigns WHERE id = $1 FOR UPDATE", campaign_id
            )
            if row is None:
                return False
            current = row["status"]
            if current not in ("scheduled", "queued"):
                raise InvalidCampaignTransition(f"cannot cancel a {current!r} campaign")
            await conn.execute(
                "UPDATE notification_campaigns SET status = 'cancelled', updated_at = now() WHERE id = $1",
                campaign_id,
            )
            # Any delivery rows a partially-started dispatch already
            # created (e.g. cancelled the instant after the worker began
            # a 'sending' batch) are cancelled too, so campaign_worker.py's
            # own dispatch loop -- which only ever selects 'pending' rows
            # -- has nothing left to pick up for this campaign.
            await conn.execute(
                "UPDATE notification_deliveries SET status = 'cancelled' "
                "WHERE campaign_id = $1 AND status = 'pending'",
                campaign_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="notification_campaigns.cancel",
                target_type="notification_campaign",
                target_id=str(campaign_id),
                before={"status": current},
                after={"status": "cancelled"},
                ip_address=ip_address,
            )
            return True


async def duplicate_campaign_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None
) -> int | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            original = await conn.fetchrow(
                "SELECT internal_name, title, body, audience_filter, exclude_user_ids, template_id "
                "FROM notification_campaigns WHERE id = $1",
                campaign_id,
            )
            if original is None:
                return None
            row = await conn.fetchrow(
                "INSERT INTO notification_campaigns "
                "(internal_name, title, body, audience_filter, exclude_user_ids, template_id, "
                " created_by_admin_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
                f"{original['internal_name']} (copy)",
                original["title"],
                original["body"],
                original["audience_filter"],
                original["exclude_user_ids"],
                original["template_id"],
                admin_id,
            )
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="notification_campaigns.duplicate",
                target_type="notification_campaign",
                target_id=str(row["id"]),
                after={"duplicated_from": campaign_id},
                ip_address=ip_address,
            )
            return int(row["id"])


async def list_campaigns_admin(
    pool: asyncpg.Pool,
    *,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []

    def _p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if status:
        clauses.append(f"status = {_p(status)}")
    if search:
        clauses.append(f"(internal_name ILIKE {_p('%' + search + '%')} OR title ILIKE {_p('%' + search + '%')})")
    where = " AND ".join(clauses) if clauses else "true"
    rows = await pool.fetch(
        f"""
        SELECT c.id, c.internal_name, c.title, c.status, c.channel, c.scheduled_at, c.started_at,
               c.completed_at, c.recipient_count, c.delivered_count, c.failed_count, c.created_at,
               a.username AS created_by
        FROM notification_campaigns c
        JOIN admin_users a ON a.id = c.created_by_admin_id
        WHERE {where}
        ORDER BY c.id DESC
        LIMIT {_p(limit)} OFFSET {_p(offset)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def get_campaign_detail_admin(pool: asyncpg.Pool, campaign_id: int) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        """
        SELECT c.*, a.username AS created_by
        FROM notification_campaigns c
        JOIN admin_users a ON a.id = c.created_by_admin_id
        WHERE c.id = $1
        """,
        campaign_id,
    )
    if row is None:
        return None
    detail = dict(row)
    audience_filter = detail["audience_filter"]
    detail["audience_filter"] = json.loads(audience_filter) if isinstance(audience_filter, str) else audience_filter
    return detail


async def list_deliveries_admin(
    pool: asyncpg.Pool, *, campaign_id: int, status: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    clauses = ["nd.campaign_id = $1"]
    params: list[Any] = [campaign_id]
    if status:
        params.append(status)
        clauses.append(f"nd.status = ${len(params)}")
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"""
        SELECT nd.id, nd.user_id, u.display_name, nd.status, nd.attempt_count, nd.failure_reason,
               nd.queued_at, nd.last_attempt_at, nd.delivered_at
        FROM notification_deliveries nd
        JOIN users u ON u.id = nd.user_id
        WHERE {' AND '.join(clauses)}
        ORDER BY nd.id
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def notification_overview_admin(pool: asyncpg.Pool) -> dict[str, Any]:
    campaign_counts = await pool.fetch(
        "SELECT status, count(*) AS n FROM notification_campaigns "
        "WHERE created_at >= now() - interval '30 days' GROUP BY status"
    )
    by_status = {r["status"]: r["n"] for r in campaign_counts}

    sent_today = await pool.fetchval(
        "SELECT COALESCE(sum(delivered_count + failed_count), 0) FROM notification_campaigns "
        "WHERE started_at >= date_trunc('day', now())"
    )
    totals_row = await pool.fetchrow(
        "SELECT COALESCE(sum(delivered_count), 0) AS delivered, COALESCE(sum(failed_count), 0) AS failed "
        "FROM notification_campaigns WHERE created_at >= now() - interval '30 days'"
    )
    assert totals_row is not None  # a bare aggregate always returns exactly one row
    delivered_total, failed_total = totals_row["delivered"], totals_row["failed"]
    recent = await pool.fetch(
        "SELECT id, internal_name, status, recipient_count, delivered_count, failed_count, created_at "
        "FROM notification_campaigns ORDER BY id DESC LIMIT 10"
    )
    total = (delivered_total or 0) + (failed_total or 0)
    return {
        "sent_today": int(sent_today or 0),
        "draft": by_status.get("draft", 0),
        "scheduled": by_status.get("scheduled", 0),
        "queued": by_status.get("queued", 0),
        "sending": by_status.get("sending", 0),
        "completed": by_status.get("completed", 0),
        "partially_failed": by_status.get("partially_failed", 0),
        "failed": by_status.get("failed", 0),
        "cancelled": by_status.get("cancelled", 0),
        "delivered_total_30d": int(delivered_total or 0),
        "failed_total_30d": int(failed_total or 0),
        # None (not 0.0) when there is nothing to divide by, rather than
        # a fabricated 0% -- distinguished explicitly so the UI can show
        # "no data yet" instead of a misleading real-looking percentage.
        "delivery_rate_30d": (round((delivered_total or 0) / total * 100, 1) if total else None),
        "recent_campaigns": [dict(r) for r in recent],
    }
