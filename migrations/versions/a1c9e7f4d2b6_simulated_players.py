"""simulated players

Revision ID: a1c9e7f4d2b6
Revises: e3a7c9f01b2d
Create Date: 2026-09-16 08:00:00.000000

Admin-controlled bot accounts (up to 10) that join real rooms and play
through the real game engine so a room doesn't feel empty during early
launch. Bots stake/win through the exact same user_cash -> pot_escrow ->
user_cash/house_revenue flow real players use -- pot_escrow/house_revenue
are singleton, platform-wide accounts (see 81d041ff4513_ledger_foundation
.py), and round_engine.py's staking/settlement code hardcodes those three
kinds with no parameter to redirect elsewhere, so a separate sim_*
escrow/account-kind set would mean round_engine.py splitting one shared
pot across two accounts while still needing one correct total for derash
math -- a real double-accounting/leak risk in the exact mixed-room
scenario this feature exists to create. Isolation from genuine customer
money instead comes from users.is_simulated (this migration) plus
explicit exclusions added to services/admin/queries.py's dashboard/
cohort queries -- no ledger schema change needed at all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c9e7f4d2b6'
down_revision: Union[str, None] = 'e3a7c9f01b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUSES = ("disabled", "idle", "joining", "playing", "paused")
STRATEGIES = ("conservative", "normal", "active", "randomized")
SCHEDULE_MODES = ("always_on", "scheduled_window", "room_specific")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN is_simulated boolean NOT NULL DEFAULT false"
    )

    statuses_sql = ", ".join(f"'{s}'" for s in STATUSES)
    strategies_sql = ", ".join(f"'{s}'" for s in STRATEGIES)
    schedule_modes_sql = ", ".join(f"'{s}'" for s in SCHEDULE_MODES)

    # One row per bot, keyed by user_id like responsible_gaming_limits
    # (35986afdada5_responsible_gaming.py) -- a bot IS a users row, this
    # is only the admin-owned config/status half of it. No games_played/
    # games_won/cards_purchased columns: this codebase already computes
    # per-player stats on demand from round_entries/round_winners
    # (services/gateway/queries.py::user_history(), services/admin/
    # queries.py::player_ltv()) rather than caching them, and with at
    # most 10 bots that's cheap -- a runner-updated counter column would
    # just be a second, driftable source of truth for a number Postgres
    # already answers exactly.
    op.execute(
        f"""
        CREATE TABLE simulated_players (
          user_id                       bigint PRIMARY KEY REFERENCES users(id),
          status                        text NOT NULL DEFAULT 'disabled'
                                        CHECK (status IN ({statuses_sql})),
          strategy                      text NOT NULL DEFAULT 'normal'
                                        CHECK (strategy IN ({strategies_sql})),
          join_probability_pct          smallint NOT NULL DEFAULT 70
                                        CHECK (join_probability_pct BETWEEN 0 AND 100),
          max_cards_per_join            smallint NOT NULL DEFAULT 1
                                        CHECK (max_cards_per_join BETWEEN 1 AND 4),
          schedule_mode                 text NOT NULL DEFAULT 'always_on'
                                        CHECK (schedule_mode IN ({schedule_modes_sql})),
          schedule_window_start_minute  smallint CHECK (schedule_window_start_minute BETWEEN 0 AND 1439),
          schedule_window_end_minute    smallint CHECK (schedule_window_end_minute BETWEEN 0 AND 1439),
          pinned_room_id                bigint REFERENCES rooms(id),
          current_room_id               bigint REFERENCES rooms(id),
          last_activity_at              timestamptz,
          created_by_admin_id           bigint NOT NULL REFERENCES admin_users(id),
          created_at                    timestamptz NOT NULL DEFAULT now(),
          activated_at                  timestamptz,
          updated_at                    timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    # A true singleton (id is always 1), same shape family as
    # payment_provider_availability (60dc29201d1c_manual_payments.py) --
    # enabled defaults to false, which is the schema-level guarantee that
    # nothing auto-activates the moment this migration/deploy completes:
    # the bot-runner's very first poll sees enabled=false and does
    # nothing, independent of any individual bot's own status.
    op.execute(
        """
        CREATE TABLE simulated_players_settings (
          id                   smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
          enabled              boolean NOT NULL DEFAULT false,
          max_concurrent_bots  smallint NOT NULL DEFAULT 10 CHECK (max_concurrent_bots BETWEEN 0 AND 10),
          updated_by_admin_id  bigint REFERENCES admin_users(id),
          updated_at           timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("INSERT INTO simulated_players_settings (id) VALUES (1)")


def downgrade() -> None:
    op.execute("DROP TABLE simulated_players_settings")
    op.execute("DROP TABLE simulated_players")
    op.execute("ALTER TABLE users DROP COLUMN is_simulated")
