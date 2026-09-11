"""telebirr ingestion devices

Revision ID: e3a7c9f01b2d
Revises: 198d7fa10f43
Create Date: 2026-09-11 00:00:00.000000

Per-device authentication for the automated Android/MacroDroid Telebirr
ingestion path. Today's MacroDroid route (services/payments/app.py's
POST /internal/telebirr/ingest) checks one single shared static bearer
token (settings.macrodroid_ingest_token) -- correct for "does this
request come from *a* trusted MacroDroid phone", but not strong enough to
answer "*which* phone", to revoke one compromised/decommissioned phone
without breaking every other one, or to see per-phone health (last seen,
success/failure/duplicate counts) the way a real fleet of ingestion
devices needs.

This migration is purely additive and purely a device *registry* --
nothing in payment_evidence, payments, ledger_transactions, or any
existing table changes, and nothing here writes payment_evidence rows
directly (services/payments/telebirr_ingest.py's ingest_sms_evidence()
remains the one canonical function that does that, unchanged). The
existing shared-token path keeps working exactly as it does today: a
device row here is a strictly *additive*, preferred/stronger credential,
checked first, falling back to the legacy shared token so an
already-configured phone is never broken by this migration landing.

token_hash stores only a SHA-256 hex digest of a 256-bit random token
(secrets.token_urlsafe(32) -- see packages/core/device_auth.py) -- the
plaintext token itself is never persisted anywhere, shown to an admin
exactly once at creation/rotation time, the same "can't be recovered,
only rotated" discipline admin_users.password_hash already follows for
human credentials.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e3a7c9f01b2d'
down_revision: Union[str, None] = '198d7fa10f43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingestion_devices (
          id                  bigserial PRIMARY KEY,
          device_id           text UNIQUE NOT NULL,
          device_name         text NOT NULL,
          token_hash          text NOT NULL,
          status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
          created_by_admin_id bigint REFERENCES admin_users(id),
          revoked_by_admin_id bigint REFERENCES admin_users(id),
          revoked_at          timestamptz,
          created_at          timestamptz NOT NULL DEFAULT now(),
          last_seen_at        timestamptz,
          last_success_at     timestamptz,
          last_error_at       timestamptz,
          last_error_reason   text,
          success_count       bigint NOT NULL DEFAULT 0,
          duplicate_count     bigint NOT NULL DEFAULT 0,
          failure_count       bigint NOT NULL DEFAULT 0,
          auth_failure_count  bigint NOT NULL DEFAULT 0
        );
        -- The hot-path lookup on every ingestion request: hash the
        -- presented bearer token, find the device it belongs to. A
        -- unique index (not just an index) also means two devices can
        -- never accidentally be provisioned with colliding tokens.
        CREATE UNIQUE INDEX ON ingestion_devices (token_hash);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ingestion_devices")
