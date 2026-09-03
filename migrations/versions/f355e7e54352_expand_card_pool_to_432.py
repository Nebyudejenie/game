"""expand card pool to 432

Revision ID: f355e7e54352
Revises: 5588f494fbbe
Create Date: 2026-09-03 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from packages.core.cards_seed import seed_rows

# revision identifiers, used by Alembic.
revision: str = 'f355e7e54352'
down_revision: Union[str, None] = '5588f494fbbe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # generate_card_pool() draws sequentially from one seeded stream, so
    # raising packages/core/bingo.py's _POOL_SIZE from 150 to 432 is a pure
    # append -- card_no 1-150 keep the exact same grid (machine-verified by
    # tests/unit/test_bingo.py::test_card_pool_first_150_cards_are_byte_
    # identical_to_the_previous_pool), only 151-432 are new. The 150 figure
    # itself was wrong, not just small: it came from an unscrolled video
    # frame of the reference app's card-selection grid that happened to end
    # exactly at row 150 and looked complete -- a second, longer recording
    # of the same app proved the grid keeps going, scrolled to a confirmed,
    # clean end at card 432 across four independent frames. Only the new
    # rows are inserted here; seed_rows() itself is untouched.
    op.execute("ALTER TABLE cards DROP CONSTRAINT cards_card_no_check")
    op.execute("ALTER TABLE cards ADD CONSTRAINT cards_card_no_check CHECK (card_no BETWEEN 1 AND 432)")

    cards_table = sa.table(
        "cards",
        sa.column("card_no", sa.SmallInteger),
        sa.column("grid", sa.JSON),
    )
    # Pinned to the exact 151-432 range, not "> 150" -- if _POOL_SIZE ever
    # grows again past 432 in the future, this migration must keep
    # inserting exactly the 282 rows it always did on a fresh database (see
    # migration b762e8ce264b's own comment for why an unpinned range broke
    # a from-scratch migrate the first time this exact pattern shipped).
    rows = [{"card_no": card_no, "grid": grid} for card_no, grid in seed_rows() if 150 < card_no <= 432]
    op.bulk_insert(cards_table, rows)


def downgrade() -> None:
    # Safe even though this doesn't check for referencing rows itself: the
    # existing round_entries.card_no FK to cards(card_no) already blocks
    # this DELETE outright if any round ever dealt a card above 150.
    op.execute("DELETE FROM cards WHERE card_no > 150")
    op.execute("ALTER TABLE cards DROP CONSTRAINT cards_card_no_check")
    op.execute("ALTER TABLE cards ADD CONSTRAINT cards_card_no_check CHECK (card_no BETWEEN 1 AND 150)")
