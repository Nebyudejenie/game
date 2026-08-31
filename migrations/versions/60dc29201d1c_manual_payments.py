"""manual payments

Revision ID: 60dc29201d1c
Revises: 5a5fe5256892
Create Date: 2026-08-31 09:58:57.623297

A P1/launch-critical product directive: Jo Bingo must keep taking deposits
and paying out withdrawals even when Chapa (today's only rail) is down,
not yet approved for a market, or simply not configured. This is the
schema half of that -- see services/payments/manual.py,
services/admin/queries.py's manual_* functions, and DECISIONS.md for the
full design, including the state-machine mapping and the two-person-
approval scope decision.

payments.provider already allows 'manual' (see cad1633f4597_payments.py)
with zero code ever writing it -- confirmed via a full-repo grep before
this migration was written. No ledger_transactions.kind migration is
needed either: deposit/withdrawal/payout/refund already cover the manual
rail's economic events exactly the way they cover Chapa's.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '60dc29201d1c'
down_revision: Union[str, None] = '5a5fe5256892'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Same vocabulary as payment_methods.kind (cad1633f4597_payments.py) --
# these are company-owned receiving accounts for manual deposits, a
# different concept from payment_methods' player-owned payout
# destinations, but the same set of real-world rails either can use.
METHOD_KINDS = ("telebirr", "cbe_birr", "cbe_account", "boa", "awash", "bank")
PROVIDERS = ("chapa", "santimpay", "arifpay", "manual")


def upgrade() -> None:
    method_kinds_sql = ", ".join(f"'{k}'" for k in METHOD_KINDS)
    providers_sql = ", ".join(f"'{p}'" for p in PROVIDERS)

    # Deposit-only by design: a manual WITHDRAWAL pays out to the
    # player's own payment_methods row (already captured today), never
    # to a company destination -- there is no "withdrawal_enabled" flag
    # here because nothing in the manual-withdrawal flow ever selects a
    # company destination row.
    op.execute(
        f"""
        CREATE TABLE manual_payment_destinations (
          id                  bigserial PRIMARY KEY,
          method_kind         text NOT NULL CHECK (method_kind IN ({method_kinds_sql})),
          account_ref         text NOT NULL,
          account_name        text NOT NULL,
          instructions        text,
          is_active           boolean NOT NULL DEFAULT true,
          created_by_admin_id bigint REFERENCES admin_users(id),
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    # Seeded with real rows below (not left for an admin to configure
    # post-deploy) so this migration can never silently break the
    # existing, already-live Chapa flow the moment it runs.
    op.execute(
        f"""
        CREATE TABLE payment_provider_availability (
          provider            text NOT NULL CHECK (provider IN ({providers_sql})),
          direction            text NOT NULL CHECK (direction IN ('in', 'out')),
          enabled              boolean NOT NULL DEFAULT false,
          updated_by_admin_id  bigint REFERENCES admin_users(id),
          updated_at           timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (provider, direction)
        );
        """
    )
    op.execute(
        """
        INSERT INTO payment_provider_availability (provider, direction, enabled) VALUES
          ('chapa', 'in', true), ('chapa', 'out', true),
          ('manual', 'in', true), ('manual', 'out', true),
          ('santimpay', 'in', false), ('santimpay', 'out', false),
          ('arifpay', 'in', false), ('arifpay', 'out', false);
        """
    )

    # provider_ref is REUSED (no new column) for the player-typed /
    # admin-typed external transaction reference on both directions --
    # it already means "the external system's reference for this
    # payment"; only the writer changes (the player at creation time for
    # a deposit, the admin at settlement time for a withdrawal).
    op.execute("ALTER TABLE payments ADD COLUMN manual_destination_id bigint "
               "REFERENCES manual_payment_destinations(id)")
    op.execute("ALTER TABLE payments ADD COLUMN receipt_telegram_file_id text")
    op.execute(
        "CREATE INDEX ON payments (manual_destination_id) WHERE manual_destination_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE payments DROP COLUMN receipt_telegram_file_id")
    op.execute("ALTER TABLE payments DROP COLUMN manual_destination_id")
    op.execute("DROP TABLE payment_provider_availability")
    op.execute("DROP TABLE manual_payment_destinations")
