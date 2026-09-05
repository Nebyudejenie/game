"""Notification Center campaign delivery worker.

Runs inside the bot process (services/bot/app.py's own lifespan starts
it, alongside notification_relay.run_forever()) since it's the process
already holding the shared Notifier/Bot instance every outbound message
goes through -- campaigns reuse that exact pipeline, they never get a
second one.

Crash-safety by construction, not a separate recovery sweep: a
campaign's own status only ever advances 'queued'/'scheduled' -> 'sending'
via one atomic claim (see _claim_due_campaigns), but *dispatching* a
sending campaign's still-pending deliveries is not a one-time event tied
to that claim -- every tick re-scans every 'sending' campaign for
deliveries still sitting at 'pending' and enqueues exactly those. A crash
between marking a delivery 'processing' and it actually reaching
Notifier just leaves it 'pending' (never advanced), so the very next
tick picks it up again -- the same outcome a dedicated startup recovery
sweep would produce, without needing one.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core.campaigns import create_deliveries, resolve_audience_user_ids
from packages.core.notifications import enqueue_campaign_message

logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 10
DISPATCH_BATCH_SIZE = 200


async def _claim_due_campaigns(pool: asyncpg.Pool) -> list[int]:
    """Atomically transitions every currently-due campaign to 'sending' in
    one statement -- FOR UPDATE SKIP LOCKED means a second worker process
    (if this is ever horizontally scaled) never claims the same row this
    one is already claiming, without needing a separate distributed lock
    the way services/engine/round_engine.py's own room-level RoomLock
    does for a much longer-lived resource.
    """
    rows = await pool.fetch(
        """
        UPDATE notification_campaigns
        SET status = 'sending', started_at = COALESCE(started_at, now())
        WHERE id IN (
            SELECT id FROM notification_campaigns
            WHERE status IN ('queued', 'scheduled')
              AND (scheduled_at IS NULL OR scheduled_at <= now())
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """
    )
    return [row["id"] for row in rows]


async def _resolve_and_seed_deliveries(pool: asyncpg.Pool, campaign_id: int) -> None:
    """Idempotent: create_deliveries() is itself ON CONFLICT DO NOTHING,
    and recipient_count is only ever set once a real audience was
    resolved -- calling this again for a campaign that already has
    deliveries (a resumed, previously-interrupted 'sending' campaign)
    changes nothing.
    """
    campaign = await pool.fetchrow(
        "SELECT audience_filter, exclude_user_ids, recipient_count FROM notification_campaigns "
        "WHERE id = $1",
        campaign_id,
    )
    if campaign is None or campaign["recipient_count"] is not None:
        return
    audience_filter = campaign["audience_filter"]
    if isinstance(audience_filter, str):
        audience_filter = json.loads(audience_filter)
    exclude_ids = list(campaign["exclude_user_ids"] or [])

    user_ids = await resolve_audience_user_ids(pool, audience_filter, exclude_ids)
    await create_deliveries(pool, campaign_id=campaign_id, user_ids=user_ids)
    await pool.execute(
        "UPDATE notification_campaigns SET recipient_count = $2 WHERE id = $1",
        campaign_id,
        len(user_ids),
    )


async def _dispatch_pending_deliveries(pool: asyncpg.Pool, redis: Redis, campaign_id: int) -> int:
    """Enqueues up to DISPATCH_BATCH_SIZE still-pending deliveries for one
    sending campaign. Marking 'processing' happens per-row, immediately
    before that row's own enqueue -- deliberately in that order, and
    deliberately not atomic across Postgres and Redis: a crash between
    the two never sends the same delivery twice (this query only ever
    selects 'pending' rows, so a row already marked 'processing' is never
    re-enqueued), matching this feature's explicit priority of no
    duplicate delivery over no lost delivery. The accepted cost is the
    narrow inverse case: a crash landing in the gap between the UPDATE
    committing and enqueue_campaign_message() completing leaves that one
    row stuck at 'processing' forever (nothing re-selects it, and its
    campaign never finalizes) -- not silent, since such a campaign stays
    visibly 'sending' with a permanently non-terminal delivery in the
    admin console's own Deliveries view, just not self-healing. See
    docs/NOTIFICATION_CENTER_ARCHITECTURE.md's "Crash safety" section.
    """
    campaign = await pool.fetchrow(
        "SELECT title, body FROM notification_campaigns WHERE id = $1", campaign_id
    )
    if campaign is None:
        return 0
    text = f"{campaign['title']}\n\n{campaign['body']}"

    rows = await pool.fetch(
        "SELECT nd.id AS delivery_id, u.telegram_id FROM notification_deliveries nd "
        "JOIN users u ON u.id = nd.user_id "
        "WHERE nd.campaign_id = $1 AND nd.status = 'pending' "
        "LIMIT $2",
        campaign_id,
        DISPATCH_BATCH_SIZE,
    )
    for row in rows:
        await pool.execute(
            "UPDATE notification_deliveries SET status = 'processing' WHERE id = $1", row["delivery_id"]
        )
        await enqueue_campaign_message(
            redis, telegram_id=row["telegram_id"], text=text, delivery_id=row["delivery_id"]
        )
    return len(rows)


async def _finalize_completed_campaigns(pool: asyncpg.Pool) -> int:
    """A 'sending' campaign becomes COMPLETED/PARTIALLY_FAILED/FAILED only
    once every one of its delivery rows has reached a terminal state
    (delivered/failed/cancelled) -- checked here rather than the instant
    the last enqueue happens, since actual delivery is asynchronous
    (Notifier's own rate pace + 429 backoff) and can finish well after
    every recipient was handed to the relay.
    """
    sending = await pool.fetch("SELECT id FROM notification_campaigns WHERE status = 'sending'")
    finalized = 0
    for row in sending:
        campaign_id = row["id"]
        counts = await pool.fetchrow(
            """
            SELECT
              count(*) FILTER (WHERE status = 'delivered') AS delivered,
              count(*) FILTER (WHERE status = 'failed') AS failed,
              count(*) FILTER (WHERE status NOT IN ('delivered', 'failed', 'cancelled')) AS pending
            FROM notification_deliveries WHERE campaign_id = $1
            """,
            campaign_id,
        )
        assert counts is not None  # a bare COUNT(*) aggregate always returns exactly one row
        if counts["pending"] > 0:
            continue
        delivered, failed = counts["delivered"], counts["failed"]
        if failed == 0:
            new_status = "completed"
        elif delivered == 0:
            new_status = "failed"
        else:
            new_status = "partially_failed"
        await pool.execute(
            "UPDATE notification_campaigns SET status = $2, completed_at = now(), "
            "delivered_count = $3, failed_count = $4 WHERE id = $1",
            campaign_id,
            new_status,
            delivered,
            failed,
        )
        finalized += 1
    return finalized


async def process_once(pool: asyncpg.Pool, redis: Redis) -> bool:
    """One tick, built as a single-shot step so tests can drive it
    deterministically -- run_forever() below is just this called
    repeatedly. Returns whether any real work happened, purely to let
    run_forever() skip its poll sleep right after a busy tick.
    """
    claimed = await _claim_due_campaigns(pool)

    sending_ids = [row["id"] for row in await pool.fetch(
        "SELECT id FROM notification_campaigns WHERE status = 'sending'"
    )]
    dispatched = 0
    for campaign_id in sending_ids:
        await _resolve_and_seed_deliveries(pool, campaign_id)
        dispatched += await _dispatch_pending_deliveries(pool, redis, campaign_id)

    finalized = await _finalize_completed_campaigns(pool)
    return bool(claimed) or dispatched > 0 or finalized > 0


async def run_forever(pool: asyncpg.Pool, redis: Redis) -> None:
    while True:
        try:
            did_work = await process_once(pool, redis)
        except Exception:
            logger.exception("campaign_worker_tick_failed")
            did_work = False
        if not did_work:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
