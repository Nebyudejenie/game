"""ledger foundation

Revision ID: 81d041ff4513
Revises:
Create Date: 2026-08-22 09:04:44.846147

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '81d041ff4513'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACCOUNT_KINDS = (
    "user_cash",
    "user_bonus",
    "user_locked",
    "house_revenue",
    "house_float",
    "pot_escrow",
    "provider_settlement",
    "promo_expense",
)

USER_BALANCE_KINDS = ("user_cash", "user_bonus", "user_locked")

TRANSACTION_KINDS = (
    "deposit",
    "withdrawal",
    "stake",
    "payout",
    "refund",
    "commission",
    "bonus_grant",
    "bonus_convert",
    "adjustment",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
          id              bigserial PRIMARY KEY,
          telegram_id     bigint UNIQUE NOT NULL,
          username        text,
          display_name    text NOT NULL,
          phone_e164      text UNIQUE,
          language        text NOT NULL DEFAULT 'am'
                          CHECK (language IN ('am', 'en', 'om', 'ti')),
          status          text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'limited', 'self_excluded', 'banned')),
          kyc_level       smallint NOT NULL DEFAULT 0 CHECK (kyc_level BETWEEN 0 AND 2),
          referred_by     bigint REFERENCES users(id),
          created_at      timestamptz NOT NULL DEFAULT now(),
          last_seen_at    timestamptz
        );
        CREATE INDEX ix_users_phone_e164 ON users (phone_e164);
        CREATE INDEX ix_users_referred_by ON users (referred_by);
        """
    )

    kinds_sql = ", ".join(f"'{k}'" for k in ACCOUNT_KINDS)
    op.execute(
        f"""
        CREATE TABLE accounts (
          id        bigserial PRIMARY KEY,
          user_id   bigint REFERENCES users(id),
          kind      text NOT NULL CHECK (kind IN ({kinds_sql})),
          currency  char(3) NOT NULL DEFAULT 'ETB',
          UNIQUE (user_id, kind, currency)
        );
        -- user_id is NULL for system accounts (house_revenue, pot_escrow, ...).
        -- Postgres treats NULLs as distinct in a UNIQUE constraint, so the
        -- constraint above does not stop two NULL-user_id house_revenue rows
        -- from coexisting. This partial index closes that gap: at most one
        -- system-level account per (kind, currency).
        CREATE UNIQUE INDEX ux_accounts_system_kind_currency
          ON accounts (kind, currency) WHERE user_id IS NULL;
        """
    )

    txn_kinds_sql = ", ".join(f"'{k}'" for k in TRANSACTION_KINDS)
    op.execute(
        f"""
        CREATE TABLE ledger_transactions (
          id               bigserial PRIMARY KEY,
          kind             text NOT NULL CHECK (kind IN ({txn_kinds_sql})),
          idempotency_key  text UNIQUE NOT NULL,
          round_id         bigint,
          payment_id       bigint,
          created_by       text NOT NULL,
          memo             text,
          created_at       timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE ledger_entries (
          id              bigserial PRIMARY KEY,
          transaction_id  bigint NOT NULL REFERENCES ledger_transactions(id),
          account_id      bigint NOT NULL REFERENCES accounts(id),
          amount          numeric(18,2) NOT NULL,
          created_at      timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_ledger_entries_account ON ledger_entries (account_id, id);
        CREATE INDEX ix_ledger_entries_transaction ON ledger_entries (transaction_id);
        """
    )

    # Deferrable constraint trigger: every ledger_transaction's entries must
    # sum to exactly zero by commit time. This is enforced by Postgres itself,
    # not by application code -- the whole point of a double-entry ledger is
    # that this invariant cannot be bypassed by a bug or a rogue script.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_ledger_transaction_balance() RETURNS trigger AS $$
        DECLARE
          total numeric(18,2);
        BEGIN
          SELECT COALESCE(SUM(amount), 0) INTO total
          FROM ledger_entries
          WHERE transaction_id = NEW.transaction_id;

          IF total <> 0 THEN
            RAISE EXCEPTION
              'ledger_transaction % entries sum to % (must sum to 0)',
              NEW.transaction_id, total
              USING ERRCODE = '23514';
          END IF;

          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_ledger_entries_balance
          AFTER INSERT ON ledger_entries
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW
          EXECUTE FUNCTION check_ledger_transaction_balance();
        """
    )

    user_balance_kinds_sql = ", ".join(f"'{k}'" for k in USER_BALANCE_KINDS)
    op.execute(
        f"""
        CREATE TABLE account_balances (
          account_id    bigint PRIMARY KEY REFERENCES accounts(id),
          -- Denormalized from accounts.kind at row creation (immutable
          -- thereafter) so a real CHECK constraint can enforce non-negative
          -- balances for player-facing accounts without a cross-table lookup.
          kind          text NOT NULL,
          balance       numeric(18,2) NOT NULL DEFAULT 0,
          last_entry_id bigint,
          CONSTRAINT chk_nonneg_user_balance
            CHECK (kind NOT IN ({user_balance_kinds_sql}) OR balance >= 0)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE account_balances")
    op.execute("DROP TRIGGER trg_ledger_entries_balance ON ledger_entries")
    op.execute("DROP FUNCTION check_ledger_transaction_balance")
    op.execute("DROP TABLE ledger_entries")
    op.execute("DROP TABLE ledger_transactions")
    op.execute("DROP TABLE accounts")
    op.execute("DROP TABLE users")
