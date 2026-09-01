"""two person approval

Revision ID: 21e6207342cd
Revises: 60dc29201d1c
Create Date: 2026-09-01 08:45:57.945430

Manual payment anti-fraud item deferred at the end of the manual-payment
subsystem's own Stage 1, now built with real numbers from the business:
manual deposits and withdrawals at or above settings.auto_approve_
withdraw_etb (2,000 ETB -- reused directly, not duplicated into a second
config field) need a second, different admin's approval before money
actually moves. See services/admin/queries.py's approve_manual_deposit_
admin/approve_manual_withdrawal_admin and DECISIONS.md for the full
design, including why this doesn't need a new payments.status value.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '21e6207342cd'
down_revision: Union[str, None] = '60dc29201d1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE payments ADD COLUMN first_approved_by_admin_id bigint "
               "REFERENCES admin_users(id)")
    op.execute("ALTER TABLE payments ADD COLUMN first_approved_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE payments DROP COLUMN first_approved_at")
    op.execute("ALTER TABLE payments DROP COLUMN first_approved_by_admin_id")
