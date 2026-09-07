"""Delivery node fleet management: every node (a MacroDroid-driven Android
phone today; the protocol never assumes that) is treated as untrusted
until it presents a valid per-node credential -- never one shared static
token (the mistake the existing Telebirr MacroDroid ingestion route
deliberately accepts for its own much narrower, single-device use case;
a multi-node fleet needs per-node revocation, which a shared token can't
give).

Registration is admin-only (POST via services/sms/app.py, sms:nodes:manage)
-- there is no public self-registration endpoint, since that would let
anyone mint an unlimited number of "nodes." The raw credential is
returned exactly once, at creation and at each rotation, and only its
sha256 digest is ever persisted -- the same "shown once, never
retrievable again" discipline services/admin/auth.py already uses for a
TOTP secret.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg

from packages.core.ledger import AsyncpgConnection

NodeStatus = str  # 'pending' | 'active' | 'maintenance' | 'disabled' | 'draining' | 'revoked'
DisplayStatus = str  # a stored NodeStatus, or the computed 'degraded' / 'offline' overlay

# A node that hasn't heartbeat-ed within this window is scored down hard
# regardless of its recent delivery history -- silence is itself a health
# signal, not a neutral one. Also the threshold for the computed OFFLINE
# display status (see display_status() below).
HEARTBEAT_STALE_AFTER = timedelta(minutes=5)

# A stored 'active' node whose health score has fallen below this is
# shown as DEGRADED -- still eligible for work (a real product decision:
# degraded means "watch this node," not "stop routing to it"; an admin
# who wants that stronger response still has drain_node/disable_node),
# but visibly flagged rather than looking identical to a fully healthy one.
DEGRADED_HEALTH_THRESHOLD = 50


class NodeNotFound(Exception):
    pass


@dataclass(frozen=True)
class DeliveryNode:
    id: int
    tenant_id: int
    name: str
    fleet_group: str
    status: NodeStatus
    health_score: int
    last_heartbeat_at: datetime | None
    app_version: str | None
    max_concurrent_jobs: int
    protocol_version: int


def display_status(node: DeliveryNode, *, now: datetime | None = None) -> DisplayStatus:
    """DEGRADED and OFFLINE are computed overlays, never stored -- there is
    exactly one place health_score/last_heartbeat_at are written
    (compute_and_store_health_score/record_heartbeat) and exactly one
    place they're interpreted into a display label, so the two can never
    drift out of sync the way a second, independently-updated status
    column could.
    """
    if node.status != "active":
        return node.status
    reference_now = now or datetime.now(timezone.utc)
    if node.last_heartbeat_at is None or (reference_now - node.last_heartbeat_at) > HEARTBEAT_STALE_AFTER:
        return "offline"
    if node.health_score < DEGRADED_HEALTH_THRESHOLD:
        return "degraded"
    return "active"


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


_NODE_COLUMNS = (
    "id, tenant_id, name, fleet_group, status, health_score, last_heartbeat_at, "
    "app_version, max_concurrent_jobs, protocol_version"
)


def _row_to_node(row: asyncpg.Record) -> DeliveryNode:
    return DeliveryNode(
        id=row["id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        fleet_group=row["fleet_group"],
        status=row["status"],
        health_score=row["health_score"],
        last_heartbeat_at=row["last_heartbeat_at"],
        app_version=row["app_version"],
        max_concurrent_jobs=row["max_concurrent_jobs"],
        protocol_version=row["protocol_version"],
    )


async def create_node(
    conn: AsyncpgConnection, *, tenant_id: int, name: str, fleet_group: str, created_by_admin_id: int | None
) -> tuple[DeliveryNode, str]:
    """Returns (node, raw_token). Starts in 'pending' -- a genuinely
    separate approve_node() call is required before it can receive work,
    a real two-step registration-then-approval flow, not registration
    doubling as approval.
    """
    raw_token = secrets.token_urlsafe(32)
    row = await conn.fetchrow(
        f"""
        INSERT INTO sms_delivery_nodes (tenant_id, name, fleet_group, token_hash, created_by_admin_id)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {_NODE_COLUMNS}
        """,
        tenant_id,
        name,
        fleet_group,
        _hash_token(raw_token),
        created_by_admin_id,
    )
    assert row is not None
    return _row_to_node(row), raw_token


async def rotate_token(conn: AsyncpgConnection, *, node_id: int) -> str:
    raw_token = secrets.token_urlsafe(32)
    result = await conn.execute(
        "UPDATE sms_delivery_nodes SET token_hash = $1, updated_at = now() WHERE id = $2",
        _hash_token(raw_token),
        node_id,
    )
    if result == "UPDATE 0":
        raise NodeNotFound(str(node_id))
    return raw_token


async def _set_status(conn: AsyncpgConnection, *, node_id: int, status: NodeStatus) -> None:
    result = await conn.execute(
        "UPDATE sms_delivery_nodes SET status = $1, updated_at = now() WHERE id = $2",
        status,
        node_id,
    )
    if result == "UPDATE 0":
        raise NodeNotFound(str(node_id))


async def approve_node(conn: AsyncpgConnection, *, node_id: int) -> None:
    await _set_status(conn, node_id=node_id, status="active")


async def disable_node(conn: AsyncpgConnection, *, node_id: int) -> None:
    await _set_status(conn, node_id=node_id, status="disabled")


async def set_maintenance(conn: AsyncpgConnection, *, node_id: int) -> None:
    """Distinct from disable_node -- same routing effect (no new work),
    different operator-facing meaning: "temporarily out for planned work,"
    not "an admin turned this off, reason unknown."
    """
    await _set_status(conn, node_id=node_id, status="maintenance")


async def resume_node(conn: AsyncpgConnection, *, node_id: int) -> None:
    await _set_status(conn, node_id=node_id, status="active")


async def drain_node(conn: AsyncpgConnection, *, node_id: int) -> None:
    """Draining stops new work assignment (fetch_job refuses a draining
    node) without corrupting any job already in flight on it -- those
    complete or reconcile normally through the existing timeout sweep,
    exactly like any other in-flight message.
    """
    await _set_status(conn, node_id=node_id, status="draining")


async def revoke_node(conn: AsyncpgConnection, *, node_id: int) -> None:
    """Irreversible in this pass (no un-revoke) -- a revoked node's
    credential is permanently untrusted; standing up the physical device
    again means registering it as a new node with a new credential.
    """
    await _set_status(conn, node_id=node_id, status="revoked")


async def authenticate_node(conn: AsyncpgConnection | asyncpg.Pool, *, raw_token: str) -> DeliveryNode | None:
    """Returns None for an unknown or revoked credential -- a revoked node
    cannot even authenticate, let alone receive work (a stronger guarantee
    than merely refusing it a job while still accepting its calls).
    Pending/disabled/draining nodes DO authenticate, so fetch_job can give
    them a clear, honest reason for having no work rather than a bare 401.
    """
    row = await conn.fetchrow(
        f"SELECT {_NODE_COLUMNS} FROM sms_delivery_nodes WHERE token_hash = $1",
        _hash_token(raw_token),
    )
    if row is None or row["status"] == "revoked":
        return None
    return _row_to_node(row)


async def record_heartbeat(
    conn: AsyncpgConnection,
    *,
    node_id: int,
    app_version: str | None,
    capabilities: dict[str, object],
    max_concurrent_jobs: int | None = None,
    protocol_version: int | None = None,
) -> None:
    """max_concurrent_jobs/protocol_version are optional -- an older node
    (or a MacroDroid device whose macro was never updated to send them)
    simply keeps whatever value it last advertised (default 1 / 1 for a
    node that has never sent one), never silently reset to a default on
    every heartbeat.
    """
    import json

    await conn.execute(
        """
        UPDATE sms_delivery_nodes
        SET last_heartbeat_at = now(), app_version = $2, capabilities = $3::jsonb,
            max_concurrent_jobs = COALESCE($4, max_concurrent_jobs),
            protocol_version = COALESCE($5, protocol_version),
            updated_at = now()
        WHERE id = $1
        """,
        node_id,
        app_version,
        json.dumps(capabilities),
        max_concurrent_jobs,
        protocol_version,
    )


async def current_assigned_count(conn: AsyncpgConnection, *, node_id: int) -> int:
    """How many messages this node currently holds in a non-terminal
    in-flight state -- the real, live number fetch-job's own capacity gate
    compares against max_concurrent_jobs, not a cached or self-reported
    count a compromised or buggy node could lie about.
    """
    count = await conn.fetchval(
        "SELECT count(*) FROM sms_messages WHERE assigned_node_id = $1 AND status IN ('assigned', 'sending')",
        node_id,
    )
    return int(count)


async def compute_and_store_health_score(conn: AsyncpgConnection, *, node_id: int) -> int:
    """A real, deterministic score (0-100) from two real, observable
    signals -- never a fabricated or ML-derived number. Heartbeat recency
    is the harder gate (a silent node cannot be "healthy" no matter its
    delivery history); delivery success rate over its last 20 real
    attempts (a fixed recent window, not the node's entire lifetime, so
    an old outage doesn't permanently depress a since-recovered node)
    contributes the rest.
    """
    row = await conn.fetchrow(
        "SELECT last_heartbeat_at FROM sms_delivery_nodes WHERE id = $1",
        node_id,
    )
    if row is None:
        raise NodeNotFound(str(node_id))
    last_heartbeat_at: datetime | None = row["last_heartbeat_at"]
    if last_heartbeat_at is None or (datetime.now(timezone.utc) - last_heartbeat_at) > HEARTBEAT_STALE_AFTER:
        score = 0
    else:
        recent = await conn.fetch(
            """
            SELECT outcome FROM sms_delivery_attempts
            WHERE node_id = $1
            ORDER BY id DESC LIMIT 20
            """,
            node_id,
        )
        if not recent:
            score = 70  # freshly heartbeat-ing, no delivery history yet -- provisionally healthy, not perfect
        else:
            successes = sum(1 for r in recent if r["outcome"] == "delivered")
            score = round(30 + 70 * successes / len(recent))
    await conn.execute(
        "UPDATE sms_delivery_nodes SET health_score = $1, updated_at = now() WHERE id = $2",
        score,
        node_id,
    )
    return score


async def get_node(conn: AsyncpgConnection, *, node_id: int) -> DeliveryNode:
    """A fresh read of a single node -- DeliveryNode is an immutable
    snapshot (a frozen dataclass), so any caller holding one from before a
    heartbeat/status change needs to re-fetch rather than assume it's
    still current, exactly like this codebase's other domain objects.
    """
    row = await conn.fetchrow(f"SELECT {_NODE_COLUMNS} FROM sms_delivery_nodes WHERE id = $1", node_id)
    if row is None:
        raise NodeNotFound(str(node_id))
    return _row_to_node(row)


async def list_nodes(conn: AsyncpgConnection, *, tenant_id: int) -> list[DeliveryNode]:
    rows = await conn.fetch(
        f"SELECT {_NODE_COLUMNS} FROM sms_delivery_nodes WHERE tenant_id = $1 ORDER BY id",
        tenant_id,
    )
    return [_row_to_node(row) for row in rows]
