"""Tests for services/admin/auth.py: account provisioning, password + TOTP
login, session resolution, and logout.
"""

import uuid

import pyotp
import pytest

from services.admin import auth


def unique_username() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}"


async def create_test_admin(pool, *, role: str = "superadmin", password: str = "correct horse battery staple"):
    username = unique_username()
    admin_id, totp_secret = await auth.create_admin_user(
        pool, username=username, password=password, role=role
    )
    return admin_id, username, password, totp_secret


async def test_create_admin_user_hashes_the_password_not_plaintext(pool):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    row = await pool.fetchrow("SELECT password_hash FROM admin_users WHERE id = $1", admin_id)
    assert row["password_hash"] != password
    assert row["password_hash"].startswith("$2b$")  # bcrypt


async def test_successful_login_returns_a_working_session(pool, redis):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    code = pyotp.TOTP(totp_secret).now()

    token = await auth.login(pool, redis, username=username, password=password, totp_code=code)
    assert token

    session = await auth.resolve_session(redis, token)
    assert session is not None
    assert session.admin_id == admin_id
    assert session.username == username


async def test_wrong_password_rejected(pool, redis):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    code = pyotp.TOTP(totp_secret).now()

    with pytest.raises(auth.LoginFailed):
        await auth.login(pool, redis, username=username, password="wrong password", totp_code=code)


async def test_wrong_totp_code_rejected(pool, redis):
    admin_id, username, password, totp_secret = await create_test_admin(pool)

    with pytest.raises(auth.LoginFailed):
        await auth.login(pool, redis, username=username, password=password, totp_code="000000")


async def test_unknown_username_rejected(pool, redis):
    with pytest.raises(auth.LoginFailed):
        await auth.login(pool, redis, username="no-such-admin", password="x", totp_code="000000")


async def test_deactivated_account_rejected_even_with_correct_credentials(pool, redis):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    await pool.execute("UPDATE admin_users SET is_active = false WHERE id = $1", admin_id)
    code = pyotp.TOTP(totp_secret).now()

    with pytest.raises(auth.LoginFailed):
        await auth.login(pool, redis, username=username, password=password, totp_code=code)


async def test_login_failure_messages_do_not_distinguish_failure_reason(pool, redis):
    # Unknown user, wrong password, and wrong TOTP must all raise the same
    # message -- otherwise the error text itself becomes a username or
    # 2FA-status oracle.
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    code = pyotp.TOTP(totp_secret).now()

    messages = set()
    for kwargs in [
        dict(username="no-such-user", password="x", totp_code="000000"),
        dict(username=username, password="wrong", totp_code=code),
        dict(username=username, password=password, totp_code="000000"),
    ]:
        try:
            await auth.login(pool, redis, **kwargs)
        except auth.LoginFailed as exc:
            messages.add(str(exc))
    assert len(messages) == 1


async def test_logout_invalidates_the_session(pool, redis):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    code = pyotp.TOTP(totp_secret).now()
    token = await auth.login(pool, redis, username=username, password=password, totp_code=code)

    assert await auth.resolve_session(redis, token) is not None
    await auth.logout(redis, token)
    assert await auth.resolve_session(redis, token) is None


async def test_resolve_session_returns_none_for_garbage_token(redis):
    assert await auth.resolve_session(redis, "not-a-real-token") is None


async def test_successful_login_updates_last_login_at(pool, redis):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    before = await pool.fetchval("SELECT last_login_at FROM admin_users WHERE id = $1", admin_id)
    assert before is None

    code = pyotp.TOTP(totp_secret).now()
    await auth.login(pool, redis, username=username, password=password, totp_code=code)

    after = await pool.fetchval("SELECT last_login_at FROM admin_users WHERE id = $1", admin_id)
    assert after is not None
