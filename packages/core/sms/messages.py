"""Message claiming, delivery-result recording, retry classification, and
timeout-based reconciliation.

Claiming reuses this codebase's own established `FOR UPDATE SKIP LOCKED`
pattern for "many independent workers pulling from one shared queue" --
the same primitive already used elsewhere in this repo for round/room
claiming, applied here to delivery nodes pulling sms_messages instead.

Retry model (the directive's own "exactly-once internal state transitions
plus idempotent delivery intent plus explicit unknown execution handling
-- never a pretense of perfect exactly-once external delivery"): a
message can be retried up to MAX_DELIVERY_ATTEMPTS times when its error is
one of RETRYABLE_ERROR_CLASSES; PERMANENT errors and attempts exhausted
both route to 'dead_letter', a real terminal state an operator must look
at, never a silently-dropped message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import asyncpg

from packages.core import metrics
from packages.core.ledger import AsyncpgConnection
from packages.core.sms import campaigns as campaigns_module

ErrorClass = Literal["temporary", "permanent", "network", "timeout", "node_failure", "unknown"]
RETRYABLE_ERROR_CLASSES: frozenset[str] = frozenset({"temporary", "network", "timeout", "node_failure", "unknown"})
MAX_DELIVERY_ATTEMPTS = 3

# How long a message may sit in 'assigned'/'sending' with no result
# reported before the reconciliation sweep treats it as UNKNOWN_EXECUTION
# rather than trusting it's still genuinely in flight.
IN_FLIGHT_TIMEOUT = timedelta(minutes=5)

def _priority_rank_sql(column: str) -> str:
    return f"CASE {column} WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END"


class MessageNotFound(Exception):
    pass


class NotOwnedByNode(Exception):
    """The message is not currently assigned to the calling node -- e.g. it
    already got reconciled to 'unknown' and reassigned elsewhere while a
    slow/partitioned node was still trying to report its own result. The
    node's stale report is rejected, never blindly applied.
    """


@dataclass(frozen=True)
class Message:
    id: int
    tenant_id: int
    campaign_id: int | None
    contact_id: int | None
    phone_e164: str
    body: str
    segment_count: int
    priority: str
    status: str
    assigned_node_id: int | None
    attempt_count: int


def _row_to_message(row: asyncpg.Record) -> Message:
    return Message(
        id=row["id"], tenant_id=row["tenant_id"], campaign_id=row["campaign_id"],
        contact_id=row["contact_id"], phone_e164=row["phone_e164"], body=row["body"],
        segment_count=row["segment_count"], priority=row["priority"], status=row["status"],
        assigned_node_id=row["assigned_node_id"], attempt_count=row["attempt_count"],
    )


_MESSAGE_COLUMNS = (
    "id, tenant_id, campaign_id, contact_id, phone_e164, body, segment_count, "
    "priority, status, assigned_node_id, attempt_count"
)


async def claim_next_message(conn: AsyncpgConnection, *, tenant_id: int, node_id: int) -> Message | None:
    """Atomically claims the oldest queued message for this tenant, in
    priority order, skipping any campaign currently paused. A paused
    campaign's already-queued messages are left untouched in place
    (`preserve queue state`) -- they simply aren't offered to any node
    until the campaign resumes.
    """
    row = await conn.fetchrow(
        f"""
        UPDATE sms_messages m
        SET status = 'assigned', assigned_node_id = $2, assigned_at = now(),
            attempt_count = attempt_count + 1, updated_at = now()
        WHERE m.id = (
            SELECT m2.id FROM sms_messages m2
            LEFT JOIN sms_campaigns c ON c.id = m2.campaign_id
            WHERE m2.tenant_id = $1 AND m2.status = 'queued'
              AND (c.id IS NULL OR c.status <> 'paused')
            ORDER BY {_priority_rank_sql('m2.priority')}, m2.created_at
            FOR UPDATE OF m2 SKIP LOCKED
            LIMIT 1
        )
        RETURNING {_MESSAGE_COLUMNS}
        """,
        tenant_id,
        node_id,
    )
    if row is None:
        return None
    message = _row_to_message(row)
    await conn.execute(
        """
        INSERT INTO sms_delivery_attempts (message_id, node_id, attempt_number, outcome, started_at)
        VALUES ($1, $2, $3, 'accepted', now())
        """,
        message.id,
        node_id,
        message.attempt_count,
    )
    return message


async def start_message(conn: AsyncpgConnection, *, message_id: int, node_id: int) -> Message:
    row = await conn.fetchrow(
        f"""
        UPDATE sms_messages SET status = 'sending', updated_at = now()
        WHERE id = $1 AND assigned_node_id = $2 AND status = 'assigned'
        RETURNING {_MESSAGE_COLUMNS}
        """,
        message_id,
        node_id,
    )
    if row is None:
        existing = await conn.fetchrow("SELECT assigned_node_id FROM sms_messages WHERE id = $1", message_id)
        if existing is None:
            raise MessageNotFound(str(message_id))
        raise NotOwnedByNode(f"message {message_id} is not assigned to node {node_id}")
    await conn.execute(
        """
        UPDATE sms_delivery_attempts SET outcome = 'started'
        WHERE message_id = $1 AND attempt_number = (SELECT attempt_count FROM sms_messages WHERE id = $1)
        """,
        message_id,
    )
    return _row_to_message(row)


async def report_result(
    conn: AsyncpgConnection,
    *,
    message_id: int,
    node_id: int,
    outcome: Literal["delivered", "failed", "unknown"],
    error_class: ErrorClass | None,
    raw_provider_response: str | None,
) -> Message:
    """The one path a node uses to report what happened. A 'delivered'
    result is always terminal. A 'failed' or 'unknown' result is retried
    (re-queued, unassigned) when attempts remain and the error is
    retryable; otherwise it becomes 'dead_letter' -- a real terminal state
    an operator must look at, never a silent drop.
    """
    row = await conn.fetchrow(
        "SELECT tenant_id, assigned_node_id, attempt_count, campaign_id FROM sms_messages WHERE id = $1 FOR UPDATE",
        message_id,
    )
    if row is None:
        raise MessageNotFound(str(message_id))
    if row["assigned_node_id"] != node_id:
        raise NotOwnedByNode(f"message {message_id} is not assigned to node {node_id}")

    await conn.execute(
        """
        UPDATE sms_delivery_attempts
        SET outcome = $3, error_class = $4, raw_provider_response = $5, completed_at = now()
        WHERE message_id = $1 AND attempt_number = $2
        """,
        message_id, row["attempt_count"], outcome, error_class, raw_provider_response,
    )
    metrics.sms_delivery_attempts_total.labels(outcome=outcome).inc()

    if outcome == "delivered":
        new_status = "delivered"
    else:
        can_retry = (
            row["attempt_count"] < MAX_DELIVERY_ATTEMPTS
            and (error_class is None or error_class in RETRYABLE_ERROR_CLASSES)
        )
        new_status = "queued" if can_retry else "dead_letter"

    if new_status == "queued":
        result = await conn.fetchrow(
            f"""
            UPDATE sms_messages
            SET status = 'queued', assigned_node_id = NULL, last_error_class = $2, updated_at = now()
            WHERE id = $1 RETURNING {_MESSAGE_COLUMNS}
            """,
            message_id, error_class,
        )
    else:
        result = await conn.fetchrow(
            f"""
            UPDATE sms_messages
            SET status = $2, assigned_node_id = NULL, last_error_class = $3, updated_at = now()
            WHERE id = $1 RETURNING {_MESSAGE_COLUMNS}
            """,
            message_id, new_status, error_class,
        )
    assert result is not None
    message = _row_to_message(result)
    if message.campaign_id is not None and new_status in ("delivered", "dead_letter"):
        await campaigns_module.maybe_complete_campaign(conn, campaign_id=message.campaign_id)
    return message


async def reconcile_stale_in_flight(
    conn: AsyncpgConnection, *, tenant_id: int, stale_after: timedelta = IN_FLIGHT_TIMEOUT
) -> int:
    """Finds messages stuck in 'assigned'/'sending' with no result
    reported for longer than stale_after and moves each through the same
    retryable-vs-dead_letter decision report_result uses, with
    error_class='timeout' -- UNKNOWN_EXECUTION, never blindly assumed
    delivered nor blindly resent without accounting for it. Returns the
    number reconciled.
    """
    cutoff = datetime.now(timezone.utc) - stale_after
    rows = await conn.fetch(
        """
        SELECT id, assigned_node_id, attempt_count, campaign_id
        FROM sms_messages
        WHERE tenant_id = $1 AND status IN ('assigned', 'sending') AND assigned_at < $2
        FOR UPDATE SKIP LOCKED
        """,
        tenant_id, cutoff,
    )
    reconciled = 0
    for row in rows:
        node_id = row["assigned_node_id"]
        assert node_id is not None
        await conn.execute(
            """
            INSERT INTO sms_delivery_attempts (message_id, node_id, attempt_number, outcome, error_class, completed_at)
            VALUES ($1, $2, $3, 'unknown', 'timeout', now())
            ON CONFLICT (message_id, attempt_number) DO UPDATE
              SET outcome = 'unknown', error_class = 'timeout', completed_at = now()
            """,
            row["id"], node_id, row["attempt_count"],
        )
        can_retry = row["attempt_count"] < MAX_DELIVERY_ATTEMPTS
        new_status = "queued" if can_retry else "dead_letter"
        await conn.execute(
            """
            UPDATE sms_messages
            SET status = $2, assigned_node_id = NULL, last_error_class = 'timeout', updated_at = now()
            WHERE id = $1
            """,
            row["id"], new_status,
        )
        metrics.sms_messages_reconciled_total.labels(to_status=new_status).inc()
        reconciled += 1
        if row["campaign_id"] is not None and new_status == "dead_letter":
            await campaigns_module.maybe_complete_campaign(conn, campaign_id=row["campaign_id"])
    return reconciled


async def get_message(conn: AsyncpgConnection, *, message_id: int) -> Message:
    row = await conn.fetchrow(f"SELECT {_MESSAGE_COLUMNS} FROM sms_messages WHERE id = $1", message_id)
    if row is None:
        raise MessageNotFound(str(message_id))
    return _row_to_message(row)


async def list_messages(
    conn: AsyncpgConnection, *, tenant_id: int, campaign_id: int | None = None, status: str | None = None, limit: int = 200
) -> list[Message]:
    conditions = ["tenant_id = $1"]
    args: list[object] = [tenant_id]
    if campaign_id is not None:
        args.append(campaign_id)
        conditions.append(f"campaign_id = ${len(args)}")
    if status is not None:
        args.append(status)
        conditions.append(f"status = ${len(args)}")
    args.append(limit)
    rows = await conn.fetch(
        f"SELECT {_MESSAGE_COLUMNS} FROM sms_messages WHERE {' AND '.join(conditions)} "
        f"ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_row_to_message(row) for row in rows]
