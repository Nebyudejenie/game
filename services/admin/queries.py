"""Admin console resource operations: users, rounds, rooms, dashboard,
reports. Every mutation here writes an audit_log entry in the same
transaction as the mutation itself (never as an afterthought) and manual
balance adjustments go through the ledger like any other money movement --
there is no code path anywhere that writes a balance directly, admin
console included (spec section 26/34: "no hidden god mode").
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import asyncpg
from redis.asyncio import Redis

from packages.core import bingo, ledger
from packages.core.notifications import notify_user
from packages.core.phone_crypto import decrypt_phone, phone_lookup_hash
from services.admin import audit
from services.bot.phone import normalize_ethiopian_phone
from services.engine.refunds import refund_round
from services.payments.withdrawals import enqueue_payout


def _with_decrypted_phone(row: dict[str, Any]) -> dict[str, Any]:
    blob = row.pop("phone_e164_encrypted")
    row["phone_e164"] = decrypt_phone(bytes(blob)) if blob is not None else None
    return row


async def search_users(pool: asyncpg.Pool, query: str, limit: int = 20) -> list[dict[str, Any]]:
    # Phone matching is exact only (spec section 9.2: numbers are
    # encrypted at rest) -- a random-nonce ciphertext can't support
    # substring search, and phone_lookup_hash is only ever computed from a
    # genuinely complete, correctly-formatted E.164 number, so a partial
    # digit string simply matches nothing rather than silently degrading
    # to name/telegram_id-only search. A real product tradeoff, confirmed
    # with the user rather than picked unilaterally -- see DECISIONS.md.
    normalized = normalize_ethiopian_phone(query)
    phone_hash = phone_lookup_hash(normalized) if normalized else None

    rows = await pool.fetch(
        """
        SELECT id, telegram_id, display_name, phone_e164_encrypted, status, kyc_level, created_at
        FROM users
        WHERE phone_lookup_hash = $1
           OR display_name ILIKE '%' || $2 || '%'
           OR telegram_id::text = $2
        ORDER BY created_at DESC
        LIMIT $3
        """,
        phone_hash,
        query,
        limit,
    )
    return [_with_decrypted_phone(dict(r)) for r in rows]


async def get_user_detail(pool: asyncpg.Pool, user_id: int) -> dict[str, Any] | None:
    user_row = await pool.fetchrow(
        "SELECT id, telegram_id, display_name, phone_e164_encrypted, status, kyc_level, language, "
        "created_at, last_seen_at FROM users WHERE id = $1",
        user_id,
    )
    if user_row is None:
        return None
    user_dict = _with_decrypted_phone(dict(user_row))

    async with pool.acquire() as conn:
        cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
        bonus = await ledger.get_or_create_account(conn, user_id, "user_bonus")
        locked = await ledger.get_or_create_account(conn, user_id, "user_locked")
        balances = {
            "cash": str(await ledger.balance(conn, cash.id)),
            "bonus": str(await ledger.balance(conn, bonus.id)),
            "locked": str(await ledger.balance(conn, locked.id)),
        }

    return {**user_dict, "balances": balances, "ltv": await player_ltv(pool, user_id)}


async def player_ltv(pool: asyncpg.Pool, user_id: int) -> dict[str, Any]:
    """Net cash this player has contributed to the platform over their
    lifetime -- total succeeded deposits minus total succeeded
    withdrawals, the standard "player value" metric a real-money operator
    tracks (spec section 11's Reports screen: "player LTV"). Computed
    directly from payments, not house_revenue, since house_revenue is one
    shared account not itemized per player -- this is the metric that
    actually is.
    """
    row = await pool.fetchrow(
        """
        SELECT
          COALESCE(SUM(amount) FILTER (WHERE direction = 'in'), 0) AS total_deposited,
          COALESCE(SUM(amount) FILTER (WHERE direction = 'out'), 0) AS total_withdrawn
        FROM payments
        WHERE user_id = $1 AND status = 'succeeded'
        """,
        user_id,
    )
    assert row is not None
    total_deposited: Decimal = row["total_deposited"]
    total_withdrawn: Decimal = row["total_withdrawn"]
    return {
        "total_deposited": str(total_deposited),
        "total_withdrawn": str(total_withdrawn),
        "net_ltv": str(total_deposited - total_withdrawn),
    }


async def top_players_by_ltv(pool: asyncpg.Pool, limit: int = 20) -> list[dict[str, Any]]:
    """The Reports-screen leaderboard version of player_ltv() -- ranked,
    not per-user, and computed in one aggregate query rather than N calls.
    """
    rows = await pool.fetch(
        """
        SELECT
          p.user_id,
          u.display_name,
          COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'in'), 0) AS total_deposited,
          COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'out'), 0) AS total_withdrawn,
          COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'in'), 0)
            - COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'out'), 0) AS net_ltv
        FROM payments p
        JOIN users u ON u.id = p.user_id
        WHERE p.status = 'succeeded'
        GROUP BY p.user_id, u.display_name
        ORDER BY net_ltv DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "user_id": r["user_id"],
            "display_name": r["display_name"],
            "total_deposited": str(r["total_deposited"]),
            "total_withdrawn": str(r["total_withdrawn"]),
            "net_ltv": str(r["net_ltv"]),
        }
        for r in rows
    ]


async def retention_cohorts(pool: asyncpg.Pool, weeks: int = 8) -> list[dict[str, Any]]:
    """Weekly signup-cohort retention (spec section 11's Reports screen:
    "retention cohorts") -- for each week's new signups, what fraction
    played at least one round in each of the following `weeks` weeks.
    "Active" means entered a round that actually started (round_entries
    joined to a round with a real started_at), not just opened the app.

    One set-based SQL query, not a per-user Python loop -- this report
    scans every user and every round entry ever recorded, so it has to
    scale with real data volume, not just look right against a handful of
    test rows.

    Every (cohort, week_offset) pair is returned even at zero activity --
    including offsets for a cohort that signed up recently, where that
    week hasn't actually happened yet. Those are marked `elapsed: false`
    and `retention_rate: null` rather than a bare 0.0: a code review pass
    caught that an un-elapsed week is otherwise indistinguishable from a
    cohort that genuinely churned to zero, in the same report row as
    fully-elapsed older cohorts a reader would reasonably compare it
    against. `active_users` itself is left as a real, honest count either
    way -- someone already active partway through an in-progress week is
    real information; only the *rate* implies a completed comparison.
    """
    rows = await pool.fetch(
        """
        WITH cohort AS (
          SELECT id, date_trunc('week', created_at)::date AS cohort_week
          FROM users
        ),
        cohort_sizes AS (
          SELECT cohort_week, count(*) AS cohort_size FROM cohort GROUP BY cohort_week
        ),
        activity AS (
          SELECT DISTINCT re.user_id, date_trunc('week', r.started_at)::date AS active_week
          FROM round_entries re
          JOIN rounds r ON r.id = re.round_id
          WHERE r.started_at IS NOT NULL
        ),
        retention AS (
          SELECT
            c.cohort_week,
            ((a.active_week - c.cohort_week) / 7)::int AS week_offset,
            count(DISTINCT c.id) AS active_users
          FROM cohort c
          JOIN activity a ON a.user_id = c.id AND a.active_week >= c.cohort_week
          WHERE a.active_week < c.cohort_week + ($1::int * interval '1 week')
          GROUP BY c.cohort_week, week_offset
        )
        SELECT
          cs.cohort_week,
          cs.cohort_size,
          gs.week_offset,
          COALESCE(r.active_users, 0) AS active_users,
          (cs.cohort_week + ((gs.week_offset + 1) * interval '1 week')) <= now() AS elapsed
        FROM cohort_sizes cs
        CROSS JOIN generate_series(0, $1::int - 1) AS gs(week_offset)
        LEFT JOIN retention r ON r.cohort_week = cs.cohort_week AND r.week_offset = gs.week_offset
        ORDER BY cs.cohort_week, gs.week_offset
        """,
        weeks,
    )

    cohorts: dict[date, dict[str, Any]] = {}
    for row in rows:
        cohort = cohorts.setdefault(
            row["cohort_week"],
            {"cohort_week": row["cohort_week"].isoformat(), "cohort_size": row["cohort_size"], "weeks": []},
        )
        cohort_size = row["cohort_size"]
        elapsed = row["elapsed"]
        cohort["weeks"].append(
            {
                "week_offset": row["week_offset"],
                "active_users": row["active_users"],
                "elapsed": elapsed,
                "retention_rate": (
                    round(row["active_users"] / cohort_size, 4) if elapsed and cohort_size else None
                ),
            }
        )
    return list(cohorts.values())


async def get_user_ledger_history(
    pool: asyncpg.Pool, user_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT t.id AS transaction_id, t.kind, t.memo, t.created_at,
               a.kind AS account_kind, e.amount
        FROM ledger_entries e
        JOIN accounts a ON a.id = e.account_id
        JOIN ledger_transactions t ON t.id = e.transaction_id
        WHERE a.user_id = $1
        ORDER BY e.id DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def adjust_balance(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    amount: Decimal,
    reason: str,
    ip_address: str | None,
) -> int:
    """Credits (amount > 0) or debits (amount < 0) a user's cash balance,
    offset against the house_float account -- a real ledger transaction of
    kind 'adjustment', never a direct UPDATE. `reason` is mandatory (the
    caller enforces non-empty; this function just requires the argument).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            house_float = await ledger.get_or_create_account(conn, None, "house_float")
            before_balance = await ledger.balance(conn, cash.id)

            txn = await ledger.post(
                conn,
                "adjustment",
                [
                    ledger.Entry(house_float.id, -amount),
                    ledger.Entry(cash.id, amount),
                ],
                idempotency_key=f"admin-adjust-{admin_id}-{user_id}-{datetime.now(UTC).timestamp()}",
                created_by=f"admin:{admin_id}",
                memo=reason,
            )

            after_balance = await ledger.balance(conn, cash.id)

            await audit.record(
                conn,
                admin_id=admin_id,
                action="users.adjust_balance",
                target_type="user",
                target_id=str(user_id),
                before={"cash_balance": str(before_balance)},
                after={"cash_balance": str(after_balance), "ledger_transaction_id": txn.id},
                reason=reason,
                ip_address=ip_address,
            )
    return txn.id


async def set_user_status(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    status: str,
    reason: str,
    ip_address: str | None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchval("SELECT status FROM users WHERE id = $1", user_id)
            await conn.execute("UPDATE users SET status = $1 WHERE id = $2", status, user_id)
            await audit.record(
                conn,
                admin_id=admin_id,
                action="users.set_status",
                target_type="user",
                target_id=str(user_id),
                before={"status": before},
                after={"status": status},
                reason=reason,
                ip_address=ip_address,
            )


async def list_rounds(pool: asyncpg.Pool, room_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if room_id is not None:
        rows = await pool.fetch(
            "SELECT id, room_id, seq, status, stake, pot, derash, player_count, "
            "started_at, ended_at FROM rounds WHERE room_id = $1 ORDER BY id DESC LIMIT $2",
            room_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, room_id, seq, status, stake, pot, derash, player_count, "
            "started_at, ended_at FROM rounds ORDER BY id DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


async def get_round_detail(pool: asyncpg.Pool, round_id: int) -> dict[str, Any] | None:
    round_row = await pool.fetchrow("SELECT * FROM rounds WHERE id = $1", round_id)
    if round_row is None:
        return None
    entries = await pool.fetch(
        "SELECT card_no, user_id, auto_mark FROM round_entries WHERE round_id = $1", round_id
    )
    winners = await pool.fetch(
        "SELECT user_id, card_no, pattern, won_on_call, amount FROM round_winners "
        "WHERE round_id = $1",
        round_id,
    )
    return {
        "round": dict(round_row),
        "entries": [dict(e) for e in entries],
        "winners": [dict(w) for w in winners],
    }


async def get_round_fairness(pool: asyncpg.Pool, round_id: int) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT server_seed, server_seed_hash, client_seed, draw_order, status "
        "FROM rounds WHERE id = $1",
        round_id,
    )
    if row is None:
        return None
    if row["server_seed"] is None or row["status"] not in ("done", "voided"):
        return {
            "revealed": False,
            "server_seed_hash": row["server_seed_hash"],
            "reason": "server_seed is only revealed after a round finishes",
        }

    server_seed: bytes = row["server_seed"]
    client_seed: str = row["client_seed"] or ""
    draw_order = list(row["draw_order"] or [])
    verified = bingo.verify_draw(server_seed, client_seed, draw_order)

    return {
        "revealed": True,
        "server_seed": server_seed.hex(),
        "server_seed_hash": row["server_seed_hash"],
        "client_seed": client_seed,
        "draw_order": draw_order,
        "verified": verified,
    }


async def void_round_admin(
    pool: asyncpg.Pool, *, admin_id: int, round_id: int, reason: str, ip_address: str | None
) -> bool:
    before = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
    refunded = await refund_round(pool, round_id, reason=f"admin_void: {reason}")
    await audit.record(
        pool,
        admin_id=admin_id,
        action="rounds.void",
        target_type="round",
        target_id=str(round_id),
        before={"status": before["status"] if before else None},
        after={"status": "voided" if refunded else "unchanged (already terminal)"},
        reason=reason,
        ip_address=ip_address,
    )
    return refunded


async def list_rooms(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT id, code, stake, house_cut_bps, min_players, max_players, "
        "lobby_seconds, call_interval_ms, result_seconds, win_patterns, is_active "
        "FROM rooms ORDER BY stake"
    )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d["win_patterns"], str):
            d["win_patterns"] = json.loads(d["win_patterns"])
        out.append(d)
    return out


async def create_room_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    code: str,
    stake: Decimal,
    house_cut_bps: int,
    min_players: int,
    max_players: int,
    lobby_seconds: int,
    call_interval_ms: int,
    result_seconds: int,
    win_patterns: list[str],
    ip_address: str | None,
) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO rooms
                    (code, stake, house_cut_bps, min_players, max_players,
                     lobby_seconds, call_interval_ms, result_seconds, win_patterns)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                code,
                stake,
                house_cut_bps,
                min_players,
                max_players,
                lobby_seconds,
                call_interval_ms,
                result_seconds,
                json.dumps(win_patterns),
            )
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="rooms.create",
                target_type="room",
                target_id=str(row["id"]),
                after={"code": code, "stake": str(stake), "house_cut_bps": house_cut_bps},
                ip_address=ip_address,
            )
    return int(row["id"])


_UPDATABLE_ROOM_FIELDS = {
    "stake",
    "house_cut_bps",
    "min_players",
    "max_players",
    "lobby_seconds",
    "call_interval_ms",
    "result_seconds",
    "win_patterns",
    "is_active",
}


async def update_room_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    room_id: int,
    changes: dict[str, Any],
    reason: str | None,
    ip_address: str | None,
) -> bool:
    """Edits room config -- spec section 26: this only ever affects rounds
    created after the change. Live/future rounds each snapshot their own
    stake/house_cut_bps/etc. at creation time (rounds.stake, not rooms.stake),
    so a config edit can never retroactively alter a round already in
    progress.
    """
    unknown = set(changes) - _UPDATABLE_ROOM_FIELDS
    if unknown:
        raise ValueError(f"not an editable room field: {unknown}")
    if not changes:
        return False

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow("SELECT * FROM rooms WHERE id = $1", room_id)
            if before is None:
                return False

            set_clauses = []
            values: list[Any] = []
            for i, (field, value) in enumerate(changes.items(), start=1):
                set_clauses.append(f"{field} = ${i}")
                values.append(json.dumps(value) if field == "win_patterns" else value)
            values.append(room_id)

            await conn.execute(
                f"UPDATE rooms SET {', '.join(set_clauses)} WHERE id = ${len(values)}",
                *values,
            )
            after = await conn.fetchrow("SELECT * FROM rooms WHERE id = $1", room_id)

            await audit.record(
                conn,
                admin_id=admin_id,
                action="rooms.update",
                target_type="room",
                target_id=str(room_id),
                before={k: str(before[k]) for k in changes},
                after={k: str(after[k]) for k in changes} if after else None,
                reason=reason,
                ip_address=ip_address,
            )
    return True


async def dashboard_summary(pool: asyncpg.Pool) -> dict[str, Any]:
    active_rounds = await pool.fetchval(
        "SELECT count(*) FROM rounds WHERE status IN ('lobby', 'running', 'settling')"
    )
    active_rooms = await pool.fetchval("SELECT count(*) FROM rooms WHERE is_active = true")
    today = date.today()
    stakes_today = await pool.fetchval(
        "SELECT COALESCE(SUM(-e.amount), 0) FROM ledger_entries e "
        "JOIN accounts a ON a.id = e.account_id "
        "JOIN ledger_transactions t ON t.id = e.transaction_id "
        "WHERE a.kind = 'user_cash' AND t.kind = 'stake' AND e.created_at::date = $1",
        today,
    )
    payouts_today = await pool.fetchval(
        "SELECT COALESCE(SUM(e.amount), 0) FROM ledger_entries e "
        "JOIN accounts a ON a.id = e.account_id "
        "JOIN ledger_transactions t ON t.id = e.transaction_id "
        "WHERE a.kind = 'user_cash' AND t.kind = 'payout' AND e.created_at::date = $1",
        today,
    )
    house_revenue_today = await pool.fetchval(
        "SELECT COALESCE(SUM(e.amount), 0) FROM ledger_entries e "
        "JOIN accounts a ON a.id = e.account_id "
        "JOIN ledger_transactions t ON t.id = e.transaction_id "
        "WHERE a.kind = 'house_revenue' AND e.created_at::date = $1",
        today,
    )
    return {
        "active_rounds": active_rounds,
        "active_rooms": active_rooms,
        "stakes_today": str(stakes_today),
        "payouts_today": str(payouts_today),
        "house_revenue_today": str(house_revenue_today),
    }


async def daily_ggr(pool: asyncpg.Pool, on_date: date) -> dict[str, Any]:
    """Gross Gaming Revenue: what the house actually kept that day --
    house_revenue credits from settlements, which already nets stakes
    against payouts (see round_engine.py's _settle_with_winners).
    """
    revenue = await pool.fetchval(
        "SELECT COALESCE(SUM(e.amount), 0) FROM ledger_entries e "
        "JOIN accounts a ON a.id = e.account_id "
        "WHERE a.kind = 'house_revenue' AND e.created_at::date = $1",
        on_date,
    )
    rounds_settled = await pool.fetchval(
        "SELECT count(*) FROM rounds WHERE status = 'done' AND ended_at::date = $1",
        on_date,
    )
    return {"date": on_date.isoformat(), "ggr": str(revenue), "rounds_settled": rounds_settled}


async def list_pending_withdrawals(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT p.id, p.user_id, u.display_name, p.our_ref, p.amount, p.status, p.created_at,
               pm.kind AS method_kind, pm.account_ref, pm.holder_name
        FROM payments p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN payment_methods pm ON pm.id = p.method_id
        WHERE p.direction = 'out' AND p.status = 'review'
        ORDER BY p.created_at
        """
    )
    return [dict(r) for r in rows]


async def approve_withdrawal_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    payment_id: int,
    reason: str | None,
    ip_address: str | None,
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT our_ref, status FROM payments WHERE id = $1 AND direction = 'out' FOR UPDATE",
                payment_id,
            )
            if row is None or row["status"] != "review":
                return False
            await conn.execute(
                "UPDATE payments SET status = 'approved', updated_at = now() WHERE id = $1", payment_id
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="withdrawals.approve",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "review"},
                after={"status": "approved"},
                reason=reason,
                ip_address=ip_address,
            )
            our_ref = row["our_ref"]

    await enqueue_payout(redis, our_ref=our_ref, payment_id=payment_id)
    return True


async def reject_withdrawal_admin(
    pool: asyncpg.Pool, redis: Redis, *, admin_id: int, payment_id: int, reason: str, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, amount, status FROM payments WHERE id = $1 AND direction = 'out' FOR UPDATE",
                payment_id,
            )
            if row is None or row["status"] != "review":
                return False

            locked = await ledger.get_or_create_account(conn, row["user_id"], "user_locked")
            cash = await ledger.get_or_create_account(conn, row["user_id"], "user_cash")
            await ledger.post(
                conn,
                "refund",
                [ledger.Entry(locked.id, -row["amount"]), ledger.Entry(cash.id, row["amount"])],
                idempotency_key=f"payout-reject-{payment_id}",
                payment_id=payment_id,
            )
            await conn.execute(
                "UPDATE payments SET status = 'rejected', failure_reason = $2, updated_at = now() "
                "WHERE id = $1",
                payment_id,
                reason,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="withdrawals.reject",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "review"},
                after={"status": "rejected"},
                reason=reason,
                ip_address=ip_address,
            )
            user_id = row["user_id"]
            amount = row["amount"]

    await notify_user(
        pool, redis, user_id=user_id, key="notify.withdrawal_rejected", amount=str(amount), reason=reason
    )
    return True
