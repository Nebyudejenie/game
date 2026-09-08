"""1,000 players rushing one stake tier at once (spec Prompt 10 / section
10.3: "1,000 players rushing one stake tier in 10 seconds -- report seat
allocation"). Exercised at the engine level (RoundEngine.join() directly)
rather than over real WebSockets, since the property actually under test
is the engine's own correctness under real concurrency -- the row-locked,
UniqueViolationError-backed card allocation -- not connection/transport
overhead, which test_load_multiroom.py already covers separately.

Ten real players contend for every one of the 100 cards simultaneously
(1000 total concurrent join() calls, 10-way collision on each card_no):
this is a harder, more adversarial shape than "1000 players each pick a
free card," and it's what actually proves no card is ever double-sold.
"""

import asyncio
import time
from decimal import Decimal

import pytest

from packages.core import ledger
from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config
from tests.integration.conftest import create_funded_user, create_room
from tests.integration.test_round_engine import wait_until

pytestmark = pytest.mark.load

PLAYERS = 1000
CARDS = 100
CONTENDERS_PER_CARD = PLAYERS // CARDS


async def test_1000_players_rush_100_cards_no_double_allocation(pool, redis, conn, card_pool):
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=CARDS, lobby_seconds=10
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        players = [await create_funded_user(conn, Decimal("100.00")) for _ in range(PLAYERS)]
        # Player i contends for card (i % CARDS) + 1 -- every card gets
        # exactly CONTENDERS_PER_CARD simultaneous claimants.
        assignments = [(players[i], (i % CARDS) + 1) for i in range(PLAYERS)]

        started = time.monotonic()
        results = await asyncio.gather(
            *(engine.join(user_id, card_no) for user_id, card_no in assignments)
        )
        elapsed = time.monotonic() - started

        won_seats = [r for r in results if r.ok]
        lost_seats = [r for r in results if not r.ok]
        reasons = {}
        for r in lost_seats:
            reasons[r.reason] = reasons.get(r.reason, 0) + 1

        print(
            f"\n[rush players={PLAYERS} cards={CARDS} contenders/card={CONTENDERS_PER_CARD}] "
            f"elapsed={elapsed:.2f}s won={len(won_seats)} lost={len(lost_seats)} "
            f"lost_reasons={reasons}"
        )

        # Exactly one winner per card -- never zero (some card left
        # unsold when there were 10 contenders for it), never more than
        # one (a double-sold card, the actual money-safety property).
        assert len(won_seats) == CARDS

        rows = await pool.fetch(
            "SELECT card_no, count(*) AS n FROM round_entries WHERE round_id = "
            "(SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1) "
            "GROUP BY card_no HAVING count(*) > 1",
            room_id,
        )
        assert rows == [], f"a card was allocated more than once: {rows}"

        pot = await pool.fetchval(
            "SELECT pot FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        assert pot == Decimal("10.00") * CARDS  # exactly 100 stakes landed, not 1000
    finally:
        # _run_lobby() waits out its full deadline unconditionally (it
        # doesn't poll _stop_requested inside that wait -- a known, narrow
        # engine characteristic, not fixed here, matching the same
        # documented tradeoff other tests in this suite already made), so
        # teardown has to comfortably outlast lobby_seconds, not just the
        # rush itself.
        await engine.stop()
        await asyncio.wait_for(task, timeout=20)


MAX_ROOM_SIZE = 432  # packages/core/bingo.py's own _POOL_SIZE -- see below


async def test_432_players_fill_one_room_to_its_real_capacity_and_the_round_settles(
    pool, redis, card_pool, conn
):
    """The real product target confirmed this session: rooms.max_players
    should reach the *actual* size of the card pool -- packages/core/
    bingo.py's _POOL_SIZE = 432, the same number services/gateway/
    queries.py's own card_pool_size field already documents as the real,
    corrected ground truth (max(card_no) over the real seeded `cards`
    rows, replacing a stale hardcoded "1..150" after a past production
    incident) -- not the arbitrary 100 the original rooms table CHECK
    constraint shipped with and nobody had revisited since.

    Two other real risks this room size raises were already separately,
    independently proven at an even larger scale before this test existed:
    fan-out latency to many simultaneous sockets on one room
    (test_gateway_fanout.py::test_many_sockets_receive_a_call_within_
    budget, 1,000 sockets) and no-double-sold-card correctness under
    adversarial concurrency (test_1000_players_rush_100_cards_no_double_
    allocation above, 1,000 joiners). What neither of those proves is the
    one thing specific to actually raising max_players: that a room
    configured at the real target size lets all 432 real players in, still
    correctly rejects a 433rd, and settles a real round for exactly that
    many entrants with the ledger reconciling -- proven here, end to end,
    in one room, not inferred from the two narrower properties above.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=MAX_ROOM_SIZE, lobby_seconds=60,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        players = [await create_funded_user(conn, Decimal("100.00")) for _ in range(MAX_ROOM_SIZE + 1)]

        started = time.monotonic()
        results = await asyncio.gather(
            *(
                engine.join(user_id, card_no + 1)
                for card_no, user_id in enumerate(players[:MAX_ROOM_SIZE])
            )
        )
        elapsed = time.monotonic() - started
        assert all(r.ok for r in results), [r for r in results if not r.ok]

        # One more than the room's real cap -- every card is already
        # taken too, but room_full must be the reason, proving the cap
        # itself is enforced at exactly this size, not silently unlimited
        # now that it's been raised.
        overflow = await engine.join(players[MAX_ROOM_SIZE], 1)
        assert overflow.ok is False
        assert overflow.reason == "room_full"

        print(f"\n[432-player room fill] elapsed={elapsed:.2f}s")

        assert engine.player_count() == MAX_ROOM_SIZE
        # lobby_seconds=60 above is deliberately generous, not tight --
        # _run_lobby() always waits out the room's full lobby deadline
        # regardless of how many players have already joined (it never
        # transitions early just because min_players is satisfied), so
        # this has to comfortably outlast however long processing all
        # 432 real, fully-successful joins (each a genuine DB round trip
        # serialized through the engine's own _join_lock) takes on
        # whatever host this runs on -- confirmed once already that a
        # too-tight lobby lets the room go idle (and, correctly, refuse a
        # second round once is_active is false) while joins are still in
        # flight, which reads as "round_id vanished mid-join" and is
        # nothing to do with join() itself being wrong.
        await wait_until(lambda: engine.status == "running", timeout=75)
        round_id = engine.round_id
        round_row = await pool.fetchrow(
            "SELECT player_count, pot FROM rounds WHERE id = $1", round_id
        )
        assert round_row["player_count"] == MAX_ROOM_SIZE
        assert round_row["pot"] == Decimal("10.00") * MAX_ROOM_SIZE

        # Let the round actually run to a real terminal outcome -- with
        # every join() above defaulting auto_mark=True and 432 real,
        # distinct cards genuinely in play, a real auto-claimed win is
        # entirely possible (not forced or faked either way); an
        # exhausted-no-winner void is equally legitimate. Either is a
        # real, correct settlement of all 432 entrants -- this proves
        # capacity and settlement hold at the real target size, not one
        # specific outcome.
        await wait_until(lambda: engine.status == "idle", timeout=60)
        final_status = await pool.fetchval("SELECT status FROM rounds WHERE id = $1", round_id)
        assert final_status in ("done", "voided")

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=20)
