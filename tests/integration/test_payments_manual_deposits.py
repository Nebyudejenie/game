"""Manual deposit request creation (services/payments/manual.py) -- the
player-facing half of the P1 "keep taking deposits when Chapa is down"
directive. Admin-side approve/reject is covered in
test_admin_manual_payments.py; this file is the domain layer:
request creation, the shared eligibility gates, and the receipt-photo
correlation helper.
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core import ledger
from services.admin import queries as admin_queries
from services.payments import manual
from tests.integration.conftest import create_funded_user, create_user
from tests.integration.test_admin_auth import create_test_admin

MIN_DEPOSIT = Decimal("10.00")
DAILY_CAP = Decimal("50000.00")


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _active_destination(conn) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, instructions)
        VALUES ('telebirr', '0911000000', 'Jo Bingo PLC', 'Send exactly the requested amount')
        RETURNING id
        """
    )
    return row["id"]


async def test_manual_deposit_request_starts_pending_review_no_credit(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)

    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("200.00"),
        manual_destination_id=destination_id,
        external_reference="FT26123ABC",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "review"
    assert await _cash(conn, user_id) == Decimal("0.00")  # not credited merely for submitting


async def test_manual_deposit_below_minimum_is_rejected(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)

    with pytest.raises(Exception) as exc_info:
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("1.00"),
            manual_destination_id=destination_id,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )
    from services.payments.deposits import BelowMinimumDeposit

    assert isinstance(exc_info.value, BelowMinimumDeposit)


async def test_manual_deposit_rejects_an_unknown_destination(pool, redis, conn):
    user_id = await create_user(conn)

    with pytest.raises(manual.UnknownManualDestination):
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("100.00"),
            manual_destination_id=999999,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_manual_deposit_rejects_a_deactivated_destination(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    await conn.execute("UPDATE manual_payment_destinations SET is_active = false WHERE id = $1", destination_id)

    with pytest.raises(manual.UnknownManualDestination):
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("100.00"),
            manual_destination_id=destination_id,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_manual_deposit_self_excluded_user_is_rejected(pool, redis, conn):
    # Proves the shared _check_deposit_eligibility gate really is shared,
    # not a second copy -- the exact same responsible-gaming rule an
    # automatic deposit already enforces.
    user_id = await create_user(conn)
    await conn.execute("UPDATE users SET status = 'self_excluded' WHERE id = $1", user_id)
    destination_id = await _active_destination(conn)

    from services.payments.deposits import DepositorSelfExcluded

    with pytest.raises(DepositorSelfExcluded):
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("100.00"),
            manual_destination_id=destination_id,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_admin_approve_credits_exactly_once(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("300.00"),
        manual_destination_id=destination_id,
        external_reference="FT26555",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    approved = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="verified externally",
        ip_address="10.0.0.1",
    )
    assert approved is True
    assert await _cash(conn, user_id) == Decimal("300.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "succeeded"

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 1


async def test_concurrent_double_approval_credits_exactly_once(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("120.00"),
        manual_destination_id=destination_id,
        external_reference="FT26777",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    async def approve():
        return await admin_queries.approve_manual_deposit_admin(
            pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="ok", ip_address="10.0.0.1"
        )

    results = await asyncio.gather(*(approve() for _ in range(20)))
    assert results.count(True) == 1
    assert results.count(False) == 19

    assert await _cash(conn, user_id) == Decimal("120.00")
    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 1


async def test_admin_reject_never_credits(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("80.00"),
        manual_destination_id=destination_id,
        external_reference="FT26888",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    rejected = await admin_queries.reject_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="reference not found",
        ip_address="10.0.0.1",
    )
    assert rejected is True
    assert await _cash(conn, user_id) == Decimal("0.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "rejected"

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 0


async def test_post_commit_retry_is_a_clean_noop_not_a_double_credit(pool, redis, conn):
    # The literal "admin's browser times out after the server already
    # committed" scenario: call approve once for real, then call it again
    # as if it were a client-side retry of a response the admin never saw.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("50.00"),
        manual_destination_id=destination_id,
        external_reference="FT26321",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    first = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="ok", ip_address="10.0.0.1"
    )
    assert first is True

    retry = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="ok", ip_address="10.0.0.1"
    )
    assert retry is False  # clean no-op, not an exception, not a second credit
    assert await _cash(conn, user_id) == Decimal("50.00")


async def test_attach_receipt_to_latest_pending_deposit(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("90.00"),
        manual_destination_id=destination_id,
        external_reference="FT26456",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    attached_to = await manual.attach_receipt_to_latest_pending_deposit(
        pool, user_id=user_id, telegram_file_id="AgACAgQ-fake-file-id"
    )
    assert attached_to == intent.payment_id

    file_id = await conn.fetchval(
        "SELECT receipt_telegram_file_id FROM payments WHERE id = $1", intent.payment_id
    )
    assert file_id == "AgACAgQ-fake-file-id"


async def test_attach_receipt_returns_none_when_nothing_pending(pool, redis, conn):
    user_id = await create_user(conn)  # never made any manual deposit request
    attached_to = await manual.attach_receipt_to_latest_pending_deposit(
        pool, user_id=user_id, telegram_file_id="AgACAgQ-fake-file-id"
    )
    assert attached_to is None
