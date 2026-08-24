"""phone encryption

Revision ID: 1d14ec5fac7d
Revises: 35986afdada5
Create Date: 2026-08-24 18:13:00.000000

Spec section 9.2: "PII: phone numbers encrypted at rest." Replaces the
plaintext `phone_e164` column with `phone_e164_encrypted` (AES-256-GCM
ciphertext, packages/core/phone_crypto.py) and `phone_lookup_hash` (a
deterministic HMAC-SHA256 blind index carrying the UNIQUE constraint and
exact-match lookups a random-nonce ciphertext can't support on its own).

Existing rows are backfilled in place using the app's own encryption
function -- the same real code path every write from here on uses, not a
one-off re-implementation -- following this repo's own precedent
(89519947d424_cards_pool.py calls packages.core.cards_seed.seed_rows()
from inside a migration the same way).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from packages.core.phone_crypto import decrypt_phone, encrypt_phone, phone_lookup_hash

# revision identifiers, used by Alembic.
revision: str = '1d14ec5fac7d'
down_revision: Union[str, None] = '35986afdada5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN phone_e164_encrypted bytea")
    op.execute("ALTER TABLE users ADD COLUMN phone_lookup_hash text UNIQUE")

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, phone_e164 FROM users WHERE phone_e164 IS NOT NULL")).fetchall()
    for user_id, phone in rows:
        conn.execute(
            sa.text(
                "UPDATE users SET phone_e164_encrypted = :enc, phone_lookup_hash = :hsh WHERE id = :id"
            ),
            {"enc": encrypt_phone(phone), "hsh": phone_lookup_hash(phone), "id": user_id},
        )

    op.execute("DROP INDEX ix_users_phone_e164")
    op.execute("ALTER TABLE users DROP COLUMN phone_e164")


def downgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN phone_e164 text UNIQUE")

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, phone_e164_encrypted FROM users WHERE phone_e164_encrypted IS NOT NULL")
    ).fetchall()
    for user_id, blob in rows:
        conn.execute(
            sa.text("UPDATE users SET phone_e164 = :phone WHERE id = :id"),
            {"phone": decrypt_phone(bytes(blob)), "id": user_id},
        )

    op.execute("CREATE INDEX ix_users_phone_e164 ON users (phone_e164)")
    op.execute("ALTER TABLE users DROP COLUMN phone_lookup_hash")
    op.execute("ALTER TABLE users DROP COLUMN phone_e164_encrypted")
