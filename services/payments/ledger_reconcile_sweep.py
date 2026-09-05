"""Ledger reconciliation sweep: periodically confirms every account's
cached balance (account_balances) still agrees with the sum of its own
ledger_entries. Runs inside the existing payout_worker process (see
main_async() there) as a sixth _run_periodic_sweep() alongside the five
jobs already living there -- no new deployable service, no new scheduler.

Read-only by design: packages/core/ledger.py::reconcile() only compares,
it never writes. A mismatch here means a real bug elsewhere already wrote
an inconsistent state -- this sweep's job is to surface that loudly
(a log line plus a scraped Prometheus gauge an alert rule watches), never
to paper over it by forcing the cache back in line. That forcing would
hide the very bug this sweep exists to catch.

Distinct from packages/core/reconcile_job.py, the standalone CLI version
of the same check: that one is a one-shot batch job intended for an
external cron/systemd-timer/k8s-CronJob (never actually wired up anywhere
per its own docstring) and pushes its result to a Prometheus Pushgateway.
This sweep reuses its same tested comparison (ledger.reconcile(), via
reconcile_job.reconcile_all()) but runs it from a process that is already
alive on a timer, so it needs no external scheduler at all.
"""

from __future__ import annotations

import asyncpg
import structlog

from packages.core import metrics
from packages.core.reconcile_job import reconcile_all

logger = structlog.get_logger()


async def sweep_ledger_reconciliation(pool: asyncpg.Pool) -> None:
    mismatches = await reconcile_all(pool)
    metrics.ledger_reconciliation_sweep_mismatch_count.set(len(mismatches))
    if mismatches:
        logger.error(
            "ledger_reconciliation_sweep_found_mismatch",
            mismatch_count=len(mismatches),
            mismatches=[
                {"account_id": account_id, "cached": str(cached), "computed": str(computed)}
                for account_id, cached, computed in mismatches
            ],
        )
    else:
        logger.info("ledger_reconciliation_sweep_ok")
