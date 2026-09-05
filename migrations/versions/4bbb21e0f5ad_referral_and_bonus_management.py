"""referral and bonus management

Revision ID: 4bbb21e0f5ad
Revises: 56f336e2dd66
Create Date: 2026-09-05 21:04:49.997324

Referral *attribution* already existed (users.referred_by, the bot's own
/start ref_{id} deep link); this is the first migration that lets
anything actually pay a reward. No changes to accounts/ledger_transactions
/ledger_entries/account_balances -- every ledger primitive this needs
(the user_bonus non-withdrawable account kind, the promo_expense system
account, the bonus_grant/bonus_convert transaction kinds) was already
declared in the ledger-foundation migration and never used until now.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4bbb21e0f5ad'
down_revision: Union[str, None] = '56f336e2dd66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE bonus_rules (
          id                      bigserial PRIMARY KEY,
          name                    text NOT NULL,
          trigger_type            text NOT NULL CHECK (trigger_type IN
                                    ('referral_reward', 'welcome_bonus', 'deposit_match', 'manual_grant')),
          reward_type             text NOT NULL CHECK (reward_type IN ('flat', 'percentage')),
          reward_amount           numeric(18,2),
          reward_percentage       numeric(5,2),
          reward_cap              numeric(18,2),
          min_qualifying_deposit  numeric(18,2) NOT NULL DEFAULT 0,
          wagering_multiplier     numeric(5,2) NOT NULL DEFAULT 3,
          expiry_days             integer,
          max_grants_per_user     integer NOT NULL DEFAULT 1,
          is_active               boolean NOT NULL DEFAULT true,
          starts_at               timestamptz,
          ends_at                 timestamptz,
          created_by_admin_id     bigint NOT NULL REFERENCES admin_users(id),
          updated_by_admin_id     bigint REFERENCES admin_users(id),
          created_at              timestamptz NOT NULL DEFAULT now(),
          updated_at              timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT chk_bonus_rules_reward_shape CHECK (
            (reward_type = 'flat' AND reward_amount IS NOT NULL)
            OR (reward_type = 'percentage' AND reward_percentage IS NOT NULL)
          )
        );
        """
    )

    op.execute(
        """
        CREATE TABLE bonuses (
          id                      bigserial PRIMARY KEY,
          user_id                 bigint NOT NULL REFERENCES users(id),
          rule_id                 bigint REFERENCES bonus_rules(id),
          referral_of_user_id     bigint REFERENCES users(id),
          amount                  numeric(18,2) NOT NULL,
          wagering_required       numeric(18,2) NOT NULL DEFAULT 0,
          wagering_progress       numeric(18,2) NOT NULL DEFAULT 0,
          status                  text NOT NULL DEFAULT 'active' CHECK (status IN
                                    ('active', 'converted', 'expired', 'revoked')),
          grant_txn_id            bigint REFERENCES ledger_transactions(id),
          convert_txn_id          bigint REFERENCES ledger_transactions(id),
          expires_at              timestamptz,
          converted_at            timestamptz,
          granted_by_admin_id     bigint REFERENCES admin_users(id),
          reason                  text,
          created_at              timestamptz NOT NULL DEFAULT now(),
          updated_at              timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_bonuses_user_status ON bonuses (user_id, status);
        CREATE INDEX ix_bonuses_active ON bonuses (id) WHERE status = 'active';
        -- A referee can only ever trigger one referral_reward grant --
        -- enforced at the database level, not just in application logic,
        -- so a retried or raced credit attempt is a no-op rather than a
        -- double payout.
        CREATE UNIQUE INDEX ux_bonuses_referral_once ON bonuses (referral_of_user_id)
          WHERE referral_of_user_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE bonuses")
    op.execute("DROP TABLE bonus_rules")
