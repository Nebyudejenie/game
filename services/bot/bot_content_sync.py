"""Keeps services/bot/i18n.py's in-process override cache in sync with
the bot_i18n_overrides table an admin edits through the console (Bot
Content screen). Polling rather than push (Redis pub/sub): this is a
rarely-changed setting, not a hot path, and polling keeps the bot process
from needing a second always-on subscription just for this.
"""

from __future__ import annotations

import asyncio

import asyncpg
import structlog

from services.bot import i18n

logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 30


async def refresh_once(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch("SELECT key, language, value FROM bot_i18n_overrides")
    i18n.set_overrides({(row["key"], row["language"]): row["value"] for row in rows})


async def run_forever(pool: asyncpg.Pool) -> None:
    while True:
        try:
            await refresh_once(pool)
        except Exception:
            logger.exception("bot_content_sync_refresh_failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
