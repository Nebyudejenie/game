"""Integration tests for services/payments/telebirr_redemption.py -- the
money-critical core. Real Postgres and Redis throughout; the concurrent-
redemption test is the one CTO directive sections 132/133 explicitly
require before this phase can be called done.
"""

import asyncio
import itertools
import random
from decimal import Decimal

from packages.core import ledger
from services.payments.telebirr_ingest import ingest_sms_evidence
from services.payments.telebirr_redemption import redeem_evidence
from tests.integration.conftest import create_funded_user

_ref_counter = itertools.count(random.randint(3 * 10**7, 4 * 10**7))


def _next_reference() -> str:
    return f"DI{next(_ref_counter):08d}"


def _build_sms(reference: str, *, amount: str = "10.00", recipient: str) -> str:
    return (
        f"Dear {recipient} \n"
        f"You have received ETB {amount} from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )


async def _make_available_evidence(pool, conn, *, amount: str = "10.00") -> str:
    reference = _next_reference()
    recipient = f"Recipient{reference}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    outcome = await ingest_sms_evidence(
        pool, raw_sms=_build_sms(reference, amount=amount, recipient=recipient),
        source="macrodroid", source_ref="test-device",
    )
    assert outcome.status == "ingested_available"
    return reference


async def test_redeeming_an_available_reference_credits_the_wallet(pool, redis, conn):
    reference = await _make_available_evidence(pool, conn, amount="10.00")
    user_id = await create_funded_user(conn, Decimal("0.00"))

    outcome = await redeem_evidence(
        pool, redis, user_id=user_id, reference=reference, daily_cap=Decimal("50000.00")
    )
    assert outcome.code == "PAYMENT_REDEEMED"
    assert outcome.amount == Decimal("10.00")

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("10.00")

    evidence_row = await conn.fetchrow(
        "SELECT status, redeemed_by_user_id, payment_id FROM payment_evidence WHERE external_reference = $1",
        reference,
    )
    assert evidence_row["status"] == "redeemed"
    assert evidence_row["redeemed_by_user_id"] == user_id

    payment_row = await conn.fetchrow(
        "SELECT provider, status, amount FROM payments WHERE id = $1", evidence_row["payment_id"]
    )
    assert payment_row["provider"] == "telebirr_sms"
    assert payment_row["status"] == "succeeded"
    assert payment_row["amount"] == Decimal("10.00")


async def test_reference_is_matched_case_and_whitespace_insensitively(pool, redis, conn):
    reference = await _make_available_evidence(pool, conn)
    user_id = await create_funded_user(conn, Decimal("0.00"))

    outcome = await redeem_evidence(
        pool, redis, user_id=user_id, reference=f"  {reference.lower()}  ", daily_cap=Decimal("50000.00")
    )
    assert outcome.code == "PAYMENT_REDEEMED"


async def test_unknown_reference_is_not_found(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("0.00"))
    outcome = await redeem_evidence(
        pool, redis, user_id=user_id, reference="DOESNOTEXIST99", daily_cap=Decimal("50000.00")
    )
    assert outcome.code == "PAYMENT_NOT_FOUND"


async def test_empty_reference_is_invalid(pool, redis):
    outcome = await redeem_evidence(
        pool, redis, user_id=1, reference="   ", daily_cap=Decimal("50000.00")
    )
    assert outcome.code == "INVALID_REFERENCE"


async def test_second_different_user_cannot_redeem_an_already_redeemed_reference(pool, redis, conn):
    reference = await _make_available_evidence(pool, conn)
    first_user = await create_funded_user(conn, Decimal("0.00"))
    second_user = await create_funded_user(conn, Decimal("0.00"))

    first = await redeem_evidence(
        pool, redis, user_id=first_user, reference=reference, daily_cap=Decimal("50000.00")
    )
    assert first.code == "PAYMENT_REDEEMED"

    second = await redeem_evidence(
        pool, redis, user_id=second_user, reference=reference, daily_cap=Decimal("50000.00")
    )
    assert second.code == "PAYMENT_ALREADY_REDEEMED"

    # Ownership never silently transferred -- the second user's cash
    # account was never touched.
    second_cash = await ledger.get_or_create_account(conn, second_user, "user_cash")
    assert await ledger.balance(conn, second_cash.id) == Decimal("0.00")


async def test_same_user_retrying_after_a_timeout_gets_the_same_success_not_a_double_credit(pool, redis, conn):
    # Section 104: the server already committed the redemption, the
    # player's connection just never saw the response -- a retry must
    # return the same successful outcome, never a second credit.
    reference = await _make_available_evidence(pool, conn, amount="25.00")
    user_id = await create_funded_user(conn, Decimal("0.00"))

    first = await redeem_evidence(
        pool, redis, user_id=user_id, reference=reference, daily_cap=Decimal("50000.00")
    )
    second = await redeem_evidence(
        pool, redis, user_id=user_id, reference=reference, daily_cap=Decimal("50000.00")
    )
    assert first.code == "PAYMENT_REDEEMED"
    assert second.code == "PAYMENT_REDEEMED"
    assert second.our_ref == first.our_ref
    assert second.payment_id == first.payment_id

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("25.00")  # not 50.00

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", first.our_ref
    )
    assert txn_count == 1


async def test_blocked_evidence_cannot_be_redeemed(pool, redis, conn):
    reference = await _make_available_evidence(pool, conn)
    await conn.execute("UPDATE payment_evidence SET status = 'blocked' WHERE external_reference = $1", reference)
    user_id = await create_funded_user(conn, Decimal("0.00"))

    outcome = await redeem_evidence(
        pool, redis, user_id=user_id, reference=reference, daily_cap=Decimal("50000.00")
    )
    assert outcome.code == "PAYMENT_BLOCKED"


async def test_daily_cap_blocks_redemption_without_crediting(pool, redis, conn):
    reference = await _make_available_evidence(pool, conn, amount="100.00")
    user_id = await create_funded_user(conn, Decimal("0.00"))

    outcome = await redeem_evidence(
        pool, redis, user_id=user_id, reference=reference, daily_cap=Decimal("50.00")
    )
    assert outcome.code == "DAILY_CAP_EXCEEDED"

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("0.00")

    row = await conn.fetchrow("SELECT status FROM payment_evidence WHERE external_reference = $1", reference)
    assert row["status"] == "available"  # untouched -- eligible to redeem again once under cap


async def test_concurrent_redemption_of_the_same_reference_settles_exactly_once(pool, redis, conn):
    """The mandatory concurrency test (CTO directive sections 132/133): two
    different users racing to redeem the exact same reference at the same
    instant. Real asyncpg connections, real Postgres row locking (FOR
    UPDATE inside redeem_evidence's own transaction) -- exactly one must
    win, the loser must be cleanly rejected, and the ledger must
    reconcile cleanly afterward. Mirrors tests/integration/
    test_round_engine.py's own test_same_user_double_claim_race_settles_
    exactly_once in structure (asyncio.gather of two real concurrent
    calls against the same pool).
    """
    reference = await _make_available_evidence(pool, conn, amount="40.00")
    user_a = await create_funded_user(conn, Decimal("0.00"))
    user_b = await create_funded_user(conn, Decimal("0.00"))

    results = await asyncio.gather(
        redeem_evidence(pool, redis, user_id=user_a, reference=reference, daily_cap=Decimal("50000.00")),
        redeem_evidence(pool, redis, user_id=user_b, reference=reference, daily_cap=Decimal("50000.00")),
    )

    redeemed = [r for r in results if r.code == "PAYMENT_REDEEMED"]
    rejected = [r for r in results if r.code == "PAYMENT_ALREADY_REDEEMED"]
    assert len(redeemed) == 1
    assert len(rejected) == 1

    winner_user_id = user_a if results[0].code == "PAYMENT_REDEEMED" else user_b
    loser_user_id = user_b if winner_user_id == user_a else user_a

    winner_cash = await ledger.get_or_create_account(conn, winner_user_id, "user_cash")
    loser_cash = await ledger.get_or_create_account(conn, loser_user_id, "user_cash")
    assert await ledger.balance(conn, winner_cash.id) == Decimal("40.00")
    assert await ledger.balance(conn, loser_cash.id) == Decimal("0.00")

    evidence_row = await conn.fetchrow(
        "SELECT status, redeemed_by_user_id FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert evidence_row["status"] == "redeemed"
    assert evidence_row["redeemed_by_user_id"] == winner_user_id

    payment_count = await conn.fetchval(
        "SELECT count(*) FROM payments WHERE provider = 'telebirr_sms' AND user_id = ANY($1)",
        [user_a, user_b],
    )
    assert payment_count == 1

    mismatches = await ledger.reconcile(conn)
    assert mismatches == []
