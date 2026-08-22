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

import time
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core import ledger


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


async def user_balance_snapshot(pool: asyncpg.Pool, user_id: int) -> dict[str, str]:
    async with pool.acquire() as conn:
        cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
        bonus = await ledger.get_or_create_account(conn, user_id, "user_bonus")
        locked = await ledger.get_or_create_account(conn, user_id, "user_locked")
        return {
            "cash": str(await ledger.balance(conn, cash.id)),
            "bonus": str(await ledger.balance(conn, bonus.id)),
            "locked": str(await ledger.balance(conn, locked.id)),
        }


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
            }
        )
    return out


async def build_state_sync(pool: asyncpg.Pool, room_id: int, user_id: int) -> dict[str, Any]:
    round_row = await pool.fetchrow(
        """
        SELECT id, status, call_index, draw_order, pot, derash, house_cut_bps,
               stake, server_seed_hash
        FROM rounds
        WHERE room_id = $1
        ORDER BY seq DESC
        LIMIT 1
        """,
        room_id,
    )

    called: list[int] = []
    your_card: int | None = None
    auto_mark: bool | None = None
    status = "idle"
    round_id = None
    call_index = 0
    pot = Decimal("0")

    if round_row is not None:
        status = round_row["status"]
        round_id = round_row["id"]
        call_index = round_row["call_index"]
        pot = round_row["pot"]
        draw_order = round_row["draw_order"] or []
        called = list(draw_order[:call_index])

        entry = await pool.fetchrow(
            "SELECT card_no, auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
            round_id,
            user_id,
        )
        if entry is not None:
            your_card = entry["card_no"]
            auto_mark = entry["auto_mark"]

    return {
        "t": "state_sync",
        "room_id": room_id,
        "round_id": round_id,
        "status": status,
        "call_index": call_index,
        "called": called,
        "pot": str(pot),
        "your_card": your_card,
        "auto_mark": auto_mark,
        "server_time": int(time.time() * 1000),
    }
