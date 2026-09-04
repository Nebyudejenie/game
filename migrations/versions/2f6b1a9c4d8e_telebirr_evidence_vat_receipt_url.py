"""telebirr evidence vat and receipt url

Revision ID: 2f6b1a9c4d8e
Revises: 9c1f4d7a2b3e
Create Date: 2026-09-04 14:00:00.000000

A real "money transferred" Telebirr SMS sample (the CTO directive,
2026-09-04) proved a second confirmed template exists alongside "money
received" -- it carries a service fee (already had a column), a VAT-on-
fee amount, and a receipt URL, none of which the first migration
anticipated. Inert like 9c1f4d7a2b3e: both new columns are nullable, no
code path writes them until this same change's telebirr_parser.py/
telebirr_ingest.py updates land in the same commit.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f6b1a9c4d8e'
down_revision: Union[str, None] = '9c1f4d7a2b3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE payment_evidence ADD COLUMN vat numeric(18,2)")
    op.execute("ALTER TABLE payment_evidence ADD COLUMN receipt_url text")
    # "received" vs "transferred" (CTO directive section 13's "SMS
    # direction check") -- which phone the SMS landed on, not who the
    # money went to (recipient_name/recipient_phone already capture
    # that regardless of direction). Nullable for the same reason
    # amount/payer_name etc. already are: a rejected-at-parse-time row
    # (no reference extractable at all) is never persisted in the first
    # place, but a historical backfill scenario shouldn't be blocked by
    # a NOT NULL default this migration can't safely guess.
    op.execute(
        "ALTER TABLE payment_evidence ADD COLUMN direction text "
        "CHECK (direction IN ('received', 'transferred'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE payment_evidence DROP COLUMN direction")
    op.execute("ALTER TABLE payment_evidence DROP COLUMN receipt_url")
    op.execute("ALTER TABLE payment_evidence DROP COLUMN vat")
