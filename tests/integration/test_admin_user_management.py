"""Admin account management (services/admin/queries.py's admin_users:*
functions): the console-facing replacement for out-of-band script
provisioning. Real Postgres, real HTTP against the real admin API for
the RBAC boundary, exactly the same rigor as every other admin-console
feature's own test file.
"""

import httpx
import pyotp
import pytest

from services.admin import auth, queries
from tests.integration.test_admin_app import _auth_headers, _login
from tests.integration.test_admin_auth import create_test_admin, unique_username


async def test_create_admin_user_admin_returns_a_working_totp_secret(pool):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    result = await queries.create_admin_user_admin(
        pool, admin_id=creator_id, username=unique_username(), password="a-strong-password-123",
        role="finance", ip_address=None,
    )
    assert result["totp_secret"]
    assert "otpauth://" in result["totp_provisioning_uri"]

    row = await pool.fetchrow("SELECT role, is_active FROM admin_users WHERE id = $1", result["id"])
    assert row["role"] == "finance"
    assert row["is_active"] is True


async def test_create_admin_user_admin_rejects_a_short_password(pool):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(ValueError):
        await queries.create_admin_user_admin(
            pool, admin_id=creator_id, username=unique_username(), password="short",
            role="finance", ip_address=None,
        )


async def test_create_admin_user_admin_rejects_an_unknown_role(pool):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(ValueError):
        await queries.create_admin_user_admin(
            pool, admin_id=creator_id, username=unique_username(), password="a-strong-password-123",
            role="owner", ip_address=None,
        )


async def test_create_admin_user_admin_rejects_a_duplicate_username(pool):
    creator_id, username, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(queries.AdminUsernameTaken):
        await queries.create_admin_user_admin(
            pool, admin_id=creator_id, username=username, password="a-strong-password-123",
            role="finance", ip_address=None,
        )


async def test_deactivated_admin_cannot_log_in(pool, redis):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    target_id, username, password, totp_secret = await create_test_admin(pool, role="ops")

    code = pyotp.TOTP(totp_secret).now()
    token = await auth.login(pool, redis, username=username, password=password, totp_code=code)
    assert await auth.resolve_session(pool, redis, token) is not None

    updated = await queries.set_admin_user_active_admin(
        pool, admin_id=creator_id, target_admin_id=target_id, is_active=False, ip_address=None
    )
    assert updated is True

    # The existing, already-issued session is revoked immediately -- not
    # just future logins -- per auth.resolve_session's own re-check.
    assert await auth.resolve_session(pool, redis, token) is None
    with pytest.raises(auth.LoginFailed):
        await auth.login(pool, redis, username=username, password=password, totp_code=code)


async def test_cannot_deactivate_your_own_account(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(queries.CannotModifyOwnAccount):
        await queries.set_admin_user_active_admin(
            pool, admin_id=admin_id, target_admin_id=admin_id, is_active=False, ip_address=None
        )


async def test_cannot_change_your_own_role(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(queries.CannotModifyOwnAccount):
        await queries.set_admin_user_role_admin(
            pool, admin_id=admin_id, target_admin_id=admin_id, role="ops", ip_address=None
        )


async def test_set_admin_user_role_changes_what_they_can_do_next_login(pool, redis):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    target_id, username, password, totp_secret = await create_test_admin(pool, role="support")

    await queries.set_admin_user_role_admin(
        pool, admin_id=creator_id, target_admin_id=target_id, role="finance", ip_address=None
    )

    code = pyotp.TOTP(totp_secret).now()
    token = await auth.login(pool, redis, username=username, password=password, totp_code=code)
    session = await auth.resolve_session(pool, redis, token)
    assert session.role == "finance"


async def test_reset_admin_user_password_lets_them_log_in_with_the_new_one(pool, redis):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    target_id, username, old_password, totp_secret = await create_test_admin(pool, role="ops")

    await queries.reset_admin_user_password_admin(
        pool, admin_id=creator_id, target_admin_id=target_id, new_password="brand-new-password-1",
        ip_address=None,
    )

    code = pyotp.TOTP(totp_secret).now()
    with pytest.raises(auth.LoginFailed):
        await auth.login(pool, redis, username=username, password=old_password, totp_code=code)
    token = await auth.login(
        pool, redis, username=username, password="brand-new-password-1", totp_code=code
    )
    assert token


async def test_reset_admin_user_password_rejects_a_short_password(pool):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    target_id, *_ = await create_test_admin(pool, role="ops")
    with pytest.raises(ValueError):
        await queries.reset_admin_user_password_admin(
            pool, admin_id=creator_id, target_admin_id=target_id, new_password="short", ip_address=None
        )


async def test_audit_log_records_admin_creation_never_the_password_or_totp(pool):
    creator_id, *_ = await create_test_admin(pool, role="superadmin")
    result = await queries.create_admin_user_admin(
        pool, admin_id=creator_id, username=unique_username(), password="a-strong-password-123",
        role="finance", ip_address="10.0.0.5",
    )
    row = await pool.fetchrow(
        "SELECT admin_id, action, target_id, after, ip_address FROM admin_audit_log "
        "WHERE action = 'admin_users.create' AND target_id = $1",
        str(result["id"]),
    )
    assert row is not None
    assert row["admin_id"] == creator_id
    assert row["ip_address"] == "10.0.0.5"
    assert "a-strong-password-123" not in row["after"]
    assert result["totp_secret"] not in row["after"]


# --- RBAC over real HTTP -------------------------------------------------


async def test_non_superadmin_roles_cannot_reach_admin_users_over_http(admin_server, pool):
    for role in ("support", "finance", "ops"):
        headers = await _auth_headers(admin_server, pool, role=role)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/admin-users", headers=headers)
        assert response.status_code == 403, f"role {role!r} should not reach /admin-users"


async def test_superadmin_can_create_and_list_admin_users_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    new_username = unique_username()
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{admin_server}/admin-users",
            json={"username": new_username, "password": "a-strong-password-123", "role": "finance"},
            headers=headers,
        )
        assert create.status_code == 200, create.text
        assert "totp_secret" in create.json()

        listing = await client.get(f"{admin_server}/admin-users", headers=headers)
    assert listing.status_code == 200
    usernames = [row["username"] for row in listing.json()]
    assert new_username in usernames
    # password_hash/totp_secret are never in the list payload at all.
    assert all("password_hash" not in row and "totp_secret" not in row for row in listing.json())


async def test_finance_cannot_create_an_admin_user_via_direct_api_bypass(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="finance")
    blocked_username = unique_username()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/admin-users",
            json={"username": blocked_username, "password": "a-strong-password-123", "role": "superadmin"},
            headers=headers,
        )
    assert response.status_code == 403
    row = await pool.fetchrow("SELECT id FROM admin_users WHERE username = $1", blocked_username)
    assert row is None


async def test_audit_log_filters_by_admin_id_and_shows_username(admin_server, pool):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    token = await _login(admin_server, username, password, totp_secret)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        created = await client.post(
            f"{admin_server}/admin-users",
            json={"username": unique_username(), "password": "a-strong-password-123", "role": "ops"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        response = await client.get(f"{admin_server}/audit-log?admin_id={admin_id}", headers=headers)
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 1
    assert all(r["admin_id"] == admin_id for r in rows)
    assert all(r["admin_username"] == username for r in rows)
