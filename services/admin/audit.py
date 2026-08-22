"""Append-only admin action audit log (spec section 26/36: every
administrative action carries admin_id, timestamp, IP, action, before,
after, reason). Postgres itself refuses UPDATE/DELETE on this table (see
the admin-console migration's trigger) -- this module is simply the one
place that ever writes to it, and it only ever inserts.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from packages.core.ledger import AsyncpgConnection


async def record(
    conn: AsyncpgConnection | asyncpg.Pool,
    *,
    admin_id: int,
    action: str,
    target_type: str,
    target_id: str | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    ip_address: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO admin_audit_log
            (admin_id, action, target_type, target_id, before, after, reason, ip_address)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        admin_id,
        action,
        target_type,
        target_id,
        json.dumps(before) if before is not None else None,
        json.dumps(after) if after is not None else None,
        reason,
        ip_address,
    )
