"""Payment-agent portal authentication.

A Payment Agent (payment_agents table) has never had a password or any
web-facing identity -- their only channel has been the private Telegram
bot. The portal needs a real login, but adding a second username/password
system here would be exactly the kind of "parallel authorization logic"
this codebase's own conventions reject. Instead this reuses the ONE
channel an agent is already provably authenticated on: Telegram itself.

Flow: an active agent sends /portal to the bot (services/bot/handlers.py's
own _is_active_payment_agent filter already gates this exactly like
on_agent_sms does) -> generate_login_link() mints a short-lived,
single-use token stored only in Redis -> the bot sends back a link
containing it -> the portal's own login endpoint calls consume_login_
token(), which deletes the token immediately (so replay is impossible
even within its own TTL) and, only if payment_agents.is_active is still
true at that exact moment, issues a normal session token -- the identical
opaque-random-token-in-Redis pattern services/admin/auth.py already uses,
not a second scheme.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

import asyncpg
from redis.asyncio import Redis

LOGIN_TOKEN_TTL_SECONDS = 5 * 60  # a Telegram-delivered link is used within minutes or not at all
SESSION_TTL_SECONDS = 8 * 60 * 60  # matches admin's own session lifetime
LOGIN_TOKEN_KEY_PREFIX = "agent_login_token:"
SESSION_KEY_PREFIX = "agent_session:"


@dataclass(frozen=True)
class AgentSession:
    telegram_user_id: int
    display_name: str | None


async def generate_login_link(redis: Redis, *, telegram_user_id: int, portal_base_url: str) -> str:
    """Mints a one-time login token and returns the full URL the bot
    should send. The token itself carries no information -- it's an
    opaque key into Redis, exactly like an admin session token, so it
    can't be decoded or forged, only looked up.
    """
    token = secrets.token_urlsafe(32)
    await redis.set(
        LOGIN_TOKEN_KEY_PREFIX + token,
        str(telegram_user_id),
        ex=LOGIN_TOKEN_TTL_SECONDS,
    )
    return f"{portal_base_url}/login?token={token}"


async def consume_login_token(pool: asyncpg.Pool, redis: Redis, token: str) -> str | None:
    """Single-use by construction: GETDEL removes the token the instant
    it's read, so a link opened twice (a forwarded message, a browser
    prefetch) only ever succeeds once. Returns the session token on
    success, None if the login token was missing/expired/already used,
    or if the agent has been deactivated since the link was sent.
    """
    raw = await redis.getdel(LOGIN_TOKEN_KEY_PREFIX + token)
    if raw is None:
        return None
    telegram_user_id = int(raw)

    row = await pool.fetchrow(
        "SELECT telegram_user_id, display_name FROM payment_agents "
        "WHERE telegram_user_id = $1 AND is_active",
        telegram_user_id,
    )
    if row is None:
        return None

    session_token = secrets.token_urlsafe(32)
    await redis.set(
        SESSION_KEY_PREFIX + session_token,
        json.dumps({"telegram_user_id": row["telegram_user_id"], "display_name": row["display_name"]}),
        ex=SESSION_TTL_SECONDS,
    )
    return session_token


async def resolve_session(pool: asyncpg.Pool, redis: Redis, token: str) -> AgentSession | None:
    """Re-checks payment_agents.is_active on every call, not just at
    login -- the exact same "a deactivated account's already-issued
    session stops working immediately, not after TTL expiry" guarantee
    services/admin/auth.py::resolve_session() already provides, applied
    here for the identical reason: deactivating a compromised or
    offboarded agent must take effect at once.
    """
    raw = await redis.get(SESSION_KEY_PREFIX + token)
    if raw is None:
        return None
    data = json.loads(raw)
    is_active = await pool.fetchval(
        "SELECT is_active FROM payment_agents WHERE telegram_user_id = $1", data["telegram_user_id"]
    )
    if not is_active:
        return None
    return AgentSession(telegram_user_id=data["telegram_user_id"], display_name=data["display_name"])


async def logout(redis: Redis, token: str) -> None:
    await redis.delete(SESSION_KEY_PREFIX + token)
