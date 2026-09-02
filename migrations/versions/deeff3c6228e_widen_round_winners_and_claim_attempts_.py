"""widen round_winners and claim_attempts for multi-card, add max_cards_per_player

Revision ID: deeff3c6228e
Revises: b762e8ce264b
Create Date: 2026-09-02 16:50:36.331339

"""
"""First half of the multi-card-per-player schema change (see
/home/prophet/.claude/plans/graceful-snacking-quail.md). Deliberately
NOT the round_entries UNIQUE(round_id, user_id) drop -- join() has no
application-level "does this user already have a card" check at all
today, that constraint is the *only* thing enforcing one-card-per-user,
so dropping it here alone (before the engine code that replaces it with
real max_cards_per_player enforcement) would open a real, if temporary,
production gap: unlimited cards per player, and self._entries's
overwrite-by-user_id would silently lose track of all but the last card
taken. That drop ships in the same change as the engine code that
replaces its enforcement (Phase 2), not standalone.

Everything in *this* migration is genuinely inert against the current
single-card engine the moment it deploys: round_winners' card_no column
already exists (just wasn't part of the key) so widening the PK is a
strict superset of the current guarantee and nothing today ever
produces a second row for one user; claim_attempts.card_no is a new
nullable column nothing reads yet; max_cards_per_player defaults to 1,
matching today's de facto limit exactly, and nothing reads this column
yet either.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'deeff3c6228e'
down_revision: Union[str, None] = 'b762e8ce264b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE round_winners DROP CONSTRAINT round_winners_pkey")
    op.execute("ALTER TABLE round_winners ADD PRIMARY KEY (round_id, user_id, card_no)")

    op.execute("ALTER TABLE claim_attempts ADD COLUMN card_no smallint REFERENCES cards(card_no)")

    op.execute(
        """
        ALTER TABLE rooms ADD COLUMN max_cards_per_player smallint NOT NULL DEFAULT 1
          CHECK (max_cards_per_player BETWEEN 1 AND 20)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE rooms DROP COLUMN max_cards_per_player")
    op.execute("ALTER TABLE claim_attempts DROP COLUMN card_no")
    op.execute("ALTER TABLE round_winners DROP CONSTRAINT round_winners_pkey")
    # Fails loudly here (not silently) if a later phase's code ever actually
    # wrote two winning cards for one user in one round -- the narrower key
    # can't represent that data, and this is exactly the case where failing
    # instead of silently dropping a row is correct.
    op.execute("ALTER TABLE round_winners ADD PRIMARY KEY (round_id, user_id)")
