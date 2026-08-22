"""Shared Redis client factory.

Redis is hot state and fan-out only, never the source of financial truth --
see packages/core/ledger.py. If Redis is wiped, the platform must recover
fully from Postgres (in-flight rounds get voided and refunded by
services/engine/recovery.py; nothing else depends on Redis surviving).
"""

from redis.asyncio import Redis

from packages.core.config import get_settings


def get_redis(*, decode_responses: bool = True) -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=decode_responses)
