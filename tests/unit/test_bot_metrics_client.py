"""services/admin/bot_metrics_client.py -- percentile interpolation and
Prometheus text parsing, exercised against a real local aiohttp server
serving prometheus_client's own generate_latest() output (not a hand-
written fake text blob), so a real format drift in either library would
actually be caught here.
"""

import pytest
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from services.admin.bot_metrics_client import _quantile_from_buckets, fetch_command_metrics


def test_quantile_interpolates_within_the_crossing_bucket():
    buckets = [(0.01, 0.0), (0.1, 1.0), (1.0, 2.0), (float("inf"), 2.0)]
    assert _quantile_from_buckets(buckets, 0.50) == pytest.approx(0.1)
    assert _quantile_from_buckets(buckets, 0.95) == pytest.approx(0.91)


def test_quantile_falling_in_the_overflow_bucket_returns_the_last_finite_boundary():
    buckets = [(0.01, 5.0), (0.1, 5.0), (float("inf"), 10.0)]
    # target = 0.99 * 10 = 9.9, only the +Inf bucket (count=10) covers it.
    assert _quantile_from_buckets(buckets, 0.99) == 0.1


def test_quantile_of_empty_buckets_is_none():
    assert _quantile_from_buckets([], 0.5) is None


def test_quantile_of_a_zero_count_histogram_is_none():
    assert _quantile_from_buckets([(0.1, 0.0), (float("inf"), 0.0)], 0.5) is None


async def test_fetch_command_metrics_against_a_real_local_server():
    from aiohttp import web

    registry = CollectorRegistry()
    commands_total = Counter("telegram_commands_total", "t", ["handler"], registry=registry)
    success_total = Counter("telegram_command_success_total", "t", ["handler"], registry=registry)
    error_total = Counter("telegram_command_error_total", "t", ["handler"], registry=registry)
    blocked_total = Counter("telegram_command_blocked_total", "t", ["handler"], registry=registry)
    latency = Histogram(
        "telegram_command_latency_seconds", "t", ["handler"], registry=registry, buckets=(0.01, 0.1, 1.0)
    )

    commands_total.labels(handler="cmd_balance").inc(5)
    success_total.labels(handler="cmd_balance").inc(4)
    error_total.labels(handler="cmd_balance").inc(1)
    blocked_total.labels(handler="cmd_balance").inc(2)
    latency.labels(handler="cmd_balance").observe(0.05)
    latency.labels(handler="cmd_balance").observe(0.5)

    app = web.Application()

    async def metrics_endpoint(request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(registry), content_type="text/plain", charset="utf-8")

    app.router.add_get("/metrics", metrics_endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        result = await fetch_command_metrics(f"http://127.0.0.1:{port}/metrics")
        m = result["cmd_balance"]
        assert m.count == 5
        assert m.success == 4
        assert m.errors == 1
        assert m.blocked == 2
        assert m.success_rate == pytest.approx(0.8)
        assert m.error_rate == pytest.approx(0.2)
        assert m.p50_seconds is not None
        assert m.p95_seconds is not None
        assert m.p99_seconds is not None

        assert "cmd_never_called" not in result
    finally:
        await runner.cleanup()


async def test_fetch_command_metrics_returns_empty_dict_when_url_is_unset():
    result = await fetch_command_metrics("")
    assert result == {}
