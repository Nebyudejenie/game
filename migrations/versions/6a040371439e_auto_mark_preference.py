"""auto mark preference

Revision ID: 6a040371439e
Revises: 98b822eaa241
Create Date: 2026-08-27 22:27:56.628689

Mini App spec (idea.md line 5268): "AUTO toggle: on = server marks and
auto-claims. Off = the player taps cells and taps BINGO. Persist the choice
per user." `round_entries.auto_mark` already exists but is per-round only --
`take_card` in services/gateway/connection.py hardcoded `auto_mark: True` on
every join, so a player who turned AUTO off was silently reset to AUTO on
in their very next round. This column is the per-user default that
`take_card` now reads and `set_auto` now writes.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a040371439e'
down_revision: Union[str, None] = '98b822eaa241'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN auto_mark_preference boolean NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN auto_mark_preference")
