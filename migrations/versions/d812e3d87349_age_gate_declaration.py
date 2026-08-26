"""age gate declaration

Revision ID: d812e3d87349
Revises: 1d14ec5fac7d
Create Date: 2026-08-26 22:11:44.132939

Spec section 12: "Age gate: 18+ declaration at registration." This is the
self-declaration half only -- a plain, durable record of when a user
affirmed they're 18+, shown as part of the same registration prompt that
already asks them to share their contact (services/bot/registration.py).
It is deliberately not identity verification -- that's spec 12's separate
"ID verification at KYC level 2" clause, already tracked against the
existing kyc_level column and the admin promotion action built for it.

Nullable because it's set going forward, at the moment a user first
completes registration (services/bot/registration.py's
register_from_contact()) -- there is no real declaration to backfill for
rows that registered before this column existed, the same reasoning
1d14ec5fac7d's phone-encryption migration backfills real data but this
one doesn't invent a declaration nobody actually made.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd812e3d87349'
down_revision: Union[str, None] = '1d14ec5fac7d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN age_confirmed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN age_confirmed_at")
