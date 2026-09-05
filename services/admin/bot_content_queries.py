"""Bot Content admin operations: lets an admin override a player-facing
bot string (services/bot/i18n.py's t()) without a code deploy. The bot
process itself picks up a change within services/bot/bot_content_sync
.py's own POLL_INTERVAL_SECONDS (30s); this module only owns the
console-facing CRUD + validation + audit trail on top of the
bot_i18n_overrides table.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from services.admin import audit
from services.bot import i18n

_CATEGORY_LABELS = {
    "menu": "Main menu",
    "register": "Registration",
    "wallet": "Wallet",
    "deposit": "Deposit",
    "withdraw": "Withdrawal",
    "language": "Language",
}


class UnknownBotContentKey(ValueError):
    pass


class InvalidBotContentPlaceholders(ValueError):
    pass


def _category(key: str) -> str:
    prefix = key.split(".", 1)[0]
    return _CATEGORY_LABELS.get(prefix, prefix)


async def _overrides_map(pool: asyncpg.Pool) -> dict[tuple[str, str], str]:
    rows = await pool.fetch("SELECT key, language, value FROM bot_i18n_overrides")
    return {(row["key"], row["language"]): row["value"] for row in rows}


async def list_bot_content_admin(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    overrides = await _overrides_map(pool)
    result: list[dict[str, Any]] = []
    for key in sorted(i18n.all_keys()):
        default_am = i18n.default_template(key, "am") or ""
        languages: dict[str, Any] = {}
        for language in i18n.SUPPORTED_LANGUAGES:
            default_value = i18n.default_template(key, language)
            override_value = overrides.get((key, language))
            languages[language] = {
                "default_value": default_value,
                "override_value": override_value,
                "current_value": override_value if override_value is not None else default_value,
                "is_overridden": override_value is not None,
            }
        result.append(
            {
                "key": key,
                "category": _category(key),
                "placeholders": sorted(i18n.required_placeholders(default_am)),
                "languages": languages,
            }
        )
    return result


async def set_bot_content_override_admin(
    pool: asyncpg.Pool, *, admin_id: int, key: str, language: str, value: str, ip_address: str | None
) -> None:
    if key not in i18n.all_keys():
        raise UnknownBotContentKey(f"unknown bot content key: {key!r}")
    if language not in i18n.SUPPORTED_LANGUAGES:
        raise ValueError(f"unknown language: {language!r}")

    default_value = i18n.default_template(key, language)
    assert default_value is not None  # every key in all_keys() resolves for every language (fallback chain)
    expected = i18n.required_placeholders(default_value)
    got = i18n.required_placeholders(value)
    if got != expected:
        raise InvalidBotContentPlaceholders(
            f"text must contain exactly these placeholders: {sorted(expected)} (got {sorted(got)})"
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchval(
                "SELECT value FROM bot_i18n_overrides WHERE key = $1 AND language = $2", key, language
            )
            await conn.execute(
                """
                INSERT INTO bot_i18n_overrides (key, language, value, updated_by_admin_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (key, language) DO UPDATE
                  SET value = EXCLUDED.value, updated_by_admin_id = EXCLUDED.updated_by_admin_id,
                      updated_at = now()
                """,
                key,
                language,
                value,
                admin_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="bot_content.set_override",
                target_type="bot_i18n_override",
                target_id=f"{key}:{language}",
                before={"value": before} if before is not None else None,
                after={"value": value},
                ip_address=ip_address,
            )


async def clear_bot_content_override_admin(
    pool: asyncpg.Pool, *, admin_id: int, key: str, language: str, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM bot_i18n_overrides WHERE key = $1 AND language = $2 RETURNING value",
                key,
                language,
            )
            if row is None:
                return False
            await audit.record(
                conn,
                admin_id=admin_id,
                action="bot_content.clear_override",
                target_type="bot_i18n_override",
                target_id=f"{key}:{language}",
                before={"value": row["value"]},
                ip_address=ip_address,
            )
    return True
