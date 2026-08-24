"""The double-entry ledger. This is the only way money moves in Jo Bingo.

There is no `UPDATE users SET balance = balance + x` anywhere in this
codebase -- every movement is one or more signed ledger_entries rows summing
to exactly zero, written inside a single database transaction alongside the
account_balances cache update. Postgres itself rejects (via a deferred
constraint trigger, see migrations/versions/81d041ff4513_ledger_foundation.py)
any transaction whose entries do not sum to zero, so this invariant survives
even a bug in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import asyncpg.pool

from packages.core import metrics

# Every function here is called both with a bare connection (tests, one-off
# scripts) and with a connection checked out of a pool via `async with
# pool.acquire() as conn`, which asyncpg wraps in a proxy that isn't a
# subclass of Connection. Accept both rather than forcing every caller to
# know which one they happen to have.
AsyncpgConnection = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy

USER_BALANCE_KINDS = frozenset({"user_cash", "user_bonus", "user_locked"})


class InsufficientFunds(Exception):
    def __init__(self, account_id: int, attempted_balance: Decimal) -> None:
        self.account_id = account_id
        self.attempted_balance = attempted_balance
        super().__init__(
            f"account {account_id} would go to {attempted_balance}, which is "
            "not allowed for a user balance account"
        )


@dataclass(frozen=True)
class Account:
    id: int
    user_id: int | None
    kind: str
    currency: str


@dataclass(frozen=True)
class Entry:
    account_id: int
    amount: Decimal


@dataclass
class _LockedBalance:
    kind: str
    balance: Decimal


@dataclass(frozen=True)
class LedgerTransaction:
    id: int
    kind: str
    idempotency_key: str
    round_id: int | None
    payment_id: int | None
    created_by: str
    memo: str | None


async def get_or_create_account(
    conn: AsyncpgConnection,
    user_id: int | None,
    kind: str,
    currency: str = "ETB",
) -> Account:
    if user_id is None:
        row = await conn.fetchrow(
            """
            INSERT INTO accounts (user_id, kind, currency)
            VALUES (NULL, $1, $2)
            ON CONFLICT (kind, currency) WHERE user_id IS NULL DO NOTHING
            RETURNING id, user_id, kind, currency
            """,
            kind,
            currency,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT id, user_id, kind, currency FROM accounts "
                "WHERE user_id IS NULL AND kind = $1 AND currency = $2",
                kind,
                currency,
            )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO accounts (user_id, kind, currency)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, kind, currency) DO NOTHING
            RETURNING id, user_id, kind, currency
            """,
            user_id,
            kind,
            currency,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT id, user_id, kind, currency FROM accounts "
                "WHERE user_id = $1 AND kind = $2 AND currency = $3",
                user_id,
                kind,
                currency,
            )
    assert row is not None
    account = Account(row["id"], row["user_id"], row["kind"], row["currency"])

    # account_balances row is created lazily, once, the first time this
    # account is touched -- not here, so an account with no history never
    # shows up in balance queries with a phantom zero row for no reason.
    # (post() creates it via the same ON CONFLICT DO NOTHING pattern.)
    return account


async def post(
    conn: AsyncpgConnection,
    kind: str,
    entries: list[Entry],
    idempotency_key: str,
    *,
    round_id: int | None = None,
    payment_id: int | None = None,
    memo: str | None = None,
    created_by: str = "system",
) -> LedgerTransaction:
    if not entries:
        raise ValueError("post() requires at least one entry")

    async with conn.transaction():
        txn_row = await conn.fetchrow(
            """
            INSERT INTO ledger_transactions
                (kind, idempotency_key, round_id, payment_id, created_by, memo)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, kind, idempotency_key, round_id, payment_id, created_by, memo
            """,
            kind,
            idempotency_key,
            round_id,
            payment_id,
            created_by,
            memo,
        )

        if txn_row is None:
            # Someone else already owns this idempotency_key. If they're
            # still mid-transaction, the unique index blocks this INSERT
            # above until they commit or roll back, so by the time we reach
            # this SELECT their row (and its entries) are already visible.
            existing = await conn.fetchrow(
                """
                SELECT id, kind, idempotency_key, round_id, payment_id, created_by, memo
                FROM ledger_transactions WHERE idempotency_key = $1
                """,
                idempotency_key,
            )
            assert existing is not None
            return LedgerTransaction(**dict(existing))

        transaction_id = txn_row["id"]

        # Lock every touched account's balance row, in a fixed order, so two
        # transactions that both touch accounts A and B can never deadlock
        # by locking them in opposite order.
        account_ids = sorted({e.account_id for e in entries})
        locked: dict[int, _LockedBalance] = {}
        for account_id in account_ids:
            row = await conn.fetchrow(
                "SELECT account_id, kind, balance FROM account_balances "
                "WHERE account_id = $1 FOR UPDATE",
                account_id,
            )
            if row is None:
                # Brand-new account, no balance row yet. Create it at zero;
                # if a concurrent post() for a different transaction is
                # racing us to create the same row, ON CONFLICT DO NOTHING
                # lets exactly one INSERT win and the re-SELECT below then
                # blocks on that winner's row lock like any other contender.
                acct = await conn.fetchrow(
                    "SELECT kind FROM accounts WHERE id = $1", account_id
                )
                assert acct is not None
                await conn.execute(
                    """
                    INSERT INTO account_balances (account_id, kind, balance)
                    VALUES ($1, $2, 0)
                    ON CONFLICT (account_id) DO NOTHING
                    """,
                    account_id,
                    acct["kind"],
                )
                row = await conn.fetchrow(
                    "SELECT account_id, kind, balance FROM account_balances "
                    "WHERE account_id = $1 FOR UPDATE",
                    account_id,
                )
                assert row is not None
            locked[account_id] = _LockedBalance(kind=row["kind"], balance=row["balance"])

        deltas: dict[int, Decimal] = {}
        for entry in entries:
            deltas[entry.account_id] = deltas.get(
                entry.account_id, Decimal("0")
            ) + entry.amount

        for account_id, delta in deltas.items():
            current = locked[account_id]
            new_balance = current.balance + delta
            if current.kind in USER_BALANCE_KINDS and new_balance < 0:
                raise InsufficientFunds(account_id, new_balance)

        for entry in entries:
            await conn.execute(
                """
                INSERT INTO ledger_entries (transaction_id, account_id, amount)
                VALUES ($1, $2, $3)
                """,
                transaction_id,
                entry.account_id,
                entry.amount,
            )

        last_entry_id = await conn.fetchval(
            "SELECT max(id) FROM ledger_entries WHERE transaction_id = $1",
            transaction_id,
        )
        for account_id, delta in deltas.items():
            await conn.execute(
                """
                UPDATE account_balances
                SET balance = balance + $2, last_entry_id = $3
                WHERE account_id = $1
                """,
                account_id,
                delta,
                last_entry_id,
            )

        metrics.ledger_transactions_total.labels(kind=txn_row["kind"]).inc()
        return LedgerTransaction(
            id=transaction_id,
            kind=txn_row["kind"],
            idempotency_key=txn_row["idempotency_key"],
            round_id=txn_row["round_id"],
            payment_id=txn_row["payment_id"],
            created_by=txn_row["created_by"],
            memo=txn_row["memo"],
        )


_CENT = Decimal("0.01")


async def balance(conn: AsyncpgConnection, account_id: int) -> Decimal:
    value = await conn.fetchval(
        "SELECT balance FROM account_balances WHERE account_id = $1", account_id
    )
    result = value if value is not None else Decimal("0")
    return result.quantize(_CENT)


async def available(conn: AsyncpgConnection, user_id: int) -> Decimal:
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(b.balance) FILTER (WHERE a.kind IN ('user_cash', 'user_bonus')), 0)
                - COALESCE(SUM(b.balance) FILTER (WHERE a.kind = 'user_locked'), 0)
                AS available
        FROM accounts a
        JOIN account_balances b ON b.account_id = a.id
        WHERE a.user_id = $1
        """,
        user_id,
    )
    result = row["available"] if row and row["available"] is not None else Decimal("0")
    return result.quantize(_CENT)


async def reconcile(conn: AsyncpgConnection) -> list[tuple[int, Decimal, Decimal]]:
    """Recomputes every account's balance from ledger_entries and compares it
    to the account_balances cache. Returns a list of (account_id, cached,
    computed) for every mismatch found -- empty means the ledger is
    reconciled. Intended to run as a nightly job; exposed here as a plain
    function so it's directly unit-testable.
    """
    rows = await conn.fetch(
        """
        SELECT
            b.account_id,
            b.balance AS cached,
            COALESCE(SUM(e.amount), 0) AS computed
        FROM account_balances b
        LEFT JOIN ledger_entries e ON e.account_id = b.account_id
        GROUP BY b.account_id, b.balance
        HAVING b.balance <> COALESCE(SUM(e.amount), 0)
        """
    )
    return [(r["account_id"], r["cached"], r["computed"]) for r in rows]
