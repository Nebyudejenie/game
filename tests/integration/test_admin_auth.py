"""Tests for services/admin/auth.py: account provisioning, password + TOTP
login, session resolution, and logout.
"""

import uuid

import pyotp
import pytest

from packages.core import rate_limit
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
        await auth.login(
            pool, redis, username=unique_username(), password="x", totp_code="000000"
        )


async def test_unknown_username_still_pays_the_real_bcrypt_cost(pool, redis, monkeypatch):
    # Regression: a real code review pass caught a timing side-channel --
    # an unknown username used to return LoginFailed immediately, while a
    # real username always paid bcrypt's ~100ms cost checking the
    # password first, letting an attacker time responses to enumerate
    # valid admin usernames (the exact thing this module's own docstring
    # already promised error *text* alone could never reveal). A strict
    # wall-clock timing assertion would be fragile on a shared host this
    # session has already documented real contention on -- what's
    # deterministically verifiable instead is that _verify_password() is
    # genuinely called (against the fixed dummy hash) for the unknown-
    # username path, not skipped.
    calls: list[tuple[str, str]] = []
    real_verify = auth._verify_password

    def spy(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return real_verify(password, password_hash)

    monkeypatch.setattr(auth, "_verify_password", spy)

    with pytest.raises(auth.LoginFailed):
        await auth.login(
            pool, redis, username=unique_username(), password="x", totp_code="000000"
        )

    assert calls == [("x", auth._DUMMY_PASSWORD_HASH)]


async def test_login_is_rate_limited_per_username(pool, redis):
    # Regression: a real code review pass caught that nothing anywhere in
    # the admin console throttled login attempts -- a known username's
    # password could be brute-forced online with no lockout at all.
    admin_id, username, password, totp_secret = await create_test_admin(pool)

    for _ in range(rate_limit.ADMIN_LOGIN["capacity"]):
        with pytest.raises(auth.LoginFailed):
            await auth.login(pool, redis, username=username, password="wrong", totp_code="000000")

    with pytest.raises(auth.LoginRateLimited):
        await auth.login(pool, redis, username=username, password="wrong", totp_code="000000")

    # The real password would have worked, were the account not rate
    # limited -- proves this actually blocks the correct-credentials
    # attempt too, not just further wrong guesses.
    code = pyotp.TOTP(totp_secret).now()
    with pytest.raises(auth.LoginRateLimited):
        await auth.login(pool, redis, username=username, password=password, totp_code=code)


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
        dict(username=unique_username(), password="x", totp_code="000000"),
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
