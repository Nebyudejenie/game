"""Referral deep links (spec section 7.1: `?start=ref_{code}`).

The code is simply the referrer's own telegram_id -- no separate
code-generation or lookup table needed, and it's already unique. `/start`
and the actual registration (a later contact-share message) are two
separate updates, so the pending referral is held in Redis between them,
keyed by the *new* user's telegram_id, with a generous TTL for someone who
takes a while to tap the registration button.
"""

from __future__ import annotations

import re

from redis.asyncio import Redis

PENDING_TTL_SECONDS = 3600
_REF_ARG_RE = re.compile(r"^ref_(\d+)$")


def parse_referral_code(start_args: str | None) -> int | None:
    if not start_args:
        return None
    match = _REF_ARG_RE.match(start_args.strip())
    return int(match.group(1)) if match else None


async def store_pending_referral(redis: Redis, telegram_id: int, referrer_telegram_id: int) -> None:
    if referrer_telegram_id == telegram_id:
        return  # can't refer yourself
    await redis.set(
        f"pending_referral:{telegram_id}", str(referrer_telegram_id), ex=PENDING_TTL_SECONDS
    )


async def pop_pending_referral(redis: Redis, telegram_id: int) -> int | None:
    key = f"pending_referral:{telegram_id}"
    value = await redis.get(key)
    if value is None:
        return None
    await redis.delete(key)
    return int(value)
