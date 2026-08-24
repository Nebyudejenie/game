"""Tests for the admin withdrawal review queue (services/admin/queries.py's
approve_withdrawal_admin/reject_withdrawal_admin and the matching routes in
services/admin/app.py): the queue itself, the ledger-backed reversal on
rejection, RBAC over real HTTP, and the audit trail.
"""

from decimal import Decimal

import httpx

from packages.core import ledger
from services.admin import queries
from services.payments import payout_worker, withdrawals
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_payout_worker import FakePayoutProvider


class _NullProvider:
    name = "chapa"

    async def create_checkout(self, **kwargs):
        raise NotImplementedError

    def verify_webhook(self, headers, raw_body):
        raise NotImplementedError

    async def fetch_status(self, our_ref):
        raise NotImplementedError

    async def create_payout(self, **kwargs):
        raise NotImplementedError


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _locked(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_locked")
    return await ledger.balance(conn, account.id)


async def _review_withdrawal(pool, redis, conn, user_id: int, amount: Decimal) -> tuple[int, str]:
    intent = await withdrawals.request_withdrawal(
        pool,
        redis,
        _NullProvider(),
        user_id=user_id,
        amount=amount,
        method_kind="telebirr",
        account_ref="0911223344",
        holder_name="Test Holder",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("0.00"),  # forces status='review' every time
        kyc_threshold=Decimal("1000000.00"),
        chargeback_window_minutes=0,
        min_account_age_hours=0,
    )
    assert intent.status == withdrawals.STATUS_REVIEW
    return intent.payment_id, intent.our_ref


async def test_list_pending_withdrawals_shows_review_items(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    rows = await queries.list_pending_withdrawals(pool)
    assert any(r["id"] == payment_id and r["our_ref"] == our_ref for r in rows)


async def test_approve_withdrawal_admin_enqueues_a_payout_job(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    approved = await queries.approve_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="looks fine", ip_address="10.0.0.1"
    )
    assert approved is True

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "approved"

    outcome = await payout_worker.process_next(pool, redis, FakePayoutProvider(), consumer_name="admin-approve-test")
    assert outcome == "succeeded"
    assert await _cash(conn, user_id) == Decimal("400.00")

    audit_row = await conn.fetchrow(
        "SELECT action, reason FROM admin_audit_log WHERE target_id = $1 ORDER BY id DESC LIMIT 1",
        str(payment_id),
    )
    assert audit_row["action"] == "withdrawals.approve"


async def test_approve_withdrawal_admin_is_a_noop_when_not_in_review(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    first = await queries.approve_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason=None, ip_address=None
    )
    assert first is True

    second = await queries.approve_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason=None, ip_address=None
    )
    assert second is False


async def test_reject_withdrawal_admin_returns_exact_amount_to_cash(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("150.00"))

    assert await _locked(conn, user_id) == Decimal("150.00")
    assert await _cash(conn, user_id) == Decimal("350.00")

    rejected = await queries.reject_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="suspected fraud", ip_address="10.0.0.1"
    )
    assert rejected is True

    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("500.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "rejected"

    audit_row = await conn.fetchrow(
        "SELECT action, reason FROM admin_audit_log WHERE target_id = $1 ORDER BY id DESC LIMIT 1",
        str(payment_id),
    )
    assert audit_row["action"] == "withdrawals.reject"
    assert audit_row["reason"] == "suspected fraud"


async def test_reject_withdrawal_admin_rejects_empty_reason_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/withdrawals/{payment_id}/reject", headers=headers, json={"reason": "   "}
        )
    assert response.status_code == 422


async def test_support_cannot_approve_withdrawals_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/withdrawals/{payment_id}/approve", headers=headers, json={"reason": "ok"}
        )
    assert response.status_code == 403


async def test_finance_can_approve_withdrawals_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/withdrawals/{payment_id}/approve", headers=headers, json={"reason": "ok"}
        )
    assert response.status_code == 200
    assert response.json()["approved"] is True


async def test_support_can_view_pending_withdrawals_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/withdrawals", headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)
