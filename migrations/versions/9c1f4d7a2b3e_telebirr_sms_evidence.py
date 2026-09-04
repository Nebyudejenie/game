"""telebirr sms evidence

Revision ID: 9c1f4d7a2b3e
Revises: 8eb513a57043
Create Date: 2026-09-04 09:30:00.000000

Schema-only phase for the Telebirr SMS-evidence deposit rail (CTO directive,
2026-09-04): a player pays into a company Telebirr account, Telebirr's own
confirmation SMS is the only real evidence a payment happened, and a phone
running MacroDroid (or a trusted Telegram "payment agent") forwards that SMS
to us. This migration is deliberately inert -- nothing in production code
writes 'telebirr_sms' as a payments.provider value yet, payment_evidence has
no reader/writer yet, and manual_payment_destinations' two new columns are
both nullable with NULL meaning "no change from today's always-valid
behavior" -- so the entire existing test suite must pass completely
unmodified against this migration. See services/payments/telebirr_*.py
(later phases) for the code that actually uses any of this.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c1f4d7a2b3e'
down_revision: Union[str, None] = '8eb513a57043'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PROVIDERS = ("chapa", "santimpay", "arifpay", "manual", "telebirr_sms")


def upgrade() -> None:
    providers_sql = ", ".join(f"'{p}'" for p in PROVIDERS)

    # Widen both provider enums together -- payments.provider and
    # payment_provider_availability.provider have always listed the exact
    # same rail set (cad1633f4597_payments.py / 60dc29201d1c_manual_
    # payments.py), so a rail that's missing from one but not the other is
    # itself a bug this migration must not introduce.
    op.execute("ALTER TABLE payments DROP CONSTRAINT payments_provider_check")
    op.execute(f"ALTER TABLE payments ADD CONSTRAINT payments_provider_check "
               f"CHECK (provider IN ({providers_sql}))")
    op.execute("ALTER TABLE payment_provider_availability "
               "DROP CONSTRAINT payment_provider_availability_provider_check")
    op.execute("ALTER TABLE payment_provider_availability ADD CONSTRAINT "
               f"payment_provider_availability_provider_check CHECK (provider IN ({providers_sql}))")

    # Off by default (section 111: feature flag) -- exactly how 'manual'
    # itself was seeded in 60dc29201d1c. Redemption code (a later phase)
    # must check this before accepting any redemption, the same gate
    # services/payments/availability.py already exposes for every rail.
    op.execute(
        "INSERT INTO payment_provider_availability (provider, direction, enabled) "
        "VALUES ('telebirr_sms', 'in', false)"
    )

    # Reused as the recipient-identity config (spec section 92) instead of
    # a second table -- manual_payment_destinations already models "an
    # account of ours that receives money"; NULL on both new columns means
    # "always valid," so every existing row's behavior is unchanged until
    # an admin explicitly sets one (a later phase's admin UI work).
    op.execute("ALTER TABLE manual_payment_destinations ADD COLUMN effective_from timestamptz")
    op.execute("ALTER TABLE manual_payment_destinations ADD COLUMN effective_until timestamptz")

    op.execute(
        """
        CREATE TABLE payment_evidence (
          id                     bigserial PRIMARY KEY,
          source                 text NOT NULL CHECK (source IN ('macrodroid', 'telegram_agent')),
          source_ref             text NOT NULL,
          raw_sms                text NOT NULL,
          evidence_hash          text NOT NULL,
          external_reference     text NOT NULL,
          raw_reference          text NOT NULL,
          amount                 numeric(18,2),
          fee                    numeric(18,2),
          payer_name             text,
          payer_phone            text,
          recipient_name         text,
          recipient_phone        text,
          transaction_at         timestamptz,
          received_at            timestamptz NOT NULL DEFAULT now(),
          status                 text NOT NULL CHECK (status IN
                                    ('available', 'redeemed', 'blocked', 'disputed', 'expired', 'rejected')),
          reject_reason          text,
          parser_version         text NOT NULL,
          redeemed_by_user_id    bigint REFERENCES users(id),
          redeemed_at            timestamptz,
          payment_id             bigint REFERENCES payments(id),
          created_at             timestamptz NOT NULL DEFAULT now(),
          updated_at             timestamptz NOT NULL DEFAULT now(),
          -- Canonical identity (section 93): a real Telebirr reference can
          -- never legitimately appear twice. evidence_hash is the extra
          -- duplicate guard -- a byte-identical resubmission (retry, dup
          -- forward) is caught even before reference parsing runs.
          UNIQUE (external_reference),
          UNIQUE (evidence_hash)
        );
        -- Redemption's own hot path (WHERE external_reference = $1 FOR
        -- UPDATE) already hits the UNIQUE index above; this one is for the
        -- admin/reconciliation "list everything still AVAILABLE" query
        -- (a later phase), which the unique index on external_reference
        -- alone doesn't serve efficiently.
        CREATE INDEX ON payment_evidence (status) WHERE status = 'available';
        CREATE INDEX ON payment_evidence (redeemed_by_user_id) WHERE redeemed_by_user_id IS NOT NULL;
        """
    )

    op.execute(
        """
        CREATE TABLE payment_agents (
          id                  bigserial PRIMARY KEY,
          telegram_user_id    bigint UNIQUE NOT NULL,
          display_name        text,
          is_active           boolean NOT NULL DEFAULT true,
          created_by_admin_id bigint REFERENCES admin_users(id),
          created_at          timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE payment_agents")
    op.execute("DROP TABLE payment_evidence")
    op.execute("ALTER TABLE manual_payment_destinations DROP COLUMN effective_until")
    op.execute("ALTER TABLE manual_payment_destinations DROP COLUMN effective_from")
    op.execute("DELETE FROM payment_provider_availability WHERE provider = 'telebirr_sms'")

    old_providers_sql = ", ".join(f"'{p}'" for p in PROVIDERS[:-1])
    op.execute("ALTER TABLE payment_provider_availability "
               "DROP CONSTRAINT payment_provider_availability_provider_check")
    op.execute("ALTER TABLE payment_provider_availability ADD CONSTRAINT "
               f"payment_provider_availability_provider_check CHECK (provider IN ({old_providers_sql}))")
    op.execute("ALTER TABLE payments DROP CONSTRAINT payments_provider_check")
    op.execute(f"ALTER TABLE payments ADD CONSTRAINT payments_provider_check "
               f"CHECK (provider IN ({old_providers_sql}))")
