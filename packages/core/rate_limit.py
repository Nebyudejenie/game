"""Redis token-bucket rate limiting (spec section 9.2).

One Lua script does the read-refill-check-consume cycle atomically so
concurrent requests against the same bucket can't race each other into
over-granting tokens. Bucket keys follow the spec's own `rl:{scope}:{id}`
convention.

Lives in packages/core, not services/gateway (where it started), because
services/payments/deposits.py needs the same "deposit 5/hour" bucket the
spec asks for -- nothing about the Lua script or the bucket constants is
gateway-specific, so packages/core is where every service-spanning
utility in this codebase already lives (ledger, bingo, telegram_auth,
responsible_gaming, ...).
"""

from __future__ import annotations

import time

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger()

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

    Fails closed (returns False) if Redis itself errors -- a code review
    pass caught that every caller here (services/gateway/connection.py's
    per-message WS_MESSAGES check in particular) had no try/except of its
    own, so an unhandled exception from a single transient Redis hiccup
    didn't just deny that one action, it killed that connection's whole
    message loop, disconnecting the player outright over a blip that had
    nothing to do with them. Failing closed here means every existing
    caller's ordinary "if not allowed: send a rate_limited error and keep
    going" path already handles a Redis error correctly, with no caller
    changes needed. Rejected failing open (treating a Redis error as
    "allowed"): this bucket set includes ADMIN_LOGIN's brute-force
    throttle and DEPOSIT's financial-abuse cap, and this platform already
    has no path to function at all without Redis (round locking, session
    state), so failing closed here doesn't meaningfully worsen a real
    outage -- it only changes behavior for the transient-blip case this
    fix actually targets.
    """
    now = time.time()
    try:
        result = await redis.eval(
            _TOKEN_BUCKET_SCRIPT,
            1,
            f"rl:{scope}:{key}",
            capacity,
            refill_per_second,
            now,
            cost,
        )
    except Exception:
        logger.warning("rate_limit_redis_error", scope=scope, key=key)
        return False
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
DEPOSIT = {"capacity": 5, "refill_per_second": 5.0 / 3600.0}
# Not one of spec 9.2's own numbers (only "IP allowlist" and "TOTP
# required" are specified for admin login) -- an engineering judgment
# call closing a real gap a code review pass caught: services/admin
# /app.py never imported or called rate_limit.allow() anywhere, so
# password-guessing against a known admin username was completely
# unthrottled (TOTP raises the bar, but doesn't stop the password half
# being brute-forced online). 5 attempts per 15 minutes per username --
# generous enough that a legitimate admin fat-fingering their password a
# couple of times never gets locked out, tight enough to make online
# brute-forcing impractical.
ADMIN_LOGIN = {"capacity": 5, "refill_per_second": 5.0 / 900.0}
