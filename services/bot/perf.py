"""Real per-command latency instrumentation (Telegram command latency
diagnosis pass).

Every hook here is attached at exactly one place -- an asyncpg query
logger and a Redis.execute_command() override, both wired once in
services/bot/app.py -- so no call site anywhere in services/bot/handlers.py
changes at all. A single contextvars.ContextVar carries "which command is
currently executing" from perf_middleware down through whatever DB/Redis
calls that command's own handler makes; contextvars snapshot per asyncio
task, and aiogram processes each webhook delivery in its own task, so
concurrent commands never cross-attribute each other's query time.
"""

from __future__ import annotations

import contextvars
import time
from typing import Any, Awaitable, Callable

import asyncpg
from aiogram.types import TelegramObject
from redis.asyncio import Redis

from packages.core.metrics import (
    telegram_command_error_total,
    telegram_command_latency_seconds,
    telegram_command_success_total,
    telegram_commands_total,
    telegram_db_duration_seconds,
    telegram_dispatch_delay_seconds,
    telegram_redis_duration_seconds,
)

# The key perf_middleware reads out of aiogram's own per-update `data`
# dict -- stashed by _dedup_middleware (services/bot/app.py) at the
# earliest point this process sees the update, immediately after webhook
# receipt. Not a metrics.py constant: this is purely an internal handoff
# between two middlewares in this one module/file pair, never read or
# observed anywhere else.
RECEIVED_AT_KEY = "_perf_received_at"

_current_command: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_command", default=None
)


def _on_query(record: asyncpg.connection.LoggedQuery) -> None:
    command = _current_command.get()
    if command is None:
        # A query made outside any instrumented command -- startup
        # (bot_content_sync.refresh_once), a background sweep
        # (bot_content_sync.run_forever, campaign_worker.run_forever), or
        # notification_relay processing a notification that originated
        # outside the bot process entirely. Real DB time, just not
        # attributable to a specific Telegram command -- left
        # unrecorded here rather than mislabeled.
        return
    telegram_db_duration_seconds.labels(handler=command).observe(record.elapsed)


async def init_connection_for_query_timing(conn: asyncpg.Connection) -> None:
    """asyncpg's own create_pool(init=...) hook -- called once per new
    physical connection, not per acquire(), so this registers exactly one
    logger per connection for that connection's whole lifetime.
    """
    conn.add_query_logger(_on_query)


class TimedRedis(Redis):
    """Every redis-py command funnels through execute_command() -- one
    override times all of them, the same reasoning as asyncpg's query
    logger above, with no change needed to any of the handful of direct
    Redis calls this bot's handlers/referral.py/dedup.py make.
    """

    async def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        try:
            return await super().execute_command(*args, **kwargs)  # type: ignore[no-untyped-call]  # untyped in redis-py itself
        finally:
            command = _current_command.get()
            if command is not None:
                telegram_redis_duration_seconds.labels(handler=command).observe(
                    time.monotonic() - start
                )


async def perf_middleware(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    """Registered as an INNER middleware on dp.message (see
    build_dispatcher() in app.py) -- aiogram sets data["handler"] to the
    matched HandlerObject before running any inner middleware for that
    observer (confirmed by reading this project's installed aiogram
    version's own TelegramEventObserver.trigger(), not assumed), so this
    only ever runs once routing has already picked one specific handler
    function, and .callback.__name__ names that real function
    (cmd_balance, on_menu_text, ...) -- never an invented category label.

    This measures *handler execution time* -- from the moment this
    specific handler starts to the moment it returns or raises. Dispatch
    delay (dedup + routing/filter-check overhead before this point) is
    recorded separately, from RECEIVED_AT_KEY.
    """
    command = handler.__name__ if hasattr(handler, "__name__") else "unknown"
    handler_obj = data.get("handler")
    if handler_obj is not None and hasattr(handler_obj, "callback"):
        command = getattr(handler_obj.callback, "__name__", command)

    received_at = data.get(RECEIVED_AT_KEY)
    start = time.monotonic()
    if received_at is not None:
        telegram_dispatch_delay_seconds.observe(start - received_at)

    telegram_commands_total.labels(handler=command).inc()
    token = _current_command.set(command)
    try:
        result = await handler(event, data)
    except Exception:
        telegram_command_error_total.labels(handler=command).inc()
        raise
    else:
        telegram_command_success_total.labels(handler=command).inc()
        return result
    finally:
        telegram_command_latency_seconds.labels(handler=command).observe(time.monotonic() - start)
        _current_command.reset(token)
