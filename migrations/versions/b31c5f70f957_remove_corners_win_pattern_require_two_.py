"""remove corners win pattern, require two winning lines

Revision ID: b31c5f70f957
Revises: 2f6b1a9c4d8e
Create Date: 2026-09-04 17:42:25.842544

Product rule change: a card now only wins once it holds at least two
complete lines (any mix of rows/columns/diagonals) -- see
packages/core/bingo.py's new MIN_WINNING_LINES. "corners" (four corners)
is not a line and is dropped as a win-pattern concept entirely, not folded
into the two-line count (a room configured with only corners enabled would
otherwise become mathematically unwinnable, since there is exactly one
corners pattern).

This migration only touches configuration data (rooms.win_patterns), never
past results: round_winners rows already recorded (pattern text column)
are historical fact and are left exactly as they were won under the rules
in effect at the time.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b31c5f70f957'
down_revision: Union[str, None] = '2f6b1a9c4d8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE rooms ALTER COLUMN win_patterns SET DEFAULT '[\"row\", \"col\", \"diag\"]'"
    )
    # jsonb `-` on an array removes a matching scalar element outright, a
    # no-op for any row that never had "corners" in the first place.
    op.execute("UPDATE rooms SET win_patterns = win_patterns - 'corners'")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE rooms ALTER COLUMN win_patterns SET DEFAULT "
        "'[\"row\", \"col\", \"diag\", \"corners\"]'"
    )
    # Deliberately not restoring "corners" into existing rows' arrays --
    # upgrade()'s removal is the intended product change, not accidental
    # data loss, so there is nothing correct to restore it from.
