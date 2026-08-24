"""Tests for packages/core/reconcile_job.py -- the nightly ledger
reconciliation job spec section 14's definition of done calls for. Both
the testable core (reconcile_all(), driven directly against the shared
test pool) and the actual CLI entrypoint (run as a real subprocess, the
same way a cron job would invoke it) are exercised for real.
"""

import asyncio
import sys
from decimal import Decimal

from packages.core import ledger
from packages.core.reconcile_job import reconcile_all
from tests.integration.conftest import create_funded_user


async def test_reconcile_all_finds_nothing_wrong_on_a_healthy_ledger(pool, conn):
    # A real deposit/stake-shaped transaction through the real ledger --
    # if post() and reconcile() ever disagreed about what "balanced" means,
    # this is what would catch it.
    await create_funded_user(conn, Decimal("250.00"))
    mismatches = await reconcile_all(pool)
    assert mismatches == []


async def test_reconcile_all_catches_a_cache_drifted_out_of_sync_with_the_ledger(pool, conn):
    user_id = await create_funded_user(conn, Decimal("100.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")

    # The one way this cache could ever legitimately drift from the ledger
    # entries is a bug -- simulate that bug directly, bypassing post()
    # entirely, since there's no other way to construct this state. This is
    # the *shared, long-lived* test database every other test (and the CLI
    # subprocess test below) also reconciles against, so the corruption is
    # undone in a finally block -- leaving it behind would permanently fail
    # every reconciliation check for the rest of this database's life, not
    # just this one test.
    try:
        await conn.execute(
            "UPDATE account_balances SET balance = balance + 999 WHERE account_id = $1", cash.id
        )
        mismatches = await reconcile_all(pool)
        assert any(account_id == cash.id for account_id, _cached, _computed in mismatches)
    finally:
        await conn.execute(
            "UPDATE account_balances SET balance = balance - 999 WHERE account_id = $1", cash.id
        )


async def _run_cli() -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "packages.core.reconcile_job",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode, (stdout + stderr).decode()


async def test_reconcile_job_cli_exits_zero_on_a_healthy_ledger(pool, conn):
    await create_funded_user(conn, Decimal("75.00"))
    returncode, output = await _run_cli()
    assert returncode == 0, output
    assert "ledger_reconciliation_ok" in output


async def test_reconcile_job_cli_exits_one_and_reports_a_real_drift(pool, conn):
    user_id = await create_funded_user(conn, Decimal("100.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    try:
        await conn.execute(
            "UPDATE account_balances SET balance = balance + 999 WHERE account_id = $1", cash.id
        )
        returncode, output = await _run_cli()
        assert returncode == 1, output
        assert "ledger_reconciliation_failed" in output
        assert str(cash.id) in output
    finally:
        await conn.execute(
            "UPDATE account_balances SET balance = balance - 999 WHERE account_id = $1", cash.id
        )
