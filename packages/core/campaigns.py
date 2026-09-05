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

    # Unconditional, not merely a `status` filter value an admin has to
    # remember to set: a production-readiness pass found that leaving
    # every audience field blank -- the UI's own documented way to
    # "reach every player" -- resolved to a bare `WHERE true`, which
    # includes self_excluded and banned users. A self-excluded player has
    # made a real, serious responsible-gambling commitment; a promotional
    # broadcast reaching them regardless of what filter an admin happened
    # to pick is exactly the server-side enforcement gap this module's
    # only two callers (the Notification Center's audience count/send
    # path -- confirmed via a repo-wide grep this function has no other
    # caller) must never have. Applied after every other clause, so it
    # can never be weakened by a status filter that requests one of these
    # explicitly (e.g. an admin filtering "status": "banned" to see who
    # would have matched otherwise still gets zero real recipients, not a
    # bypass).
    clauses.append(f"status NOT IN ({_p('self_excluded')}, {_p('banned')})")
    # A currently-cooling-off user (a temporary, self-requested pause --
    # packages/core/responsible_gaming.py::cool_off(), distinct from the
    # permanent `self_excluded` status above) must be excluded the same
    # way. packages/core/responsible_gaming.py::marketing_eligible_
    # user_ids() already encodes this exact rule -- its own docstring
    # calls it "the one query any future marketing/promotional send must
    # use" -- but was never actually wired into this module when it was
    # built; confirmed unused anywhere in the codebase outside its own
    # test file. Mirrored here (a NOT EXISTS subquery rather than a JOIN,
    # so this function's own callers don't need to change their base
    # query) instead of routing through it directly, since this function
    # only ever produces a WHERE-clause fragment, never runs a query of
    # its own.
    clauses.append(
        "id NOT IN (SELECT user_id FROM responsible_gaming_limits WHERE cooloff_until > now())"
    )

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
