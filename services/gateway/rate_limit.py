"""Redis token-bucket rate limiting (spec section 9.2).

One Lua script does the read-refill-check-consume cycle atomically so
concurrent requests against the same bucket can't race each other into
over-granting tokens. Bucket keys follow the spec's own `rl:{scope}:{id}`
convention.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis

_TOKEN_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local bucket = redis.call("HMGET", KEYS[1], "tokens", "ts")
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill_per_second)

local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end

redis.call("HMSET", KEYS[1], "tokens", tokens, "ts", now)
local ttl = math.ceil(capacity / refill_per_second) + 1
redis.call("EXPIRE", KEYS[1], ttl)

return allowed
"""


async def allow(
    redis: Redis,
    scope: str,
    key: str,
    *,
    capacity: float,
    refill_per_second: float,
    cost: float = 1.0,
) -> bool:
    """True if a request against this bucket is allowed right now (and
    consumes `cost` tokens), False if the bucket doesn't have enough.
    """
    now = time.time()
    result = await redis.eval(
        _TOKEN_BUCKET_SCRIPT,
        1,
        f"rl:{scope}:{key}",
        capacity,
        refill_per_second,
        now,
        cost,
    )
    return bool(result)


# Spec section 9.2 limits. WS_MESSAGES is a blanket per-connection backstop
# applied to every inbound frame; the others are additionally applied to
# their specific action. "claim 5/round" from the spec is naturally mostly
# covered by round_engine.py's own per-round false-claim lockout (one bad
# manual claim ends a user's claims for that round already) -- this adds a
# time-windowed backstop on top rather than trying to model "per round" as
# a token bucket, which doesn't map cleanly onto a refill-rate bucket.
WS_MESSAGES = {"capacity": 30, "refill_per_second": 30.0}
TAKE_CARD = {"capacity": 10, "refill_per_second": 10.0 / 60.0}
CLAIM = {"capacity": 5, "refill_per_second": 5.0 / 60.0}
