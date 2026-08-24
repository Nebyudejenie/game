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

from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config
from tests.integration.conftest import create_funded_user, create_room

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
