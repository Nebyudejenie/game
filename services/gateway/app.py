"""FastAPI gateway app: the WebSocket entrypoint players connect to.

Stateless in the sense that matters for horizontal scaling -- any replica
can serve any player, because `state_sync` is served from Postgres rather
than from in-memory state pinned to a specific replica (queries.py). What is
process-local is the FanoutHub's Redis subscription and the set of
currently-open connections, both scoped to this process's lifetime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles

from packages.core import telegram_auth
from packages.core.config import get_settings
from packages.core.redis_conn import get_redis
from services.gateway import queries
from services.gateway.connection import ConnectionHandler
from services.gateway.fanout import FanoutHub

# Anchored to this file's location, not the process's cwd -- the gateway
# must serve the Mini App correctly regardless of the directory it's
# launched from.
MINIAPP_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "miniapp"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await asyncpg.create_pool(
        dsn=settings.database_url, min_size=5, max_size=50
    )
    app.state.redis = get_redis()
    app.state.bot_token = settings.telegram_bot_token
    app.state.hub = FanoutHub(app.state.redis)
    await app.state.hub.start()
    app.state.connections = set()
    try:
        yield
    finally:
        for handler in list(app.state.connections):
            await handler.close_for_shutdown()
        await app.state.hub.stop()
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    handler = ConnectionHandler(
        websocket,
        app.state.pool,
        app.state.redis,
        app.state.hub,
        app.state.bot_token,
    )
    app.state.connections.add(handler)
    try:
        await handler.run()
    finally:
        app.state.connections.discard(handler)


async def _authenticated_user_id(authorization: str = Header(default="")) -> int:
    """Validates the Telegram convention `Authorization: tma <initData>`
    header -- the REST-side equivalent of the WebSocket handshake's auth
    frame, same validate_init_data() boundary, same rules (constant-time
    hash comparison, 24h replay window).
    """
    if not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="missing tma authorization header")
    raw_init_data = authorization[len("tma ") :]
    try:
        data = telegram_auth.validate_init_data(raw_init_data, app.state.bot_token)
    except telegram_auth.InvalidInitData as exc:
        raise HTTPException(status_code=401, detail=f"invalid init data: {exc.reason}") from exc
    return await queries.get_or_create_user_by_telegram_id(
        app.state.pool, data.user.id, data.user.first_name or str(data.user.id)
    )


@app.get("/api/me")
async def api_me(authorization: str = Header(default="")) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    return await queries.user_balance_snapshot(app.state.pool, user_id)


@app.get("/api/history")
async def api_history(authorization: str = Header(default="")) -> list[dict[str, object]]:
    user_id = await _authenticated_user_id(authorization)
    return await queries.user_history(app.state.pool, user_id)


# Mounted last: FastAPI matches routes in registration order, and static
# files are served at "/" -- every /api/* and /ws route above must be
# registered first or the static mount would shadow them.
app.mount("/", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
