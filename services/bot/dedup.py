"""Telegram webhook update deduplication (spec section 5: `seen:tg:{update_id}`).

Telegram guarantees at-least-once delivery, not exactly-once -- the same
update can arrive twice (retried webhook, Telegram-side hiccup). Every
webhook request checks this before doing anything with side effects.
"""

from __future__ import annotations

from redis.asyncio import Redis

SEEN_TTL_SECONDS = 600


async def claim_update(redis: Redis, update_id: int) -> bool:
    """True if this update_id hasn't been seen in the last 10 minutes (and
    marks it seen); False if it's a duplicate that should be dropped.
    """
    key = f"seen:tg:{update_id}"
    was_new = await redis.set(key, "1", nx=True, ex=SEEN_TTL_SECONDS)
    return bool(was_new)
