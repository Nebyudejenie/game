"""add rooms.no_player_next_round_delay_seconds

Revision ID: 5588f494fbbe
Revises: 77c749fb9eea
Create Date: 2026-09-03 11:20:00.000000

"""
"""Supports round_engine.py's continuous, server-owned round lifecycle: a
room's round is now created proactively by the engine itself the moment it
claims the room, not lazily by the first player's take_card. When a round's
selection window closes underfilled (too few or zero players), the engine
waits this many seconds before creating the next one -- long enough that a
genuinely empty room doesn't hammer Postgres with a tight insert-void-
insert loop, short enough that the room still reads as "continuously live"
to a player who happens to arrive during that gap. DEFAULT 5 is a
placeholder judgment call: the reference video (this project's own source
of truth for other round timings) doesn't clearly show a fully-idle room
long enough to measure this one, unlike lobby_seconds/call_interval_ms/
result_seconds, which is why it's a per-room column an admin can tune
rather than a hardcoded constant.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5588f494fbbe'
down_revision: Union[str, None] = '77c749fb9eea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE rooms ADD COLUMN no_player_next_round_delay_seconds smallint "
        "NOT NULL DEFAULT 5 "
        "CHECK (no_player_next_round_delay_seconds BETWEEN 0 AND 300)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE rooms DROP COLUMN no_player_next_round_delay_seconds")
