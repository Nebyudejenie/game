"""game tables

Revision ID: 88556f01eaf8
Revises: 89519947d424
Create Date: 2026-08-22 09:23:08.192949

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '88556f01eaf8'
down_revision: Union[str, None] = '89519947d424'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROUND_STATUSES = ("lobby", "running", "settling", "done", "voided")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE rooms (
          id               bigserial PRIMARY KEY,
          code             text UNIQUE NOT NULL,
          stake            numeric(18,2) NOT NULL CHECK (stake > 0),
          house_cut_bps    int NOT NULL DEFAULT 2000
                           CHECK (house_cut_bps BETWEEN 0 AND 10000),
          min_players      int NOT NULL DEFAULT 2 CHECK (min_players >= 2),
          max_players      int NOT NULL DEFAULT 100 CHECK (max_players BETWEEN 1 AND 100),
          lobby_seconds    int NOT NULL DEFAULT 30 CHECK (lobby_seconds > 0),
          call_interval_ms int NOT NULL DEFAULT 4000 CHECK (call_interval_ms > 0),
          result_seconds   int NOT NULL DEFAULT 10 CHECK (result_seconds >= 0),
          win_patterns     jsonb NOT NULL DEFAULT '["row", "col", "diag", "corners"]',
          is_active        boolean NOT NULL DEFAULT true,
          CHECK (min_players <= max_players)
        );
        """
    )

    statuses_sql = ", ".join(f"'{s}'" for s in ROUND_STATUSES)
    op.execute(
        f"""
        CREATE TABLE rounds (
          id                bigserial PRIMARY KEY,
          room_id           bigint NOT NULL REFERENCES rooms(id),
          seq               bigint NOT NULL,
          status            text NOT NULL CHECK (status IN ({statuses_sql})),
          stake             numeric(18,2) NOT NULL,
          house_cut_bps     int NOT NULL,
          player_count      int NOT NULL DEFAULT 0,
          pot               numeric(18,2) NOT NULL DEFAULT 0,
          derash            numeric(18,2) NOT NULL DEFAULT 0,
          server_seed       bytea,
          server_seed_hash  text NOT NULL,
          client_seed       text,
          draw_order        smallint[],
          call_index        int NOT NULL DEFAULT 0,
          lobby_deadline    timestamptz,
          started_at        timestamptz,
          ended_at          timestamptz,
          UNIQUE (room_id, seq)
        );
        CREATE INDEX ix_rounds_room_status ON rounds (room_id, status);
        """
    )

    op.execute(
        """
        CREATE TABLE round_entries (
          round_id     bigint NOT NULL REFERENCES rounds(id),
          card_no      smallint NOT NULL REFERENCES cards(card_no),
          user_id      bigint NOT NULL REFERENCES users(id),
          auto_mark    boolean NOT NULL DEFAULT true,
          stake_txn_id bigint REFERENCES ledger_transactions(id),
          joined_at    timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (round_id, card_no),
          UNIQUE (round_id, user_id)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE round_winners (
          round_id      bigint NOT NULL REFERENCES rounds(id),
          user_id       bigint NOT NULL REFERENCES users(id),
          card_no       smallint NOT NULL,
          pattern       text NOT NULL,
          won_on_call   int NOT NULL,
          amount        numeric(18,2) NOT NULL,
          payout_txn_id bigint REFERENCES ledger_transactions(id),
          PRIMARY KEY (round_id, user_id)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE claim_attempts (
          id         bigserial PRIMARY KEY,
          round_id   bigint NOT NULL REFERENCES rounds(id),
          user_id    bigint NOT NULL REFERENCES users(id),
          call_index int NOT NULL,
          valid      boolean NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_claim_attempts_round_user ON claim_attempts (round_id, user_id);
        """
    )

    # Phase 0 left ledger_transactions.round_id FK-less because `rounds`
    # didn't exist yet (see DECISIONS.md). It exists now.
    op.execute(
        """
        ALTER TABLE ledger_transactions
          ADD CONSTRAINT fk_ledger_transactions_round
          FOREIGN KEY (round_id) REFERENCES rounds(id);
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ledger_transactions DROP CONSTRAINT fk_ledger_transactions_round"
    )
    op.execute("DROP TABLE claim_attempts")
    op.execute("DROP TABLE round_winners")
    op.execute("DROP TABLE round_entries")
    op.execute("DROP TABLE rounds")
    op.execute("DROP TABLE rooms")
