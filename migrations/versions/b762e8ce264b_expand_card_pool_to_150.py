"""expand card pool to 150

Revision ID: b762e8ce264b
Revises: 21e6207342cd
Create Date: 2026-09-02 16:23:51.051144

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from packages.core.cards_seed import seed_rows

# revision identifiers, used by Alembic.
revision: str = 'b762e8ce264b'
down_revision: Union[str, None] = '21e6207342cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # generate_card_pool() draws sequentially from one seeded stream, so
    # raising packages/core/bingo.py's _POOL_SIZE from 100 to 150 is a pure
    # append -- card_no 1-100 keep the exact same grid (machine-verified by
    # tests/unit/test_bingo.py::test_card_pool_first_100_cards_are_byte_
    # identical_to_the_original_pool), only 101-150 are new. Only the new
    # rows are inserted here; seed_rows() itself is untouched.
    op.execute("ALTER TABLE cards DROP CONSTRAINT cards_card_no_check")
    op.execute("ALTER TABLE cards ADD CONSTRAINT cards_card_no_check CHECK (card_no BETWEEN 1 AND 150)")

    cards_table = sa.table(
        "cards",
        sa.column("card_no", sa.SmallInteger),
        sa.column("grid", sa.JSON),
    )
    # Pinned to the exact 101-150 range, not "> 100" -- if _POOL_SIZE ever
    # grows again past 150 in the future, this migration must keep
    # inserting exactly the 50 rows it always did on a fresh database, the
    # same reasoning that migration 89519947d424 needed retrofitted here
    # (caught directly: a from-scratch migrate broke on that one the
    # moment _POOL_SIZE changed, since it called this same live helper
    # unpinned).
    rows = [{"card_no": card_no, "grid": grid} for card_no, grid in seed_rows() if 100 < card_no <= 150]
    op.bulk_insert(cards_table, rows)


def downgrade() -> None:
    # Safe even though this doesn't check for referencing rows itself: the
    # existing round_entries.card_no FK to cards(card_no) already blocks
    # this DELETE outright if any round ever dealt a card above 100.
    op.execute("DELETE FROM cards WHERE card_no > 100")
    op.execute("ALTER TABLE cards DROP CONSTRAINT cards_card_no_check")
    op.execute("ALTER TABLE cards ADD CONSTRAINT cards_card_no_check CHECK (card_no BETWEEN 1 AND 100)")
