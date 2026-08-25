"""HTTP-layer tests for services/admin/app.py: login end-to-end over real
requests, RBAC enforced through the actual dependency chain (not by calling
has_permission() directly), audit log immutability enforced by Postgres
itself, and IP allowlist enforcement.
"""

from decimal import Decimal

import asyncpg
import httpx
import pyotp
import pytest

from packages.core import ledger
from services.admin.app import app as admin_app
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_auth import create_test_admin


async def _login(admin_server: str, username: str, password: str, totp_secret: str) -> str:
    code = pyotp.TOTP(totp_secret).now()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/auth/login",
            json={"username": username, "password": password, "totp_code": code},
        )
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


async def _auth_headers(admin_server: str, pool, role: str = "superadmin") -> dict[str, str]:
    admin_id, username, password, totp_secret = await create_test_admin(pool, role=role)
    token = await _login(admin_server, username, password, totp_secret)
    return {"Authorization": f"Bearer {token}"}


async def test_login_rejects_wrong_totp_over_http(admin_server, pool):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/auth/login",
            json={"username": username, "password": password, "totp_code": "000000"},
        )
    assert response.status_code == 401


async def test_protected_route_requires_bearer_token(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard")
    assert response.status_code == 401


async def test_protected_route_rejects_garbage_token(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{admin_server}/dashboard", headers={"Authorization": "Bearer not-a-real-token"}
        )
    assert response.status_code == 401


async def test_superadmin_can_reach_dashboard_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard", headers=headers)
    assert response.status_code == 200
    assert "active_rounds" in response.json()


async def test_rbac_support_cannot_adjust_balance_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_funded_user(conn, Decimal("10.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/adjust",
            headers=headers,
            json={"amount": "5.00", "reason": "should be blocked by rbac"},
        )
    assert response.status_code == 403

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("10.00")


async def test_rbac_finance_can_adjust_balance_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/adjust",
            headers=headers,
            json={"amount": "5.00", "reason": "goodwill credit over http"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["ledger_transaction_id"]

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("15.00")


async def test_audit_log_route_requires_superadmin(admin_server, pool):
    for role in ("support", "finance", "ops"):
        headers = await _auth_headers(admin_server, pool, role=role)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/audit-log", headers=headers)
        assert response.status_code == 403, role

    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/audit-log", headers=headers)
    assert response.status_code == 200


async def test_logout_invalidates_token_over_http(admin_server, pool):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    token = await _login(admin_server, username, password, totp_secret)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard", headers=headers)
    assert response.status_code == 200

    async with httpx.AsyncClient() as client:
        response = await client.post(f"{admin_server}/auth/logout", headers=headers)
    assert response.status_code == 200

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard", headers=headers)
    assert response.status_code == 401


async def test_ip_allowlist_blocks_disallowed_source(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    admin_app.state.ip_allowlist = ["10.0.0.1"]
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/dashboard", headers=headers)
        assert response.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_metrics_endpoint_is_reachable_with_no_session_token(admin_server):
    # Unlike every other route, /metrics doesn't require a bearer session
    # (a Prometheus scraper can't practically present one) -- but it must
    # still go through the IP allowlist, confirmed by the next test.
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/metrics")
    assert response.status_code == 200
    assert "house_revenue_total" in response.text


async def test_metrics_endpoint_is_blocked_by_the_ip_allowlist(admin_server):
    # Regression: a real code review pass caught this endpoint bypassing
    # the IP allowlist entirely -- house_revenue_total (live revenue in
    # ETB), deposit_outcomes_total, and payout_queue_depth were reachable
    # by anyone on the network with no protection at all, unlike every
    # other route in this file.
    admin_app.state.ip_allowlist = ["10.0.0.1"]
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/metrics")
        assert response.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_audit_log_is_immutable_at_the_database_level(pool):
    admin_id, *_ = await create_test_admin(pool)
    row = await pool.fetchrow(
        "INSERT INTO admin_audit_log (admin_id, action, target_type, target_id) "
        "VALUES ($1, 'test.action', 'user', '1') RETURNING id",
        admin_id,
    )

    with pytest.raises(asyncpg.PostgresError):
        await pool.execute(
            "UPDATE admin_audit_log SET reason = 'tampered' WHERE id = $1", row["id"]
        )

    with pytest.raises(asyncpg.PostgresError):
        await pool.execute("DELETE FROM admin_audit_log WHERE id = $1", row["id"])
