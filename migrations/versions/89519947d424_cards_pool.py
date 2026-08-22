"""cards pool

Revision ID: 89519947d424
Revises: 81d041ff4513
Create Date: 2026-08-22 09:04:45.478115

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from packages.core.cards_seed import seed_rows

# revision identifiers, used by Alembic.
revision: str = '89519947d424'
down_revision: Union[str, None] = '81d041ff4513'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE cards (
          card_no  smallint PRIMARY KEY CHECK (card_no BETWEEN 1 AND 100),
          grid     jsonb NOT NULL
        );
        """
    )

    cards_table = sa.table(
        "cards",
        sa.column("card_no", sa.SmallInteger),
        sa.column("grid", sa.JSON),
    )
    rows = [{"card_no": card_no, "grid": grid} for card_no, grid in seed_rows()]
    op.bulk_insert(cards_table, rows)


def downgrade() -> None:
    op.execute("DROP TABLE cards")
