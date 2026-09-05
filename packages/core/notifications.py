"""Producer side of the bot-notification relay: any process that just moved
a player's money (deposits, payouts, admin actions) enqueues a notification
here instead of calling the Telegram Bot API directly.

Why a queue instead of a direct call: services/payments (and
services/admin) run in separate processes from services/bot, and
services/bot/handlers.py's own docstring makes "nothing calls
bot.send_message except Notifier" a load-bearing, tested invariant --
respecting that from another process means going through the same kind of
worker Notifier itself is, not reaching around it. services/bot/
notification_relay.py is the consumer.

Never lets a notification failure break the caller's own transaction: this
is enqueued *after* the money has already moved and committed, and a
failure to enqueue (or nobody ever consuming the queue) must never look
like a failure to move the money. Callers don't need to catch anything
here; enqueue failures are logged and swallowed.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core.ledger import AsyncpgConnection

logger = structlog.get_logger()

NOTIFICATIONS_STREAM = "bot_notifications"


async def notify_user(
    conn: AsyncpgConnection | asyncpg.Pool, redis: Redis, *, user_id: int, key: str, **kwargs: Any
) -> None:
    try:
        telegram_id = await conn.fetchval("SELECT telegram_id FROM users WHERE id = $1", user_id)
        if telegram_id is None:
            return
        await redis.xadd(
            NOTIFICATIONS_STREAM,
            {"telegram_id": str(telegram_id), "key": key, "kwargs": json.dumps(kwargs, default=str)},
        )
    except Exception as exc:
        logger.error("notify_user_enqueue_failed", user_id=user_id, key=key, error=str(exc))


async def enqueue_campaign_message(
    redis: Redis, *, telegram_id: int, text: str, delivery_id: int
) -> None:
    """The Notification Center's own producer: an admin-authored campaign
    sends raw text, never an i18n key (there is no translated string to
    look up for content an admin typed themselves) -- the one real
    difference from notify_user() above. Same stream, same consumer
    (services/bot/notification_relay.py), same Notifier underneath, so
    campaign traffic gets the exact same rate limiting and 429 backoff as
    every other outbound message, not a second pipeline that could
    together exceed Telegram's real rate limit. delivery_id lets the
    relay report the real per-recipient outcome back to
    notification_deliveries once Notifier reaches a terminal state --
    absent for every other (non-campaign) caller of this stream, so the
    relay's own handling stays a no-op for them.
    """
    await redis.xadd(
        NOTIFICATIONS_STREAM,
        {"telegram_id": str(telegram_id), "raw_text": text, "delivery_id": str(delivery_id)},
    )
