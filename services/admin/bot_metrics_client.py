"""Fetches and parses the bot process's own /metrics endpoint for the
admin Commands/Performance screens.

The admin process and the bot process are separate services (services/
admin/app.py, services/bot/app.py) with no shared in-process Prometheus
registry -- real per-command P50/P95/P99 has to come from an HTTP fetch
of the bot's own already-existing /metrics endpoint (packages/core/
metrics.py's telegram_* histograms, built in the Telegram command latency
diagnosis pass), parsed with prometheus_client's own official parser
(prometheus_client.parser.text_string_to_metric_families) rather than a
hand-rolled one. Reuses the exact metrics that pass already shipped and
tested; adds no new instrumentation of its own.

Percentiles are computed with the same linear-interpolation-within-bucket
algorithm PromQL's histogram_quantile() uses, so a number shown here and
the equivalent Grafana panel (deploy/grafana/dashboards/jo-bingo.json)
should never meaningfully disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from prometheus_client.parser import text_string_to_metric_families


@dataclass(frozen=True)
class CommandMetrics:
    handler: str
    count: int
    success: int
    errors: int
    blocked: int
    rate_limited: int
    p50_seconds: float | None
    p95_seconds: float | None
    p99_seconds: float | None

    @property
    def success_rate(self) -> float | None:
        if self.count == 0:
            return None
        return self.success / self.count

    @property
    def error_rate(self) -> float | None:
        if self.count == 0:
            return None
        return self.errors / self.count


def _quantile_from_buckets(buckets: list[tuple[float, float]], quantile: float) -> float | None:
    """buckets: [(le, cumulative_count), ...] sorted ascending by le,
    including a final (+inf, total_count) entry -- exactly the shape
    prometheus_client's own Histogram exposes. Same linear-interpolation
    algorithm as PromQL's histogram_quantile().
    """
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    target = quantile * total
    prev_le, prev_count = 0.0, 0.0
    for le, count in buckets:
        if count >= target:
            if le == float("inf"):
                # The target rank falls in the +Inf overflow bucket --
                # every finite bucket undercounts it. Returning the last
                # finite boundary is a conservative, honest estimate
                # ("at least this slow") rather than a literal, useless
                # infinity in a UI table.
                return prev_le
            if count == prev_count:
                return le
            fraction = (target - prev_count) / (count - prev_count)
            return prev_le + fraction * (le - prev_le)
        prev_le, prev_count = le, count
    return buckets[-1][0]


async def fetch_command_metrics(metrics_url: str) -> dict[str, CommandMetrics]:
    """Returns {} if metrics_url is unset or unreachable -- callers show
    "NO DATA" for every row in that case, never a fabricated zero.
    """
    if not metrics_url:
        return {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(metrics_url)
        response.raise_for_status()
        text = response.text

    commands_total: dict[str, float] = {}
    success_total: dict[str, float] = {}
    error_total: dict[str, float] = {}
    blocked_total: dict[str, float] = {}
    rate_limited_total: dict[str, float] = {}
    buckets_by_handler: dict[str, list[tuple[float, float]]] = {}

    for family in text_string_to_metric_families(text):
        if family.name == "telegram_commands":
            for sample in family.samples:
                if sample.name == "telegram_commands_total":
                    commands_total[sample.labels["handler"]] = sample.value
        elif family.name == "telegram_command_success":
            for sample in family.samples:
                if sample.name == "telegram_command_success_total":
                    success_total[sample.labels["handler"]] = sample.value
        elif family.name == "telegram_command_error":
            for sample in family.samples:
                if sample.name == "telegram_command_error_total":
                    error_total[sample.labels["handler"]] = sample.value
        elif family.name == "telegram_command_blocked":
            for sample in family.samples:
                if sample.name == "telegram_command_blocked_total":
                    blocked_total[sample.labels["handler"]] = sample.value
        elif family.name == "telegram_command_rate_limited":
            for sample in family.samples:
                if sample.name == "telegram_command_rate_limited_total":
                    handler = sample.labels["handler"]
                    # Summed across limit_type ("cooldown"/"rate_limit") --
                    # the admin table shows one combined "Blocked
                    # Attempts" figure per Section 4; the Telegram
                    # Performance Grafana panel is where the cooldown-vs-
                    # rate-limit split matters.
                    rate_limited_total[handler] = rate_limited_total.get(handler, 0.0) + sample.value
        elif family.name == "telegram_command_latency_seconds":
            for sample in family.samples:
                if sample.name == "telegram_command_latency_seconds_bucket":
                    handler = sample.labels["handler"]
                    le = float(sample.labels["le"])
                    buckets_by_handler.setdefault(handler, []).append((le, sample.value))

    result: dict[str, CommandMetrics] = {}
    handlers = set(commands_total) | set(buckets_by_handler)
    for handler in handlers:
        buckets = sorted(buckets_by_handler.get(handler, []), key=lambda b: b[0])
        result[handler] = CommandMetrics(
            handler=handler,
            count=int(commands_total.get(handler, 0)),
            success=int(success_total.get(handler, 0)),
            errors=int(error_total.get(handler, 0)),
            blocked=int(blocked_total.get(handler, 0)),
            rate_limited=int(rate_limited_total.get(handler, 0)),
            p50_seconds=_quantile_from_buckets(buckets, 0.50),
            p95_seconds=_quantile_from_buckets(buckets, 0.95),
            p99_seconds=_quantile_from_buckets(buckets, 0.99),
        )
    return result
