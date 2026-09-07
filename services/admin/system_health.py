"""Real system health checks for the Admin Control Center home (Phase 4,
Sections 4-5). "Every indicator must map to a real check" -- each
function below either runs a genuine, cheap probe (a real DB round trip,
a real Redis PING, a real getWebhookInfo call reusing services/admin/
telegram_diagnostics.py from an earlier phase) or is explicitly absent
from this module rather than faked green. See docs/ADMIN_FEATURE_
INVENTORY.md for the full, honest list of what does and doesn't have a
real check wired up yet (payments reconciliation, backups, and a
composite "risk" signal are not included here for exactly that reason).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import asyncpg
from redis.asyncio import Redis

from services.admin import telegram_diagnostics
from services.engine.recovery import NON_TERMINAL_STATUSES

# A round legitimately sits in a non-terminal status for the ordinary
# duration of play (lobby countdown + up to 75 calls + a brief
# settlement window) -- real rooms in this codebase are configured with
# call intervals in the single-digit seconds, so a genuinely healthy
# round is done in well under this. Past it, "still running" stops being
# normal variance and starts being evidence of a stuck/crashed engine
# that recover_orphaned_rounds() (services/engine/recovery.py) hasn't
# caught yet -- either because the worker itself is down, or because the
# round is stuck for a reason that isn't "the process that owned it
# died" (recovery's own scope).
STUCK_ROUND_THRESHOLD = timedelta(minutes=30)


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: str  # "green" | "amber" | "red" | "unknown"
    why: str


async def check_database(pool: asyncpg.Pool) -> HealthCheck:
    try:
        await pool.fetchval("SELECT 1")
        return HealthCheck("database", "green", "A real SELECT 1 round trip succeeded.")
    except Exception as exc:
        return HealthCheck("database", "red", f"SELECT 1 failed: {exc}")


async def check_redis(redis: Redis) -> HealthCheck:
    try:
        await redis.ping()
        return HealthCheck("redis", "green", "A real PING succeeded.")
    except Exception as exc:
        return HealthCheck("redis", "red", f"PING failed: {exc}")


async def check_telegram(bot_token: str) -> HealthCheck:
    if not bot_token:
        return HealthCheck("telegram", "unknown", "telegram_bot_token is not configured in this environment.")
    try:
        health = await telegram_diagnostics.get_webhook_health(bot_token)
    except Exception as exc:
        return HealthCheck("telegram", "red", f"getWebhookInfo failed: {exc}")
    if health.status == "critical":
        return HealthCheck(
            "telegram", "red", f"{health.pending_update_count} pending updates — Telegram cannot reach the webhook."
        )
    if health.status == "warning":
        return HealthCheck("telegram", "amber", f"{health.pending_update_count} pending updates — a backlog is building.")
    return HealthCheck("telegram", "green", "Webhook is configured and updates are flowing normally.")


async def check_bingo(pool: asyncpg.Pool) -> HealthCheck:
    stuck = await pool.fetch(
        f"""
        SELECT r.id, ro.code, r.status,
               COALESCE(r.started_at, now()) AS reference_time
        FROM rounds r
        JOIN rooms ro ON ro.id = r.room_id
        WHERE r.status = ANY($1::text[])
          AND COALESCE(r.started_at, now() - interval '1 hour') < now() - $2::interval
        LIMIT 10
        """,
        list(NON_TERMINAL_STATUSES),
        STUCK_ROUND_THRESHOLD,
    )
    if not stuck:
        return HealthCheck("bingo", "green", "No round has been stuck in a non-terminal status past the threshold.")
    codes = ", ".join(f"{r['code']} (round #{r['id']}, {r['status']})" for r in stuck)
    return HealthCheck(
        "bingo", "red",
        f"{len(stuck)} round(s) stuck in a non-terminal status for over "
        f"{int(STUCK_ROUND_THRESHOLD.total_seconds() // 60)} minutes: {codes}",
    )


async def run_all_checks(pool: asyncpg.Pool, redis: Redis, *, bot_token: str) -> list[HealthCheck]:
    return [
        await check_database(pool),
        await check_redis(redis),
        await check_telegram(bot_token),
        await check_bingo(pool),
    ]
