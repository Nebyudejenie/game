"""Keeps an in-process cache of the bot_commands admin registry in sync
with the database -- same polling-cache shape as bot_content_sync.py
(30s refresh, not push), for the same reason: a rarely-changed admin
setting, not a hot path, and the bot process shouldn't need a second
always-on subscription just for this.

A handler_name with no row in bot_commands (or a row that was never
seeded/inserted, e.g. a brand-new handler added after this registry
shipped) is treated as enabled/visible -- the registry is additive
opt-out, not opt-in, so a code deploy that adds a new command never
silently starts disabled just because nobody has registered it yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import asyncpg
import structlog
from aiogram.types import Message, TelegramObject

from packages.core.metrics import telegram_command_blocked_total
from services.bot.i18n import t

logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class CommandConfig:
    handler_name: str
    command: str | None
    display_name: str
    description: str
    category: str
    enabled: bool
    visible: bool
    sort_order: int
    cooldown_seconds: int
    rate_limit_per_minute: int | None
    content_key: str | None
    analytics_key: str | None
    admin_managed: bool


_cache: dict[str, CommandConfig] = {}


def set_cache(configs: dict[str, CommandConfig]) -> None:
    """Test/startup seam -- mirrors bot_content_sync's own set_overrides()
    shape in services/bot/i18n.py.
    """
    global _cache
    _cache = configs


def get_config(handler_name: str) -> CommandConfig | None:
    return _cache.get(handler_name)


def is_enabled(handler_name: str) -> bool:
    """No row (or no cache populated yet, e.g. this exact instant during
    startup before the first refresh_once() completes) means enabled --
    see this module's own docstring for why absence must never mean
    silently disabled.
    """
    config = _cache.get(handler_name)
    return config is None or config.enabled


def analytics_label(handler_name: str) -> str:
    """The label perf.py's metrics should use for this handler -- an
    admin-configured analytics_key overrides the raw function name only
    when explicitly set, so existing Grafana panels/alerts keyed on the
    real function name (e.g. "cmd_balance") keep working unchanged for
    every handler that never sets one.
    """
    config = _cache.get(handler_name)
    if config is not None and config.analytics_key:
        return config.analytics_key
    return handler_name


async def refresh_once(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        """
        SELECT handler_name, command, display_name, description, category, enabled, visible,
               sort_order, cooldown_seconds, rate_limit_per_minute, content_key, analytics_key,
               admin_managed
        FROM bot_commands
        """
    )
    set_cache(
        {
            row["handler_name"]: CommandConfig(
                handler_name=row["handler_name"],
                command=row["command"],
                display_name=row["display_name"],
                description=row["description"],
                category=row["category"],
                enabled=row["enabled"],
                visible=row["visible"],
                sort_order=row["sort_order"],
                cooldown_seconds=row["cooldown_seconds"],
                rate_limit_per_minute=row["rate_limit_per_minute"],
                content_key=row["content_key"],
                analytics_key=row["analytics_key"],
                admin_managed=row["admin_managed"],
            )
            for row in rows
        }
    )


async def run_forever(pool: asyncpg.Pool) -> None:
    while True:
        try:
            await refresh_once(pool)
        except Exception:
            logger.exception("command_registry_refresh_failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def command_gate_middleware(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    """Registered as the first dp.message inner middleware (see
    services/bot/app.py::build_dispatcher()) -- runs before perf
    .perf_middleware, so a command an admin has disabled never reaches
    that middleware's own timing/success/error counters at all (see
    packages/core/metrics.py's own telegram_command_blocked_total
    docstring for why that separation is deliberate).

    Section 10's own requirement: a disabled command must return a
    controlled, configured reply -- never a 404/500/stack trace. This
    reuses services/bot/registration.py::get_user_and_language() (an
    already-tested query from the Telegram command latency diagnosis
    pass) purely for this rare, admin-controlled path; the common case
    (command enabled, the overwhelming majority of traffic) never pays
    for this extra query since is_enabled() short-circuits first.
    """
    handler_obj = data.get("handler")
    handler_name = getattr(handler_obj, "callback", None)
    handler_name = getattr(handler_name, "__name__", None) if handler_name is not None else None

    if handler_name is not None and not is_enabled(handler_name):
        telegram_command_blocked_total.labels(handler=handler_name).inc()
        if isinstance(event, Message) and event.from_user is not None:
            from services.bot.registration import get_user_and_language

            pool = data.get("pool")
            notifier = data.get("notifier")
            if pool is not None and notifier is not None:
                _, language = await get_user_and_language(pool, event.from_user.id)
                await notifier.send(event.chat.id, t("error.command_disabled", language))
        return None

    return await handler(event, data)
