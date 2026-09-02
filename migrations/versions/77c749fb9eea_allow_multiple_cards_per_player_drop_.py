"""allow multiple cards per player: drop round_entries per-user uniqueness

Revision ID: 77c749fb9eea
Revises: deeff3c6228e
Create Date: 2026-09-02 17:15:14.185491

"""
"""Second half of the multi-card-per-player schema change (see
/home/prophet/.claude/plans/graceful-snacking-quail.md's Phase 2). Ships
in the same change as the engine code that replaces this constraint's
enforcement (join()'s new max_cards_per_player check, self._entries keyed
by (user_id, card_no)) -- deliberately not standalone, since join() has
no application-level "does this user already have a card" check of its
own; this constraint was the only thing enforcing one-card-per-user, and
dropping it without the replacement code deployed at the same time would
open a real gap (unlimited cards, silent state loss).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77c749fb9eea'
down_revision: Union[str, None] = 'deeff3c6228e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PRIMARY KEY (round_id, card_no) is untouched -- a card is still sold
    # at most once per round. Only the per-user uniqueness goes.
    op.execute("ALTER TABLE round_entries DROP CONSTRAINT round_entries_round_id_user_id_key")


def downgrade() -> None:
    # Fails loudly (not silently) if real multi-card data already exists --
    # Postgres refuses to add a UNIQUE constraint over duplicate
    # (round_id, user_id) pairs, which is exactly correct: this downgrade
    # can only be safe before any round actually used more than one card
    # per player.
    op.execute(
        "ALTER TABLE round_entries ADD CONSTRAINT round_entries_round_id_user_id_key "
        "UNIQUE (round_id, user_id)"
    )
