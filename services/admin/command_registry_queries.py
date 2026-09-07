"""Telegram Command Center admin operations: CRUD over the bot_commands
registry (services/bot/command_registry.py is the bot process's own
read-only, polling-cached consumer of this same table), live metrics
joined in from the bot's own /metrics endpoint (services/admin/
bot_metrics_client.py), and a safe "send test" primitive that reuses the
existing cross-process notification pipeline (packages/core/
notifications.py's NOTIFICATIONS_STREAM) rather than a new one.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from redis.asyncio import Redis

from packages.core.notifications import NOTIFICATIONS_STREAM
from services.admin import audit, bot_metrics_client
from services.bot import i18n

# Only these columns are admin-settable -- a fixed, code-reviewed
# whitelist, not whatever a request body happens to contain. Column
# names below are interpolated into SQL (never a request-supplied
# string) only after being checked against this exact set, so this is
# the one and only thing standing between "safe configuration" and "SQL
# injection" -- keep it a literal set, never derived from user input.
_ALLOWED_FIELDS = {
    "display_name",
    "description",
    "category",
    "enabled",
    "visible",
    "sort_order",
    "cooldown_seconds",
    "rate_limit_per_minute",
    "content_key",
    "analytics_key",
}

# Security regression finding (Section 46's own "input validation" check):
# without this, a malformed value for any field not already given its own
# bespoke check (cooldown_seconds/rate_limit_per_minute below) reached
# asyncpg's own type binding raw -- e.g. {"enabled": "yes"} surfaced as an
# unhandled asyncpg.exceptions.DataError, a 500 for an authenticated admin
# request that should have been a clean 422. `str | None` fields
# (content_key/analytics_key are nullable) accept None; every other
# string field does not, since bot_commands has no nullable text column
# in _ALLOWED_FIELDS besides those two.
_NULLABLE_STRING_FIELDS = {"content_key", "analytics_key"}
_FIELD_TYPES: dict[str, type] = {
    "display_name": str,
    "description": str,
    "category": str,
    "enabled": bool,
    "visible": bool,
    "sort_order": int,
    "content_key": str,
    "analytics_key": str,
}

# Section 4's own "validate: minimum, maximum, reasonable range... no
# unlimited accidental configuration unless explicitly supported"
# requirement. The database's own CHECK constraints (migrations/versions/
# 232a259a3baa_bot_command_registry.py) already reject a negative
# cooldown or a non-positive rate limit -- these are a *tighter*,
# application-level ceiling so a fat-fingered "36000" (10 hours) comes
# back as a clear 422 with a real explanation instead of either a raw
# constraint violation (below zero) or silently accepted nonsense (an
# unbounded-looking cooldown a human almost certainly didn't intend).
# `rate_limit_per_minute=None` (unlimited) is unaffected -- that's the
# explicit, deliberate way to express "no limit", not an oversight.
MAX_COOLDOWN_SECONDS = 3600
MAX_RATE_LIMIT_PER_MINUTE = 1000


class UnknownBotCommand(ValueError):
    pass


class CommandNotAdminManaged(ValueError):
    pass


class InvalidCommandField(ValueError):
    pass


class MissingContentKey(ValueError):
    pass


async def list_commands_admin(pool: asyncpg.Pool, *, bot_metrics_url: str) -> list[dict[str, Any]]:
    rows = await pool.fetch("SELECT * FROM bot_commands ORDER BY sort_order, handler_name")
    live = await bot_metrics_client.fetch_command_metrics(bot_metrics_url)
    result = []
    for row in rows:
        record = dict(row)
        # Phase 3's "one canonical command identity": services/bot/perf.py
        # and command_registry.py both label every metric by
        # analytics_key when one is set, falling back to the real
        # handler_name otherwise (services/bot/command_registry.py::
        # analytics_label()) -- the lookup here has to resolve the exact
        # same way, or a command with a custom analytics_key would show
        # NO DATA despite the bot actually reporting real numbers for it
        # under that other name.
        metrics_label = row["analytics_key"] or row["handler_name"]
        m = live.get(metrics_label)
        # None here is the explicit "NO DATA" signal the admin UI must
        # render as such -- never coerced to 0, per the parent
        # directive's own "if a metric has insufficient real traffic,
        # show NO DATA, not zero" requirement.
        record["metrics"] = (
            None
            if m is None
            else {
                "count": m.count,
                "success_rate": m.success_rate,
                "error_rate": m.error_rate,
                "blocked": m.blocked,
                "rate_limited": m.rate_limited,
                "p50_ms": round(m.p50_seconds * 1000, 1) if m.p50_seconds is not None else None,
                "p95_ms": round(m.p95_seconds * 1000, 1) if m.p95_seconds is not None else None,
                "p99_ms": round(m.p99_seconds * 1000, 1) if m.p99_seconds is not None else None,
            }
        )
        result.append(record)
    return result


async def update_command_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    handler_name: str,
    changes: dict[str, Any],
    reason: str | None,
    ip_address: str | None,
) -> dict[str, Any]:
    unknown = set(changes) - _ALLOWED_FIELDS
    if unknown:
        raise InvalidCommandField(f"cannot set: {sorted(unknown)}")
    if not changes:
        raise InvalidCommandField("no fields given")

    for field, expected_type in _FIELD_TYPES.items():
        if field not in changes:
            continue
        value = changes[field]
        if value is None and field in _NULLABLE_STRING_FIELDS:
            continue
        # isinstance(True, int) is True in Python -- explicitly reject a
        # bool where an int (sort_order) is expected, the same guard
        # cooldown_seconds/rate_limit_per_minute already use below.
        if expected_type is int and isinstance(value, bool):
            raise InvalidCommandField(f"{field} must be an integer")
        if not isinstance(value, expected_type):
            raise InvalidCommandField(f"{field} must be a {expected_type.__name__}")

    if "cooldown_seconds" in changes:
        cooldown = changes["cooldown_seconds"]
        if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
            raise InvalidCommandField("cooldown_seconds must be a non-negative integer")
        if cooldown > MAX_COOLDOWN_SECONDS:
            raise InvalidCommandField(
                f"cooldown_seconds cannot exceed {MAX_COOLDOWN_SECONDS} ({MAX_COOLDOWN_SECONDS // 60} minutes)"
            )
    if "rate_limit_per_minute" in changes:
        rate_limit_value = changes["rate_limit_per_minute"]
        if rate_limit_value is not None:
            if (
                not isinstance(rate_limit_value, int)
                or isinstance(rate_limit_value, bool)
                or rate_limit_value <= 0
            ):
                raise InvalidCommandField("rate_limit_per_minute must be a positive integer, or null for unlimited")
            if rate_limit_value > MAX_RATE_LIMIT_PER_MINUTE:
                raise InvalidCommandField(
                    f"rate_limit_per_minute cannot exceed {MAX_RATE_LIMIT_PER_MINUTE}"
                )

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT * FROM bot_commands WHERE handler_name = $1 FOR UPDATE", handler_name
            )
            if before is None:
                raise UnknownBotCommand(handler_name)
            if not before["admin_managed"] and ("enabled" in changes or "visible" in changes):
                raise CommandNotAdminManaged(
                    f"{handler_name} is a structural/registration command and cannot be "
                    "disabled or hidden from the admin console"
                )

            set_clauses = []
            values: list[Any] = []
            for field, value in changes.items():
                values.append(value)
                set_clauses.append(f"{field} = ${len(values)}")
            values.append(admin_id)
            set_clauses.append(f"updated_by_admin_id = ${len(values)}")
            values.append(handler_name)

            after = await conn.fetchrow(
                f"""
                UPDATE bot_commands SET {', '.join(set_clauses)}, updated_at = now()
                WHERE handler_name = ${len(values)}
                RETURNING *
                """,
                *values,
            )
            assert after is not None

            await audit.record(
                conn,
                admin_id=admin_id,
                action="telegram_commands.update",
                target_type="bot_command",
                target_id=handler_name,
                before={field: before[field] for field in changes},
                after={field: after[field] for field in changes},
                reason=reason,
                ip_address=ip_address,
            )
    return dict(after)


async def _current_content_value(pool: asyncpg.Pool, *, content_key: str, language: str) -> str | None:
    override = await pool.fetchval(
        "SELECT value FROM bot_i18n_overrides WHERE key = $1 AND language = $2", content_key, language
    )
    return override if override is not None else i18n.default_template(content_key, language)


async def preview_command_content_admin(
    pool: asyncpg.Pool, *, handler_name: str, language: str
) -> dict[str, Any]:
    """Section 7's "Preview": what a player would actually see, rendered
    with clearly-marked sample values for any required placeholder
    (real values -- a real balance, a real amount -- are never fabricated
    for a preview an admin didn't ask this system to know).
    """
    row = await pool.fetchrow("SELECT content_key FROM bot_commands WHERE handler_name = $1", handler_name)
    if row is None:
        raise UnknownBotCommand(handler_name)
    content_key = row["content_key"]
    if not content_key:
        raise MissingContentKey(f"{handler_name} has no content_key configured")

    template = await _current_content_value(pool, content_key=content_key, language=language)
    if template is None:
        raise MissingContentKey(f"content_key {content_key!r} has no value for language {language!r}")

    placeholders = sorted(i18n.required_placeholders(template))
    sample_kwargs = {name: f"[{name}]" for name in placeholders}
    rendered = template.format(**sample_kwargs) if sample_kwargs else template
    return {
        "content_key": content_key,
        "language": language,
        "template": template,
        "placeholders": placeholders,
        "rendered_preview": rendered,
    }


async def send_test_command_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    handler_name: str,
    target_telegram_id: int,
    language: str,
    ip_address: str | None,
) -> str:
    """Section 8's "Send Test": always exactly one explicit, admin-typed
    telegram_id -- there is no "test group"/"default test account"
    concept to default to and no code path from here that can reach more
    than the one recipient given, so there is no way for this to become
    an accidental broadcast. Reuses NOTIFICATIONS_STREAM (packages/core/
    notifications.py) -- the same cross-process pipeline every other
    admin-originated Telegram message already goes through -- rather
    than a second delivery mechanism.
    """
    preview = await preview_command_content_admin(pool, handler_name=handler_name, language=language)
    test_text = f"[TEST -- sent by an administrator] {preview['rendered_preview']}"

    await redis.xadd(
        NOTIFICATIONS_STREAM, {"telegram_id": str(target_telegram_id), "raw_text": test_text}
    )
    await audit.record(
        pool,
        admin_id=admin_id,
        action="telegram_commands.send_test",
        target_type="bot_command",
        target_id=handler_name,
        after={"target_telegram_id": target_telegram_id, "language": language, "text": test_text},
        ip_address=ip_address,
    )
    return test_text
