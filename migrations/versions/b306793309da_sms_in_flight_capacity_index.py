"""sms in flight capacity index

Revision ID: b306793309da
Revises: fb759e477bcd
Create Date: 2026-09-07 13:00:00.000000

Production-gate audit finding (DECISIONS.md, 2026-09-07): `EXPLAIN
(ANALYZE, BUFFERS)` on `current_assigned_count()`'s own query --
`WHERE assigned_node_id = $1 AND status IN ('assigned', 'sending')` --
showed a full sequential scan with no supporting index. This query is
now called on every single fetch-job claim attempt (Phase 2's per-node
capacity gate), a genuinely hot path, unlike a purely speculative index
this same audit deliberately declined to add elsewhere for a
still-tiny, not-yet-demonstrated-hot query.

The existing `ix_sms_messages_in_flight (assigned_at) WHERE status IN
(...)` serves a different access pattern (the reconciliation sweep's
"how long has this been in flight" scan) and does not help a lookup by
`assigned_node_id`.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b306793309da'
down_revision: Union[str, None] = 'fb759e477bcd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_sms_messages_node_in_flight ON sms_messages (assigned_node_id)
          WHERE status IN ('assigned', 'sending');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_sms_messages_node_in_flight")
