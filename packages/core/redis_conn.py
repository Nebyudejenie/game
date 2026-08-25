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
# manufactures.
#
# A second code review pass (independently, by two separate finder
# agents) caught that the first fix's original value, 5.0, collided
# exactly with services/payments/payout_worker.py's and services/bot/
# notification_relay.py's own `xreadgroup(..., block=5000)` polling
# reads. redis-py's async client applies `socket_timeout` as the raw
# socket read deadline for *every* ordinary command -- confirmed by
# reading redis.asyncio.connection.Connection.read_response()'s own
# source in this project's installed redis-py version, not assumed --
# and does not extend it for a command's own BLOCK argument (that
# opt-out, passing `timeout=math.inf` to bypass socket_timeout entirely,
# is real but only ever taken by PubSub's listen()/get_message(), which
# is why services/engine/commands.py's send_command() -- built on
# pubsub.listen(), with asyncio.wait_for() supplying its own 5s command
# -level timeout -- was never at risk here, only the two Stream-based
# blocking reads were. A 5000ms BLOCK window racing an exactly-5000ms
# client socket timeout meant an ordinary idle stream (the ubiquitous
# "nothing to do yet" case, not a Redis outage) could raise an unhandled
# redis.exceptions.TimeoutError inside either worker's bare `while True:`
# loop, crashing it under everyday operation.
#
# 10 seconds keeps a real margin (2x) over every `block=` value this
# codebase currently uses (round_engine.py's own xread() uses 1000ms;
# the two Streams above use 5000ms) -- if a future blocking read ever
# needs a longer BLOCK window, it must stay comfortably below whatever
# this constant is at the time, not the other way around.
SOCKET_TIMEOUT_SECONDS = 10.0


def get_redis(*, decode_responses: bool = True) -> Redis:
    settings = get_settings()
    return Redis.from_url(
        settings.redis_url,
        decode_responses=decode_responses,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        health_check_interval=30,
    )
