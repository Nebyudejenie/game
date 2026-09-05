"""Notification Center campaign delivery worker.

Runs inside the bot process (services/bot/app.py's own lifespan starts
it, alongside notification_relay.run_forever()) since it's the process
already holding the shared Notifier/Bot instance every outbound message
goes through -- campaigns reuse that exact pipeline, they never get a
second one.

Crash-safety by construction for most of the pipeline: a campaign's own
status only ever advances 'queued'/'scheduled' -> 'sending' via one
atomic claim (see _claim_due_campaigns), but *dispatching* a sending
campaign's still-pending deliveries is not a one-time event tied to that
claim -- every tick re-scans every 'sending' campaign for deliveries
still sitting at 'pending' and enqueues exactly those. The one narrower
window this didn't originally cover -- a delivery stuck at 'processing'
because the process died between marking it and actually enqueueing it
-- is closed by _reclaim_stuck_deliveries(), run every tick before
dispatch: any 'processing' row idle past RECLAIM_STUCK_AFTER_SECONDS
resets to 'pending' and gets picked up by the normal dispatch path on
this same tick. This can occasionally cause a delivery to be enqueued
twice (once before a crash, once by the reclaim, if the original enqueue
had actually succeeded) -- notification_relay.py::process_one() is what
guarantees that never becomes a real duplicate Telegram message, by
checking the delivery's own current status before ever calling
notifier.send().
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
# A production-readiness pass closed the one previously-accepted gap in
# this worker (see docs/NOTIFICATION_CENTER_ARCHITECTURE.md's "Crash
# safety" section, written when this was still an open limitation): a
# delivery stuck at 'processing' because the process died between the
# Postgres UPDATE and the Redis XADD no longer sits there forever.
# 15 minutes is deliberately generous, not tightly tuned -- Notifier's
# own worst case for one message (services/bot/notifier.py's
# max_attempts=5, each a real TelegramRetryAfter wait) is on the order of
# a few minutes, plus whatever the global rate-limited queue's own depth
# adds ahead of it. Reclaiming too early only costs a harmless extra
# stream entry (see notification_relay.py's own idempotency check, which
# is what actually makes any reclaim safe against a duplicate send, not
# this threshold's precision) -- reclaiming too late just delays recovery.
RECLAIM_STUCK_AFTER_SECONDS = 900


async def _reclaim_stuck_deliveries(pool: asyncpg.Pool) -> int:
    """Resets a delivery stuck at 'processing' well past any plausible
    real in-flight time back to 'pending', so the very next dispatch pass
    in this same tick (process_once() calls this before
    _dispatch_pending_deliveries()) picks it up again through the normal
    path -- no separate re-enqueue code path to keep in sync with the
    real one. Safe against ever double-sending: if the original enqueue
    actually did succeed and the relay just hasn't gotten to it yet, a
    second dispatch would enqueue a second stream entry for the same
    delivery_id, but notification_relay.py::process_one() checks the
    delivery's own current status before ever calling notifier.send()
    and skips a redundant entry outright.
    """
    rows = await pool.fetch(
        """
        UPDATE notification_deliveries
        SET status = 'pending'
        WHERE status = 'processing'
          AND last_attempt_at < now() - make_interval(secs => $1)
        RETURNING id
        """,
        RECLAIM_STUCK_AFTER_SECONDS,
    )
    if rows:
        logger.warning(
            "notification_delivery_reclaimed_from_stuck_processing",
            delivery_ids=[r["id"] for r in rows],
        )
    return len(rows)


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
    before that row's own enqueue -- deliberately in that order: this
    query only ever selects 'pending' rows, so a row already marked
    'processing' is never re-enqueued by this loop itself, meaning a
    crash between the two steps can never double-send *from this path
    alone*. The former gap this left (a delivery stuck at 'processing'
    forever if the process died in that exact window) is now closed by
    _reclaim_stuck_deliveries(), called every tick before this function;
    see its own docstring for why a reclaim-triggered re-enqueue still
    can't cause a real duplicate Telegram message either.
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
            "UPDATE notification_deliveries SET status = 'processing', last_attempt_at = now() "
            "WHERE id = $1",
            row["delivery_id"],
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
    reclaimed = await _reclaim_stuck_deliveries(pool)

    sending_ids = [row["id"] for row in await pool.fetch(
        "SELECT id FROM notification_campaigns WHERE status = 'sending'"
    )]
    dispatched = 0
    for campaign_id in sending_ids:
        await _resolve_and_seed_deliveries(pool, campaign_id)
        dispatched += await _dispatch_pending_deliveries(pool, redis, campaign_id)

    finalized = await _finalize_completed_campaigns(pool)
    return bool(claimed) or reclaimed > 0 or dispatched > 0 or finalized > 0


async def run_forever(pool: asyncpg.Pool, redis: Redis) -> None:
    while True:
        try:
            did_work = await process_once(pool, redis)
        except Exception:
            logger.exception("campaign_worker_tick_failed")
            did_work = False
        if not did_work:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
