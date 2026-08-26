"""Full-refund path for a round that didn't produce a real payout.

One function, three callers: a lobby that never reached min_players, a
round that exhausted all 75 calls with no valid winner, and crash recovery
voiding a round an engine died in the middle of. All three are the same
operation -- give every entrant their stake back, no house cut -- and all
three must be safe to call more than once for the same round (idempotent),
since a lobby-underfill path and a concurrent crash-recovery sweep could in
principle race for the same round.
"""

from __future__ import annotations

import asyncpg

from packages.core import ledger, metrics
from packages.core.ledger import AsyncpgConnection, Entry

TERMINAL_STATUSES = frozenset({"done", "voided"})


async def refund_round(pool: asyncpg.Pool, round_id: int, *, reason: str) -> int:
    """Refunds every entrant's stake for round_id and marks it voided.

    Returns the number of entrants refunded (0, a no-op, if the round was
    already in a terminal state -- that's the idempotency guarantee, not
    an error). 0 is falsy, matching this function's old bool contract, so
    every existing `if await refund_round(...):` caller keeps working
    unchanged.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            refunded_count = await refund_round_in_transaction(conn, round_id, reason=reason)
    # Only reachable once the transaction above has actually committed --
    # safe to record here, unlike refund_round_in_transaction() itself
    # (see its own comment for why): this function always owns a real,
    # non-nested transaction (a freshly acquired connection), so unlike
    # that shared helper, there's no ambiguity about who's responsible.
    if refunded_count:
        metrics.ledger_transactions_total.labels(kind="refund").inc(refunded_count)
    return refunded_count


async def refund_round_in_transaction(
    conn: AsyncpgConnection, round_id: int, *, reason: str
) -> int:
    """The same operation as refund_round(), for a caller that already
    owns a transaction and needs the refund to commit or roll back
    atomically together with something else it writes in the same
    transaction -- the admin console's void action needs its audit-log
    entry to be genuinely inseparable from the refund itself (a real bug
    a code review pass caught: writing the audit entry in a *second*,
    independent transaction after refund_round() already committed meant
    a crash in between left real money refunded with no audit trail at
    all, for exactly the kind of action this codebase's own "no hidden
    god mode" discipline exists to prevent).

    Returns the number of entrants refunded (0 for a no-op). This
    function is always called from inside some caller's own transaction
    (refund_round()'s, or an external one like the admin void action's),
    so -- like ledger.post() itself -- it can't know whether that
    transaction will ultimately commit, and deliberately does not touch
    ledger_transactions_total itself; each of its two callers records it
    (by this returned count, since one round can refund many entrants,
    each its own ledger transaction) once their own transaction commits.
    """
    round_row = await conn.fetchrow(
        "SELECT stake, status FROM rounds WHERE id = $1 FOR UPDATE",
        round_id,
    )
    if round_row is None or round_row["status"] in TERMINAL_STATUSES:
        return 0

    stake = round_row["stake"]
    entrants = await conn.fetch(
        "SELECT user_id FROM round_entries WHERE round_id = $1", round_id
    )
    pot_account = await ledger.get_or_create_account(conn, None, "pot_escrow")

    for entrant in entrants:
        cash_account = await ledger.get_or_create_account(
            conn, entrant["user_id"], "user_cash"
        )
        await ledger.post(
            conn,
            "refund",
            [Entry(pot_account.id, -stake), Entry(cash_account.id, stake)],
            idempotency_key=f"refund-{round_id}-{entrant['user_id']}",
            round_id=round_id,
            memo=reason,
        )

    await conn.execute(
        "UPDATE rounds SET status = 'voided', ended_at = now() WHERE id = $1",
        round_id,
    )
    metrics.engine_rounds_voided_total.inc()
    return len(entrants)
