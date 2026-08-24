"""responsible gaming

Revision ID: 35986afdada5
Revises: cad1633f4597
Create Date: 2026-08-24 14:15:37.094259

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '35986afdada5'
down_revision: Union[str, None] = 'cad1633f4597'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE responsible_gaming_limits (
          user_id                                bigint PRIMARY KEY REFERENCES users(id),
          daily_deposit_cap                       numeric(18,2),
          pending_daily_deposit_cap               numeric(18,2),
          pending_daily_deposit_cap_effective_at  timestamptz,
          daily_loss_cap                          numeric(18,2),
          pending_daily_loss_cap                  numeric(18,2),
          pending_daily_loss_cap_effective_at     timestamptz,
          cooloff_until                           timestamptz,
          self_excluded_until                     timestamptz,
          updated_at                              timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE responsible_gaming_limits")
