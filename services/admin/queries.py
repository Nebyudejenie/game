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
from zoneinfo import ZoneInfo

import asyncpg
from redis.asyncio import Redis

from packages.core import bingo, ledger, metrics
from packages.core.notifications import notify_user
from packages.core.phone_crypto import decrypt_phone, phone_lookup_hash
from services.admin import audit
from services.bot.phone import normalize_ethiopian_phone
from services.engine.refunds import refund_round_in_transaction
from services.payments.withdrawals import enqueue_payout


# A code review pass caught dashboard_summary()/daily_ggr() computing
# "today" with Python's date.today() (whatever the admin process's own
# host/container timezone happens to be) while the SQL cast `created_at
# ::date` converts using the *Postgres session's* own timezone setting --
# two independent, unconfigured ambient defaults that were never
# guaranteed to agree, and even if they happened to (both defaulting to
# UTC, say), neither would match the Ethiopian calendar day these reports
# are actually meant to describe. A transaction between 21:00-24:00 UTC
# (00:00-03:00 EAT) is a real, everyday occurrence, not an edge case, so
# this isn't a theoretical risk -- it's routinely wrong by up to 3 hours
# on both sides of midnight. Both sides now compute the boundary the same
# explicit way instead of trusting ambient defaults.
ETHIOPIA_TZ = ZoneInfo("Africa/Addis_Ababa")


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

    # A code review pass caught this reimplementing user_balance_snapshot
    # -- three get_or_create_account() + balance() round trips, the same
    # shape ledger.py's own shared helper already replaced with a single
    # query when it was consolidated out of services/gateway/queries.py
    # (packages/core/ledger.py's own docstring covers why it lives there,
    # not here or gateway/queries.py). Purely a reuse/efficiency fix --
    # same three balance figures, just not reimplemented a third time.
    balances = await ledger.user_balance_snapshot(pool, user_id)

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
          SELECT id, date_trunc('week', created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS cohort_week
          FROM users
        ),
        cohort_sizes AS (
          SELECT cohort_week, count(*) AS cohort_size FROM cohort GROUP BY cohort_week
        ),
        activity AS (
          SELECT DISTINCT re.user_id,
            date_trunc('week', r.started_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS active_week
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
        # Only reachable once the transaction above has actually
        # committed -- see ledger.post()'s own comment for why it can't
        # safely record this itself when called nested, which every real
        # call is.
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
    return txn.id


# Self-exclusion (packages.core.responsible_gaming.self_exclude()) is
# deliberately, permanently irreversible for its duration -- "there is
# deliberately no 'lift my own self-exclusion' function anywhere in this
# codebase" per that module's own docstring. This generic admin
# status-change endpoint was exactly such a function in disguise (a real
# bug a code review pass caught: any ops/finance admin holding
# users:suspend could silently reverse a legally-mandated exclusion by
# calling this with status="active"). self_excluded is excluded from both
# ends of the transition: an admin can never set it directly (that would
# also be incomplete -- it wouldn't set self_excluded_until, producing a
# broken half-exclusion), and once a user is self_excluded, this endpoint
# refuses to touch their status at all, in either direction.
_ADMIN_SETTABLE_STATUSES = frozenset({"active", "limited", "banned"})


class InvalidStatusTransition(Exception):
    pass


async def set_user_status(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    status: str,
    reason: str,
    ip_address: str | None,
) -> None:
    if status not in _ADMIN_SETTABLE_STATUSES:
        raise InvalidStatusTransition(f"admins cannot set user status to {status!r}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchval(
                "SELECT status FROM users WHERE id = $1 FOR UPDATE", user_id
            )
            if before == "self_excluded":
                raise InvalidStatusTransition(
                    "self-exclusion cannot be changed by an admin -- it is "
                    "permanent for its duration by design"
                )
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


_VALID_KYC_LEVELS = frozenset({0, 1, 2})


class InvalidKycLevel(Exception):
    pass


async def set_kyc_level(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    kyc_level: int,
    reason: str,
    ip_address: str | None,
) -> None:
    """The manual half of KYC verification: an admin who has reviewed a
    user's identity documents (through whatever out-of-band channel this
    platform actually collects them through -- that verification method
    itself is a real, separate, not-yet-made product decision, tracked in
    DECISIONS.md, not invented here) records the outcome. Before this,
    `users.kyc_level` had a real consumer (withdrawals.py's own threshold
    gate) but no writer anywhere in the codebase -- a real, live gap a
    code review pass caught: any user who genuinely needed KYC to clear a
    large withdrawal had no path through the gate at all, not even a slow
    manual one. Promotions and demotions both go through this same
    function and the same audit trail -- a level can be revoked (fraud
    discovered, documents later found invalid) exactly the same
    accountable way it was granted.
    """
    if kyc_level not in _VALID_KYC_LEVELS:
        raise InvalidKycLevel(f"kyc_level must be one of {sorted(_VALID_KYC_LEVELS)}, got {kyc_level!r}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchval(
                "SELECT kyc_level FROM users WHERE id = $1 FOR UPDATE", user_id
            )
            if before is None:
                raise InvalidKycLevel(f"no such user: {user_id}")
            await conn.execute("UPDATE users SET kyc_level = $1 WHERE id = $2", kyc_level, user_id)
            await audit.record(
                conn,
                admin_id=admin_id,
                action="users.set_kyc_level",
                target_type="user",
                target_id=str(user_id),
                before={"kyc_level": before},
                after={"kyc_level": kyc_level},
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
    # The refund and its audit-log entry commit or roll back together, in
    # one transaction -- a real bug a code review pass caught: this used
    # to call refund_round(pool, ...) (its own independent transaction,
    # already committed) and then write the audit entry afterward on a
    # separate connection, so a crash in between left real money refunded
    # with no audit trail for it at all.
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
            refunded_count = await refund_round_in_transaction(
                conn, round_id, reason=f"admin_void: {reason}"
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="rounds.void",
                target_type="round",
                target_id=str(round_id),
                before={"status": before["status"] if before else None},
                after={"status": "voided" if refunded_count else "unchanged (already terminal)"},
                reason=reason,
                ip_address=ip_address,
            )
        # Only reachable once this function's own transaction above has
        # actually committed -- safe to record here even though
        # refund_round_in_transaction() itself can't (see its own
        # comment): this call site owns a real, non-nested transaction.
        if refunded_count:
            metrics.ledger_transactions_total.labels(kind="refund").inc(refunded_count)
    return bool(refunded_count)


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


def _room_audit_value(row: asyncpg.Record, key: str) -> Any:
    """A code review pass caught that update_room_admin()'s audit
    before/after values ran every changed field through a blanket
    str(...), including win_patterns -- asyncpg returns a jsonb column as
    a raw JSON string with no codec registered (the same reason
    list_rooms() above does its own isinstance(..., str) + json.loads()),
    so str()-ing it was a no-op that left a JSON string sitting as a
    dict value, which audit.record()'s own json.dumps(before) then
    double-encodes into an escaped string in the stored audit row --
    readable as ``"win_patterns": "[\\"row_0\\"]"`` instead of a clean
    nested array. Not a financial bug (the actual room update is
    unaffected either way), just a real audit-readability gap for
    anyone reviewing this specific field's change history.
    """
    value = row[key]
    if key == "win_patterns" and isinstance(value, str):
        return json.loads(value)
    return str(value)


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
                before={k: _room_audit_value(before, k) for k in changes},
                after={k: _room_audit_value(after, k) for k in changes} if after else None,
                reason=reason,
                ip_address=ip_address,
            )
    return True


async def dashboard_summary(pool: asyncpg.Pool) -> dict[str, Any]:
    active_rounds = await pool.fetchval(
        "SELECT count(*) FROM rounds WHERE status IN ('lobby', 'running', 'settling')"
    )
    active_rooms = await pool.fetchval("SELECT count(*) FROM rooms WHERE is_active = true")
    today = datetime.now(ETHIOPIA_TZ).date()
    # A code review pass caught these as three near-identical scans over
    # the same table for the same day -- one FILTER-based query, bucketing
    # by kind in a single pass, produces the exact same three figures
    # (verified against the original per-query WHERE clauses one for one,
    # including that house_revenue_today has no t.kind restriction of its
    # own, matching the original) instead of three separate round trips.
    row = await pool.fetchrow(
        """
        SELECT
            COALESCE(SUM(-e.amount) FILTER (WHERE a.kind = 'user_cash' AND t.kind = 'stake'), 0)
                AS stakes_today,
            COALESCE(SUM(e.amount) FILTER (WHERE a.kind = 'user_cash' AND t.kind = 'payout'), 0)
                AS payouts_today,
            COALESCE(SUM(e.amount) FILTER (WHERE a.kind = 'house_revenue'), 0)
                AS house_revenue_today
        FROM ledger_entries e
        JOIN accounts a ON a.id = e.account_id
        JOIN ledger_transactions t ON t.id = e.transaction_id
        WHERE (e.created_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $1
          AND (t.kind IN ('stake', 'payout') OR a.kind = 'house_revenue')
        """,
        today,
    )
    assert row is not None
    return {
        "active_rounds": active_rounds,
        "active_rooms": active_rooms,
        "stakes_today": str(row["stakes_today"]),
        "payouts_today": str(row["payouts_today"]),
        "house_revenue_today": str(row["house_revenue_today"]),
    }


async def daily_ggr(pool: asyncpg.Pool, on_date: date) -> dict[str, Any]:
    """Gross Gaming Revenue: what the house actually kept that day --
    house_revenue credits from settlements, which already nets stakes
    against payouts (see round_engine.py's _settle_with_winners).
    """
    revenue = await pool.fetchval(
        "SELECT COALESCE(SUM(e.amount), 0) FROM ledger_entries e "
        "JOIN accounts a ON a.id = e.account_id "
        "WHERE a.kind = 'house_revenue' "
        "AND (e.created_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $1",
        on_date,
    )
    rounds_settled = await pool.fetchval(
        "SELECT count(*) FROM rounds WHERE status = 'done' "
        "AND (ended_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $1",
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


async def list_stuck_processing_payouts(
    pool: asyncpg.Pool, *, older_than_seconds: int = 3600
) -> list[dict[str, Any]]:
    """Withdrawals still sitting at status='processing' longer than
    expected -- Chapa accepted the transfer request but this codebase has
    no payout webhook route or status-polling fallback to ever learn what
    actually happened to it (see services/payments/payout_worker.py's own
    module docstring). A code review pass caught the worker previously
    treating "processing" as fully settled, a real silent-money-loss risk
    if a transfer Chapa accepted was later actually rejected on their
    side; the fix leaves it genuinely unresolved instead of guessing, but
    "genuinely unresolved with no way to ever find out" is only an
    improvement if something actually surfaces it. This is that surface
    -- a real automated resolution remains blocked on confirming Chapa's
    transfer-status response vocabulary (see DECISIONS.md), so for now
    this is read-only: an admin who sees an entry here needs to check the
    transfer's real status directly with Chapa and resolve it manually
    (payments.status is not itself constrained to only 'succeeded'/
    'failed' by anything that would block a direct correction).
    """
    rows = await pool.fetch(
        """
        SELECT p.id, p.user_id, u.display_name, p.our_ref, p.amount, p.provider_ref, p.updated_at
        FROM payments p
        JOIN users u ON u.id = p.user_id
        WHERE p.direction = 'out' AND p.status = 'processing'
          AND p.updated_at < now() - make_interval(secs => $1)
        ORDER BY p.updated_at
        """,
        older_than_seconds,
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
            txn = await ledger.post(
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
        # Only reachable once the transaction above has actually
        # committed -- see ledger.post()'s own comment for why it can't
        # safely record this itself when called nested, which every real
        # call is.
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()

    await notify_user(
        pool, redis, user_id=user_id, key="notify.withdrawal_rejected", amount=str(amount), reason=reason
    )
    return True
