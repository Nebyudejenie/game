"""Notification Center: audience resolution and delivery bookkeeping.

Pure, reusable logic shared by the admin API (services/admin) and the
campaign worker (services/bot/campaign_worker.py) -- neither depends on
the other, both depend on this. Audience filters are a small, fixed JSON
shape the backend itself turns into a real parameterized query; a client
never supplies SQL or a raw filter string.

Delivery reuses the existing bot_notifications Redis Stream + Notifier
(packages/core/notifications.py, services/bot/notifier.py,
services/bot/notification_relay.py) -- this module only decides *who*
gets a campaign and records the *outcome* once the existing relay
reports one back; it never talks to Telegram directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg

# Every key here maps to one real, indexed users column -- no dynamic
# column names are ever built from client input, only these fixed clauses.
_STATUS_VALUES = {"active", "limited", "self_excluded", "banned"}
_LANGUAGE_VALUES = {"am", "en", "om", "ti"}


class InvalidAudienceFilter(ValueError):
    pass


def _build_where(filter_: dict[str, Any], exclude_user_ids: list[int]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    def _p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    user_ids = filter_.get("user_ids")
    if user_ids is not None:
        if not isinstance(user_ids, list) or not all(isinstance(v, int) for v in user_ids):
            raise InvalidAudienceFilter("user_ids must be a list of integers")
        clauses.append(f"id = ANY({_p(user_ids)})")

    status = filter_.get("status")
    if status is not None:
        if status not in _STATUS_VALUES:
            raise InvalidAudienceFilter(f"unknown status: {status!r}")
        clauses.append(f"status = {_p(status)}")

    language = filter_.get("language")
    if language is not None:
        if language not in _LANGUAGE_VALUES:
            raise InvalidAudienceFilter(f"unknown language: {language!r}")
        clauses.append(f"language = {_p(language)}")

    min_kyc = filter_.get("min_kyc_level")
    if min_kyc is not None:
        clauses.append(f"kyc_level >= {_p(int(min_kyc))}")

    registered_after = filter_.get("registered_after")
    if registered_after is not None:
        clauses.append(f"created_at >= {_p(registered_after)}")

    registered_before = filter_.get("registered_before")
    if registered_before is not None:
        clauses.append(f"created_at <= {_p(registered_before)}")

    active_since = filter_.get("active_since")
    if active_since is not None:
        clauses.append(f"last_seen_at >= {_p(active_since)}")

    if exclude_user_ids:
        clauses.append(f"id != ALL({_p(list(exclude_user_ids))})")

    where = " AND ".join(clauses) if clauses else "true"
    return where, params


async def count_audience(
    pool: asyncpg.Pool, filter_: dict[str, Any], exclude_user_ids: list[int] | None = None
) -> int:
    where, params = _build_where(filter_, exclude_user_ids or [])
    return await pool.fetchval(f"SELECT count(*) FROM users WHERE {where}", *params)  # type: ignore[no-any-return]


async def resolve_audience_user_ids(
    pool: asyncpg.Pool, filter_: dict[str, Any], exclude_user_ids: list[int] | None = None
) -> list[int]:
    where, params = _build_where(filter_, exclude_user_ids or [])
    rows = await pool.fetch(f"SELECT id FROM users WHERE {where}", *params)
    return [row["id"] for row in rows]


@dataclass(frozen=True)
class CampaignContent:
    campaign_id: int
    title: str
    body: str


async def create_deliveries(pool: asyncpg.Pool, *, campaign_id: int, user_ids: list[int]) -> int:
    """Idempotent by construction: (campaign_id, user_id) is UNIQUE, so
    calling this twice for the same campaign (a worker retry after a
    crash between "resolved audience" and "created delivery rows", for
    instance) never double-creates a recipient's delivery row.
    """
    if not user_ids:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO notification_deliveries (campaign_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT (campaign_id, user_id) DO NOTHING",
            [(campaign_id, uid) for uid in user_ids],
        )
    return len(user_ids)


async def mark_delivery_outcome(
    pool: asyncpg.Pool, *, delivery_id: int, outcome: str, failure_reason: str | None = None
) -> None:
    """outcome is one of "delivered", "failed" -- the two terminal states
    the campaign worker's own retry loop (services/bot/campaign_worker.py)
    ever settles a delivery into. "retrying"/"processing" are set
    separately, before an attempt, not here.
    """
    if outcome == "delivered":
        await pool.execute(
            "UPDATE notification_deliveries SET status = 'delivered', delivered_at = now(), "
            "last_attempt_at = now(), attempt_count = attempt_count + 1 WHERE id = $1",
            delivery_id,
        )
    else:
        await pool.execute(
            "UPDATE notification_deliveries SET status = 'failed', failure_reason = $2, "
            "last_attempt_at = now(), attempt_count = attempt_count + 1 WHERE id = $1",
            delivery_id,
            failure_reason,
        )
