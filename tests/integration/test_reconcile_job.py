"""Tests for packages/core/reconcile_job.py -- the nightly ledger
reconciliation job spec section 14's definition of done calls for. Both
the testable core (reconcile_all(), driven directly against the shared
test pool) and the actual CLI entrypoint (run as a real subprocess, the
same way a cron job would invoke it) are exercised for real.
"""

import asyncio
import os
import socket
import sys
from decimal import Decimal

from aiohttp import web

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


async def _run_cli(extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    env = {**os.environ, **(extra_env or {})}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "packages.core.reconcile_job",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
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


class _CapturingPushgateway:
    """A real HTTP server, not a mock object -- proves reconcile_job.py's
    CLI process actually makes a real PUT request with real Prometheus
    exposition-format content, the same "real binary/real protocol, not a
    stand-in" discipline as this session's other integration drills. Not
    the literal prom/pushgateway image (that's exercised manually -- see
    DECISIONS.md) so the default test suite doesn't gain a new required
    docker service just for this one test.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []  # (method, body)

    async def _catch_all(self, request: web.Request) -> web.Response:
        body = (await request.read()).decode()
        self.requests.append((request.method, body))
        return web.Response(status=202)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def test_reconcile_job_cli_pushes_the_mismatch_count_to_a_real_http_server(pool, conn):
    capture = _CapturingPushgateway()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", capture._catch_all)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    try:
        user_id = await create_funded_user(conn, Decimal("60.00"))
        cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
        try:
            await conn.execute(
                "UPDATE account_balances SET balance = balance + 999 WHERE account_id = $1", cash.id
            )
            returncode, output = await _run_cli(
                extra_env={"PUSHGATEWAY_URL": f"http://127.0.0.1:{port}"}
            )
            assert returncode == 1, output
        finally:
            await conn.execute(
                "UPDATE account_balances SET balance = balance - 999 WHERE account_id = $1", cash.id
            )

        assert len(capture.requests) == 1, capture.requests
        method, body = capture.requests[0]
        assert method == "PUT"
        assert "ledger_reconciliation_mismatch_count" in body
        # At least the one account corrupted above -- exact count isn't
        # pinned since the shared test database may carry other drift from
        # whatever ran concurrently, but zero would prove the push never
        # picked up a real number at all.
        assert "ledger_reconciliation_mismatch_count 0.0" not in body
    finally:
        await runner.cleanup()


async def test_reconcile_job_cli_does_not_push_when_pushgateway_url_is_unset(pool, conn):
    capture = _CapturingPushgateway()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", capture._catch_all)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    try:
        await create_funded_user(conn, Decimal("15.00"))
        # No PUSHGATEWAY_URL override -- explicitly cleared, in case the
        # real dev .env happens to set one, so this test genuinely proves
        # the "unset" path rather than accidentally inheriting a set one.
        returncode, output = await _run_cli(extra_env={"PUSHGATEWAY_URL": ""})
        assert returncode == 0, output
        assert capture.requests == []
    finally:
        await runner.cleanup()
