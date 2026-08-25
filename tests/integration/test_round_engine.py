"""Integration tests for services/engine/round_engine.py -- the state
machine and money-critical paths the spec itself flags as the ones to
review line by line.
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core import bingo, ledger
from services.engine.round_engine import ClaimResult, RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def make_engine(pool, redis, card_pool, room_id) -> RoundEngine:
    room = await load_room_config(pool, room_id)
    return RoundEngine(pool, redis, room, card_pool)


async def test_full_round_35_players_ledger_balances(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, call_interval_ms=5
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        users = [await create_funded_user(conn) for _ in range(35)]
        for i, user_id in enumerate(users):
            result = await engine.join(user_id, card_no=i + 1)
            assert result.ok, result.reason

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)

        round_row = await pool.fetchrow(
            "SELECT id, pot, derash, status FROM rounds WHERE room_id = $1 "
            "ORDER BY seq DESC LIMIT 1",
            room_id,
        )
        assert round_row["status"] == "done"
        assert round_row["pot"] == Decimal("700.00")
        assert round_row["derash"] == Decimal("560.00")
        assert round_row["pot"] - round_row["derash"] == Decimal("140.00")

        winners = await pool.fetch(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        assert len(winners) >= 1
        assert sum((w["amount"] for w in winners), Decimal("0")) == round_row["derash"]

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_two_simultaneous_claims_split_derash_evenly(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a, card_b = 1, 2

        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, card_b, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_a = card_pool[card_a]
        grid_b = card_pool[card_b]

        def both_ready() -> bool:
            # Reaching into the engine's own called-numbers set here is
            # deliberate: it's how the test drives two claims to land in
            # the exact same instant without needing to control the
            # (intentionally provably-fair-random) draw order itself.
            called = engine._called  # noqa: SLF001
            return bool(
                bingo.winning_patterns(grid_a, called, room.win_patterns)
            ) and bool(bingo.winning_patterns(grid_b, called, room.win_patterns))

        await wait_until(both_ready, timeout=10)

        results = await asyncio.gather(engine.claim(user_a), engine.claim(user_b))
        assert all(r.ok for r in results), results

        await wait_until(lambda: engine.status == "idle", timeout=5)

        round_row = await pool.fetchrow(
            "SELECT id, pot, derash FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1",
            room_id,
        )
        assert round_row["pot"] == Decimal("40.00")
        assert round_row["derash"] == Decimal("32.00")

        winners = await pool.fetch(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1 ORDER BY user_id",
            round_row["id"],
        )
        assert len(winners) == 2
        assert winners[0]["amount"] == winners[1]["amount"] == Decimal("16.00")
        assert winners[0]["amount"] + winners[1]["amount"] == round_row["derash"]
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_same_user_double_claim_race_settles_exactly_once(pool, redis, card_pool, conn):
    """Regression test: a real crash found by the Mini App's E2E test.

    A single player can produce two independent valid claims for the same
    round -- the server's own AUTO-mode scan and a client-sent `claim`
    message racing each other (a player with client-side AUTO on, or a
    manual double-tap). Both used to land in _pending_winners, crashing
    round_winners' (round_id, user_id) primary key at settlement. Exactly
    one must win; the other must be cleanly rejected, and settlement must
    still complete.
    """
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a = 1

        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, 2, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_a = card_pool[card_a]

        def a_ready() -> bool:
            return bool(bingo.winning_patterns(grid_a, engine._called, room.win_patterns))  # noqa: SLF001

        await wait_until(a_ready, timeout=10)

        results = await asyncio.gather(
            engine.claim(user_a, source="auto"),
            engine.claim(user_a, source="manual"),
        )
        oks = [r for r in results if r.ok]
        rejected = [r for r in results if not r.ok]
        assert len(oks) == 1, results
        assert len(rejected) == 1 and rejected[0].reason == "already_claimed", results

        await wait_until(lambda: engine.status == "idle", timeout=5)

        round_row = await pool.fetchrow(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        winners = await pool.fetch(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        assert len(winners) == 1
        assert winners[0]["user_id"] == user_a

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_claim_from_user_not_in_round_rejected_and_logged(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        intruder = await create_funded_user(conn)
        await engine.join(p1, 1)
        await engine.join(p2, 2)
        await wait_until(lambda: engine.status == "running", timeout=5)

        result = await engine.claim(intruder)
        assert result == ClaimResult(False, "not_in_round")

        round_id = engine.round_id
        row = await pool.fetchrow(
            "SELECT valid FROM claim_attempts WHERE round_id = $1 AND user_id = $2",
            round_id,
            intruder,
        )
        assert row is not None
        assert row["valid"] is False
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_round_exhausted_no_winner_full_refund(pool, redis, card_pool, conn):
    # win_patterns=[] means nothing can ever validate -- guarantees
    # exhaustion of all 75 calls with zero winners, deterministically.
    room_id = await create_room(
        conn,
        stake=Decimal("15.00"),
        min_players=2,
        call_interval_ms=2,
        win_patterns=[],
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("100.00"))
        p2 = await create_funded_user(conn, Decimal("100.00"))
        await engine.join(p1, 1)
        await engine.join(p2, 2)

        round_id_before = engine.round_id
        await wait_until(lambda: engine.status == "idle", timeout=15)

        round_row = await pool.fetchrow(
            "SELECT status, call_index FROM rounds WHERE id = $1", round_id_before
        )
        assert round_row["status"] == "voided"
        assert round_row["call_index"] == 75

        winners = await pool.fetch(
            "SELECT 1 FROM round_winners WHERE round_id = $1", round_id_before
        )
        assert winners == []

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        cash2 = await ledger.get_or_create_account(conn, p2, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("100.00")
        assert await ledger.balance(conn, cash2.id) == Decimal("100.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_lobby_underfilled_all_refunded_room_returns_to_idle(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=1)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        result = await engine.join(p1, 1)
        assert result.ok

        round_id = engine.round_id
        await wait_until(lambda: engine.status == "idle", timeout=10)

        round_row = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert round_row["status"] == "voided"

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")

        assert engine.status == "idle"
        assert engine.round_id is None
        assert engine.player_count() == 0
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_drop_card_during_lobby_refunds(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("40.00")

        drop_result = await engine.drop_card(p1)
        assert drop_result.ok
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")
        assert engine.player_count() == 0
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_duplicate_card_and_double_join_rejected(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)

        assert (await engine.join(p1, 7)).ok

        same_card = await engine.join(p2, 7)
        assert same_card.ok is False
        assert same_card.reason == "card_taken"

        second_card_same_user = await engine.join(p1, 8)
        assert second_card_same_user.ok is False
        assert second_card_same_user.reason == "already_joined"

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        # Only the one successful stake should have been charged.
        assert await ledger.balance(conn, cash1.id) == Decimal("990.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_max_players_cap_holds_under_real_concurrent_joins(pool, redis, card_pool, conn):
    # Regression: a real code review pass caught that the capacity check
    # (len(self._entries) >= max_players) had no lock of its own -- two
    # different users with two different card numbers (so the round_
    # entries UNIQUE constraint on card_no can't catch it) could both
    # read the same under-capacity count before either updated
    # self._entries, overfilling the room past its configured cap. A
    # small max_players and more concurrent joins than that cap, fired
    # genuinely simultaneously via asyncio.gather (not sequentially),
    # proves the fix holds under real concurrency, not just sequential
    # calls -- exactly the load/chaos style of test this codebase already
    # uses for its other real concurrency guarantees.
    # min_players=2 is met partway through this batch, but _run_lobby()
    # doesn't poll engine.stop() -- it only re-checks between its own
    # 1-second ticks -- so lobby_seconds has to stay short (matching
    # every other test in this file) or this test's own cleanup
    # (engine.stop() + wait_for(task, timeout=10)) would itself time out
    # waiting for a long lobby to naturally elapse. 3 seconds is still
    # comfortably longer than 10 concurrent, now-serialized local joins
    # should ever take, even under contention.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=3, lobby_seconds=3
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        players = [await create_funded_user(conn) for _ in range(10)]
        results = await asyncio.gather(
            *(engine.join(user_id, card_no) for card_no, user_id in enumerate(players, start=1))
        )
        successes = [r for r in results if r.ok]
        failures = [r for r in results if not r.ok]
        assert len(successes) == 3
        assert len(failures) == 7
        assert all(r.reason == "room_full" for r in failures)
        assert engine.player_count() == 3

        row = await pool.fetchrow("SELECT player_count FROM rounds WHERE id = $1", engine.round_id)
        assert row["player_count"] == 3  # in-memory count and the DB row agree
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_insufficient_balance_join_rejected_no_partial_state(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("500.00"), min_players=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        poor_user = await create_funded_user(conn, Decimal("10.00"))
        result = await engine.join(poor_user, 1)
        assert result.ok is False
        assert result.reason == "insufficient_funds"

        # No stake means no round_entries row either -- the two must move
        # together or not at all.
        row = await pool.fetchrow(
            "SELECT 1 FROM round_entries WHERE round_id = $1 AND user_id = $2",
            engine.round_id,
            poor_user,
        )
        assert row is None
        assert engine.player_count() == 0
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)
