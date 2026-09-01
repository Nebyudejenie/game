"""Nightly ledger reconciliation job (spec section 14's definition of
done: "ledger sum equals balance cache for every account, verified
nightly, zero drift over 30 days"). `ledger.reconcile()` is the tested
comparison logic; this is the thin wrapper a real scheduler actually
invokes.

Run directly: `python -m packages.core.reconcile_job`. Exit code 0 means
every account's cached balance agrees with the sum of its own
ledger_entries; 1 means at least one doesn't, which should never happen
given ledger.post()'s row-locked, transactional writes -- so a real
deployment's cron/systemd-timer/k8s-CronJob should alert loudly on a
non-zero exit here, not retry quietly. This module doesn't set up its own
schedule; wiring a `0 3 * * *` cron entry (or equivalent) to invoke this
is a deployment-time decision this session has consistently left out of
scope, the same as every other worker entrypoint in this codebase.

If `PUSHGATEWAY_URL` is set, the mismatch count is also pushed to a real
Prometheus Pushgateway (job="reconcile_job") -- the standard pattern for a
one-shot batch job that has no long-running `/metrics` endpoint of its own
to scrape, and what `deploy/prometheus/alerts.yml`'s
`LedgerReconciliationMismatch` rule needs to ever actually fire. A push
failure is logged and never changes this job's own exit code -- an
observability-pipeline outage must never mask, or get mistaken for, an
actual ledger mismatch.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

import asyncpg
from prometheus_client import push_to_gateway

from packages.core import ledger, metrics
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def reconcile_all(pool: asyncpg.Pool) -> list[tuple[int, Decimal, Decimal]]:
    """The testable core: reconcile every account through a pool a test
    can supply directly, no process/CLI concerns involved.
    """
    async with pool.acquire() as conn:
        return await ledger.reconcile(conn)


async def run_reconciliation() -> list[tuple[int, Decimal, Decimal]]:
    settings = get_settings()
    pool = await create_pool(dsn=settings.database_url, min_size=1, max_size=2)
    try:
        return await reconcile_all(pool)
    finally:
        await pool.close()


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    mismatches = asyncio.run(run_reconciliation())

    metrics.ledger_reconciliation_mismatch_count.set(len(mismatches))
    if settings.pushgateway_url:
        try:
            push_to_gateway(
                settings.pushgateway_url, job="reconcile_job", registry=metrics.reconcile_registry
            )
        except Exception:
            # A Pushgateway outage is an observability-pipeline problem,
            # not a ledger-integrity one -- it must never flip this job's
            # own exit code (the signal a real scheduler actually alerts
            # on) or mask a real mismatch found above.
            logger.warning("pushgateway_push_failed", exc_info=True)

    if mismatches:
        logger.error(
            "ledger_reconciliation_failed",
            mismatch_count=len(mismatches),
            mismatches=[
                {"account_id": account_id, "cached": str(cached), "computed": str(computed)}
                for account_id, cached, computed in mismatches
            ],
        )
        return 1
    logger.info("ledger_reconciliation_ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
