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
import re
import shutil
import socket
import subprocess
import time
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


# --- WAL archiving + point-in-time recovery -----------------------------
#
# The capability deploy/backup.sh/restore.sh's logical pg_dump/pg_restore
# path above architecturally cannot provide: replaying forward to an EXACT
# point in time strictly between two backups, not just restoring to
# whenever the last dump happened to be taken. Spec section 9.2 (idea.md
# ~line 6161) names this explicitly: "PostgreSQL PITR with WAL archiving,
# 30-day retention, and a restore drill run monthly." See DECISIONS.md for
# the full design and deploy/basebackup.sh/restore_pitr.sh's own comments.


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _ensure_wal_archive_dir_writable() -> None:
    # deploy/docker-compose.yml's postgres service writes archived WAL
    # segments here (as its own container uid), and deploy/restore_pitr.sh
    # reads them back from a SEPARATE throwaway container running as the
    # host user -- Docker auto-creates a missing bind-mount source as
    # root:root, which neither of those uids can write into. A real
    # deployment does this once, host-user-owned and world-writable,
    # BEFORE the postgres container's first start (README documents it,
    # mirroring deploy/.env's own one-time-setup precedent). This call
    # makes the test self-healing for a fresh checkout that hasn't run
    # that step yet -- it can create the directory (or fix perms on one
    # this same host user already owns) but can't fix one Docker already
    # auto-created as root; see DECISIONS.md for the real gotcha this
    # documents.
    wal_dir = DEPLOY_DIR / ".." / "backups" / "wal_archive"
    wal_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(wal_dir, 0o777)


async def _psql(sql: str) -> None:
    # Each statement its own real round trip against the dev compose
    # Postgres -- a genuine, non-obvious gotcha found while building this
    # test: batching an INSERT and SELECT pg_switch_wal() into ONE
    # multi-statement psql call defers the INSERT's own COMMIT record
    # until AFTER the switch, landing it in the segment the switch just
    # rotated INTO (which then sits unarchived) rather than the one that
    # gets archived -- silently breaking recovery_target_time's ability to
    # ever find it, since the commit it needs to see never made it into
    # any archived segment. See DECISIONS.md.
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "-f", str(DEPLOY_DIR / "docker-compose.yml"),
        "exec", "-T", "postgres", "psql", "-U", "jobingo", "-d", "jobingo", "-c", sql,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, (stdout + stderr).decode()


async def test_wal_archiving_supports_point_in_time_recovery(pool, conn):
    _ensure_wal_archive_dir_writable()

    table = f"pitr_test_{os.urandom(4).hex()}"
    port = _free_port()
    container_name = f"jobingo-pitr-drill-test-{os.urandom(4).hex()}"
    base_tar: str | None = None
    pgdata_dir: str | None = None

    try:
        await _psql(f"CREATE TABLE {table} (label text)")
        await _psql(f"INSERT INTO {table} (label) VALUES ('user_A')")
        await _psql("SELECT pg_switch_wal()")

        base_output = await _run(str(DEPLOY_DIR / "basebackup.sh"))
        base_tar = base_output.strip().rsplit(" ", 1)[-1]
        assert Path(base_tar).is_file()

        # recovery_target_time must be chronologically AFTER the base
        # backup's own redo point, or there's nothing for replay to "stop
        # before" -- another real gotcha this test's own first draft ran
        # into (see DECISIONS.md). Captured via the server's own clock
        # (not the host's), same reasoning the capstone manual-payment
        # e2e test already established for T1-style timestamps elsewhere
        # in this suite.
        target_time = await conn.fetchval("SELECT clock_timestamp()")

        before_archived = await conn.fetchval("SELECT archived_count FROM pg_stat_archiver")
        await _psql(f"INSERT INTO {table} (label) VALUES ('user_B')")
        await _psql("SELECT pg_switch_wal()")
        for _ in range(20):
            after_archived = await conn.fetchval("SELECT archived_count FROM pg_stat_archiver")
            if after_archived > before_archived:
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("WAL segment containing the post-target insert was never archived")

        restore_output = await _run(
            str(DEPLOY_DIR / "restore_pitr.sh"), base_tar, str(target_time), str(port), container_name,
        )
        match = re.search(r"pgdata=(\S+)", restore_output)
        assert match, restore_output
        pgdata_dir = match.group(1)

        recovered_conn = await asyncpg.connect(dsn=f"postgresql://jobingo:jobingo@127.0.0.1:{port}/jobingo")
        try:
            rows = await recovered_conn.fetch(f"SELECT label FROM {table} ORDER BY label")
            # The real proof recovery stopped exactly at the target time,
            # not "at the end of whatever WAL happened to be archived":
            # user_A (committed before target_time) survived; user_B
            # (committed after) does not exist at all in the recovered
            # instance.
            assert [r["label"] for r in rows] == ["user_A"]
        finally:
            await recovered_conn.close()
    finally:
        stop = await asyncio.create_subprocess_exec(
            "docker", "stop", container_name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await stop.communicate()
        if pgdata_dir:
            shutil.rmtree(pgdata_dir, ignore_errors=True)
        await _psql(f"DROP TABLE IF EXISTS {table}")
        if base_tar:
            shutil.rmtree(Path(base_tar).parent, ignore_errors=True)


async def test_prune_wal_archive_deletes_only_items_older_than_the_retention_window(tmp_path):
    wal_dir = tmp_path / "wal_archive"
    wal_dir.mkdir()
    old_wal = wal_dir / "000000010000000000000001"
    new_wal = wal_dir / "000000010000000000000002"
    old_wal.write_bytes(b"old")
    new_wal.write_bytes(b"new")
    old_time = time.time() - 40 * 86400  # well past the default 30-day window
    os.utime(old_wal, (old_time, old_time))

    basebackup_dir = tmp_path / "basebackups"
    old_backup = basebackup_dir / "20260101T000000Z"
    new_backup = basebackup_dir / "20260901T000000Z"
    old_backup.mkdir(parents=True)
    new_backup.mkdir(parents=True)
    (old_backup / "base.tar").write_bytes(b"old")
    (new_backup / "base.tar").write_bytes(b"new")
    os.utime(old_backup, (old_time, old_time))

    proc = await asyncio.create_subprocess_exec(
        str(DEPLOY_DIR / "prune_wal_archive.sh"), "30",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "BACKUP_DIR": str(tmp_path)},
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, (stdout + stderr).decode()

    assert not old_wal.exists()
    assert new_wal.exists()
    assert not old_backup.exists()
    assert new_backup.exists()
