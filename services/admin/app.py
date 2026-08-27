"""Admin console API. Separate authentication from players entirely (spec
section 33): username + password + TOTP, session tokens in Redis, RBAC
enforced on every mutating route, every mutation audit-logged. No route in
this file ever writes a balance directly -- adjust_balance goes through
the ledger like any other money movement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

ADMIN_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "admin"

from packages.core.config import get_settings
from packages.core.redis_conn import get_redis
from services.admin import auth, queries
from services.admin.auth import AdminSession
from services.admin.rbac import has_permission


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=2, max_size=20)
    app.state.redis = get_redis()
    app.state.ip_allowlist = [
        ip.strip() for ip in settings.admin_ip_allowlist.split(",") if ip.strip()
    ]
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan, title="Jo Bingo Admin API")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_ip_allowlist(request: Request) -> None:
    allowlist = app.state.ip_allowlist
    if not allowlist:
        return
    if _client_ip(request) not in allowlist:
        raise HTTPException(status_code=403, detail="source IP not permitted")


# Routes with no Depends() of their own to run the allowlist check
# through, so it's enforced here as middleware instead. A code-review
# pass that actually enumerated app.routes (not just the routes anyone
# had written by hand) found /docs, /redoc, and /openapi.json here too --
# FastAPI adds these automatically, so they'd never show up in a search
# for a hand-written route missing the check the way /metrics and
# /auth/login did in earlier passes, but they leak this real-money
# panel's entire API surface (every route, every request/response
# field) to anyone on the network regardless of the allowlist,
# confirmed live: with a real allowlist configured, /dashboard and
# /metrics correctly 403 an excluded IP while /docs/openapi.json/redoc
# still returned 200.
_UNAUTHENTICATED_DOC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


@app.middleware("http")
async def _unauthenticated_route_ip_allowlist(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # The frontend mounted below at /console is plain StaticFiles, which
    # (unlike every API route) can't run a Depends(current_admin) IP
    # check either -- and every other unauthenticated route in this file
    # (/auth/login, /metrics) already learned the hard way, via a real
    # code review finding, that "no bearer token yet" is not license to
    # skip the allowlist spec section 9.2 asks the whole admin panel to
    # have.
    path = request.url.path
    if (path.startswith("/console") or path in _UNAUTHENTICATED_DOC_PATHS) and app.state.ip_allowlist:
        if _client_ip(request) not in app.state.ip_allowlist:
            return Response(status_code=403, content="source IP not permitted")
    return await call_next(request)


def _require_reason(reason: str) -> None:
    """Every financially-consequential admin action needs an accountable
    reason on the record (spec: "no hidden god mode") -- a code review
    pass caught this exact check copy-pasted across four routes, and,
    more importantly, silently *missing* from a fifth (approve_
    withdrawal): the one route that had a required `reason: str` field
    on its own request model but never actually enforced it, letting an
    empty string through to become a `None` reason on real-money-release
    audit log entry. Every sibling route (reject, void, adjust, set
    -status) already required one; nothing about approving a withdrawal
    is less consequential than rejecting one.
    """
    if not reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")


async def current_admin(
    request: Request, authorization: str = Header(default="")
) -> AdminSession:
    _check_ip_allowlist(request)
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer session token")
    token = authorization[len("Bearer ") :]
    session = await auth.resolve_session(app.state.redis, token)
    if session is None:
        raise HTTPException(status_code=401, detail="session expired or invalid")
    return session


def require(permission: str) -> Any:
    async def _dependency(admin: Annotated[AdminSession, Depends(current_admin)]) -> AdminSession:
        if not has_permission(admin.role, permission):
            raise HTTPException(status_code=403, detail=f"role {admin.role!r} lacks {permission!r}")
        return admin

    return _dependency


# --- auth ------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str


@app.post("/auth/login")
async def login(request: Request, body: LoginRequest) -> dict[str, str]:
    # A code review pass caught that every other route enforces the IP
    # allowlist either via current_admin() (the session dependency almost
    # every route uses) or, for the one unauthenticated exception besides
    # this route (/metrics), by calling this directly -- but login() takes
    # no bearer token yet (that's the whole point: it's how one is
    # obtained), so it never went through either path. This is actually
    # the single most exposed route to check it on: an attacker outside
    # the allowlist could otherwise still throw password/TOTP guesses at
    # it even though every other admin route was already unreachable to
    # them.
    _check_ip_allowlist(request)
    try:
        token = await auth.login(
            app.state.pool,
            app.state.redis,
            username=body.username,
            password=body.password,
            totp_code=body.totp_code,
        )
    except auth.LoginRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except auth.LoginFailed as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"token": token}


@app.post("/auth/logout")
async def logout(
    admin: Annotated[AdminSession, Depends(current_admin)],
    authorization: str = Header(default=""),
) -> dict[str, str]:
    # current_admin already validated this is a real "Bearer <token>"
    # header; re-slice it here to get the actual token to delete.
    token = authorization[len("Bearer ") :]
    await auth.logout(app.state.redis, token)
    return {"status": "ok"}


# --- dashboard ---------------------------------------------------------


@app.get("/dashboard")
async def dashboard(admin: Annotated[AdminSession, Depends(require("dashboard:view"))]) -> dict[str, Any]:
    return await queries.dashboard_summary(app.state.pool)


# --- users -------------------------------------------------------------


@app.get("/users")
async def search_users(
    admin: Annotated[AdminSession, Depends(require("users:view"))], q: str
) -> list[dict[str, Any]]:
    return await queries.search_users(app.state.pool, q)


@app.get("/users/{user_id}")
async def get_user(
    admin: Annotated[AdminSession, Depends(require("users:view"))], user_id: int
) -> dict[str, Any]:
    detail = await queries.get_user_detail(app.state.pool, user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="user not found")
    return detail


@app.get("/users/{user_id}/ledger")
async def get_user_ledger(
    admin: Annotated[AdminSession, Depends(require("users:view"))], user_id: int
) -> list[dict[str, Any]]:
    return await queries.get_user_ledger_history(app.state.pool, user_id)


class AdjustBalanceRequest(BaseModel):
    amount: str
    reason: str
    # One per "Apply" click, generated client-side (crypto.randomUUID() in
    # web/admin/js/screens/users.js) -- becomes the ledger idempotency key.
    # Required, not defaulted: a missing/blank value would silently reopen
    # the double-submission gap this field exists to close.
    request_id: str


@app.post("/users/{user_id}/adjust")
async def adjust_balance(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("users:adjust_balance"))],
    user_id: int,
    body: AdjustBalanceRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    if not body.request_id.strip():
        raise HTTPException(status_code=422, detail="request_id is required")
    try:
        amount = Decimal(body.amount)
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="amount must be a decimal number") from exc

    try:
        txn_id = await queries.adjust_balance(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            amount=amount,
            reason=body.reason,
            ip_address=_client_ip(request),
            request_id=body.request_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ledger_transaction_id": txn_id}


class SetStatusRequest(BaseModel):
    status: str
    reason: str


@app.post("/users/{user_id}/status")
async def set_user_status(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("users:suspend"))],
    user_id: int,
    body: SetStatusRequest,
) -> dict[str, str]:
    _require_reason(body.reason)
    try:
        await queries.set_user_status(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            status=body.status,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except queries.InvalidStatusTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok"}


class SetKycLevelRequest(BaseModel):
    kyc_level: int
    reason: str


@app.post("/users/{user_id}/kyc")
async def set_kyc_level(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("users:verify_kyc"))],
    user_id: int,
    body: SetKycLevelRequest,
) -> dict[str, str]:
    _require_reason(body.reason)
    try:
        await queries.set_kyc_level(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            kyc_level=body.kyc_level,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except queries.InvalidKycLevel as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok"}


# --- rounds --------------------------------------------------------------


@app.get("/rounds")
async def list_rounds(
    admin: Annotated[AdminSession, Depends(require("rounds:view"))], room_id: int | None = None
) -> list[dict[str, Any]]:
    return await queries.list_rounds(app.state.pool, room_id)


@app.get("/rounds/{round_id}")
async def get_round(
    admin: Annotated[AdminSession, Depends(require("rounds:view"))], round_id: int
) -> dict[str, Any]:
    detail = await queries.get_round_detail(app.state.pool, round_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="round not found")
    return detail


@app.get("/rounds/{round_id}/fairness")
async def get_round_fairness(
    admin: Annotated[AdminSession, Depends(require("rounds:view"))], round_id: int
) -> dict[str, Any]:
    fairness = await queries.get_round_fairness(app.state.pool, round_id)
    if fairness is None:
        raise HTTPException(status_code=404, detail="round not found")
    return fairness


class VoidRoundRequest(BaseModel):
    reason: str


@app.post("/rounds/{round_id}/void")
async def void_round(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("rounds:void"))],
    round_id: int,
    body: VoidRoundRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    refunded = await queries.void_round_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        round_id=round_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"refunded": refunded}


# --- withdrawals -----------------------------------------------------------


@app.get("/withdrawals")
async def list_pending_withdrawals(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.list_pending_withdrawals(app.state.pool)


class WithdrawalDecisionRequest(BaseModel):
    reason: str


@app.post("/withdrawals/{payment_id}/approve")
async def approve_withdrawal(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: WithdrawalDecisionRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    approved = await queries.approve_withdrawal_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"approved": approved}


@app.post("/withdrawals/{payment_id}/reject")
async def reject_withdrawal(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: WithdrawalDecisionRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    rejected = await queries.reject_withdrawal_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"rejected": rejected}


# --- rooms ---------------------------------------------------------------


@app.get("/rooms")
async def list_rooms(admin: Annotated[AdminSession, Depends(require("rooms:view"))]) -> list[dict[str, Any]]:
    return await queries.list_rooms(app.state.pool)


class CreateRoomRequest(BaseModel):
    code: str
    stake: str
    house_cut_bps: int = 2000
    min_players: int = 2
    max_players: int = 100
    lobby_seconds: int = 30
    call_interval_ms: int = 4000
    result_seconds: int = 10
    win_patterns: list[str] = ["row", "col", "diag", "corners"]


@app.post("/rooms")
async def create_room(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("rooms:manage"))],
    body: CreateRoomRequest,
) -> dict[str, int]:
    room_id = await queries.create_room_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        code=body.code,
        stake=Decimal(body.stake),
        house_cut_bps=body.house_cut_bps,
        min_players=body.min_players,
        max_players=body.max_players,
        lobby_seconds=body.lobby_seconds,
        call_interval_ms=body.call_interval_ms,
        result_seconds=body.result_seconds,
        win_patterns=body.win_patterns,
        ip_address=_client_ip(request),
    )
    return {"room_id": room_id}


class UpdateRoomRequest(BaseModel):
    changes: dict[str, Any]
    reason: str | None = None


@app.patch("/rooms/{room_id}")
async def update_room(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("rooms:manage"))],
    room_id: int,
    body: UpdateRoomRequest,
) -> dict[str, bool]:
    try:
        updated = await queries.update_room_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            room_id=room_id,
            changes=body.changes,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="room not found")
    return {"updated": updated}


# --- reports ---------------------------------------------------------------


@app.get("/reports/ggr")
async def report_ggr(
    admin: Annotated[AdminSession, Depends(require("reports:view"))], on_date: date
) -> dict[str, Any]:
    return await queries.daily_ggr(app.state.pool, on_date)


@app.get("/reports/ltv")
async def report_ltv(
    admin: Annotated[AdminSession, Depends(require("reports:view"))], limit: int = 20
) -> list[dict[str, Any]]:
    return await queries.top_players_by_ltv(app.state.pool, limit)


@app.get("/reports/retention")
async def report_retention(
    admin: Annotated[AdminSession, Depends(require("reports:view"))], weeks: int = 8
) -> list[dict[str, Any]]:
    return await queries.retention_cohorts(app.state.pool, weeks)


# --- risk --------------------------------------------------------------


@app.get("/risk/shared-payout-accounts")
async def risk_shared_payout_accounts(
    admin: Annotated[AdminSession, Depends(require("risk:view"))],
) -> list[dict[str, Any]]:
    return await queries.shared_payout_account_clusters(app.state.pool)


@app.get("/risk/repeat-pairings")
async def risk_repeat_pairings(
    admin: Annotated[AdminSession, Depends(require("risk:view"))],
    min_shared_rounds: int = 3,
    since_days: int = 30,
) -> list[dict[str, Any]]:
    return await queries.repeat_room_pairings(
        app.state.pool, min_shared_rounds=min_shared_rounds, since_days=since_days
    )


# --- audit log ---------------------------------------------------------


@app.get("/audit-log")
async def audit_log(
    admin: Annotated[AdminSession, Depends(require("audit:view"))], limit: int = 100
) -> list[dict[str, Any]]:
    rows = await app.state.pool.fetch(
        "SELECT id, admin_id, action, target_type, target_id, before, after, "
        "reason, ip_address, created_at FROM admin_audit_log ORDER BY id DESC LIMIT $1",
        limit,
    )
    return [dict(r) for r in rows]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    # Every other route in this file goes through current_admin (session
    # token + this same IP check) or, at minimum, this IP check alone --
    # a real code review pass caught this endpoint bypassing both,
    # exposing house_revenue_total (live revenue in ETB), deposit_outcomes
    # _total, and payout_queue_depth to anyone on the network with no
    # session token and no allowlist check. A full session isn't required
    # here (a Prometheus scraper can't practically present one), but the
    # IP allowlist -- the one baseline control spec section 9.2 asks the
    # whole admin panel to have -- now applies here too.
    _check_ip_allowlist(request)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Mounted last, same reasoning as services/gateway/app.py's own miniapp
# mount: FastAPI matches routes in registration order, so every API route
# above must be registered first or this catch-all would shadow them.
# Protected by _console_frontend_ip_allowlist above, not by StaticFiles
# itself -- the frontend calls back into this same app's API routes for
# actual data, each of which still requires a real session token on top.
app.mount("/console", StaticFiles(directory=ADMIN_WEB_DIR, html=True), name="console")
