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
from decimal import Decimal, InvalidOperation
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from packages.core import telegram_auth
from packages.core.config import get_settings
from packages.core.redis_conn import get_redis
from services.gateway import queries
from services.gateway.connection import ConnectionHandler
from services.gateway.fanout import FanoutHub
from services.payments import deposits, withdrawals
from services.payments.chapa import ChapaProvider

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
    app.state.chapa = ChapaProvider(settings.chapa_api_key) if settings.chapa_api_key else None
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


# Every DepositRejected/WithdrawalRejected subclass maps to a short error
# code the Mini App looks up its own translated message for -- the same
# "distinct exception type, not a string reason" pattern
# services/bot/handlers.py uses, just surfaced as JSON instead of a bot
# reply. Provider-side/unknown failures collapse to one generic code,
# matching the bot's own choice not to expose raw internal error text.
_DEPOSIT_ERROR_CODES: dict[type[Exception], str] = {
    deposits.BelowMinimumDeposit: "below_minimum",
    deposits.DailyDepositCapExceeded: "daily_cap_exceeded",
    deposits.DepositorSelfExcluded: "self_excluded",
    deposits.DepositorCoolingOff: "cooling_off",
}
_WITHDRAWAL_ERROR_CODES: dict[type[Exception], str] = {
    withdrawals.BelowMinimumWithdrawal: "below_minimum",
    withdrawals.InsufficientAvailableBalance: "insufficient_balance",
    withdrawals.KycLevelTooLow: "kyc_required",
    withdrawals.RecentReversibleDeposit: "recent_deposit",
}


class DepositRequest(BaseModel):
    amount: str


@app.post("/api/deposit")
async def api_create_deposit(
    body: DepositRequest, authorization: str = Header(default="")
) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()
    if app.state.chapa is None or not settings.public_base_url:
        raise HTTPException(status_code=503, detail="deposits are not available yet")

    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_amount") from None
    if amount <= 0:
        raise HTTPException(status_code=422, detail="invalid_amount")

    phone = await queries.user_phone(app.state.pool, user_id)
    if not phone:
        raise HTTPException(status_code=422, detail="phone_required")

    try:
        intent = await deposits.create_deposit_intent(
            app.state.pool,
            app.state.chapa,
            user_id=user_id,
            amount=amount,
            phone_e164=phone,
            return_url=f"{settings.public_base_url}/deposit/return",
            min_deposit=settings.min_deposit_etb,
            daily_cap=settings.daily_deposit_cap_etb,
        )
    except deposits.DepositRejected as exc:
        code = _DEPOSIT_ERROR_CODES.get(type(exc), "provider_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"checkout_url": intent.checkout_url, "our_ref": intent.our_ref}


class WithdrawRequest(BaseModel):
    amount: str
    account_ref: str
    holder_name: str


@app.post("/api/withdraw")
async def api_create_withdrawal(
    body: WithdrawRequest, authorization: str = Header(default="")
) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()
    if app.state.chapa is None:
        raise HTTPException(status_code=503, detail="withdrawals are not available yet")

    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_amount") from None
    if amount <= 0 or not body.account_ref.strip() or not body.holder_name.strip():
        raise HTTPException(status_code=422, detail="invalid_amount")

    try:
        intent = await withdrawals.request_withdrawal(
            app.state.pool,
            app.state.redis,
            app.state.chapa,
            user_id=user_id,
            amount=amount,
            method_kind=withdrawals.DEFAULT_METHOD_KIND,
            account_ref=body.account_ref,
            holder_name=body.holder_name,
            min_withdraw=settings.min_withdraw_etb,
            auto_approve_limit=settings.auto_approve_withdraw_etb,
            kyc_threshold=settings.kyc_required_above_etb,
            chargeback_window_minutes=settings.withdraw_chargeback_window_minutes,
        )
    except withdrawals.WithdrawalRejected as exc:
        code = _WITHDRAWAL_ERROR_CODES.get(type(exc), "unknown_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"status": intent.status, "our_ref": intent.our_ref}


# Mounted last: FastAPI matches routes in registration order, and static
# files are served at "/" -- every /api/* and /ws route above must be
# registered first or the static mount would shadow them.
app.mount("/", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
