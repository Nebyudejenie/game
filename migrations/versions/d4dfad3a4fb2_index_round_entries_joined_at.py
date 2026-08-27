"""index round entries joined at

Revision ID: d4dfad3a4fb2
Revises: d812e3d87349
Create Date: 2026-08-27 09:27:36.785622

A code-review pass caught that services/admin/queries.py's
repeat_room_pairings() (the Risk screen's collusion-pairing query) filters
`WHERE e1.joined_at > now() - make_interval(days => $2)` specifically to
keep the query bounded as round_entries grows -- the function's own
docstring says so -- but round_entries had no index on joined_at at all,
only PRIMARY KEY (round_id, card_no) and UNIQUE (round_id, user_id). Every
call still forced a full sequential scan of a table that grows with every
single stake ever made platform-wide, regardless of the day-window filter,
so the stated goal wasn't actually delivered.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4dfad3a4fb2'
down_revision: Union[str, None] = 'd812e3d87349'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_round_entries_joined_at ON round_entries (joined_at)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_round_entries_joined_at")
