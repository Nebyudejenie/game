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
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from packages.core import telegram_auth
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.ledger import user_balance_snapshot
from packages.core.redis_conn import get_redis
from services.admin.queries import get_round_fairness
from services.gateway import queries
from services.gateway.connection import ConnectionHandler
from services.gateway.fanout import FanoutHub
from services.payments import availability, deposits, manual, withdrawals
from services.payments.chapa import ChapaProvider
from services.payments.manual_provider import ManualProvider

# Anchored to this file's location, not the process's cwd -- the gateway
# must serve the Mini App correctly regardless of the directory it's
# launched from.
MINIAPP_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "miniapp"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await create_pool(dsn=settings.database_url, min_size=5, max_size=50)
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


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
    return await user_balance_snapshot(app.state.pool, user_id)


@app.get("/api/history")
async def api_history(authorization: str = Header(default="")) -> list[dict[str, object]]:
    user_id = await _authenticated_user_id(authorization)
    return await queries.user_history(app.state.pool, user_id)


@app.get("/api/rounds/{round_id}/fairness")
async def api_round_fairness(
    round_id: int, authorization: str = Header(default="")
) -> dict[str, Any]:
    """Spec section 14's definition of done: "a player can independently
    verify any round's draw from the published seed." Reuses
    services.admin.queries.get_round_fairness() directly -- the same
    server_seed/hash/draw_order/verified data an admin sees, since none of
    it is sensitive once a round is terminal (that's the entire point of a
    commit-reveal provably-fair scheme: it's meant to be publishable).
    Requires a valid session only to keep this off the open internet, not
    because the data itself is restricted to any particular player or
    round they were in.
    """
    await _authenticated_user_id(authorization)
    fairness = await get_round_fairness(app.state.pool, round_id)
    if fairness is None:
        raise HTTPException(status_code=404, detail="round not found")
    return fairness


@app.get("/api/manual-payment-destinations")
async def api_manual_payment_destinations(
    authorization: str = Header(default=""),
) -> list[dict[str, Any]]:
    await _authenticated_user_id(authorization)
    return await queries.list_active_manual_payment_destinations(app.state.pool)


@app.get("/api/payment-methods")
async def api_payment_methods(authorization: str = Header(default="")) -> dict[str, list[str]]:
    await _authenticated_user_id(authorization)
    return await availability.get_payment_availability(app.state.pool, get_settings())


# Every DepositRejected/WithdrawalRejected subclass maps to a short error
# code the Mini App looks up its own translated message for -- the same
# "distinct exception type, not a string reason" pattern
# services/bot/handlers.py uses, just surfaced as JSON instead of a bot
# reply. Provider-side/unknown failures collapse to one generic code,
# matching the bot's own choice not to expose raw internal error text.
_DEPOSIT_ERROR_CODES: dict[type[Exception], str] = {
    deposits.DepositRateLimited: "rate_limited",
    deposits.BelowMinimumDeposit: "below_minimum",
    deposits.DailyDepositCapExceeded: "daily_cap_exceeded",
    deposits.DepositorSelfExcluded: "self_excluded",
    deposits.DepositorCoolingOff: "cooling_off",
    manual.UnknownManualDestination: "no_manual_destination",
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
    # A code-review pass caught that this only ever checked static
    # process-startup config (app.state.chapa/miniapp_url/
    # payments_public_base_url), never the admin's own live
    # payment_provider_availability toggle -- GET /api/payment-methods
    # and the bot's /deposit both already gate on
    # availability.get_payment_availability() (the documented single
    # source of truth), but this endpoint, the one that actually moves
    # money, didn't. An admin disabling Chapa deposits (a compromised
    # merchant account, a provider outage) had no effect here: the UI
    # hid the button, but a client that already had the page open (or
    # any direct POST) could still complete a "disabled" deposit.
    # get_payment_availability() already folds in every static
    # reachability check this used to do ad hoc (chapa_api_key,
    # miniapp_url, payments_public_base_url -- see its own
    # chapa_deposit_configured), so this single call replaces the old
    # check rather than adding a second one to drift from it.
    methods = await availability.get_payment_availability(app.state.pool, settings)
    if "chapa" not in methods["deposit"]:
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
            app.state.redis,
            app.state.chapa,
            user_id=user_id,
            amount=amount,
            phone_e164=phone,
            return_url=settings.miniapp_url,
            callback_url=f"{settings.payments_public_base_url}/webhooks/chapa",
            min_deposit=settings.min_deposit_etb,
            daily_cap=settings.daily_deposit_cap_etb,
        )
    except deposits.DepositRejected as exc:
        code = _DEPOSIT_ERROR_CODES.get(type(exc), "provider_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"checkout_url": intent.checkout_url, "our_ref": intent.our_ref}


class ManualDepositRequest(BaseModel):
    amount: str
    manual_destination_id: int
    external_reference: str


@app.post("/api/deposit/manual")
async def api_create_manual_deposit(
    body: ManualDepositRequest, authorization: str = Header(default="")
) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()

    # Same gap as api_create_deposit's: an admin's live availability
    # toggle was only ever enforced by the UI hiding the button, never by
    # this endpoint, the one that actually creates the review-queue row.
    methods = await availability.get_payment_availability(app.state.pool, settings)
    if "manual" not in methods["deposit"]:
        raise HTTPException(status_code=503, detail="deposits are not available yet")

    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_amount") from None
    if amount <= 0:
        raise HTTPException(status_code=422, detail="invalid_amount")
    if not body.external_reference.strip():
        raise HTTPException(status_code=422, detail="external_reference_required")

    try:
        intent = await manual.create_manual_deposit_request(
            app.state.pool,
            app.state.redis,
            user_id=user_id,
            amount=amount,
            manual_destination_id=body.manual_destination_id,
            external_reference=body.external_reference,
            receipt_telegram_file_id=None,
            min_deposit=settings.min_deposit_etb,
            daily_cap=settings.daily_deposit_cap_etb,
        )
    except deposits.DepositRejected as exc:
        code = _DEPOSIT_ERROR_CODES.get(type(exc), "provider_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"status": "review", "our_ref": intent.our_ref}


class WithdrawRequest(BaseModel):
    amount: str
    account_ref: str
    holder_name: str
    provider: str = "chapa"


@app.post("/api/withdraw")
async def api_create_withdrawal(
    body: WithdrawRequest, authorization: str = Header(default="")
) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()

    if body.provider not in ("chapa", "manual"):
        raise HTTPException(status_code=422, detail="unknown_provider")
    # Same gap as api_create_deposit's, for both rails: this used to only
    # check chapa's own static config, with no check at all for manual --
    # an admin's live availability toggle had no effect on either.
    methods = await availability.get_payment_availability(app.state.pool, settings)
    if body.provider not in methods["withdraw"]:
        raise HTTPException(status_code=503, detail="withdrawals are not available yet")

    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_amount") from None
    if amount <= 0 or not body.account_ref.strip() or not body.holder_name.strip():
        raise HTTPException(status_code=422, detail="invalid_amount")

    provider = ManualProvider() if body.provider == "manual" else app.state.chapa

    try:
        intent = await withdrawals.request_withdrawal(
            app.state.pool,
            app.state.redis,
            provider,
            user_id=user_id,
            amount=amount,
            method_kind=withdrawals.DEFAULT_METHOD_KIND,
            account_ref=body.account_ref,
            holder_name=body.holder_name,
            min_withdraw=settings.min_withdraw_etb,
            auto_approve_limit=settings.auto_approve_withdraw_etb,
            kyc_threshold=settings.kyc_required_above_etb,
            chargeback_window_minutes=settings.withdraw_chargeback_window_minutes,
            max_withdrawals_per_day=settings.max_withdrawals_per_day,
            force_review=(body.provider == "manual"),
        )
    except withdrawals.WithdrawalRejected as exc:
        code = _WITHDRAWAL_ERROR_CODES.get(type(exc), "unknown_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"status": intent.status, "our_ref": intent.our_ref}


# Mounted last: FastAPI matches routes in registration order, and static
# files are served at "/" -- every /api/* and /ws route above must be
# registered first or the static mount would shadow them.
app.mount("/", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
