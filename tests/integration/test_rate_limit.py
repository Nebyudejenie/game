"""Tests for packages/core/rate_limit.py -- the token-bucket rate limiter
spec section 9.2 requires ("claim 5/round, take_card 10/min, deposit
5/hour, WS messages 30/s"). Had zero test coverage anywhere in this
codebase before this file: not a single existing test, gateway or
otherwise, sent enough rapid requests to actually hit a limit, so the
one thing the Lua script exists to guarantee -- concurrent requests
against the same bucket can never over-grant tokens -- was never
verified.

Every test uses its own unique (scope, key) pair (a fresh uuid4 each
time) so bucket state from one test can never leak into another via the
same real, shared Redis instance every other integration test also uses.
"""

import asyncio
import uuid

import pytest

from packages.core import rate_limit

# Low enough that a whole test's execution time (milliseconds, or up to a
# second or two even under heavy contention for the 50-concurrent test)
# can never accumulate a full extra token and change an assertion's
# outcome -- but not the 0.0001 an earlier draft used, which (via
# rate_limit.py's own ttl = ceil(capacity/refill)+1 formula) gave every
# key in this file a multi-hour TTL, leaving stale rl:test-* keys on the
# real, shared Redis instance every integration test uses for hours after
# each run. At 0.1, the worst case here (capacity=10) still only produces
# a ~101s TTL.
_NEGLIGIBLE_REFILL = 0.1


def _bucket() -> tuple[str, str]:
    return f"test-{uuid.uuid4()}", f"key-{uuid.uuid4()}"


async def test_allow_grants_up_to_capacity_then_rejects(redis):
    scope, key = _bucket()
    # refill_per_second effectively zero -- capacity is the only thing
    # that matters within this test's lifetime.
    bucket = {"capacity": 3, "refill_per_second": _NEGLIGIBLE_REFILL}

    results = [await rate_limit.allow(redis, scope, key, **bucket) for _ in range(4)]
    assert results == [True, True, True, False]


async def test_allow_refills_over_time(redis):
    scope, key = _bucket()
    bucket = {"capacity": 2, "refill_per_second": 20.0}  # 1 token per 50ms

    assert await rate_limit.allow(redis, scope, key, **bucket) is True
    assert await rate_limit.allow(redis, scope, key, **bucket) is True
    assert await rate_limit.allow(redis, scope, key, **bucket) is False

    await asyncio.sleep(0.1)  # >= 2 tokens' worth of refill time

    assert await rate_limit.allow(redis, scope, key, **bucket) is True


async def test_different_keys_get_independent_buckets(redis):
    scope = f"test-{uuid.uuid4()}"
    key_a, key_b = f"a-{uuid.uuid4()}", f"b-{uuid.uuid4()}"
    bucket = {"capacity": 1, "refill_per_second": _NEGLIGIBLE_REFILL}

    assert await rate_limit.allow(redis, scope, key_a, **bucket) is True
    assert await rate_limit.allow(redis, scope, key_a, **bucket) is False
    # Exhausting key_a's bucket must not touch key_b's -- these are two
    # different users (or two different rooms), not one shared limit.
    assert await rate_limit.allow(redis, scope, key_b, **bucket) is True


async def test_different_scopes_get_independent_buckets(redis):
    key = f"key-{uuid.uuid4()}"
    scope_a, scope_b = f"scope-a-{uuid.uuid4()}", f"scope-b-{uuid.uuid4()}"
    bucket = {"capacity": 1, "refill_per_second": _NEGLIGIBLE_REFILL}

    assert await rate_limit.allow(redis, scope_a, key, **bucket) is True
    assert await rate_limit.allow(redis, scope_a, key, **bucket) is False
    # Same user, but claim vs take_card vs deposit are different actions --
    # exhausting one must never block another.
    assert await rate_limit.allow(redis, scope_b, key, **bucket) is True


async def test_concurrent_requests_never_over_grant_tokens(redis):
    # The entire reason this is a Lua script and not a plain Python
    # read-then-write against Redis: a bucket with capacity 10 hit by 50
    # genuinely concurrent requests must let exactly 10 through, never 11+
    # from a read-refill-check-consume race. This is the real guarantee
    # spec 9.2's rate limits depend on to actually hold under load, not
    # just under a single sequential caller.
    scope, key = _bucket()
    bucket = {"capacity": 10, "refill_per_second": _NEGLIGIBLE_REFILL}

    results = await asyncio.gather(
        *(rate_limit.allow(redis, scope, key, **bucket) for _ in range(50))
    )
    assert results.count(True) == 10
    assert results.count(False) == 40


async def test_cost_can_consume_more_than_one_token(redis):
    scope, key = _bucket()
    bucket = {"capacity": 10, "refill_per_second": _NEGLIGIBLE_REFILL}

    assert await rate_limit.allow(redis, scope, key, cost=7.0, **bucket) is True
    assert await rate_limit.allow(redis, scope, key, cost=4.0, **bucket) is False
    assert await rate_limit.allow(redis, scope, key, cost=3.0, **bucket) is True


async def test_a_redis_error_fails_closed_instead_of_raising(redis, monkeypatch):
    # A code review pass caught that allow() had no try/except of its own,
    # so a single transient Redis error didn't just deny that one request
    # -- it propagated as an unhandled exception all the way up. In
    # services/gateway/connection.py's per-message WS_MESSAGES check in
    # particular, that exception isn't caught anywhere between there and
    # _message_loop()'s top-level `while True:`, so one flaky Redis
    # round-trip killed that player's *entire* connection, not just the
    # one action that happened to hit the blip.
    scope, key = _bucket()
    bucket = {"capacity": 10, "refill_per_second": _NEGLIGIBLE_REFILL}

    async def flaky_eval(*args, **kwargs):
        raise ConnectionError("simulated Redis blip")

    monkeypatch.setattr(redis, "eval", flaky_eval)
    assert await rate_limit.allow(redis, scope, key, **bucket) is False


def test_spec_bucket_constants_match_section_9_2():
    # A regression guard against a silent typo changing a real security
    # control -- spec 9.2: "claim 5/round, take_card 10/min, deposit
    # 5/hour, WS messages 30/s". WS_MESSAGES/TAKE_CARD/CLAIM/DEPOSIT are
    # the only place these numbers exist; nothing else would catch one of
    # them quietly drifting.
    assert rate_limit.WS_MESSAGES == {"capacity": 30, "refill_per_second": 30.0}
    assert rate_limit.TAKE_CARD["capacity"] == 10
    assert rate_limit.TAKE_CARD["refill_per_second"] == pytest.approx(10.0 / 60.0)
    assert rate_limit.CLAIM["capacity"] == 5
    assert rate_limit.CLAIM["refill_per_second"] == pytest.approx(5.0 / 60.0)
    assert rate_limit.DEPOSIT["capacity"] == 5
    assert rate_limit.DEPOSIT["refill_per_second"] == pytest.approx(5.0 / 3600.0)


# --- allow_with_retry_after: same bucket, plus a real computed wait ----


async def test_allow_with_retry_after_returns_none_when_allowed(redis):
    scope, key = _bucket()
    bucket = {"capacity": 3, "refill_per_second": _NEGLIGIBLE_REFILL}

    allowed, retry_after = await rate_limit.allow_with_retry_after(redis, scope, key, **bucket)
    assert allowed is True
    assert retry_after is None


async def test_allow_with_retry_after_returns_a_real_positive_wait_when_denied(redis):
    scope, key = _bucket()
    # 1 token/second refill -- exhaust the single-token bucket, then the
    # very next request needs to wait ~1s for the next token.
    bucket = {"capacity": 1, "refill_per_second": 1.0}

    first_allowed, first_retry = await rate_limit.allow_with_retry_after(redis, scope, key, **bucket)
    assert first_allowed is True
    assert first_retry is None

    second_allowed, second_retry = await rate_limit.allow_with_retry_after(redis, scope, key, **bucket)
    assert second_allowed is False
    assert second_retry is not None
    assert 0.5 < second_retry <= 1.0, second_retry


async def test_allow_with_retry_after_wait_shrinks_the_longer_you_wait(redis):
    scope, key = _bucket()
    bucket = {"capacity": 1, "refill_per_second": 1.0}

    await rate_limit.allow_with_retry_after(redis, scope, key, **bucket)  # consumes the only token
    _, retry_immediately = await rate_limit.allow_with_retry_after(redis, scope, key, **bucket)

    await asyncio.sleep(0.5)
    _, retry_after_half_second = await rate_limit.allow_with_retry_after(redis, scope, key, **bucket)

    assert retry_immediately is not None and retry_after_half_second is not None
    assert retry_after_half_second < retry_immediately


async def test_allow_still_works_unchanged_after_the_retry_after_addition(redis):
    # allow() is now implemented in terms of allow_with_retry_after() --
    # every pre-existing caller (deposits.py, gateway, admin login) must
    # see byte-for-byte the same boolean behavior as before.
    scope, key = _bucket()
    bucket = {"capacity": 2, "refill_per_second": _NEGLIGIBLE_REFILL}

    assert await rate_limit.allow(redis, scope, key, **bucket) is True
    assert await rate_limit.allow(redis, scope, key, **bucket) is True
    assert await rate_limit.allow(redis, scope, key, **bucket) is False


async def test_allow_with_retry_after_fails_closed_with_no_retry_after_on_redis_error(redis, monkeypatch):
    scope, key = _bucket()
    bucket = {"capacity": 3, "refill_per_second": 1.0}

    async def flaky_eval(*args, **kwargs):
        raise ConnectionError("simulated Redis blip")

    monkeypatch.setattr(redis, "eval", flaky_eval)
    allowed, retry_after = await rate_limit.allow_with_retry_after(redis, scope, key, **bucket)
    assert allowed is False
    assert retry_after is None
