"""Read-only Postgres access for the gateway.

Everything here reads from Postgres directly rather than asking a live
RoundEngine for its state. That's a deliberate choice: Postgres is already
the durable source of truth (round_engine.py writes every state transition
there before publishing anything), so `state_sync` can be served correctly
even if the room's engine just crashed and hasn't been replaced yet -- the
alternative (routing every read through the engine's command channel) would
make reconnection depend on a live engine for no real benefit.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core.phone_crypto import decrypt_phone


async def get_or_create_user_by_telegram_id(
    pool: asyncpg.Pool, telegram_id: int, display_name: str
) -> int:
    """Bridges Telegram identity to our internal user id.

    In the target architecture the bot (Phase 1, not yet built) creates the
    user row during registration, before anyone ever opens the Mini App --
    this lazy get-or-create exists so the gateway is usable standalone until
    that phase lands, and becomes a pure no-op read once it does.
    """
    row = await pool.fetchrow(
        "SELECT id FROM users WHERE telegram_id = $1", telegram_id
    )
    if row is not None:
        return int(row["id"])

    row = await pool.fetchrow(
        """
        INSERT INTO users (telegram_id, display_name)
        VALUES ($1, $2)
        ON CONFLICT (telegram_id) DO NOTHING
        RETURNING id
        """,
        telegram_id,
        display_name,
    )
    if row is None:
        row = await pool.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1", telegram_id
        )
    assert row is not None
    return int(row["id"])


async def list_active_manual_payment_destinations(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Player-facing: only what a player choosing Manual Deposit needs to
    see (which account to pay into, and the instructions) -- no admin
    bookkeeping columns (created_by_admin_id, timestamps), and only rows
    an admin has actually left active.
    """
    rows = await pool.fetch(
        "SELECT id, method_kind, account_ref, account_name, instructions "
        "FROM manual_payment_destinations WHERE is_active ORDER BY method_kind, id"
    )
    return [dict(r) for r in rows]


async def user_phone(pool: asyncpg.Pool, user_id: int) -> str | None:
    blob = await pool.fetchval("SELECT phone_e164_encrypted FROM users WHERE id = $1", user_id)
    return decrypt_phone(bytes(blob)) if blob is not None else None


async def get_user_language(pool: asyncpg.Pool, user_id: int) -> str:
    value = await pool.fetchval("SELECT language FROM users WHERE id = $1", user_id)
    return str(value) if value is not None else "am"


async def get_auto_mark_preference(pool: asyncpg.Pool, user_id: int) -> bool:
    value = await pool.fetchval(
        "SELECT auto_mark_preference FROM users WHERE id = $1", user_id
    )
    return bool(value) if value is not None else True


async def set_auto_mark_preference(pool: asyncpg.Pool, user_id: int, auto: bool) -> None:
    await pool.execute(
        "UPDATE users SET auto_mark_preference = $1 WHERE id = $2", auto, user_id
    )


async def user_history(pool: asyncpg.Pool, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT rd.id, rd.seq, rd.stake, rd.ended_at,
               (rw.round_id IS NOT NULL) AS won,
               rw.amount AS won_amount
        FROM round_entries re
        JOIN rounds rd ON rd.id = re.round_id
        LEFT JOIN round_winners rw ON rw.round_id = re.round_id AND rw.user_id = re.user_id
        WHERE re.user_id = $1 AND rd.status IN ('done', 'voided')
        ORDER BY rd.ended_at DESC NULLS LAST
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [
        {
            "round_id": row["id"],
            "seq": row["seq"],
            "stake": str(row["stake"]),
            "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
            "won": row["won"],
            "won_amount": str(row["won_amount"]) if row["won_amount"] is not None else None,
        }
        for row in rows
    ]


async def list_rooms(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT
            r.id, r.code, r.stake, r.max_players,
            latest.status, latest.player_count, latest.pot,
            latest.lobby_deadline
        FROM rooms r
        LEFT JOIN LATERAL (
            SELECT status, player_count, pot, lobby_deadline
            FROM rounds
            WHERE room_id = r.id
            ORDER BY seq DESC
            LIMIT 1
        ) latest ON true
        WHERE r.is_active = true
        ORDER BY r.stake
        """
    )
    out = []
    for row in rows:
        out.append(
            {
                "room_id": row["id"],
                "code": row["code"],
                "stake": str(row["stake"]),
                "max_players": row["max_players"],
                "status": row["status"] or "idle",
                "players": row["player_count"] or 0,
                "pot": str(row["pot"]) if row["pot"] is not None else "0.00",
                # Mini App spec 2.1: the room list's own countdown ("0:18")
                # for a room still in its lobby -- the SQL above already
                # selected this column, but it was silently dropped here
                # before ever reaching a client, leaving the Mini App with
                # no real deadline to count down to (an architecture audit
                # found this).
                "lobby_deadline_ms": (
                    int(row["lobby_deadline"].timestamp() * 1000)
                    if row["lobby_deadline"] is not None
                    else None
                ),
            }
        )
    return out


async def build_state_sync(pool: asyncpg.Pool, room_id: int, user_id: int) -> dict[str, Any]:
    """The one-message reconnect payload (spec section 6.5): everything the
    Mini App needs to redraw exactly where the player left off, sourced
    entirely from Postgres so it works even if the room's engine just
    crashed and hasn't been replaced yet.
    """
    # A code review pass caught these two queries running sequentially --
    # neither depends on the other's result (both filter on the same
    # room_id, nothing more), so this reconnect payload's latency was
    # paying for two round trips end to end where one round trip's worth
    # (the slower of the two) would do. Genuinely independent reads,
    # unlike the get_or_create_account chains elsewhere in this codebase
    # that lock rows in a specific order for a reason -- there's no
    # ordering constraint to preserve here.
    room_row, round_row = await asyncio.gather(
        pool.fetchrow("SELECT stake, win_patterns, max_players FROM rooms WHERE id = $1", room_id),
        pool.fetchrow(
            """
            SELECT id, status, call_index, draw_order, pot, derash, house_cut_bps,
                   stake, player_count, lobby_deadline
            FROM rounds
            WHERE room_id = $1
            ORDER BY seq DESC
            LIMIT 1
            """,
            room_id,
        ),
    )
    if room_row is None:
        raise ValueError(f"no such room: {room_id}")
    win_patterns = room_row["win_patterns"]
    if isinstance(win_patterns, str):
        win_patterns = json.loads(win_patterns)

    called: list[int] = []
    your_card: int | None = None
    your_card_grid: list[list[int]] | None = None
    auto_mark: bool | None = None
    status = "idle"
    round_id = None
    call_index = 0
    pot = Decimal("0")
    derash = Decimal("0")
    players = 0
    stake = room_row["stake"]
    lobby_deadline_ms: int | None = None

    if round_row is not None:
        status = round_row["status"]
        round_id = round_row["id"]
        call_index = round_row["call_index"]
        pot = round_row["pot"]
        derash = round_row["derash"] or Decimal("0")
        players = round_row["player_count"] or 0
        stake = round_row["stake"]
        draw_order = round_row["draw_order"] or []
        called = list(draw_order[:call_index])
        if round_row["lobby_deadline"] is not None:
            lobby_deadline_ms = int(round_row["lobby_deadline"].timestamp() * 1000)

        entry = await pool.fetchrow(
            "SELECT card_no, auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
            round_id,
            user_id,
        )
        if entry is not None:
            your_card = entry["card_no"]
            auto_mark = entry["auto_mark"]
            card_row = await pool.fetchrow(
                "SELECT grid FROM cards WHERE card_no = $1", your_card
            )
            if card_row is not None:
                grid = card_row["grid"]
                your_card_grid = json.loads(grid) if isinstance(grid, str) else grid

    return {
        "t": "state_sync",
        "room_id": room_id,
        "round_id": round_id,
        "status": status,
        "call_index": call_index,
        "called": called,
        "pot": str(pot),
        "derash": str(derash),
        "players": players,
        "stake": str(stake),
        "win_patterns": win_patterns,
        "your_card": your_card,
        "your_card_grid": your_card_grid,
        "auto_mark": auto_mark,
        "lobby_deadline_ms": lobby_deadline_ms,
        "server_time": int(time.time() * 1000),
    }
