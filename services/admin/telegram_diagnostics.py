"""Live Telegram webhook health -- Section 41/42's "TELEGRAM HEALTH" /
"ONE-CLICK DIAGNOSTICS" ask: a real getWebhookInfo() call made fresh on
every request, not a cached/derived approximation, so a stale webhook
subscription or a growing backlog shows up the moment an admin opens this
page rather than waiting on the next scheduled poll.

Telegram's own getWebhookInfo response does not include "oldest pending
update age" -- only a raw pending_update_count. Reported honestly as such
rather than inventing an age this API cannot actually provide; the
closest real proxy (received-update rate vs. processed-command rate) is
this codebase's own telegram_updates_received_total/telegram_commands_total
counters, visible on the Grafana dashboard instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot

# Matches the Grafana dashboard's own orange/red stat thresholds
# (deploy/grafana/dashboards/jo-bingo.json) so an admin sees the exact
# same "is this normal" line everywhere it's shown.
PENDING_UPDATES_WARNING_THRESHOLD = 50
PENDING_UPDATES_CRITICAL_THRESHOLD = 200


@dataclass(frozen=True)
class WebhookHealth:
    url: str
    pending_update_count: int
    last_error_date: datetime | None
    last_error_message: str | None
    last_synchronization_error_date: datetime | None
    ip_address: str | None
    max_connections: int | None

    @property
    def status(self) -> str:
        if self.pending_update_count >= PENDING_UPDATES_CRITICAL_THRESHOLD:
            return "critical"
        if self.pending_update_count >= PENDING_UPDATES_WARNING_THRESHOLD:
            return "warning"
        return "healthy"


async def get_webhook_health(bot_token: str) -> WebhookHealth:
    """Builds a throwaway Bot purely to make this one call -- the admin
    process has no long-lived Bot/Dispatcher of its own (that's
    services/bot/app.py's job, a separate process), and a getWebhookInfo
    call is cheap and infrequent enough (an admin opening one page) that a
    persistent client isn't worth the added lifecycle to manage.
    """
    bot = Bot(token=bot_token)
    try:
        info = await bot.get_webhook_info()
    finally:
        await bot.session.close()
    return WebhookHealth(
        url=info.url,
        pending_update_count=info.pending_update_count,
        last_error_date=info.last_error_date,
        last_error_message=info.last_error_message,
        last_synchronization_error_date=info.last_synchronization_error_date,
        ip_address=info.ip_address,
        max_connections=info.max_connections,
    )
