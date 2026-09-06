"""configurable min winning lines per room

Revision ID: 2e5e67f7b227
Revises: 4bbb21e0f5ad
Create Date: 2026-09-06 22:14:29.464657

The two-line win rule (packages/core/bingo.py::MIN_WINNING_LINES) was a
single, hardcoded module constant shared by every room -- this makes it
per-room configurable, following the exact existing precedent
rooms.win_patterns already set for "which line shapes count" (jsonb,
admin-editable, snapshotted per-round via the same RoomConfig loading
path). No new table, no parallel configuration system: same rooms row,
same admin room-edit form, same load-once-per-engine-claim lifecycle
every other room field already has.

Bounded 1-4, not unbounded: a 5x5 grid has only 12 total lines (5 rows +
5 columns + 2 diagonals), and even generously counting the FREE space,
requiring more than a handful of simultaneous complete lines makes a
room practically unwinnable -- the same "prevent invalid configuration"
principle applies here as to every other bounded room field
(house_cut_bps, min_players<=max_players, etc.). 1 preserves the
classic single-line game as a real, supported configuration (not
removed, just no longer the default); the product default of 2 is
unchanged from what MIN_WINNING_LINES already was.

DEFAULT 2 applies to every existing row untouched -- this is the exact
value every room has been running under since the two-line rule was
first built, so backfilling the column with the same default is not a
behavior change for any existing room, just making an already-true
value visible and editable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2e5e67f7b227'
down_revision: Union[str, None] = '4bbb21e0f5ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE rooms
          ADD COLUMN min_winning_lines smallint NOT NULL DEFAULT 2
            CHECK (min_winning_lines BETWEEN 1 AND 4)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE rooms DROP COLUMN min_winning_lines")
