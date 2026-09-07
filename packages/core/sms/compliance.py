"""Suppression list management: admin-curated for this pass (no inbound
SMS gateway exists yet to auto-detect a STOP reply -- see DECISIONS.md).
A phone in this table is excluded unconditionally by
packages/core/sms/audience.py's resolve_recipients and re-checked again
at dispatch time by packages/core/sms/messages.py -- belt and suspenders,
never a single point that, if skipped, could send to a suppressed
contact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from packages.core.ledger import AsyncpgConnection

SuppressionReason = str  # 'opt_out' | 'manual' | 'bounce' | 'complaint'


@dataclass(frozen=True)
class Suppression:
    id: int
    tenant_id: int
    phone_e164: str
    reason: SuppressionReason
    note: str | None
    created_at: datetime


async def add_suppression(
    conn: AsyncpgConnection,
    *,
    tenant_id: int,
    phone_e164: str,
    reason: SuppressionReason,
    note: str | None,
    created_by_admin_id: int | None,
) -> Suppression:
    row = await conn.fetchrow(
        """
        INSERT INTO sms_suppressions (tenant_id, phone_e164, reason, note, created_by_admin_id)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (tenant_id, phone_e164) DO UPDATE
          SET reason = EXCLUDED.reason, note = EXCLUDED.note, created_by_admin_id = EXCLUDED.created_by_admin_id
        RETURNING id, tenant_id, phone_e164, reason, note, created_at
        """,
        tenant_id,
        phone_e164,
        reason,
        note,
        created_by_admin_id,
    )
    assert row is not None
    return Suppression(**dict(row))


async def remove_suppression(conn: AsyncpgConnection, *, tenant_id: int, phone_e164: str) -> bool:
    result = await conn.execute(
        "DELETE FROM sms_suppressions WHERE tenant_id = $1 AND phone_e164 = $2",
        tenant_id,
        phone_e164,
    )
    return result != "DELETE 0"


async def list_suppressions(conn: AsyncpgConnection, *, tenant_id: int) -> list[Suppression]:
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, phone_e164, reason, note, created_at
        FROM sms_suppressions WHERE tenant_id = $1 ORDER BY created_at DESC
        """,
        tenant_id,
    )
    return [Suppression(**dict(row)) for row in rows]
