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
from redis.asyncio import Redis

from packages.core import rate_limit
from packages.core.metrics import telegram_command_blocked_total, telegram_command_rate_limited_total
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


async def _reply_in_users_language(data: dict[str, Any], event: Message, key: str, **kwargs: Any) -> None:
    from services.bot.registration import get_user_and_language

    pool = data.get("pool")
    notifier = data.get("notifier")
    if pool is not None and notifier is not None:
        _, language = await get_user_and_language(pool, event.from_user.id)  # type: ignore[union-attr]
        await notifier.send(event.chat.id, t(key, language, **kwargs))


async def _check_rate_limit(
    redis: Redis, *, handler_name: str, telegram_id: int, config: CommandConfig
) -> tuple[bool, str | None, float | None]:
    """Checks cooldown first, then the per-minute rate limit -- both are
    the exact same Redis token-bucket primitive every other rate limit in
    this codebase already uses (packages/core/rate_limit.py, also backing
    services/payments/deposits.py's deposit cap and the gateway's own
    per-connection limits), just parameterized differently: a cooldown is
    a capacity-1 bucket refilling once every cooldown_seconds (so a
    second attempt before that time is up is denied outright); a rate
    limit is a capacity-N bucket refilling continuously at N/60 per
    second (a smoother, burst-tolerant version of "N per minute", not a
    hard reset-every-60-seconds window). Returns (allowed, limit_type,
    retry_after_seconds) -- limit_type/retry_after are only meaningful
    when allowed is False.

    The Redis bucket key is deliberately keyed by the real handler_name,
    not analytics_label(handler_name) -- an admin renaming a command's
    analytics_key later must never reset or fragment a player's
    already-in-progress cooldown/rate-limit state. Only the *metric
    labels* this function's caller records use the canonical analytics
    identity; the enforcement bucket itself is tied to the one thing
    that's actually stable across an admin edit, the code's own function
    name.
    """
    if config.cooldown_seconds > 0:
        allowed, retry_after = await rate_limit.allow_with_retry_after(
            redis,
            "tg-cooldown",
            f"{handler_name}:{telegram_id}",
            capacity=1,
            refill_per_second=1.0 / config.cooldown_seconds,
        )
        if not allowed:
            return False, "cooldown", retry_after

    if config.rate_limit_per_minute is not None:
        allowed, retry_after = await rate_limit.allow_with_retry_after(
            redis,
            "tg-ratelimit",
            f"{handler_name}:{telegram_id}",
            capacity=config.rate_limit_per_minute,
            refill_per_second=config.rate_limit_per_minute / 60.0,
        )
        if not allowed:
            return False, "rate_limit", retry_after

    return True, None, None


async def command_gate_middleware(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    """Registered as the first dp.message inner middleware (see
    services/bot/app.py::build_dispatcher()) -- runs before perf
    .perf_middleware, so a command an admin has disabled or rate-limited
    never reaches that middleware's own timing/success/error counters at
    all (see packages/core/metrics.py's own telegram_command_blocked_total
    docstring for why that separation is deliberate). Blocking here, before
    ever calling the real handler, is also what makes rate limiting
    inherently safe against duplicate financial effects: a denied request
    never reaches cmd_deposit/cmd_withdraw/etc. at all, so there is no
    financial code path to have run twice in the first place.

    Section 10/Section 3's own requirements: a disabled or rate-limited
    command must return a controlled, configured reply -- never a
    404/500/stack trace, and never a bare "Too many requests" when a real
    wait time is known. Reuses services/bot/registration.py::
    get_user_and_language() (an already-tested query from the Telegram
    command latency diagnosis pass) purely for these rare, non-happy-path
    replies; the common case (command enabled, under its limits -- the
    overwhelming majority of traffic) pays for at most the rate-limit
    Redis check when one is actually configured, never this extra query.
    """
    handler_obj = data.get("handler")
    handler_name = getattr(handler_obj, "callback", None)
    handler_name = getattr(handler_name, "__name__", None) if handler_name is not None else None

    if handler_name is None:
        return await handler(event, data)

    if not is_enabled(handler_name):
        telegram_command_blocked_total.labels(handler=analytics_label(handler_name)).inc()
        if isinstance(event, Message) and event.from_user is not None:
            await _reply_in_users_language(data, event, "error.command_disabled")
        return None

    config = get_config(handler_name)
    if (
        config is not None
        and (config.cooldown_seconds > 0 or config.rate_limit_per_minute is not None)
        and isinstance(event, Message)
        and event.from_user is not None
    ):
        redis = data.get("redis")
        if redis is not None:
            allowed, limit_type, retry_after = await _check_rate_limit(
                redis, handler_name=handler_name, telegram_id=event.from_user.id, config=config
            )
            if not allowed:
                assert limit_type is not None
                telegram_command_rate_limited_total.labels(
                    handler=analytics_label(handler_name), limit_type=limit_type
                ).inc()
                if retry_after is not None and retry_after > 0:
                    await _reply_in_users_language(
                        data, event, "error.rate_limited_retry", seconds=max(1, round(retry_after))
                    )
                else:
                    await _reply_in_users_language(data, event, "error.rate_limited_generic")
                return None

    return await handler(event, data)
