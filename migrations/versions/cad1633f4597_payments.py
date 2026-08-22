"""payments

Revision ID: cad1633f4597
Revises: 1c85c3d09653
Create Date: 2026-08-22 18:44:14.294734

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cad1633f4597'
down_revision: Union[str, None] = '1c85c3d09653'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAYMENT_METHOD_KINDS = ("telebirr", "cbe_birr", "cbe_account", "boa", "awash", "bank")
PAYMENT_PROVIDERS = ("chapa", "santimpay", "arifpay", "manual")
PAYMENT_STATUSES = (
    "pending", "processing", "succeeded", "failed", "cancelled",
    "review", "approved", "rejected",
)


def upgrade() -> None:
    method_kinds_sql = ", ".join(f"'{k}'" for k in PAYMENT_METHOD_KINDS)
    providers_sql = ", ".join(f"'{p}'" for p in PAYMENT_PROVIDERS)
    statuses_sql = ", ".join(f"'{s}'" for s in PAYMENT_STATUSES)

    # our_ref human-readable numbering ('DEP-2026-000123' / 'WD-2026-000045')
    # -- a dedicated sequence, separate from payments.id, so the numbering
    # scheme isn't tied to the primary key's own gaps/rollbacks story.
    op.execute("CREATE SEQUENCE payment_ref_seq START 1;")

    op.execute(
        f"""
        CREATE TABLE payment_methods (
          id           bigserial PRIMARY KEY,
          user_id      bigint NOT NULL REFERENCES users(id),
          kind         text NOT NULL CHECK (kind IN ({method_kinds_sql})),
          account_ref  text NOT NULL,
          holder_name  text NOT NULL,
          verified     boolean NOT NULL DEFAULT false,
          created_at   timestamptz NOT NULL DEFAULT now(),
          UNIQUE (user_id, kind, account_ref)
        );
        """
    )

    op.execute(
        f"""
        CREATE TABLE payments (
          id                bigserial PRIMARY KEY,
          user_id           bigint NOT NULL REFERENCES users(id),
          direction         text NOT NULL CHECK (direction IN ('in', 'out')),
          provider          text NOT NULL CHECK (provider IN ({providers_sql})),
          provider_ref      text,
          our_ref           text UNIQUE NOT NULL,
          amount            numeric(18,2) NOT NULL CHECK (amount > 0),
          fee               numeric(18,2) NOT NULL DEFAULT 0,
          status            text NOT NULL CHECK (status IN ({statuses_sql})),
          method_id         bigint REFERENCES payment_methods(id),
          ledger_txn_id     bigint REFERENCES ledger_transactions(id),
          failure_reason    text,
          raw_request       jsonb,
          raw_response      jsonb,
          created_at        timestamptz NOT NULL DEFAULT now(),
          updated_at        timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ON payments (user_id, created_at DESC);
        CREATE INDEX ON payments (status) WHERE status IN ('pending', 'processing', 'review');
        """
    )

    op.execute(
        """
        CREATE TABLE payment_events (
          id           bigserial PRIMARY KEY,
          payment_id   bigint REFERENCES payments(id),
          provider     text NOT NULL,
          event_id     text,
          signature_ok boolean NOT NULL,
          payload      jsonb NOT NULL,
          received_at  timestamptz NOT NULL DEFAULT now(),
          UNIQUE (provider, event_id)
        );
        """
    )

    # Phase 0 left ledger_transactions.payment_id FK-less because `payments`
    # didn't exist yet -- same deferred-FK pattern used for round_id once
    # `rounds` showed up in the game-tables migration.
    op.execute(
        "ALTER TABLE ledger_transactions ADD CONSTRAINT ledger_transactions_payment_id_fkey "
        "FOREIGN KEY (payment_id) REFERENCES payments(id);"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ledger_transactions DROP CONSTRAINT ledger_transactions_payment_id_fkey")
    op.execute("DROP TABLE payment_events")
    op.execute("DROP TABLE payments")
    op.execute("DROP TABLE payment_methods")
    op.execute("DROP SEQUENCE payment_ref_seq")
