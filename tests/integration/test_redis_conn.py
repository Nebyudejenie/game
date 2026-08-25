"""Tests for packages/core/redis_conn.py's get_redis() -- specifically
that its socket_timeout doesn't collide with this codebase's own
blocking Stream reads.
"""

import time

from packages.core.redis_conn import SOCKET_TIMEOUT_SECONDS


async def test_socket_timeout_has_real_margin_over_every_blocking_read(redis):
    # A code review pass caught this exact collision independently, twice:
    # redis-py's async client applies socket_timeout as the raw socket
    # read deadline for every ordinary command (confirmed by reading
    # Connection.read_response()'s own source, not assumed), and does not
    # extend it for a command's own BLOCK argument. The original fix set
    # SOCKET_TIMEOUT_SECONDS to exactly 5.0 -- the same value services/
    # payments/payout_worker.py and services/bot/notification_relay.py
    # both already used for their own xreadgroup(..., block=5000) polling
    # reads -- so an ordinary idle stream (not a Redis outage; the
    # ubiquitous "nothing to do yet" case) could raise an unhandled
    # TimeoutError inside either worker's bare `while True:` loop.
    largest_block_ms = 5000
    assert SOCKET_TIMEOUT_SECONDS > (largest_block_ms / 1000) * 1.5, (
        f"SOCKET_TIMEOUT_SECONDS={SOCKET_TIMEOUT_SECONDS} leaves no real margin "
        f"over the largest block= value this codebase uses ({largest_block_ms}ms)"
    )

    # And the real, empirical proof, not just an arithmetic comparison --
    # a genuinely empty stream, blocked on for exactly the value payout_
    # worker.py/notification_relay.py actually use, through the real
    # get_redis()-configured client, must return an empty result rather
    # than raise.
    stream = f"test-empty-stream-{time.monotonic_ns()}"
    group = "testgroup"
    await redis.xgroup_create(stream, group, id="0", mkstream=True)
    try:
        start = time.monotonic()
        result = await redis.xreadgroup(
            group, "consumer1", {stream: ">"}, count=10, block=largest_block_ms
        )
        elapsed = time.monotonic() - start
        assert result in ([], {}), f"expected an empty read, got {result}"
        assert elapsed >= largest_block_ms / 1000 - 0.5, (
            f"returned after {elapsed:.2f}s, suspiciously earlier than the "
            f"{largest_block_ms}ms BLOCK window -- did this actually block?"
        )
    finally:
        await redis.delete(stream)
