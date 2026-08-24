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

from packages.core.config import Settings
from packages.core.tracing import configure_tracing
from services.bot import dedup
from services.bot.handlers import router
from services.bot.notifier import Notifier

WEBHOOK_PATH = "/webhook"


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
