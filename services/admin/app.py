"""Admin console API. Separate authentication from players entirely (spec
section 33): username + password + TOTP, session tokens in Redis, RBAC
enforced on every mutating route, every mutation audit-logged. No route in
this file ever writes a balance directly -- adjust_balance goes through
the ledger like any other money movement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

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
async def login(body: LoginRequest) -> dict[str, str]:
    try:
        token = await auth.login(
            app.state.pool,
            app.state.redis,
            username=body.username,
            password=body.password,
            totp_code=body.totp_code,
        )
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


@app.post("/users/{user_id}/adjust")
async def adjust_balance(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("users:adjust_balance"))],
    user_id: int,
    body: AdjustBalanceRequest,
) -> dict[str, Any]:
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
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
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    await queries.set_user_status(
        app.state.pool,
        admin_id=admin.admin_id,
        user_id=user_id,
        status=body.status,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
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
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
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
    approved = await queries.approve_withdrawal_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        reason=body.reason or None,
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
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    rejected = await queries.reject_withdrawal_admin(
        app.state.pool,
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
