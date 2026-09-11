"""Per-device authentication and health tracking for the automated
Android/MacroDroid Telebirr ingestion path (migrations/versions/
e3a7c9f01b2d). This module never touches payment_evidence and never
calls the parser -- services/payments/telebirr_ingest.py's
ingest_sms_evidence() remains the single canonical pipeline every
ingestion source (Telegram agent, legacy shared-token MacroDroid, this
per-device path) converges on. All this module does is answer "which
registered device (if any) does this bearer token belong to" and keep
that device's own operational counters current.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import asyncpg
import structlog

from packages.core import metrics
from packages.core.device_auth import hash_device_token

logger = structlog.get_logger()

# Evidence that something is actually wrong with the message itself, not
# just "already seen" (duplicate) or "a valid message the business rules
# happened to reject" (ingested_rejected still means the device did its
# job -- it delivered a parseable SMS). Mirrors telebirr_ingest.py's own
# IngestStatus literals without importing them, to keep this module
# usable even if that module's status vocabulary ever grows -- an unknown
# future status falls through to "success" (the device delivered
# *something* the pipeline could act on), never silently miscounted as a
# device fault.
_FAILURE_STATUSES = frozenset({"unparseable", "conflicting_duplicate"})


@dataclass(frozen=True)
class DeviceIdentity:
    id: int
    device_id: str
    device_name: str


@dataclass(frozen=True)
class DeviceAuthResult:
    """identity is set only for a live, active device whose token matched.
    revoked_device_pk is set when the token matched a real device row that
    is no longer active -- distinct from "no device registered this token
    at all" so record_auth_failure() can still attribute the attempt to a
    real device for the admin console to see (e.g. an old phone still
    trying to submit after its credential was revoked).
    """

    identity: DeviceIdentity | None
    revoked_device_pk: int | None = None


async def authenticate_device(pool: asyncpg.Pool, token: str) -> DeviceAuthResult:
    row = await pool.fetchrow(
        "SELECT id, device_id, device_name, status FROM ingestion_devices WHERE token_hash = $1",
        hash_device_token(token),
    )
    if row is None:
        return DeviceAuthResult(identity=None)
    if row["status"] != "active":
        return DeviceAuthResult(identity=None, revoked_device_pk=row["id"])
    return DeviceAuthResult(
        identity=DeviceIdentity(id=row["id"], device_id=row["device_id"], device_name=row["device_name"])
    )


async def record_auth_failure(pool: asyncpg.Pool, *, device_pk: int | None) -> None:
    # None means the presented token matched no device row at all (most
    # likely: this request is using the legacy shared token instead, or a
    # typo) -- nothing to attribute an auth failure to.
    if device_pk is None:
        return
    metrics.ingestion_device_auth_failures_total.labels(reason="revoked_device").inc()
    await pool.execute(
        "UPDATE ingestion_devices SET auth_failure_count = auth_failure_count + 1 WHERE id = $1",
        device_pk,
    )


async def record_ingestion_outcome(
    pool: asyncpg.Pool, *, device_pk: int, device_id: str, outcome_status: str, reason: str | None
) -> None:
    if outcome_status == "duplicate":
        await pool.execute(
            "UPDATE ingestion_devices SET last_seen_at = now(), duplicate_count = duplicate_count + 1 "
            "WHERE id = $1",
            device_pk,
        )
    elif outcome_status in _FAILURE_STATUSES:
        await pool.execute(
            "UPDATE ingestion_devices SET last_seen_at = now(), last_error_at = now(), "
            "last_error_reason = $2, failure_count = failure_count + 1 WHERE id = $1",
            device_pk,
            reason,
        )
    else:
        await pool.execute(
            "UPDATE ingestion_devices SET last_seen_at = now(), last_success_at = now(), "
            "success_count = success_count + 1 WHERE id = $1",
            device_pk,
        )
        metrics.ingestion_device_last_success_timestamp.labels(device_id=device_id).set(time.time())


# Same threshold services/admin/queries.py's list_ingestion_devices() uses
# to badge a device "degraded" in the console -- kept as its own constant
# here rather than imported from that admin-service module (this is the
# payments-service side, and the two products already repeat small
# "how long before we call this stale" constants independently elsewhere
# in this codebase, e.g. payout_worker.py's CLAIM_STALE_AFTER_MS).
_DEGRADED_AFTER_HOURS = 6


async def check_for_degraded_devices(pool: asyncpg.Pool) -> None:
    """Part of a periodic sweep (services/payments/payout_worker.py), not
    a new alerting channel -- this only ever logs a structured warning per
    degraded device, which existing log-based ops tooling can already
    alert on. See docs/TELEBIRR_MACRODROID_QUICK_SETUP.md's "PRIMARY
    INGESTION DEGRADED" section for what an operator does once they see
    one: check the phone, and know the Telegram payment-agent path
    remains available as a fallback in the meantime. Never disables a
    device or changes any financial behavior on its own.
    """
    rows = await pool.fetch(
        """
        SELECT device_id, device_name, last_success_at, created_at
        FROM ingestion_devices
        WHERE status = 'active'
          AND (last_success_at IS NULL OR last_success_at < now() - ($1 * interval '1 hour'))
          -- A brand-new device that has simply never been set up on the
          -- phone yet is "awaiting first ingestion", not "degraded" --
          -- only warn once it's had the same grace period to prove
          -- itself as an established device would need to go quiet.
          AND created_at < now() - ($1 * interval '1 hour')
        """,
        _DEGRADED_AFTER_HOURS,
    )
    for row in rows:
        logger.warning(
            "ingestion_device_degraded",
            device_id=row["device_id"],
            device_name=row["device_name"],
            last_success_at=row["last_success_at"].isoformat() if row["last_success_at"] else None,
        )
