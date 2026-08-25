"""Shared Redis client factory.

Redis is hot state and fan-out only, never the source of financial truth --
see packages/core/ledger.py. If Redis is wiped, the platform must recover
fully from Postgres (in-flight rounds get voided and refunded by
services/engine/recovery.py; nothing else depends on Redis surviving).
"""

from redis.asyncio import Redis

from packages.core.config import get_settings


# A code review pass caught that no timeout was configured at all: a
# degraded/unreachable Redis (a network partition, Redis under load) could
# hang any caller indefinitely instead of failing fast -- the opposite of
# this module's own docstring promise that the platform survives Redis
# loss, since a *hang* isn't a loss, it's an outage this client itself
# manufactures. 5 seconds matches the real-time budget already established
# elsewhere in this codebase (services/engine/commands.py's own
# CommandTimeout).
SOCKET_TIMEOUT_SECONDS = 5.0


def get_redis(*, decode_responses: bool = True) -> Redis:
    settings = get_settings()
    return Redis.from_url(
        settings.redis_url,
        decode_responses=decode_responses,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        health_check_interval=30,
    )
