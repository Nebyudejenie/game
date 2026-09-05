"""bot content overrides

Revision ID: 56f336e2dd66
Revises: 72cd4cae946c
Create Date: 2026-09-05 19:09:22.510315

Lets an admin override a player-facing bot string (services/bot/i18n.py's
t()) without a code deploy -- the main menu button labels shown in the
Telegram reply keyboard (services/bot/keyboards.py's main_menu_keyboard)
being the motivating case, but every i18n key is covered the same way,
not just those. One row per (key, language) actually overridden; every
key/language not present here still resolves to the shipped default in
services/bot/locales/*.json (services/bot/i18n.py's own fallback chain),
so this table starts and can stay completely empty with zero behavior
change.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '56f336e2dd66'
down_revision: Union[str, None] = '72cd4cae946c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE bot_i18n_overrides (
          id                   bigserial PRIMARY KEY,
          key                  text NOT NULL,
          language             text NOT NULL CHECK (language IN ('am', 'en', 'om', 'ti')),
          value                text NOT NULL,
          updated_by_admin_id  bigint NOT NULL REFERENCES admin_users(id),
          updated_at           timestamptz NOT NULL DEFAULT now(),
          UNIQUE (key, language)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE bot_i18n_overrides")
