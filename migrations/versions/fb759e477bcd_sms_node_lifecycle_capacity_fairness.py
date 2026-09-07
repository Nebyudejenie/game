"""sms node lifecycle, capacity, fairness

Revision ID: fb759e477bcd
Revises: a2ac063da449
Create Date: 2026-09-07 12:00:00.000000

Phase 2 slice 1 (see DECISIONS.md, 2026-09-07 "Phase 2" entry): node
lifecycle maturity, per-node capacity control, node-group campaign
eligibility, and cross-campaign fairness at claim time -- all additive,
zero changes to any existing column's meaning.

- sms_delivery_nodes.status gains 'maintenance' (admin-settable, distinct
  from 'disabled' for operator clarity -- "temporarily out for hardware
  work" reads differently in an incident than "an admin turned this off
  and hasn't said why"). DEGRADED/OFFLINE are deliberately NOT stored
  states: they are computed at read time from health_score/heartbeat
  recency (packages/core/sms/nodes.py::display_status()), so there is
  never a second source of truth to keep in sync with the real signals
  that already exist.
- max_concurrent_jobs: a node advertises (via heartbeat) how much work it
  can actually hold at once; fetch-job now refuses to hand out more than
  this many concurrently-assigned jobs to one node. Defaults to 1 (today's
  de facto behavior for every already-registered node, so this is a
  behavior-preserving default, not a silent capacity change).
- protocol_version: the node protocol version a device advertises --
  stored so a future rolling-fleet-upgrade decision has real data to work
  from; nothing enforces a minimum version yet (see DECISIONS.md).
- sms_campaigns.required_fleet_group: NULL means "any node may deliver
  this campaign" (today's only behavior) -- set, it restricts claiming to
  nodes in that exact fleet_group, a real, minimal node-group routing
  eligibility mechanism.
- sms_delivery_attempts.routing_snapshot: a real, queryable record of why
  a claim happened the way it did (node's fleet_group/health_score/
  concurrent-job-count at claim time, the campaign's required_fleet_group,
  its fair-share sequence number) -- routing-decision forensics, not
  reconstructed after the fact from other tables.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'fb759e477bcd'
down_revision: Union[str, None] = 'a2ac063da449'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sms_delivery_nodes DROP CONSTRAINT sms_delivery_nodes_status_check;
        ALTER TABLE sms_delivery_nodes ADD CONSTRAINT sms_delivery_nodes_status_check
          CHECK (status IN ('pending', 'active', 'maintenance', 'disabled', 'draining', 'revoked'));

        ALTER TABLE sms_delivery_nodes
          ADD COLUMN max_concurrent_jobs integer NOT NULL DEFAULT 1 CHECK (max_concurrent_jobs > 0),
          ADD COLUMN protocol_version integer NOT NULL DEFAULT 1;

        ALTER TABLE sms_campaigns ADD COLUMN required_fleet_group text;

        ALTER TABLE sms_delivery_attempts ADD COLUMN routing_snapshot jsonb;

        -- The fair-share claim query's own ROW_NUMBER() OVER (PARTITION BY
        -- campaign_id ORDER BY created_at) needs this to stay cheap: it
        -- must rank a campaign's *entire* message history (not just its
        -- still-queued rows -- see DECISIONS.md for why a queued-only
        -- ranking silently defeats fairness), so it can no longer lean on
        -- the existing partial ix_sms_messages_queue index alone.
        CREATE INDEX ix_sms_messages_campaign_created ON sms_messages (campaign_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX ix_sms_messages_campaign_created;
        ALTER TABLE sms_delivery_attempts DROP COLUMN routing_snapshot;
        ALTER TABLE sms_campaigns DROP COLUMN required_fleet_group;
        ALTER TABLE sms_delivery_nodes DROP COLUMN protocol_version;
        ALTER TABLE sms_delivery_nodes DROP COLUMN max_concurrent_jobs;

        ALTER TABLE sms_delivery_nodes DROP CONSTRAINT sms_delivery_nodes_status_check;
        ALTER TABLE sms_delivery_nodes ADD CONSTRAINT sms_delivery_nodes_status_check
          CHECK (status IN ('pending', 'active', 'disabled', 'draining', 'revoked'));
        """
    )
