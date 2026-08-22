"""Payments API: the one inbound HTTP surface a payment provider's server
reaches over the public internet (spec section 8.1-8.2). Deposit *creation*
is a plain Python call from services/bot/handlers.py, the same way the bot
already reads/writes the ledger directly for /balance and /history -- only
the webhook, which genuinely originates outside this process, needs to be
a real route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request, Response

from packages.core.config import get_settings
from packages.core.redis_conn import get_redis
from services.payments import deposits
from services.payments.chapa import ChapaProvider
from services.payments.provider import InvalidSignature


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=2, max_size=20)
    app.state.redis = get_redis()
    app.state.chapa = ChapaProvider(settings.chapa_api_key)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan, title="Jo Bingo Payments API")


@app.post("/webhooks/chapa")
async def chapa_webhook(request: Request) -> Response:
    raw_body = await request.body()
    headers = dict(request.headers)
    try:
        outcome = await deposits.handle_webhook(
            app.state.pool, app.state.redis, app.state.chapa, headers=headers, raw_body=raw_body
        )
    except InvalidSignature:
        # Chapa's own docs: discard and do not process further. No detail
        # in the response body -- an attacker probing signature checks
        # doesn't get to learn which part of their forgery was wrong.
        return Response(status_code=401)
    return Response(status_code=200, content=outcome)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}
