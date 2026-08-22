"""Integration tests for services/payments/deposits.py against a real
Postgres + Redis, exercising the exact scenarios spec Prompt 7 requires:
a webhook delivered 100 times concurrently credits exactly once, an
invalid signature is rejected, a webhook arriving after a successful poll
is a no-op, and a mismatched amount does not credit. All against a
FakePaymentProvider standing in for Chapa's network -- the business logic
under test (idempotency, locking, ledger crediting) is entirely real; only
the network boundary to an actual payment rail is faked, the same way no
test in this suite makes a live call to Telegram's servers either.
"""

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from packages.core import ledger
from services.payments import deposits
from services.payments.provider import CheckoutResult, InvalidSignature, StatusResult, VerifiedEvent
from tests.integration.conftest import create_user

MIN_DEPOSIT = Decimal("10.00")
DAILY_CAP = Decimal("50000.00")


@dataclass
class FakePaymentProvider:
    name: str = "chapa"
    checkouts: dict[str, dict[str, object]] = field(default_factory=dict)
    statuses: dict[str, StatusResult] = field(default_factory=dict)

    async def create_checkout(self, *, amount, user_ref, our_ref, return_url):
        self.checkouts[our_ref] = {"amount": amount, "user_ref": user_ref, "return_url": return_url}
        return CheckoutResult(
            checkout_url=f"https://pay.test/{our_ref}", provider_ref=our_ref, raw_response={"our_ref": our_ref}
        )

    def verify_webhook(self, headers, raw_body):
        if headers.get("x-signature") != "valid":
            raise InvalidSignature("bad test signature")
        data = json.loads(raw_body)
        return VerifiedEvent(
            event_id=data["event_id"],
            our_ref=data["our_ref"],
            status=data["status"],
            amount=Decimal(data["amount"]),
            provider_ref=data.get("provider_ref", data["event_id"]),
            raw=data,
        )

    async def fetch_status(self, our_ref):
        return self.statuses.get(our_ref, StatusResult(status="pending", amount=None, provider_ref=None, raw={}))

    async def create_payout(self, *, method, amount, our_ref):
        raise NotImplementedError


def _webhook(*, event_id: str, our_ref: str, status: str, amount: str) -> tuple[dict[str, str], bytes]:
    body = json.dumps(
        {"event_id": event_id, "our_ref": our_ref, "status": status, "amount": amount}
    ).encode()
    return {"x-signature": "valid"}, body


async def _cash_balance(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def test_deposit_below_minimum_is_rejected(pool, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    with pytest.raises(deposits.BelowMinimumDeposit):
        await deposits.create_deposit_intent(
            pool,
            provider,
            user_id=user_id,
            amount=Decimal("5.00"),
            phone_e164="+251911000000",
            return_url="https://app.test/return",
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_self_excluded_user_cannot_deposit(pool, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    await conn.execute("UPDATE users SET status = 'self_excluded' WHERE id = $1", user_id)
    with pytest.raises(deposits.DepositorSelfExcluded):
        await deposits.create_deposit_intent(
            pool,
            provider,
            user_id=user_id,
            amount=Decimal("100.00"),
            phone_e164="+251911000000",
            return_url="https://app.test/return",
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_daily_cap_exceeded_on_second_deposit(pool, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    small_cap = Decimal("150.00")
    await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("100.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=small_cap,
    )
    with pytest.raises(deposits.DailyDepositCapExceeded):
        await deposits.create_deposit_intent(
            pool,
            provider,
            user_id=user_id,
            amount=Decimal("100.00"),
            phone_e164="+251911000000",
            return_url="https://app.test/return",
            min_deposit=MIN_DEPOSIT,
            daily_cap=small_cap,
        )


async def test_checkout_creation_marks_payment_processing(pool, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    intent = await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("200.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    assert intent.checkout_url == f"https://pay.test/{intent.our_ref}"
    row = await conn.fetchrow("SELECT status, amount FROM payments WHERE id = $1", intent.payment_id)
    assert row["status"] == "processing"
    assert row["amount"] == Decimal("200.00")


async def test_valid_webhook_credits_the_ledger_exactly_once(pool, redis, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    intent = await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("200.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    headers, body = _webhook(
        event_id=f"evt-{intent.our_ref}", our_ref=intent.our_ref, status="succeeded", amount="200.00"
    )
    outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
    assert outcome == "credited"
    assert await _cash_balance(conn, user_id) == Decimal("200.00")

    status_row = await conn.fetchrow("SELECT status, ledger_txn_id FROM payments WHERE id = $1", intent.payment_id)
    assert status_row["status"] == "succeeded"
    assert status_row["ledger_txn_id"] is not None


async def test_same_webhook_delivered_100_times_concurrently_credits_exactly_once(pool, redis, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    intent = await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("50.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    headers, body = _webhook(
        event_id=f"evt-dup-{intent.our_ref}", our_ref=intent.our_ref, status="succeeded", amount="50.00"
    )

    outcomes = await asyncio.gather(
        *(
            deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
            for _ in range(100)
        )
    )
    assert outcomes.count("credited") == 1
    assert outcomes.count("duplicate") == 99
    assert await _cash_balance(conn, user_id) == Decimal("50.00")

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 1


async def test_invalid_signature_is_rejected_and_does_not_credit(pool, redis, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    intent = await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("200.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    bad_headers = {"x-signature": "not-valid"}
    _, body = _webhook(event_id=f"evt-{intent.our_ref}", our_ref=intent.our_ref, status="succeeded", amount="200.00")

    with pytest.raises(InvalidSignature):
        await deposits.handle_webhook(pool, redis, provider, headers=bad_headers, raw_body=body)

    assert await _cash_balance(conn, user_id) == Decimal("0.00")
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "processing"


async def test_mismatched_amount_does_not_credit_and_flags_for_review(pool, redis, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    intent = await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("200.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    headers, body = _webhook(
        event_id=f"evt-{intent.our_ref}", our_ref=intent.our_ref, status="succeeded", amount="999.00"
    )
    outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
    assert outcome == "amount_mismatch"

    assert await _cash_balance(conn, user_id) == Decimal("0.00")
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "review"

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 0


async def test_webhook_arriving_after_a_successful_poll_is_a_noop(pool, redis, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    intent = await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("75.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    provider.statuses[intent.our_ref] = StatusResult(
        status="succeeded", amount=Decimal("75.00"), provider_ref=intent.our_ref, raw={}
    )
    credited = await deposits.poll_pending_deposits(pool, redis, provider, older_than_seconds=0)
    assert credited == 1
    assert await _cash_balance(conn, user_id) == Decimal("75.00")

    headers, body = _webhook(
        event_id=f"evt-late-{intent.our_ref}", our_ref=intent.our_ref, status="succeeded", amount="75.00"
    )
    outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
    assert outcome == "duplicate"
    assert await _cash_balance(conn, user_id) == Decimal("75.00")


async def test_unknown_our_ref_returns_not_found(pool, redis):
    provider = FakePaymentProvider()
    headers, body = _webhook(
        event_id="evt-ghost", our_ref="DEP-does-not-exist", status="succeeded", amount="1.00"
    )
    outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
    assert outcome == "not_found"


async def test_failed_status_marks_payment_failed_without_crediting(pool, redis, conn):
    provider = FakePaymentProvider()
    user_id = await create_user(conn)
    intent = await deposits.create_deposit_intent(
        pool,
        provider,
        user_id=user_id,
        amount=Decimal("30.00"),
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    headers, body = _webhook(event_id=f"evt-fail-{intent.our_ref}", our_ref=intent.our_ref, status="failed", amount="30.00")
    outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
    assert outcome == "not_succeeded"
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "failed"
    assert await _cash_balance(conn, user_id) == Decimal("0.00")
