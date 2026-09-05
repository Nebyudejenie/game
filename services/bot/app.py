"""Webhook app assembly.

Secret-token verification is aiogram's own `SimpleRequestHandler` (constant-
time compare, built in) -- not reimplemented here. What this module adds is
update-id deduplication as an outer middleware, so it runs before *any*
handler, for every update type, with no way for a future handler to forget
to check it (spec section 5's `seen:tg:{update_id}`, Prompt 5's own
requirement: "nothing sends a Telegram message except through [the rate-
limited] worker" -- the dedup gate is the same kind of single-choke-point
guarantee, applied to inbound instead of outbound).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from prometheus_client import generate_latest
from redis.asyncio import Redis

from packages.core.config import Settings, get_settings
from packages.core.db_pool import create_pool
from packages.core.logging import configure_logging
from packages.core.redis_conn import get_redis
from packages.core.tracing import configure_tracing
from services.bot import campaign_worker, dedup, notification_relay
from services.bot.handlers import router
from services.bot.notifier import Notifier

WEBHOOK_PATH = "/webhook"
DEFAULT_PORT = 8003


async def _dedup_middleware(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    assert isinstance(event, Update)
    redis: Redis = data["redis"]
    if not await dedup.claim_update(redis, event.update_id):
        return None
    return await handler(event, data)


def build_dispatcher(pool: asyncpg.Pool, redis: Redis, notifier: Notifier, settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(_dedup_middleware)
    dp.include_router(router)
    dp["pool"] = pool
    dp["redis"] = redis
    dp["notifier"] = notifier
    dp["settings"] = settings
    return dp


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_app(bot: Bot, dp: Dispatcher, settings: Settings) -> web.Application:
    configure_tracing("bot", settings.otel_exporter_endpoint)
    app = web.Application()

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/healthz", healthz)

    async def metrics_endpoint(_request: web.Request) -> web.Response:
        # aiohttp's Response rejects a content_type string with an embedded
        # "; charset=..." (unlike FastAPI's Response, used for this same
        # endpoint on every other service) -- it wants the media type and
        # charset as two separate arguments, so CONTENT_TYPE_LATEST can't be
        # passed through directly here the way it is elsewhere.
        return web.Response(body=generate_latest(), content_type="text/plain", charset="utf-8")

    app.router.add_get("/metrics", metrics_endpoint)

    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=settings.telegram_webhook_secret or None
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    return app


def main() -> None:
    """Real production entrypoint: registers the webhook with Telegram,
    starts the Notifier's send loop and notification_relay.run_forever()
    (services/bot/notification_relay.py's own docstring: the only other
    thing allowed to call Notifier.send(), for notifications that
    originate outside this process entirely -- sharing one Notifier
    instance here is what keeps its global rate pace and per-chat 429
    backoff enforced in exactly one place, per its own docstring, rather
    than needing a second process to coordinate with this one), and serves
    the webhook app until the container runtime sends SIGTERM/SIGINT.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    pool: asyncpg.Pool | None = None
    redis: Redis | None = None
    notifier: Notifier | None = None
    relay_task: asyncio.Task[None] | None = None
    campaign_task: asyncio.Task[None] | None = None

    async def _on_startup() -> None:
        nonlocal relay_task, campaign_task
        assert notifier is not None and pool is not None and redis is not None
        if settings.public_base_url:
            await bot.set_webhook(
                url=f"{settings.public_base_url.rstrip('/')}{WEBHOOK_PATH}",
                secret_token=settings.telegram_webhook_secret or None,
            )
        notifier.start()
        relay_task = asyncio.create_task(notification_relay.run_forever(pool, redis, notifier))
        # Notification Center campaign delivery -- same process, same
        # Notifier instance, same reasoning as relay_task above: this is
        # the one process already holding the shared rate-limited/
        # 429-backed-off outbound pipeline every Telegram message goes
        # through, campaigns reuse it rather than starting a second one.
        campaign_task = asyncio.create_task(campaign_worker.run_forever(pool, redis))

    async def _on_shutdown() -> None:
        if relay_task is not None:
            relay_task.cancel()
        if campaign_task is not None:
            campaign_task.cancel()
        assert notifier is not None and pool is not None and redis is not None
        await notifier.stop()
        if pool is not None:
            await pool.close()
        if redis is not None:
            await redis.aclose()

    async def _build() -> web.Application:
        nonlocal pool, redis, notifier
        pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=10)
        redis = get_redis()
        notifier = Notifier(bot)
        dp = build_dispatcher(pool, redis, notifier, settings)
        dp.startup.register(_on_startup)
        dp.shutdown.register(_on_shutdown)
        return build_app(bot, dp, settings)

    bot = build_bot(settings)
    web.run_app(_build(), host="0.0.0.0", port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
