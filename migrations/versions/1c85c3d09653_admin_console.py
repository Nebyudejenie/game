"""admin console

Revision ID: 1c85c3d09653
Revises: 88556f01eaf8
Create Date: 2026-08-22 18:08:45.872112

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c85c3d09653'
down_revision: Union[str, None] = '88556f01eaf8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ADMIN_ROLES = ("support", "finance", "ops", "superadmin")


def upgrade() -> None:
    roles_sql = ", ".join(f"'{r}'" for r in ADMIN_ROLES)
    op.execute(
        f"""
        CREATE TABLE admin_users (
          id             bigserial PRIMARY KEY,
          username       text UNIQUE NOT NULL,
          password_hash  text NOT NULL,
          totp_secret    text NOT NULL,
          role           text NOT NULL CHECK (role IN ({roles_sql})),
          is_active      boolean NOT NULL DEFAULT true,
          created_at     timestamptz NOT NULL DEFAULT now(),
          last_login_at  timestamptz
        );
        """
    )

    op.execute(
        """
        CREATE TABLE admin_audit_log (
          id           bigserial PRIMARY KEY,
          admin_id     bigint NOT NULL REFERENCES admin_users(id),
          action       text NOT NULL,
          target_type  text NOT NULL,
          target_id    text,
          before       jsonb,
          after        jsonb,
          reason       text,
          ip_address   text,
          created_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_admin_audit_log_admin ON admin_audit_log (admin_id, created_at DESC);
        CREATE INDEX ix_admin_audit_log_target ON admin_audit_log (target_type, target_id);
        """
    )

    # Append-only, enforced by Postgres itself, same principle as the
    # ledger's sum-to-zero trigger: an invariant this important shouldn't
    # depend on every future code path remembering to respect it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'admin_audit_log is append-only; % is not allowed', TG_OP
            USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_admin_audit_log_immutable
          BEFORE UPDATE OR DELETE ON admin_audit_log
          FOR EACH ROW EXECUTE FUNCTION prevent_audit_log_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_admin_audit_log_immutable ON admin_audit_log")
    op.execute("DROP FUNCTION prevent_audit_log_mutation")
    op.execute("DROP TABLE admin_audit_log")
    op.execute("DROP TABLE admin_users")
