"""withdrawal review reason

Revision ID: 98b822eaa241
Revises: d4dfad3a4fb2
Create Date: 2026-08-27 14:23:56.002847

A code-review finding: request_withdrawal() computes auto_ok from four
independent rules (amount ceiling, account age, lifetime deposits vs
withdrawals, velocity) but never recorded *which* rule(s) actually failed
when a request landed in review -- an admin working the review queue had
to manually re-derive it by re-querying every related table by hand for
every flagged withdrawal. This is a dedicated column, not a reuse of the
existing `failure_reason` (which is consistently used only for the
terminal 'rejected'/'failed' states elsewhere in this codebase, a
different lifecycle stage than "pending review" -- conflating the two
would lose the original review reason the moment an admin later rejects
the same payment).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '98b822eaa241'
down_revision: Union[str, None] = 'd4dfad3a4fb2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE payments ADD COLUMN review_reason text")


def downgrade() -> None:
    op.execute("ALTER TABLE payments DROP COLUMN review_reason")
