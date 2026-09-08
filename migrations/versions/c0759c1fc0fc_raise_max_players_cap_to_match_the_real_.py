"""raise max_players cap to match the real card pool size

Revision ID: c0759c1fc0fc
Revises: b306793309da
Create Date: 2026-09-08 14:49:54.641776

"""
"""rooms.max_players was capped at 100 by the very first game-tables
migration (88556f01eaf8) and nobody had revisited it since -- an
arbitrary ceiling that has nothing to do with this product's real
capacity. packages/core/bingo.py's own _POOL_SIZE is 432, and the
`cards` table has always been seeded with exactly that many rows (432,
confirmed directly against the real database before writing this) --
services/gateway/queries.py's own card_pool_size field already treats
432 as the corrected, real ground truth for how many cards a player can
ever be dealt (its own comment there documents the past production
incident that established this number, after a stale hardcoded "1..150"
disagreed with it). A single room's real seat capacity can never
meaningfully exceed the number of distinct cards that exist to deal --
max_players caps distinct card-holding players (see round_engine.py's
own join(), `if not already_has_a_card and player_count() >=
max_players`) -- so 432 is the actual ceiling this system was always
built for, not an arbitrary larger number chosen for this migration.

Explicitly confirmed with the product owner this session: multiple
same-stake rooms (each up to the old 100) was the first option offered;
432-in-one-room was the one actually wanted. Proven safe at this size by
test_load_rush.py::test_432_players_fill_one_room_to_its_real_capacity_
and_the_round_settles (all 432 seats fill, a 433rd is correctly
rejected, the round settles, the ledger reconciles) plus two properties
already proven separately at an even larger scale before this migration
existed: fan-out latency to many simultaneous sockets on one room
(test_gateway_fanout.py, 1,000 sockets) and no-double-sold-card
correctness under adversarial concurrency
(test_1000_players_rush_100_cards_no_double_allocation, 1,000 joiners).

No existing room needs a data backfill for this (raising a ceiling never
invalidates a value already below it, unlike the min_players floor
8eb513a57043 raised) -- this only ever widens what an admin is allowed
to configure going forward.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0759c1fc0fc'
down_revision: Union[str, None] = 'b306793309da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CHECK = "max_players BETWEEN 1 AND 100"
_NEW_CHECK = "max_players BETWEEN 1 AND 432"


def upgrade() -> None:
    op.execute("ALTER TABLE rooms DROP CONSTRAINT rooms_max_players_check")
    op.execute(f"ALTER TABLE rooms ADD CONSTRAINT rooms_max_players_check CHECK ({_NEW_CHECK})")


def downgrade() -> None:
    # Fails loudly, not silently, if any room has genuinely been
    # reconfigured above 100 in the meantime -- same "can only safely
    # restore exactly what this changed, never guess at anyone else's
    # intent since" reasoning every other narrow-range downgrade in this
    # migration history already follows (see 8eb513a57043).
    op.execute("ALTER TABLE rooms DROP CONSTRAINT rooms_max_players_check")
    op.execute(f"ALTER TABLE rooms ADD CONSTRAINT rooms_max_players_check CHECK ({_OLD_CHECK})")
