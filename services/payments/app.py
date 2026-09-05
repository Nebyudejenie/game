"""Payments API: the one inbound HTTP surface a payment provider's server
reaches over the public internet (spec section 8.1-8.2). Deposit *creation*
is a plain Python call from services/bot/handlers.py, the same way the bot
already reads/writes the ledger directly for /balance and /history -- only
the webhook, which genuinely originates outside this process, needs to be
a real route.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

AGENT_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "agent"

from packages.core import metrics
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.redis_conn import get_redis
from packages.core.tracing import configure_tracing
from services.payments import agent_auth, deposits
from services.payments.chapa import ChapaProvider
from services.payments.provider import InvalidSignature
from services.payments.telebirr_ingest import SOURCE_MACRODROID, ingest_sms_evidence
from services.payments.withdrawals import PAYOUT_STREAM


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_tracing("payments", settings.otel_exporter_endpoint)
    app.state.pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=20)
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


class TelebirrIngestRequest(BaseModel):
    raw_sms: str
    device_id: str


def _check_macrodroid_token(authorization: str) -> None:
    """A thin, single-purpose bearer check -- MacroDroid is a thin adapter
    (section 114) with no financial logic of its own, so its only job here
    is proving it's really our configured device before the real pipeline
    (ingest_sms_evidence) ever sees the payload. hmac.compare_digest avoids
    a timing side-channel on the comparison, same discipline packages/core/
    telegram_auth.py already uses for the Telegram HMAC check.
    """
    settings = get_settings()
    if not settings.macrodroid_ingest_token:
        raise HTTPException(status_code=503, detail="telebirr ingestion is not configured")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer ") :]
    if not hmac.compare_digest(token, settings.macrodroid_ingest_token):
        raise HTTPException(status_code=401, detail="invalid bearer token")


@app.post("/internal/telebirr/ingest")
async def telebirr_ingest(
    body: TelebirrIngestRequest, authorization: Annotated[str, Header()] = ""
) -> dict[str, str | int | None]:
    _check_macrodroid_token(authorization)
    if not body.raw_sms.strip():
        raise HTTPException(status_code=422, detail="raw_sms_required")
    outcome = await ingest_sms_evidence(
        app.state.pool, raw_sms=body.raw_sms, source=SOURCE_MACRODROID, source_ref=body.device_id
    )
    return {
        "status": outcome.status,
        "evidence_id": outcome.evidence_id,
        "external_reference": outcome.external_reference,
        "reason": outcome.reason,
    }


# --- Payment Agent Portal ---------------------------------------------
#
# An agent's only prior identity was a row in payment_agents plus
# whatever Telegram already authenticated them as (see services/bot/
# handlers.py's /portal command and agent_auth.py's own docstring for
# why this reuses that rather than adding a second login system). These
# three routes are the entire portal backend: exchange a one-time
# Telegram-delivered link for a session, read who that session belongs
# to, and read that agent's own submission history -- nothing else.
# Never their raw SMS, never another agent's or player's data.


class AgentLoginRequest(BaseModel):
    token: str


def _agent_bearer_token(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization[len("Bearer ") :]


async def _current_agent(authorization: Annotated[str, Header()] = "") -> agent_auth.AgentSession:
    token = _agent_bearer_token(authorization)
    session = await agent_auth.resolve_session(app.state.pool, app.state.redis, token)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return session


@app.post("/agent-portal/login")
async def agent_portal_login(body: AgentLoginRequest) -> dict[str, str]:
    session_token = await agent_auth.consume_login_token(app.state.pool, app.state.redis, body.token)
    if session_token is None:
        raise HTTPException(status_code=401, detail="invalid, expired, or already-used login link")
    return {"session_token": session_token}


@app.post("/agent-portal/logout")
async def agent_portal_logout(authorization: Annotated[str, Header()] = "") -> dict[str, bool]:
    token = _agent_bearer_token(authorization)
    await agent_auth.logout(app.state.redis, token)
    return {"ok": True}


@app.get("/agent-portal/me")
async def agent_portal_me(
    session: Annotated[agent_auth.AgentSession, Depends(_current_agent)],
) -> dict[str, str | int | None]:
    return {"telegram_user_id": session.telegram_user_id, "display_name": session.display_name}


@app.get("/agent-portal/submissions")
async def agent_portal_submissions(
    session: Annotated[agent_auth.AgentSession, Depends(_current_agent)],
) -> list[dict[str, str | float | None]]:
    # Deliberately not raw_sms, payer_name, payer_phone, recipient_name,
    # or recipient_phone -- an agent sees enough to know their own
    # submission's fate, never another person's private information (the
    # exact same fields the admin console's own non-finance roles are
    # kept away from, see docs/TELEBIRR_ROLES_AND_ACCESS.md).
    rows = await app.state.pool.fetch(
        """
        SELECT external_reference, amount, status, reject_reason, received_at
        FROM payment_evidence
        WHERE source = 'telegram_agent' AND source_ref = $1
        ORDER BY received_at DESC
        LIMIT 50
        """,
        str(session.telegram_user_id),
    )
    return [
        {
            "reference": row["external_reference"],
            "amount": float(row["amount"]) if row["amount"] is not None else None,
            "status": row["status"],
            "reject_reason": row["reject_reason"],
            "received_at": row["received_at"].isoformat(),
        }
        for row in rows
    ]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    # payout_queue_depth and house_revenue_total are "live" gauges (spec
    # section 10.4) -- queried fresh on every scrape rather than maintained
    # incrementally, since a scrape is exactly the moment Prometheus wants
    # their current value and this avoids a background polling loop for
    # numbers nothing else in this process needs continuously updated.
    depth = await app.state.redis.xlen(PAYOUT_STREAM)
    metrics.payout_queue_depth.set(depth)

    revenue = await app.state.pool.fetchval(
        """
        SELECT COALESCE(SUM(b.balance), 0)
        FROM account_balances b JOIN accounts a ON a.id = b.account_id
        WHERE a.kind = 'house_revenue'
        """
    )
    metrics.house_revenue_total.set(float(revenue))

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Mounted last, same discipline gateway/admin's own static mounts already
# follow: every real API route above must be registered first, or this
# catch-all swallows it. agent.arada.fun and payments.arada.fun currently
# route to this exact same container (see docs/PRODUCTION_DOMAIN_AND_
# CLOUDFLARE.md) -- the Agent Portal is genuinely part of the payments
# service, not a second service, so this static bundle also happens to
# be reachable at payments.arada.fun/. That's harmless: it carries no
# secrets and every real capability still requires the bearer-token/
# session checks above regardless of which hostname reached it.
app.mount("/", StaticFiles(directory=AGENT_WEB_DIR, html=True), name="agent_portal")
