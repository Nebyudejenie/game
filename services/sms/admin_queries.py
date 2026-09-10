"""Admin-facing wrappers around packages/core/sms/* -- the layer that adds
the audit trail (services/admin/audit.py, the exact same table and
function every other admin mutation in this codebase writes through) on
top of the pure domain functions. Mirrors services/admin/queries.py's own
"domain logic is audit-agnostic, the admin-facing wrapper adds audit.record()
inside the same transaction" layering exactly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from services.admin import audit
from packages.core.sms import campaigns as campaigns_module
from packages.core.sms import compliance, csv_import, nodes, templates as templates_module
from packages.core.sms.campaigns import Campaign
from packages.core.sms.compliance import Suppression
from packages.core.sms.csv_import import ImportFormat, ImportSummary
from packages.core.sms.nodes import DeliveryNode


async def create_campaign_admin(
    pool: asyncpg.Pool,
    *,
    tenant_id: int,
    admin_id: int,
    name: str,
    template_id: int | None,
    body_override: str | None,
    audience_filter: dict[str, Any],
    ip_address: str | None,
    required_fleet_group: str | None = None,
    import_job_id: int | None = None,
) -> Campaign:
    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign = await campaigns_module.create_campaign(
                conn, tenant_id=tenant_id, name=name, template_id=template_id,
                body_override=body_override, audience_filter=audience_filter, created_by_admin_id=admin_id,
                required_fleet_group=required_fleet_group, import_job_id=import_job_id,
            )
            await audit.record(
                conn, admin_id=admin_id, action="sms.campaigns.create", target_type="sms_campaign",
                target_id=str(campaign.id), before=None,
                after={
                    "name": name, "status": campaign.status, "required_fleet_group": required_fleet_group,
                    "import_job_id": import_job_id,
                },
                ip_address=ip_address,
            )
    return campaign


async def validate_campaign_admin(pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None) -> Campaign:
    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign = await campaigns_module.validate_campaign(conn, campaign_id=campaign_id, admin_id=admin_id)
            await audit.record(
                conn, admin_id=admin_id, action="sms.campaigns.validate", target_type="sms_campaign",
                target_id=str(campaign_id), before=None,
                after={"status": campaign.status, "recipient_count": campaign.recipient_count},
                ip_address=ip_address,
            )
    return campaign


async def schedule_campaign_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, scheduled_at: datetime, ip_address: str | None
) -> Campaign:
    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign = await campaigns_module.schedule_campaign(
                conn, campaign_id=campaign_id, admin_id=admin_id, scheduled_at=scheduled_at
            )
            await audit.record(
                conn, admin_id=admin_id, action="sms.campaigns.schedule", target_type="sms_campaign",
                target_id=str(campaign_id), before=None, after={"scheduled_at": scheduled_at.isoformat()},
                ip_address=ip_address,
            )
    return campaign


async def start_campaign_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None
) -> tuple[Campaign, int]:
    """The one action that actually sends real messages to real people --
    gated at the route layer by the narrower sms:campaigns:approve
    permission, not the broader sms:campaigns:manage a draft/edit needs.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign, created = await campaigns_module.start_campaign(conn, campaign_id=campaign_id, admin_id=admin_id)
            await audit.record(
                conn, admin_id=admin_id, action="sms.campaigns.start", target_type="sms_campaign",
                target_id=str(campaign_id), before=None,
                after={"status": campaign.status, "messages_created": created}, ip_address=ip_address,
            )
    return campaign, created


async def pause_campaign_admin(pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None) -> Campaign:
    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign = await campaigns_module.pause_campaign(conn, campaign_id=campaign_id, admin_id=admin_id)
            await audit.record(
                conn, admin_id=admin_id, action="sms.campaigns.pause", target_type="sms_campaign",
                target_id=str(campaign_id), before=None, after={"status": campaign.status}, ip_address=ip_address,
            )
    return campaign


async def resume_campaign_admin(pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, ip_address: str | None) -> Campaign:
    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign = await campaigns_module.resume_campaign(conn, campaign_id=campaign_id, admin_id=admin_id)
            await audit.record(
                conn, admin_id=admin_id, action="sms.campaigns.resume", target_type="sms_campaign",
                target_id=str(campaign_id), before=None, after={"status": campaign.status}, ip_address=ip_address,
            )
    return campaign


async def cancel_campaign_admin(
    pool: asyncpg.Pool, *, admin_id: int, campaign_id: int, reason: str, ip_address: str | None
) -> Campaign:
    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign = await campaigns_module.cancel_campaign(conn, campaign_id=campaign_id, admin_id=admin_id, reason=reason)
            await audit.record(
                conn, admin_id=admin_id, action="sms.campaigns.cancel", target_type="sms_campaign",
                target_id=str(campaign_id), before=None, after={"status": campaign.status}, reason=reason,
                ip_address=ip_address,
            )
    return campaign


async def upload_csv_admin(
    pool: asyncpg.Pool,
    *,
    tenant_id: int,
    admin_id: int,
    raw_bytes: bytes,
    original_filename: str | None,
    format: ImportFormat,
    idempotency_key: str,
    ip_address: str | None,
) -> ImportSummary:
    """Never records raw CSV content in the audit trail (Section 18:
    "never log ... unnecessary message content") -- only the summary
    counts, exactly what an operator reviewing the audit log actually
    needs to know happened.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            summary = await csv_import.create_import_job(
                conn, tenant_id=tenant_id, admin_id=admin_id, raw_bytes=raw_bytes,
                original_filename=original_filename, format=format, idempotency_key=idempotency_key,
            )
            await audit.record(
                conn, admin_id=admin_id, action="sms.imports.upload", target_type="sms_import_job",
                target_id=str(summary.job_id), before=None,
                after={
                    "original_filename": original_filename, "format": format,
                    "total_rows": summary.total_rows, "valid_rows": summary.valid_rows,
                    "invalid_rows": summary.invalid_rows, "duplicate_rows": summary.duplicate_rows,
                    "suppressed_rows": summary.suppressed_rows,
                },
                ip_address=ip_address,
            )
    return summary


async def create_template_admin(
    pool: asyncpg.Pool, *, tenant_id: int, admin_id: int, name: str, body: str, ip_address: str | None
) -> dict[str, Any]:
    variables = templates_module.extract_variables(body)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO sms_templates (tenant_id, name, body, variables, created_by_admin_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, tenant_id, name, body, variables, is_active
                """,
                tenant_id, name, body, variables, admin_id,
            )
            assert row is not None
            await audit.record(
                conn, admin_id=admin_id, action="sms.templates.create", target_type="sms_template",
                target_id=str(row["id"]), before=None, after={"name": name, "variables": variables},
                ip_address=ip_address,
            )
    return dict(row)


async def create_contact_admin(
    pool: asyncpg.Pool,
    *,
    tenant_id: int,
    admin_id: int,
    phone_e164: str,
    display_name: str | None,
    attributes: dict[str, Any],
    ip_address: str | None,
) -> dict[str, Any]:
    import json

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO sms_contacts (tenant_id, phone_e164, display_name, attributes)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (tenant_id, phone_e164) DO UPDATE
                  SET display_name = EXCLUDED.display_name, attributes = EXCLUDED.attributes, updated_at = now()
                RETURNING id, tenant_id, phone_e164, display_name, attributes, opted_out
                """,
                tenant_id, phone_e164, display_name, json.dumps(attributes),
            )
            assert row is not None
            await audit.record(
                conn, admin_id=admin_id, action="sms.contacts.upsert", target_type="sms_contact",
                target_id=str(row["id"]), before=None, after={"phone_e164": phone_e164}, ip_address=ip_address,
            )
    return dict(row)


async def add_suppression_admin(
    pool: asyncpg.Pool,
    *,
    tenant_id: int,
    admin_id: int,
    phone_e164: str,
    reason: str,
    note: str | None,
    ip_address: str | None,
) -> Suppression:
    async with pool.acquire() as conn:
        async with conn.transaction():
            suppression = await compliance.add_suppression(
                conn, tenant_id=tenant_id, phone_e164=phone_e164, reason=reason, note=note, created_by_admin_id=admin_id
            )
            await audit.record(
                conn, admin_id=admin_id, action="sms.suppressions.add", target_type="sms_suppression",
                target_id=phone_e164, before=None, after={"reason": reason}, ip_address=ip_address,
            )
    return suppression


async def remove_suppression_admin(
    pool: asyncpg.Pool, *, tenant_id: int, admin_id: int, phone_e164: str, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            removed = await compliance.remove_suppression(conn, tenant_id=tenant_id, phone_e164=phone_e164)
            if removed:
                await audit.record(
                    conn, admin_id=admin_id, action="sms.suppressions.remove", target_type="sms_suppression",
                    target_id=phone_e164, before=None, after=None, ip_address=ip_address,
                )
    return removed


async def create_node_admin(
    pool: asyncpg.Pool, *, tenant_id: int, admin_id: int, name: str, fleet_group: str, ip_address: str | None
) -> tuple[DeliveryNode, str]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            node, raw_token = await nodes.create_node(
                conn, tenant_id=tenant_id, name=name, fleet_group=fleet_group, created_by_admin_id=admin_id
            )
            await audit.record(
                conn, admin_id=admin_id, action="sms.nodes.create", target_type="sms_delivery_node",
                target_id=str(node.id), before=None, after={"name": name, "fleet_group": fleet_group},
                ip_address=ip_address,
            )
    return node, raw_token


async def _node_lifecycle_action(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    node_id: int,
    action: str,
    fn: Any,
    ip_address: str | None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchval("SELECT status FROM sms_delivery_nodes WHERE id = $1", node_id)
            await fn(conn, node_id=node_id)
            after = await conn.fetchval("SELECT status FROM sms_delivery_nodes WHERE id = $1", node_id)
            await audit.record(
                conn, admin_id=admin_id, action=action, target_type="sms_delivery_node",
                target_id=str(node_id), before={"status": before}, after={"status": after}, ip_address=ip_address,
            )


async def approve_node_admin(pool: asyncpg.Pool, *, admin_id: int, node_id: int, ip_address: str | None) -> None:
    await _node_lifecycle_action(pool, admin_id=admin_id, node_id=node_id, action="sms.nodes.approve", fn=nodes.approve_node, ip_address=ip_address)


async def disable_node_admin(pool: asyncpg.Pool, *, admin_id: int, node_id: int, ip_address: str | None) -> None:
    await _node_lifecycle_action(pool, admin_id=admin_id, node_id=node_id, action="sms.nodes.disable", fn=nodes.disable_node, ip_address=ip_address)


async def resume_node_admin(pool: asyncpg.Pool, *, admin_id: int, node_id: int, ip_address: str | None) -> None:
    await _node_lifecycle_action(pool, admin_id=admin_id, node_id=node_id, action="sms.nodes.resume", fn=nodes.resume_node, ip_address=ip_address)


async def drain_node_admin(pool: asyncpg.Pool, *, admin_id: int, node_id: int, ip_address: str | None) -> None:
    await _node_lifecycle_action(pool, admin_id=admin_id, node_id=node_id, action="sms.nodes.drain", fn=nodes.drain_node, ip_address=ip_address)


async def set_maintenance_admin(pool: asyncpg.Pool, *, admin_id: int, node_id: int, ip_address: str | None) -> None:
    await _node_lifecycle_action(pool, admin_id=admin_id, node_id=node_id, action="sms.nodes.maintenance", fn=nodes.set_maintenance, ip_address=ip_address)


async def revoke_node_admin(pool: asyncpg.Pool, *, admin_id: int, node_id: int, ip_address: str | None) -> None:
    await _node_lifecycle_action(pool, admin_id=admin_id, node_id=node_id, action="sms.nodes.revoke", fn=nodes.revoke_node, ip_address=ip_address)


async def rotate_node_token_admin(pool: asyncpg.Pool, *, admin_id: int, node_id: int, ip_address: str | None) -> str:
    async with pool.acquire() as conn:
        async with conn.transaction():
            raw_token = await nodes.rotate_token(conn, node_id=node_id)
            await audit.record(
                conn, admin_id=admin_id, action="sms.nodes.rotate_token", target_type="sms_delivery_node",
                target_id=str(node_id), before=None, after=None, ip_address=ip_address,
            )
    return raw_token
