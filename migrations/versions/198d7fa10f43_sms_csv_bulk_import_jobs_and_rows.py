"""sms csv bulk import jobs and rows

Revision ID: 198d7fa10f43
Revises: c0759c1fc0fc
Create Date: 2026-09-10 09:04:10.315135

"""
"""CSV bulk-SMS import: the "production go-live" directive's own explicit
instruction (Section 9/23) is to extend the existing campaign/message
model, not build a parallel one -- these two new tables are exactly the
"import job / import batch" entities that section anticipated as the
only genuinely new data this needs. Nothing else changes: a CSV-sourced
campaign still lives in sms_campaigns, its messages still land in the
one real sms_messages table, still claimed by the exact same pull
protocol, still subject to the exact same suppression/retry/dead-letter/
reconciliation machinery.

sms_import_rows keeps the *parsed, validated* rows (never the raw
uploaded file itself -- Section 23: "prefer processing and securely
discarding the raw upload"), long enough to power the operator preview
screen's error table and, permanently, as a real forensic record of
exactly what an import contained (Section 15's "message forensics"
principle applied to imports too).

sms_campaigns gains one nullable column, import_job_id, and its own
existing template_id/body_override CHECK constraint widens to also
accept an import-job-sourced campaign -- a real campaign with neither a
template nor a shared body override, because a "phone_number,message"
CSV supplies each recipient's own message directly (packages/core/sms/
campaigns.py's validate_campaign()/start_campaign() branch on this
column to resolve recipients from sms_import_rows instead of the usual
audience_filter path).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '198d7fa10f43'
down_revision: Union[str, None] = 'c0759c1fc0fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sms_import_jobs (
          id                    bigserial PRIMARY KEY,
          tenant_id             bigint NOT NULL REFERENCES sms_tenants(id),
          -- Set once /import/{id}/campaign actually creates the campaign
          -- this import feeds -- null while the operator is still on the
          -- upload/preview screen, before committing to anything.
          campaign_id           bigint REFERENCES sms_campaigns(id),
          -- 'phone_message': each row supplies its own message text (the
          -- directive's minimum required format). 'phone_only': every
          -- row is just a phone number, sent through a campaign's own
          -- template/body_override (the directive's second, template
          -- -driven format).
          format                text NOT NULL CHECK (format IN ('phone_message', 'phone_only')),
          status                text NOT NULL DEFAULT 'processing' CHECK (status IN
                                  ('processing', 'completed', 'failed')),
          original_filename     text,
          -- Client-generated once per upload attempt and re-sent on any
          -- retry (double-click, browser retry, refresh) -- the real
          -- idempotency-safety mechanism Section 11 requires. A second
          -- upload with the same key returns the already-parsed job
          -- instead of parsing (and persisting) the file again.
          idempotency_key       text NOT NULL,
          total_rows            integer NOT NULL DEFAULT 0,
          valid_rows            integer NOT NULL DEFAULT 0,
          invalid_rows          integer NOT NULL DEFAULT 0,
          duplicate_rows        integer NOT NULL DEFAULT 0,
          suppressed_rows       integer NOT NULL DEFAULT 0,
          error_summary         text,
          created_by_admin_id   bigint NOT NULL REFERENCES admin_users(id),
          created_at            timestamptz NOT NULL DEFAULT now(),
          completed_at          timestamptz,
          UNIQUE (tenant_id, idempotency_key)
        );

        CREATE TABLE sms_import_rows (
          id                bigserial PRIMARY KEY,
          import_job_id     bigint NOT NULL REFERENCES sms_import_jobs(id),
          row_number        integer NOT NULL,
          raw_phone         text NOT NULL,
          normalized_phone  text,
          -- Only ever set for format='phone_message' -- a phone_only
          -- import's rows carry no message of their own, the campaign's
          -- template/body_override supplies it at start_campaign time.
          message_body      text,
          status            text NOT NULL CHECK (status IN
                              ('valid', 'invalid', 'duplicate', 'suppressed')),
          error_reason      text,
          -- Filled in once start_campaign() actually upserts this row's
          -- phone into sms_contacts -- null until the campaign is
          -- actually started, same "resolved lazily at send time, not
          -- guessed at preview time" discipline campaigns.py's own
          -- audience-filter path already follows.
          contact_id        bigint REFERENCES sms_contacts(id),
          created_at        timestamptz NOT NULL DEFAULT now(),
          UNIQUE (import_job_id, row_number)
        );
        -- The preview screen's own error-table query: one status at a
        -- time, in row order.
        CREATE INDEX ix_sms_import_rows_job_status ON sms_import_rows (import_job_id, status, row_number);

        ALTER TABLE sms_campaigns ADD COLUMN import_job_id bigint REFERENCES sms_import_jobs(id);
        ALTER TABLE sms_campaigns DROP CONSTRAINT sms_campaigns_check;
        ALTER TABLE sms_campaigns ADD CONSTRAINT sms_campaigns_check CHECK (
          template_id IS NOT NULL OR body_override IS NOT NULL OR import_job_id IS NOT NULL
        );
        """
    )


def downgrade() -> None:
    # Fails loudly, not silently, if a real phone_message campaign
    # (import_job_id set, template_id and body_override both null)
    # exists by the time this runs -- same "only safely restores exactly
    # what this migration changed, never guesses at data created since"
    # reasoning every other narrow-range downgrade in this history
    # follows (see 8eb513a57043's own comment).
    op.execute(
        """
        ALTER TABLE sms_campaigns DROP CONSTRAINT sms_campaigns_check;
        ALTER TABLE sms_campaigns ADD CONSTRAINT sms_campaigns_check CHECK (
          template_id IS NOT NULL OR body_override IS NOT NULL
        );
        ALTER TABLE sms_campaigns DROP COLUMN import_job_id;
        DROP TABLE sms_import_rows;
        DROP TABLE sms_import_jobs;
        """
    )
