"""Integration tests for services/payments/ledger_reconcile_sweep.py
against real Postgres: a clean ledger sweeps to zero mismatches, and a
deliberately introduced cache/ledger disagreement is detected (surfaced
on the scraped Prometheus gauge and logged loudly) without ever being
silently corrected -- this sweep only ever compares, it must never write
to account_balances itself.
"""

import uuid
from decimal import Decimal

from packages.core import ledger, metrics
from services.payments.ledger_reconcile_sweep import sweep_ledger_reconciliation
from tests.integration.conftest import create_user


def key() -> str:
    return f"test-reconcile-sweep-{uuid.uuid4()}"


async def _fund_cash(conn, user_id: int, amount: Decimal) -> None:
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    await ledger.post(
        conn, "deposit", [ledger.Entry(provider.id, -amount), ledger.Entry(cash.id, amount)],
        idempotency_key=key(),
    )


async def test_sweep_finds_zero_mismatches_against_a_consistent_ledger(pool, conn):
    user_id = await create_user(conn)
    await _fund_cash(conn, user_id, Decimal("25.00"))

    await sweep_ledger_reconciliation(pool)

    assert metrics.ledger_reconciliation_sweep_mismatch_count._value.get() == 0.0


async def test_sweep_detects_a_mismatch_without_correcting_it(pool, conn):
    user_id = await create_user(conn)
    await _fund_cash(conn, user_id, Decimal("25.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")

    # Deliberately desync the cache from the real ledger entries -- exactly
    # the class of bug this sweep exists to catch. ledger.post() itself
    # would never produce this; only a direct, out-of-band write (a real
    # bug elsewhere, simulated here) can.
    await conn.execute(
        "UPDATE account_balances SET balance = balance + 1 WHERE account_id = $1", cash.id
    )
    try:
        await sweep_ledger_reconciliation(pool)

        assert metrics.ledger_reconciliation_sweep_mismatch_count._value.get() >= 1.0

        # Never silently "fixed" -- the sweep is read-only by design.
        # ledger.balance() itself just reads this same cache, so compare
        # against the known-correct, pre-tamper value directly instead.
        still_tampered = await conn.fetchval(
            "SELECT balance FROM account_balances WHERE account_id = $1", cash.id
        )
        assert still_tampered == Decimal("26.00")
    finally:
        # This dev database is shared across the whole test session and
        # never truncated between runs -- leaving this row mismatched
        # would permanently poison every later reconciliation check
        # (this sweep's own, and any other test's) against the same DB.
        await conn.execute(
            "UPDATE account_balances SET balance = balance - 1 WHERE account_id = $1", cash.id
        )
