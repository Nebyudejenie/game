"""Tests for services/admin/queries.py: every mutation must move money
through the ledger (never a direct balance write) and leave an audit trail.
"""

import asyncio
import json
from decimal import Decimal

import pytest

from packages.core import ledger
from services.admin import queries
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import (
    create_funded_user,
    create_room,
    next_telegram_id,
    unique_phone,
)
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_round_engine import wait_until


async def test_search_users_finds_by_phone_fragment(pool, conn):
    telegram_id = next_telegram_id()
    phone = unique_phone()
    row = await conn.fetchrow(
        "INSERT INTO users (telegram_id, display_name, phone_e164) VALUES ($1, $2, $3) RETURNING id",
        telegram_id,
        "Findable Person",
        phone,
    )
    results = await queries.search_users(pool, phone[-9:])
    assert any(r["id"] == row["id"] for r in results)


async def test_get_user_detail_includes_real_balances(pool, conn):
    user_id = await create_funded_user(conn, Decimal("55.00"))
    detail = await queries.get_user_detail(pool, user_id)
    assert detail is not None
    assert detail["balances"]["cash"] == "55.00"


async def test_get_user_detail_returns_none_for_unknown_user(pool):
    assert await queries.get_user_detail(pool, 999_999_999) is None


async def test_adjust_balance_credits_via_ledger_and_writes_audit_log(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("10.00"))

    txn_id = await queries.adjust_balance(
        pool,
        admin_id=admin_id,
        user_id=user_id,
        amount=Decimal("25.00"),
        reason="goodwill credit for a support ticket",
        ip_address="127.0.0.1",
    )
    assert txn_id

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("35.00")

    txn_row = await conn.fetchrow(
        "SELECT kind, memo, created_by FROM ledger_transactions WHERE id = $1", txn_id
    )
    assert txn_row["kind"] == "adjustment"
    assert txn_row["created_by"] == f"admin:{admin_id}"

    audit_row = await conn.fetchrow(
        "SELECT action, reason, before, after FROM admin_audit_log "
        "WHERE admin_id = $1 AND target_id = $2 ORDER BY id DESC LIMIT 1",
        admin_id,
        str(user_id),
    )
    assert audit_row["action"] == "users.adjust_balance"
    assert "goodwill" in audit_row["reason"]

    mismatches = await ledger.reconcile(conn)
    assert mismatches == []


async def test_adjust_balance_debit_cannot_overdraw(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("10.00"))

    with pytest.raises(ledger.InsufficientFunds):
        await queries.adjust_balance(
            pool,
            admin_id=admin_id,
            user_id=user_id,
            amount=Decimal("-50.00"),
            reason="correcting a duplicate credit",
            ip_address=None,
        )

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("10.00")


async def test_set_user_status_writes_audit_log_with_before_and_after(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn)

    await queries.set_user_status(
        pool,
        admin_id=admin_id,
        user_id=user_id,
        status="self_excluded",
        reason="player requested self-exclusion",
        ip_address="127.0.0.1",
    )

    status = await conn.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "self_excluded"

    audit_row = await conn.fetchrow(
        "SELECT before, after FROM admin_audit_log WHERE target_id = $1 ORDER BY id DESC LIMIT 1",
        str(user_id),
    )
    # asyncpg returns jsonb columns as raw JSON text by default (no type
    # codec registered) -- same as every other jsonb read in this codebase.
    before = json.loads(audit_row["before"])
    after = json.loads(audit_row["after"])
    assert before["status"] == "active"
    assert after["status"] == "self_excluded"


async def test_void_round_admin_refunds_and_is_idempotent(pool, redis, card_pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await create_room(conn, stake=Decimal("15.00"), min_players=5, lobby_seconds=1)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id

        # Lobby will time out and void itself (only 1 of 5 min_players) --
        # but exercise the admin void path directly and immediately instead,
        # which is the realistic "something's stuck, an ops admin steps in"
        # scenario, and confirm it's a safe no-op once already terminal.
        refunded_first = await queries.void_round_admin(
            pool, admin_id=admin_id, round_id=round_id, reason="stuck room", ip_address="10.0.0.1"
        )
        assert refunded_first is True

        cash = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash.id) == Decimal("50.00")

        refunded_second = await queries.void_round_admin(
            pool, admin_id=admin_id, round_id=round_id, reason="retry", ip_address="10.0.0.1"
        )
        assert refunded_second is False  # already terminal, no double refund
        assert await ledger.balance(conn, cash.id) == Decimal("50.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_round_fairness_verification_matches_a_real_round(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=10)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)

        round_row = await pool.fetchrow(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        fairness = await queries.get_round_fairness(pool, round_row["id"])
        assert fairness is not None
        assert fairness["revealed"] is True
        assert fairness["verified"] is True
        assert len(fairness["draw_order"]) == 75
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_fairness_not_revealed_before_round_finishes(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=5)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id

        fairness = await queries.get_round_fairness(pool, round_id)
        assert fairness is not None
        assert fairness["revealed"] is False
        assert "server_seed_hash" in fairness
        assert "server_seed" not in fairness
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_create_and_update_room_admin_writes_audit_log(pool):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("50.00"),
        house_cut_bps=1500,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address="127.0.0.1",
    )

    row = await pool.fetchrow("SELECT stake, house_cut_bps FROM rooms WHERE id = $1", room_id)
    assert row["stake"] == Decimal("50.00")
    assert row["house_cut_bps"] == 1500

    updated = await queries.update_room_admin(
        pool,
        admin_id=admin_id,
        room_id=room_id,
        changes={"house_cut_bps": 2500},
        reason="adjusting margin",
        ip_address="127.0.0.1",
    )
    assert updated is True

    row = await pool.fetchrow("SELECT house_cut_bps FROM rooms WHERE id = $1", room_id)
    assert row["house_cut_bps"] == 2500

    audit_rows = await pool.fetch(
        "SELECT action FROM admin_audit_log WHERE target_id = $1 ORDER BY id", str(room_id)
    )
    actions = [r["action"] for r in audit_rows]
    assert "rooms.create" in actions
    assert "rooms.update" in actions


async def test_update_room_rejects_unknown_field(pool):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("20.00"),
        house_cut_bps=2000,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address=None,
    )
    with pytest.raises(ValueError):
        await queries.update_room_admin(
            pool,
            admin_id=admin_id,
            room_id=room_id,
            changes={"code": "sneaky rename"},
            reason=None,
            ip_address=None,
        )


async def test_room_config_edit_does_not_affect_an_in_flight_round(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=5, lobby_seconds=5
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id

        admin_id, *_ = await create_test_admin(pool)
        await queries.update_room_admin(
            pool,
            admin_id=admin_id,
            room_id=room_id,
            changes={"house_cut_bps": 9000, "stake": "999.00"},
            reason="test: must not retroactively affect the live round",
            ip_address=None,
        )

        round_row = await pool.fetchrow(
            "SELECT stake, house_cut_bps FROM rounds WHERE id = $1", round_id
        )
        assert round_row["stake"] == Decimal("20.00")
        assert round_row["house_cut_bps"] == 2000
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_dashboard_summary_reflects_real_state(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=5)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok

        summary = await queries.dashboard_summary(pool)
        assert summary["active_rounds"] >= 1
        assert summary["active_rooms"] >= 1
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


_suffix_counter = [0]


def room_id_suffix() -> str:
    _suffix_counter[0] += 1
    return f"{next_telegram_id()}-{_suffix_counter[0]}"
