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

from packages.core import ledger
from packages.core.ledger import Entry

TERMINAL_STATUSES = frozenset({"done", "voided"})


async def refund_round(pool: asyncpg.Pool, round_id: int, *, reason: str) -> bool:
    """Refunds every entrant's stake for round_id and marks it voided.

    Returns False (a no-op) if the round is already in a terminal state --
    that's the idempotency guarantee, not an error.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            round_row = await conn.fetchrow(
                "SELECT stake, status FROM rounds WHERE id = $1 FOR UPDATE",
                round_id,
            )
            if round_row is None or round_row["status"] in TERMINAL_STATUSES:
                return False

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
    return True
