"""bot command registry

Revision ID: 232a259a3baa
Revises: 2e5e67f7b227
Create Date: 2026-09-07 00:00:00.000000

Telegram Command Center (Phase 2): a single, authoritative, admin-visible
registry of every real Telegram command handler in services/bot/handlers
.py -- one row per handler function, identified by that function's own
real name (handler_name), never a duplicated/invented identifier. This
table stores *configuration only* (enabled/visible/sort_order/cooldown/
rate limit/display metadata) -- it never stores executable code; the
application (services/bot/handlers.py) continues to own every handler's
actual behavior. A command whose handler_name has no row here still runs
exactly as before (services/bot/command_registry.py's own default-enabled
fallback), so this table can start empty with zero behavior change,
matching the same "safe to ship empty" discipline as bot_i18n_overrides.

Seeded with all 18 real handlers this migration ships alongside (see
docs/BOT_COMMAND_CATALOG.md) so the registry is never "empty and
therefore invisible" in the admin UI on day one.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '232a259a3baa'
down_revision: Union[str, None] = '2e5e67f7b227'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE bot_commands (
          id                      bigserial PRIMARY KEY,
          handler_name            text NOT NULL UNIQUE,
          command                 text,
          display_name            text NOT NULL,
          description             text NOT NULL DEFAULT '',
          category                text NOT NULL DEFAULT 'general',
          enabled                 boolean NOT NULL DEFAULT true,
          visible                 boolean NOT NULL DEFAULT true,
          sort_order              integer NOT NULL DEFAULT 0,
          cooldown_seconds        integer NOT NULL DEFAULT 0 CHECK (cooldown_seconds >= 0),
          rate_limit_per_minute   integer CHECK (rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0),
          content_key             text,
          analytics_key           text,
          -- A small, deliberate exception to "everything is admin-
          -- manageable": /start and the contact-share handler are the
          -- registration entry point itself -- disabling either would
          -- lock every new player out of the platform with no recovery
          -- path except a code deploy, a materially different risk class
          -- than disabling e.g. /invite. admin_managed=false means the
          -- enable/disable toggle is hidden for that row in the admin UI
          -- (services/admin/queries.py enforces this server-side too,
          -- not just a hidden button), not that the row itself can't be
          -- viewed or have its description/content edited.
          admin_managed           boolean NOT NULL DEFAULT true,
          updated_by_admin_id     bigint REFERENCES admin_users(id),
          updated_at              timestamptz NOT NULL DEFAULT now(),
          created_at              timestamptz NOT NULL DEFAULT now()
        );

        INSERT INTO bot_commands (handler_name, command, display_name, description, category, sort_order, admin_managed, content_key) VALUES
          ('cmd_start', 'start', 'Start', 'Registration entry point and returning-user welcome', 'core', 10, false, 'welcome.back'),
          ('on_contact', NULL, 'Contact registration', 'Completes registration from a shared phone number', 'core', 20, false, NULL),
          ('cmd_play', 'play', 'Play', 'Sends the Mini App launch keyboard', 'core', 30, true, 'play.open'),
          ('cmd_balance', 'balance', 'Balance', 'Reports cash/bonus/locked balance', 'wallet', 40, true, 'balance.summary'),
          ('cmd_history', 'history', 'History', 'Last 10 finished rounds', 'wallet', 50, true, 'history.header'),
          ('cmd_invite', 'invite', 'Invite', 'Referral link and referred-user count', 'referral', 60, true, 'invite.summary'),
          ('cmd_rules', 'rules', 'Rules', 'Static Bingo rules text', 'info', 70, true, 'rules.text'),
          ('cmd_support', 'support', 'Support', 'Static support contact info', 'info', 80, true, 'support.info'),
          ('cmd_deposit', 'deposit', 'Deposit', 'Starts a Chapa or manual deposit', 'wallet', 90, true, NULL),
          ('cmd_withdraw', 'withdraw', 'Withdraw', 'Starts a withdrawal', 'wallet', 100, true, NULL),
          ('cmd_limits', 'limits', 'Limits', 'Responsible-gaming deposit/loss limits, cool-off, self-exclusion', 'responsible_gaming', 110, true, NULL),
          ('cmd_language', 'language', 'Language', 'Sets the player''s language', 'info', 120, true, NULL),
          ('cmd_change_username', 'change_username', 'Change username', 'Sets the player''s display name', 'info', 130, true, NULL),
          ('on_photo', NULL, 'Receipt photo', 'Attaches a receipt image to the latest pending manual deposit', 'wallet', 140, true, NULL),
          ('on_agent_portal_command', 'portal', 'Agent portal', 'Mints a one-time Agent Portal login link (active payment agents only)', 'agent', 150, true, NULL),
          ('on_agent_sms', NULL, 'Agent SMS ingest', 'Forwards Telebirr SMS text into the ingestion pipeline (active payment agents only)', 'agent', 160, true, NULL),
          ('on_menu_text', NULL, 'Menu button dispatch', 'Dispatches a localized reply-keyboard button press to its matching command', 'core', 170, false, NULL),
          ('on_unhandled_message', NULL, 'Unhandled catch-all', 'Silent drop (registered) / registration prompt (unregistered)', 'core', 180, false, NULL)
        ;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE bot_commands")
