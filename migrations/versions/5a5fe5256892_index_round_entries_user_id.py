"""index round entries user id

Revision ID: 5a5fe5256892
Revises: 6a040371439e
Create Date: 2026-08-28 16:29:16.509052

An architecture audit caught the same class of bug the prior
ix_round_entries_joined_at migration already fixed once for this table:
services/gateway/queries.py's user_history() (the bot's /history command
and the Mini App's own history tab) filters `WHERE re.user_id = $1`
against round_entries, but every index on the table leads with round_id
(PRIMARY KEY (round_id, card_no), UNIQUE (round_id, user_id), plus the
joined_at-only index) -- none supports a user_id-first lookup, so this
normal, frequent action forces a full sequential scan of a table that
grows with every single stake ever made platform-wide.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a5fe5256892'
down_revision: Union[str, None] = '6a040371439e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_round_entries_user_id ON round_entries (user_id, round_id)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_round_entries_user_id")
