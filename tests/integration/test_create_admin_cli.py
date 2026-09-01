"""Tests for services/admin/create_admin_cli.py -- the real subprocess
entrypoint an operator runs by hand to provision the very first admin
account on a fresh production deploy (no self-registration path exists,
on purpose -- see services/admin/auth.py's own module docstring). Run as
a real subprocess with real stdin, the same discipline packages/core/
reconcile_job.py's own CLI test already established, not just calling the
underlying function directly.
"""

import asyncio
import os
import sys

import pyotp

from services.admin import auth
from tests.integration.test_admin_auth import unique_username


async def _run_cli(username: str, role: str, password: str, confirm: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "services.admin.create_admin_cli",
        "--username", username, "--role", role,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=os.environ,
    )
    stdin_text = f"{password}\n{confirm if confirm is not None else password}\n"
    stdout, stderr = await proc.communicate(input=stdin_text.encode())
    assert proc.returncode is not None
    return proc.returncode, (stdout + stderr).decode()


async def test_creates_a_real_working_admin_account(pool):
    username = unique_username()
    returncode, output = await _run_cli(username, "finance", "a genuinely long enough password")
    assert returncode == 0, output
    assert username in output
    assert "role=finance" in output

    row = await pool.fetchrow("SELECT id, role, password_hash, totp_secret FROM admin_users WHERE username = $1", username)
    assert row is not None
    assert row["role"] == "finance"
    assert row["password_hash"] != "a genuinely long enough password"  # real hash, not plaintext

    # The printed TOTP secret is real and actually verifies -- not just a
    # string that happened to get echoed back.
    match = None
    for line in output.splitlines():
        if "TOTP secret" in line:
            match = line.rsplit(": ", 1)[-1].strip()
    assert match == row["totp_secret"]
    code = pyotp.TOTP(match).now()
    assert pyotp.TOTP(row["totp_secret"]).verify(code)


async def test_rejects_a_short_password_without_creating_an_account(pool):
    username = unique_username()
    returncode, output = await _run_cli(username, "support", "short1")
    assert returncode == 1
    assert "at least" in output

    row = await pool.fetchval("SELECT id FROM admin_users WHERE username = $1", username)
    assert row is None


async def test_rejects_mismatched_password_confirmation(pool):
    username = unique_username()
    returncode, output = await _run_cli(
        username, "ops", "a genuinely long enough password", confirm="a different long enough password"
    )
    assert returncode == 1
    assert "did not match" in output

    row = await pool.fetchval("SELECT id FROM admin_users WHERE username = $1", username)
    assert row is None


async def test_rejects_an_invalid_role_before_touching_the_database(pool):
    username = unique_username()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "services.admin.create_admin_cli",
        "--username", username, "--role", "not_a_real_role",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=os.environ,
    )
    stdout, stderr = await proc.communicate(input=b"")
    assert proc.returncode != 0
    # Specifically argparse's own rejection (not just "some" nonzero exit,
    # which a missing module or any other unrelated failure would also
    # produce) -- a genuine regression test has to fail for the right
    # reason, not just any reason.
    assert "invalid choice" in (stdout + stderr).decode()

    row = await pool.fetchval("SELECT id FROM admin_users WHERE username = $1", username)
    assert row is None


async def test_the_created_account_can_actually_log_in(pool, redis):
    username = unique_username()
    password = "a genuinely long enough password"
    returncode, output = await _run_cli(username, "superadmin", password)
    assert returncode == 0, output

    totp_secret = next(
        line.rsplit(": ", 1)[-1].strip() for line in output.splitlines() if "TOTP secret" in line
    )
    code = pyotp.TOTP(totp_secret).now()
    token = await auth.login(pool, redis, username=username, password=password, totp_code=code)
    assert token
