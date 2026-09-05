"""notification center schema

Revision ID: 72cd4cae946c
Revises: b31c5f70f957
Create Date: 2026-09-05 10:00:00.000000

Real, minimal schema for the admin Notification Center. Deliberately
just three tables: campaigns own their own audience filter + schedule +
lifecycle status; templates are reusable content; deliveries are one row
per (campaign, recipient) pair, the join point audience resolution,
worker delivery, and analytics all read from. No new user/auth table --
audience always resolves against the existing `users` table, and
delivery reuses the existing bot_notifications Redis Stream + Notifier
(see packages/core/notifications.py, services/bot/notifier.py) rather
than a second queue.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '72cd4cae946c'
down_revision: Union[str, None] = 'b31c5f70f957'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE notification_templates (
          id                bigserial PRIMARY KEY,
          name              text NOT NULL UNIQUE,
          category          text NOT NULL,
          title             text NOT NULL,
          body              text NOT NULL,
          channel           text NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram')),
          is_active         boolean NOT NULL DEFAULT true,
          created_by_admin_id bigint NOT NULL REFERENCES admin_users(id),
          updated_by_admin_id bigint REFERENCES admin_users(id),
          created_at        timestamptz NOT NULL DEFAULT now(),
          updated_at        timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE notification_campaigns (
          id                  bigserial PRIMARY KEY,
          internal_name       text NOT NULL,
          title               text NOT NULL,
          body                text NOT NULL,
          channel             text NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram')),
          template_id         bigint REFERENCES notification_templates(id),
          -- Audience filter is a small, fixed JSON shape the backend
          -- itself validates and turns into a real parameterized SQL
          -- query (packages/core/campaigns.py::resolve_audience) --
          -- never client-supplied SQL, never a raw filter string.
          audience_filter     jsonb NOT NULL DEFAULT '{}',
          exclude_user_ids    bigint[] NOT NULL DEFAULT '{}',
          status              text NOT NULL DEFAULT 'draft' CHECK (status IN (
                                'draft', 'scheduled', 'queued', 'sending',
                                'completed', 'partially_failed', 'failed', 'cancelled'
                              )),
          scheduled_at        timestamptz,
          started_at          timestamptz,
          completed_at        timestamptz,
          recipient_count     integer,
          delivered_count     integer NOT NULL DEFAULT 0,
          failed_count        integer NOT NULL DEFAULT 0,
          created_by_admin_id bigint NOT NULL REFERENCES admin_users(id),
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_notification_campaigns_status ON notification_campaigns (status);
        CREATE INDEX ix_notification_campaigns_scheduled_at ON notification_campaigns (scheduled_at)
          WHERE status = 'scheduled';
        """
    )

    op.execute(
        """
        CREATE TABLE notification_deliveries (
          id            bigserial PRIMARY KEY,
          campaign_id   bigint NOT NULL REFERENCES notification_campaigns(id),
          user_id       bigint NOT NULL REFERENCES users(id),
          status        text NOT NULL DEFAULT 'pending' CHECK (status IN (
                          'pending', 'processing', 'delivered', 'failed', 'retrying', 'cancelled'
                        )),
          attempt_count smallint NOT NULL DEFAULT 0,
          failure_reason text,
          queued_at     timestamptz NOT NULL DEFAULT now(),
          last_attempt_at timestamptz,
          delivered_at  timestamptz,
          UNIQUE (campaign_id, user_id)
        );
        CREATE INDEX ix_notification_deliveries_campaign_status
          ON notification_deliveries (campaign_id, status);
        CREATE INDEX ix_notification_deliveries_pending
          ON notification_deliveries (status) WHERE status = 'pending';
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE notification_deliveries")
    op.execute("DROP TABLE notification_campaigns")
    op.execute("DROP TABLE notification_templates")
