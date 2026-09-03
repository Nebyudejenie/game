"""allow solo play: relax min_players to allow 1

Revision ID: 8eb513a57043
Revises: f355e7e54352
Create Date: 2026-09-03 15:40:00.000000

"""
"""A real, explicit, repeated product correction: the live round engine
must never be gated on player count beyond what settlement genuinely
requires. round_engine.py's own _run_lobby() already reads this purely as
config (`if self.player_count() >= self._room.min_players:
transition_to_running()`), with no other hardcoded threshold anywhere in
the engine -- min_players was always meant to be a per-room tunable, not
a baked-in rule. The floor of 2 was a deliberate earlier decision
(confirmed directly with the user at the time, when a manual `UPDATE
rooms SET min_players = 1` was rejected by this exact constraint), but a
later, explicit, repeated instruction reversed it: a live round must be
able to go active and pay out with a single participating player, not
just exist-but-never-play. Nothing about player_count()'s own distinct
-user counting (added earlier to stop one player's multiple cards from
impersonating multiple players) needs to change for this -- lowering the
floor to 1 doesn't reopen that abuse vector, since a real single distinct
player was always the honest case that logic was protecting, not
attacking.

Sets every existing room currently at the old default (2) down to the
new one (1) in the same migration, not just the constraint -- in
practice this is only ever the one production 'main' room today, but a
schema change that permits 1 without ever setting any real room to 1
would leave the actual live behavior unchanged, which is the whole point
of this migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8eb513a57043'
down_revision: Union[str, None] = 'f355e7e54352'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE rooms DROP CONSTRAINT rooms_min_players_check")
    op.execute("ALTER TABLE rooms ADD CONSTRAINT rooms_min_players_check CHECK (min_players >= 1)")
    op.execute("ALTER TABLE rooms ALTER COLUMN min_players SET DEFAULT 1")
    op.execute("UPDATE rooms SET min_players = 1 WHERE min_players = 2")


def downgrade() -> None:
    # Fails loudly, not silently, if any room has been reconfigured to a
    # genuinely different value in between -- same reasoning as every
    # other narrow-range downgrade in this migration history (see
    # b762e8ce264b's own comment): this can only safely restore exactly
    # what it changed, not guess at anyone else's intent since.
    op.execute("UPDATE rooms SET min_players = 2 WHERE min_players = 1")
    op.execute("ALTER TABLE rooms ALTER COLUMN min_players SET DEFAULT 2")
    op.execute("ALTER TABLE rooms DROP CONSTRAINT rooms_min_players_check")
    op.execute("ALTER TABLE rooms ADD CONSTRAINT rooms_min_players_check CHECK (min_players >= 2)")
