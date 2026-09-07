"""sms control plane core

Revision ID: a2ac063da449
Revises: 232a259a3baa
Create Date: 2026-09-07 11:13:28.432047

Enterprise SMS Control Plane (see DECISIONS.md 2026-09-07 for the scoping
rationale): a tenant-scoped campaign/message/delivery-node domain, built
as a genuinely separate product sharing this platform's existing admin
auth/RBAC/audit infrastructure rather than a parallel one.

tenant_id is a real column on every table, enforced by every query this
migration's own application code writes -- but exactly one tenant is
seeded below (the business itself). There is no tenant self-service
provisioning in this pass; see DECISIONS.md for why that's an honest,
explicit deferral rather than a partially-built feature.

Node credentials are never stored raw -- only a sha256 hex digest
(token_hash), the same "shown once, never retrievable again" discipline
services/admin/auth.py already uses for a TOTP secret.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a2ac063da449'
down_revision: Union[str, None] = '232a259a3baa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sms_tenants (
          id            bigserial PRIMARY KEY,
          slug          text NOT NULL UNIQUE,
          name          text NOT NULL,
          status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
          created_at    timestamptz NOT NULL DEFAULT now()
        );

        INSERT INTO sms_tenants (slug, name) VALUES ('default', 'Arada Bingo');

        CREATE TABLE sms_contacts (
          id                bigserial PRIMARY KEY,
          tenant_id         bigint NOT NULL REFERENCES sms_tenants(id),
          phone_e164        text NOT NULL,
          display_name      text,
          attributes        jsonb NOT NULL DEFAULT '{}',
          opted_out         boolean NOT NULL DEFAULT false,
          opted_out_at      timestamptz,
          created_at        timestamptz NOT NULL DEFAULT now(),
          updated_at        timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, phone_e164)
        );

        -- Multi-level suppression (platform precedence handled in app code
        -- by simply checking this table first, before any campaign/contact
        -- level rule) -- a phone in here is never dispatched to, full stop.
        CREATE TABLE sms_suppressions (
          id                    bigserial PRIMARY KEY,
          tenant_id             bigint NOT NULL REFERENCES sms_tenants(id),
          phone_e164            text NOT NULL,
          reason                text NOT NULL DEFAULT 'manual' CHECK (reason IN
                                  ('opt_out', 'manual', 'bounce', 'complaint')),
          note                  text,
          created_by_admin_id   bigint REFERENCES admin_users(id),
          created_at            timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, phone_e164)
        );

        CREATE TABLE sms_templates (
          id                    bigserial PRIMARY KEY,
          tenant_id             bigint NOT NULL REFERENCES sms_tenants(id),
          name                  text NOT NULL,
          body                  text NOT NULL,
          variables             text[] NOT NULL DEFAULT '{}',
          is_active             boolean NOT NULL DEFAULT true,
          created_by_admin_id   bigint REFERENCES admin_users(id),
          created_at            timestamptz NOT NULL DEFAULT now(),
          updated_at            timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, name)
        );

        CREATE TABLE sms_campaigns (
          id                    bigserial PRIMARY KEY,
          tenant_id             bigint NOT NULL REFERENCES sms_tenants(id),
          name                  text NOT NULL,
          template_id           bigint REFERENCES sms_templates(id),
          body_override         text,
          status                text NOT NULL DEFAULT 'draft' CHECK (status IN (
                                  'draft', 'validating', 'ready', 'scheduled', 'running',
                                  'paused', 'completing', 'completed', 'completed_with_errors',
                                  'cancelled', 'failed')),
          -- A fixed, backend-validated JSON shape (packages/core/sms/audience.py)
          -- -- a client never supplies raw SQL or a filter string, matching
          -- packages/core/campaigns.py's own existing precedent exactly.
          audience_filter       jsonb NOT NULL DEFAULT '{}',
          recipient_count       integer,
          scheduled_at          timestamptz,
          started_at            timestamptz,
          completed_at          timestamptz,
          created_by_admin_id   bigint NOT NULL REFERENCES admin_users(id),
          created_at            timestamptz NOT NULL DEFAULT now(),
          updated_at            timestamptz NOT NULL DEFAULT now(),
          CHECK (template_id IS NOT NULL OR body_override IS NOT NULL)
        );

        -- The campaign state machine's own transition audit trail -- distinct
        -- from admin_audit_log (services/admin/audit.py), which records WHO
        -- changed WHAT admin-configurable setting; this instead records the
        -- campaign's own lifecycle, most of which is system-driven (e.g. a
        -- reconciliation sweep moving running -> completed), not an admin
        -- action, so it doesn't belong in that generic table.
        CREATE TABLE sms_campaign_events (
          id            bigserial PRIMARY KEY,
          campaign_id   bigint NOT NULL REFERENCES sms_campaigns(id),
          from_status   text,
          to_status     text NOT NULL,
          admin_id      bigint REFERENCES admin_users(id),
          reason        text,
          created_at    timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_sms_campaign_events_campaign ON sms_campaign_events (campaign_id, created_at);

        CREATE TABLE sms_delivery_nodes (
          id                    bigserial PRIMARY KEY,
          tenant_id             bigint NOT NULL REFERENCES sms_tenants(id),
          name                  text NOT NULL,
          -- Anticipates future geographic/policy routing (DECISIONS.md) --
          -- a plain grouping column today, not yet read by any routing
          -- logic.
          fleet_group           text NOT NULL DEFAULT 'default',
          token_hash            text NOT NULL UNIQUE,
          status                text NOT NULL DEFAULT 'pending' CHECK (status IN (
                                  'pending', 'active', 'disabled', 'draining', 'revoked')),
          capabilities          jsonb NOT NULL DEFAULT '{}',
          app_version           text,
          last_heartbeat_at     timestamptz,
          health_score          integer NOT NULL DEFAULT 0 CHECK (health_score BETWEEN 0 AND 100),
          created_by_admin_id   bigint REFERENCES admin_users(id),
          created_at            timestamptz NOT NULL DEFAULT now(),
          updated_at            timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, name)
        );

        CREATE TABLE sms_messages (
          id                    bigserial PRIMARY KEY,
          tenant_id             bigint NOT NULL REFERENCES sms_tenants(id),
          campaign_id           bigint REFERENCES sms_campaigns(id),
          contact_id            bigint REFERENCES sms_contacts(id),
          phone_e164            text NOT NULL,
          body                  text NOT NULL,
          segment_count         integer NOT NULL DEFAULT 1,
          priority              text NOT NULL DEFAULT 'normal' CHECK (priority IN
                                  ('critical', 'high', 'normal', 'low')),
          status                text NOT NULL DEFAULT 'queued' CHECK (status IN (
                                  'queued', 'assigned', 'sending', 'delivered', 'failed',
                                  'unknown', 'dead_letter', 'suppressed', 'cancelled')),
          assigned_node_id      bigint REFERENCES sms_delivery_nodes(id),
          assigned_at           timestamptz,
          attempt_count         integer NOT NULL DEFAULT 0,
          last_error_class      text CHECK (last_error_class IS NULL OR last_error_class IN
                                  ('temporary', 'permanent', 'network', 'timeout',
                                   'node_failure', 'unknown')),
          -- The idempotent-dispatch-intent identity (directive: "idempotent
          -- delivery intent, explicit unknown execution handling, never a
          -- pretense of perfect exactly-once external delivery"). Campaign
          -- messages use f"campaign:{campaign_id}:{contact_id}" so a re-run
          -- of the same enqueue step can never create a second message for
          -- the same recipient.
          idempotency_key       text NOT NULL UNIQUE,
          created_at            timestamptz NOT NULL DEFAULT now(),
          updated_at            timestamptz NOT NULL DEFAULT now()
        );
        -- The claim query's own index: SKIP LOCKED over exactly this shape.
        CREATE INDEX ix_sms_messages_queue ON sms_messages (tenant_id, priority, created_at)
          WHERE status = 'queued';
        CREATE INDEX ix_sms_messages_campaign ON sms_messages (campaign_id);
        -- The reconciliation sweep's own index: find in-flight messages
        -- that have gone quiet.
        CREATE INDEX ix_sms_messages_in_flight ON sms_messages (assigned_at)
          WHERE status IN ('assigned', 'sending');

        CREATE TABLE sms_delivery_attempts (
          id                        bigserial PRIMARY KEY,
          message_id                bigint NOT NULL REFERENCES sms_messages(id),
          node_id                   bigint NOT NULL REFERENCES sms_delivery_nodes(id),
          attempt_number            integer NOT NULL,
          outcome                   text NOT NULL CHECK (outcome IN
                                      ('accepted', 'started', 'delivered', 'failed', 'unknown')),
          error_class               text CHECK (error_class IS NULL OR error_class IN
                                      ('temporary', 'permanent', 'network', 'timeout',
                                       'node_failure', 'unknown')),
          raw_provider_response     text,
          started_at                timestamptz,
          completed_at              timestamptz,
          created_at                timestamptz NOT NULL DEFAULT now(),
          UNIQUE (message_id, attempt_number)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE sms_delivery_attempts;
        DROP TABLE sms_messages;
        DROP TABLE sms_delivery_nodes;
        DROP TABLE sms_campaign_events;
        DROP TABLE sms_campaigns;
        DROP TABLE sms_templates;
        DROP TABLE sms_suppressions;
        DROP TABLE sms_contacts;
        DROP TABLE sms_tenants;
        """
    )
