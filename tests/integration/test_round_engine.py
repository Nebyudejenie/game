"""Integration tests for services/engine/round_engine.py -- the state
machine and money-critical paths the spec itself flags as the ones to
review line by line.
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core import bingo, ledger
from services.engine import round_engine, settlement
from services.engine.round_engine import ClaimResult, RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room, recv_balance_update


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def make_engine(pool, redis, card_pool, room_id) -> RoundEngine:
    room = await load_room_config(pool, room_id)
    return RoundEngine(pool, redis, room, card_pool)


async def test_full_round_35_players_ledger_balances(pool, redis, card_pool, conn):
    # lobby_seconds needs real margin here, unlike this file's other tests:
    # the lobby deadline is fixed the moment the first join starts the
    # round (round_engine.py's own _lobby_deadline_monotonic), not
    # extended by later joins, and this test joins 35 users *sequentially*
    # -- each one several real DB round trips -- rather than concurrently.
    # A flaky "not_joinable" failure a code review pass caught (this ran
    # comfortably inside the old 1-second default in isolation, but failed
    # 3 of 5 runs under real host contention -- other unrelated Docker
    # containers on the same shared 4-core box) confirmed 1 second leaves
    # no real margin for 35 sequential joins once the host is under any
    # load at all.
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2,
        lobby_seconds=20, call_interval_ms=5,
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        users = [await create_funded_user(conn) for _ in range(35)]
        for i, user_id in enumerate(users):
            result = await engine.join(user_id, card_no=i + 1)
            assert result.ok, result.reason

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=45)

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
        # A real draw over 35 real cards can legitimately produce more
        # than one simultaneous winner, not just the single-winner case --
        # settlement.split_derash() rounds each share DOWN to the cent and
        # sends whatever fraction that leaves on the table to the house
        # (tested directly in tests/unit/test_settlement.py, and exercised
        # end to end by test_two_simultaneous_claims_split_derash_evenly
        # below), so summing winners' amounts only equals derash exactly
        # when it divides evenly among however many winners this draw
        # actually produced -- recompute the real expected split rather
        # than assuming a single winner always gets the whole derash.
        expected_shares, _leftover_to_house = settlement.split_derash(
            round_row["derash"], len(winners)
        )
        assert sorted(w["amount"] for w in winners) == sorted(expected_shares)

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


async def test_two_simultaneous_auto_mark_winners_both_split_derash(pool, redis, card_pool, conn, monkeypatch):
    # Regression: a real code review pass caught that _call_next_number()'s
    # own auto-mark scan returned as soon as the *first* winning entry
    # flipped self._status away from "running" -- any *later* entry in
    # that same call who also completed a winning pattern on this exact
    # same number (a genuine simultaneous auto-mark tie) was never even
    # offered to claim(), silently losing that player's share of the
    # derash to whoever happened to come first in dict order.
    #
    # Unlike test_two_simultaneous_claims_split_derash_evenly (which polls
    # for two *real* cards to naturally both become winning, close enough
    # in time to land within the 50ms tie window via two manual claim()
    # calls), this fix specifically needs both winning conditions true
    # within the exact same _call_next_number() invocation -- the real
    # card pool's natural draw order doesn't reliably put two specific
    # cards' final winning numbers on the exact same call. bingo.
    # winning_patterns() is monkeypatched instead, so this test controls
    # precisely when each of the two real, real-joined players' cards
    # "complete" -- deterministic, not relying on draw-order luck, and it
    # still exercises the real _call_next_number()/claim()/settlement
    # path end to end, only the pattern-matching decision itself is faked.
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
        grid_a = card_pool[card_a]
        grid_b = card_pool[card_b]
        winning_pattern = bingo.Pattern(name="row_0", kind="row", cells=((0, 0), (0, 1), (0, 2), (0, 3), (0, 4)))

        def fake_winning_patterns(grid, called, enabled):
            if grid is grid_a or grid is grid_b:
                return [winning_pattern]
            return []

        assert (await engine.join(user_a, card_a, auto_mark=True)).ok
        assert (await engine.join(user_b, card_b, auto_mark=True)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)
        monkeypatch.setattr(round_engine.bingo, "winning_patterns", fake_winning_patterns)

        await wait_until(lambda: engine.status == "idle", timeout=15)

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
        # The actual bug, made concrete: without the fix this is 1 winner
        # (whichever of user_a/user_b came first in dict order) taking the
        # full derash, not 2 splitting it evenly.
        assert len(winners) == 2
        assert winners[0]["amount"] == winners[1]["amount"] == Decimal("16.00")
        assert winners[0]["amount"] + winners[1]["amount"] == round_row["derash"]
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_settlement_publishes_winner_balance_updates_concurrently(
    pool, redis, card_pool, conn, monkeypatch
):
    # A code review pass caught _settle_with_winners()'s own balance-update
    # push as a plain sequential `for ... await` loop -- each publish is
    # fully independent (a different user, its own pool connection, its
    # own Redis channel), so a simultaneous-tie round with several winners
    # used to serialize several round trips before round_end could even
    # broadcast, delaying that message for every player in the room, not
    # just whichever winner's own push was still waiting its turn.
    #
    # Same bingo.winning_patterns() monkeypatch technique as the sibling
    # tie test above, for the same reason: makes both real, real-joined
    # players' cards deterministic simultaneous winners, no reliance on
    # the real card pool's draw-order luck. Additionally monkeypatches
    # ledger.publish_balance_update() itself to make user_a's own publish
    # artificially slow -- proving the property under test directly:
    # user_b's publish must not be delayed behind it.
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
        grid_a = card_pool[card_a]
        grid_b = card_pool[card_b]
        winning_pattern = bingo.Pattern(name="row_0", kind="row", cells=((0, 0), (0, 1), (0, 2), (0, 3), (0, 4)))

        def fake_winning_patterns(grid, called, enabled):
            if grid is grid_a or grid is grid_b:
                return [winning_pattern]
            return []

        assert (await engine.join(user_a, card_a, auto_mark=True)).ok
        assert (await engine.join(user_b, card_b, auto_mark=True)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)
        monkeypatch.setattr(round_engine.bingo, "winning_patterns", fake_winning_patterns)

        real_publish = ledger.publish_balance_update
        published_at: dict[int, float] = {}

        async def instrumented_publish(pool_, redis_, user_id):
            if user_id == user_a:
                await asyncio.sleep(0.5)
            result = await real_publish(pool_, redis_, user_id)
            published_at[user_id] = asyncio.get_running_loop().time()
            return result

        monkeypatch.setattr(round_engine.ledger, "publish_balance_update", instrumented_publish)

        start = asyncio.get_running_loop().time()
        await wait_until(lambda: engine.status == "idle", timeout=15)

        assert user_a in published_at and user_b in published_at
        # Sequential (the old bug) would put user_b's publish ~0.5s after
        # user_a's slow one finished; concurrent puts it close to `start`
        # regardless of user_a's own delay.
        b_delay = published_at[user_b] - start
        assert b_delay < 0.3, (
            f"user_b's balance update landed {b_delay:.2f}s after settlement "
            "started -- stalled behind user_a's slow publish"
        )
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_an_unexpected_exception_during_auto_claim_does_not_crash_the_room(
    pool, redis, card_pool, conn, monkeypatch
):
    # Regression: a real code review pass caught that _call_next_number()'s
    # auto-claim scan had no exception isolation at all, unlike
    # _handle_command()'s own identical fix for the manual command path
    # (see its own comment there). An unexpected exception from claim() --
    # realistically _record_claim_attempt()'s own audit-log write, the one
    # real DB call left unguarded in claim() -- propagated straight out of
    # this loop, through _call_next_number(), _run_running()'s bare for
    # loop, and run_forever()'s own while loop, killing this room's entire
    # engine task. Nothing restarts it: the round would sit stuck until a
    # *different* engine worker started and recovery.py's crash sweep
    # found it -- which VOIDS AND REFUNDS the round rather than resuming
    # it, so the legitimate winner would lose their win entirely, along
    # with every other player in the room losing their round to a refund,
    # over one exception.
    #
    # Same bingo.winning_patterns() monkeypatch technique as the test
    # above, for the same reason: makes exactly one real, real-joined
    # player's card a deterministic immediate winner, so the auto-claim
    # scan's only ever call to claim() is fully predictable -- no reliance
    # on the real card pool's draw-order luck to land a specific player's
    # win on a specific call.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a, card_b = 1, 2
        grid_a = card_pool[card_a]
        winning_pattern = bingo.Pattern(name="row_0", kind="row", cells=((0, 0), (0, 1), (0, 2), (0, 3), (0, 4)))

        def fake_winning_patterns(grid, called, enabled):
            return [winning_pattern] if grid is grid_a else []

        assert (await engine.join(user_a, card_a, auto_mark=True)).ok
        assert (await engine.join(user_b, card_b, auto_mark=True)).ok

        real_claim = engine.claim
        call_count = 0

        async def flaky_claim(user_id, *, source="manual"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated bug in claim()")
            return await real_claim(user_id, source=source)

        await wait_until(lambda: engine.status == "running", timeout=5)
        monkeypatch.setattr(round_engine.bingo, "winning_patterns", fake_winning_patterns)
        monkeypatch.setattr(engine, "claim", flaky_claim)

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)

        # The whole point of isolation: the engine task survived the
        # exception instead of dying and waiting for recovery.py's next
        # crash sweep to void and refund the round.
        assert not task.done()
        assert call_count >= 2, "user_a's failed auto-claim was never retried on a later call"

        round_row = await pool.fetchrow(
            "SELECT id, status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        assert round_row["status"] == "done"  # not voided/refunded
        winners = await pool.fetch(
            "SELECT user_id FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        # user_a still won -- the retry actually recovered the claim, not
        # just avoided crashing while silently losing it.
        assert {w["user_id"] for w in winners} == {user_a}
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


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


async def test_claim_settlement_pushes_a_live_balance_update_to_the_winner(pool, redis, card_pool, conn):
    # A code review pass caught that only services/payments/deposits.py
    # ever pushed a live balance_update -- staking and winning moved real
    # money but never told a connected player's UI its balance had
    # changed. round_engine.py's _settle_with_winners() now does.
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

        async def _claim() -> None:
            result = await engine.claim(user_a)
            assert result.ok, result.reason

        push = await recv_balance_update(redis, user_a, _claim)
        assert Decimal(push["cash"]) > Decimal("0.00")

        await wait_until(lambda: engine.status == "idle", timeout=5)
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


async def test_exhausted_no_winner_refund_pushes_a_live_balance_update(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=2, call_interval_ms=2, win_patterns=[],
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("100.00"))
        p2 = await create_funded_user(conn, Decimal("100.00"))
        await engine.join(p1, 1)
        await engine.join(p2, 2)

        push = await recv_balance_update(
            redis, p1, lambda: wait_until(lambda: engine.status == "idle", timeout=15)
        )
        assert push["cash"] == "100.00"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_exhausted_no_winner_refund_publishes_balance_updates_concurrently(
    pool, redis, card_pool, conn, monkeypatch
):
    # Same fix, same proof technique as
    # test_lobby_underfilled_refund_publishes_balance_updates_concurrently
    # above, for this file's other refund-then-publish loop.
    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=2, call_interval_ms=2, win_patterns=[],
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn, Decimal("100.00"))
        user_b = await create_funded_user(conn, Decimal("100.00"))
        await engine.join(user_a, 1)
        await engine.join(user_b, 2)

        real_publish = ledger.publish_balance_update
        entered_at: dict[int, float] = {}
        published_at: dict[int, float] = {}

        async def instrumented_publish(pool_, redis_, user_id):
            entered_at[user_id] = asyncio.get_running_loop().time()
            if user_id == user_a:
                await asyncio.sleep(0.5)
            result = await real_publish(pool_, redis_, user_id)
            published_at[user_id] = asyncio.get_running_loop().time()
            return result

        monkeypatch.setattr(round_engine.ledger, "publish_balance_update", instrumented_publish)

        await wait_until(lambda: engine.status == "idle", timeout=15)

        assert user_a in entered_at and user_b in entered_at
        assert published_at[user_a] - entered_at[user_a] >= 0.5, "user_a's own artificial delay didn't apply"
        entry_gap = abs(entered_at[user_b] - entered_at[user_a])
        assert entry_gap < 0.3, (
            f"user_b's publish call started {entry_gap:.2f}s after user_a's -- "
            "stalled behind it instead of starting concurrently"
        )
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


async def test_lobby_underfilled_refund_pushes_a_live_balance_update(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=1)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok

        push = await recv_balance_update(
            redis, p1, lambda: wait_until(lambda: engine.status == "idle", timeout=10)
        )
        assert push["cash"] == "50.00"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_lobby_underfilled_refund_publishes_balance_updates_concurrently(
    pool, redis, card_pool, conn, monkeypatch
):
    # A code-review pass caught this as the same plain sequential
    # for/await loop already fixed for _settle_with_winners() (see
    # test_settlement_publishes_winner_balance_updates_concurrently
    # above, same technique mirrored here) -- a lobby that fills with
    # several entrants and then times out under min_players used to
    # serialize their refund balance-update pushes one after another.
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=1)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn, Decimal("50.00"))
        user_b = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(user_a, 1)).ok
        assert (await engine.join(user_b, 2)).ok

        real_publish = ledger.publish_balance_update
        entered_at: dict[int, float] = {}
        published_at: dict[int, float] = {}

        async def instrumented_publish(pool_, redis_, user_id):
            entered_at[user_id] = asyncio.get_running_loop().time()
            if user_id == user_a:
                await asyncio.sleep(0.5)
            result = await real_publish(pool_, redis_, user_id)
            published_at[user_id] = asyncio.get_running_loop().time()
            return result

        monkeypatch.setattr(round_engine.ledger, "publish_balance_update", instrumented_publish)

        await wait_until(lambda: engine.status == "idle", timeout=15)

        assert user_a in entered_at and user_b in entered_at
        assert published_at[user_a] - entered_at[user_a] >= 0.5, "user_a's own artificial delay didn't apply"
        # The property that actually distinguishes concurrent from
        # sequential: *when each call started*, not how long its own
        # work took. asyncio.gather() schedules both coroutines together,
        # so both entered_at timestamps land close together regardless of
        # how slow user_a's own publish is. Sequential (the old bug)
        # can't even call instrumented_publish(user_b) until user_a's own
        # await -- including its artificial 0.5s sleep -- fully resolves,
        # so entered_at[user_b] would land ~0.5s after entered_at[user_a].
        # Measured this way rather than against a fixed test-start
        # baseline on purpose: this test's first draft measured against
        # test start and failed on real numbers, not because the fix was
        # wrong, but because the lobby_seconds=1 wait before the engine
        # even calls refund_round() swamped that baseline with unrelated
        # time having nothing to do with publish concurrency.
        entry_gap = abs(entered_at[user_b] - entered_at[user_a])
        assert entry_gap < 0.3, (
            f"user_b's publish call started {entry_gap:.2f}s after user_a's -- "
            "stalled behind it instead of starting concurrently"
        )
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_join_and_drop_card_push_live_balance_updates(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))

        async def _join() -> None:
            result = await engine.join(p1, 1)
            assert result.ok, result.reason

        join_push = await recv_balance_update(redis, p1, _join)
        assert join_push["cash"] == "40.00"

        async def _drop() -> None:
            result = await engine.drop_card(p1)
            assert result.ok, result.reason

        drop_push = await recv_balance_update(redis, p1, _drop)
        assert drop_push["cash"] == "50.00"
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
