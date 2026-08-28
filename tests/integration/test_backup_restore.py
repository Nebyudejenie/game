"""A real backup/restore drill -- spec section 14's definition of done:
"a full restore from backup has been performed in the last 30 days." That
exact claim is a production operating fact this session can't manufacture
(no production deployment exists, and no 30 days have elapsed), but the
underlying capability -- can a dump taken by deploy/backup.sh actually be
restored by deploy/restore.sh with the data intact -- is genuinely testable
right now, for real: real pg_dump and pg_restore binaries, invoked the same
way an operator would, against the real docker-compose Postgres.

The restore target is always a throwaway database
(jobingo_restore_drill_test), never the shared `jobingo` database every
other test in this suite depends on -- this file creates and drops its own
database on the same Postgres server without touching anyone else's
connection, so it needs no chaos_infra-style isolation.
"""

import asyncio
import os
import subprocess
from decimal import Decimal
from pathlib import Path

import asyncpg

from packages.core.config import get_settings
from tests.integration.conftest import create_funded_user

DEPLOY_DIR = Path(__file__).resolve().parent.parent.parent / "deploy"
DRILL_DB = "jobingo_restore_drill_test"


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, (stdout + stderr).decode()
    return stdout.decode()


async def _drop_drill_db(pool: asyncpg.Pool) -> None:
    settings = get_settings()
    maintenance_dsn = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    conn = await asyncpg.connect(dsn=maintenance_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DRILL_DB}" WITH (FORCE)')
    finally:
        await conn.close()


async def test_a_real_backup_restores_with_the_data_intact(pool, conn, tmp_path):
    # A uniquely identifiable amount, so this test can never pass by
    # accident against stale data left over from a previous run.
    user_id = await create_funded_user(conn, Decimal("543.21"))
    source_balance = await pool.fetchval(
        """
        SELECT b.balance FROM account_balances b
        JOIN accounts a ON a.id = b.account_id
        WHERE a.user_id = $1 AND a.kind = 'user_cash'
        """,
        user_id,
    )
    assert source_balance == Decimal("543.21")

    await _drop_drill_db(pool)
    try:
        backup_output = await _run(str(DEPLOY_DIR / "backup.sh"), "jobingo")
        # backup.sh prints "Backup written to <path>" -- parse its own
        # reported path rather than guessing a filename pattern, so this
        # test breaks loudly if that contract ever changes instead of
        # silently checking the wrong file.
        dump_path = backup_output.strip().rsplit(" ", 1)[-1]
        assert Path(dump_path).is_file()

        await _run(str(DEPLOY_DIR / "restore.sh"), dump_path, DRILL_DB)

        settings = get_settings()
        drill_dsn = settings.database_url.rsplit("/", 1)[0] + f"/{DRILL_DB}"
        drill_conn = await asyncpg.connect(dsn=drill_dsn)
        try:
            restored_balance = await drill_conn.fetchval(
                """
                SELECT b.balance FROM account_balances b
                JOIN accounts a ON a.id = b.account_id
                WHERE a.user_id = $1 AND a.kind = 'user_cash'
                """,
                user_id,
            )
            assert restored_balance == source_balance == Decimal("543.21")

            # Prove this is a full restore, not a lucky single-row match --
            # the whole cards pool (100 rows, seeded once in migrations)
            # must have made it across intact too.
            restored_card_count = await drill_conn.fetchval("SELECT count(*) FROM cards")
            source_card_count = await pool.fetchval("SELECT count(*) FROM cards")
            assert restored_card_count == source_card_count == 100
        finally:
            await drill_conn.close()

        Path(dump_path).unlink()
    finally:
        await _drop_drill_db(pool)


async def test_restore_refuses_a_missing_backup_file(tmp_path):
    missing = tmp_path / "does-not-exist.dump"
    proc = await asyncio.create_subprocess_exec(
        str(DEPLOY_DIR / "restore.sh"), str(missing), DRILL_DB,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode != 0
    assert "not found" in (stdout + stderr).decode()


async def test_backup_honors_a_compose_file_override(tmp_path):
    # An architecture audit caught both scripts hardcoding docker-compose
    # .yml (the dev stack) with no way to point at production at all --
    # this proves COMPOSE_FILE is actually read, not silently ignored, by
    # pointing it at a real, deliberately-nonexistent file and confirming
    # the script fails trying to use it (docker compose's own real exit
    # code and error, not a mocked one) rather than quietly falling back
    # to the dev compose file and succeeding anyway.
    env = {**os.environ, "COMPOSE_FILE": str(tmp_path / "does-not-exist.yml")}
    proc = await asyncio.create_subprocess_exec(
        str(DEPLOY_DIR / "backup.sh"), "jobingo",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode != 0
    assert "does-not-exist.yml" in (stdout + stderr).decode()


async def test_prod_compose_reconcile_job_is_valid_and_profile_gated():
    # Spec section 14's definition of done needs the ledger reconciliation
    # job "verified nightly" -- deploy/docker-compose.prod.yml now has a
    # reconcile-job service for that (README.md's "Nightly ledger
    # reconciliation" section), gated behind profiles: ["reconcile"] so a
    # plain `up -d` never runs it once per deploy the way migrate
    # correctly does. This is real `docker compose config` output, the
    # exact tool a deploy actually uses -- not just eyeballing the YAML --
    # proving both that the service definition is genuinely valid and
    # that the profile gate genuinely works, not just that it looks right.
    env_path = DEPLOY_DIR / ".env"
    backup_path = env_path.with_suffix(".env.bak-test")
    had_existing_env = env_path.exists()
    if had_existing_env:
        env_path.rename(backup_path)
    try:
        # Only what config validation actually needs: POSTGRES_PASSWORD
        # and PHONE_ENCRYPTION_KEY have no default in the compose file
        # (deliberately -- see .env.prod.example), so config resolution
        # fails without *some* value for them.
        env_path.write_text("POSTGRES_PASSWORD=test-dummy\nPHONE_ENCRYPTION_KEY=" + "0" * 64 + "\n")

        default_services = await _run(
            "docker", "compose", "-f", str(DEPLOY_DIR / "docker-compose.prod.yml"), "config", "--services",
        )
        assert "reconcile-job" not in default_services.split()

        gated_services = await _run(
            "docker", "compose", "-f", str(DEPLOY_DIR / "docker-compose.prod.yml"),
            "--profile", "reconcile", "config", "--services",
        )
        assert "reconcile-job" in gated_services.split()
    finally:
        env_path.unlink(missing_ok=True)
        if had_existing_env:
            backup_path.rename(env_path)
