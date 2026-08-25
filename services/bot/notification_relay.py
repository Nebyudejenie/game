"""Consumer side of the bot-notification relay (packages/core/
notifications.py is the producer): reads the 'bot_notifications' Redis
Stream with a real consumer group -- same reasoning as
services/payments/payout_worker.py, the first consumer group in this
codebase -- and is the only thing in this module allowed to call
Notifier.send(), keeping services/bot/handlers.py's "nothing sends a
Telegram message except through Notifier" invariant intact even for
notifications that originate outside the bot process entirely.

A Telegram private chat's id is always the same as the user's telegram_id
-- no separate chat-id lookup needed, matching how the rest of this
codebase already treats them as interchangeable (e.g. dedup.py, the bot's
own handlers).
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
from redis.asyncio import Redis

from packages.core.notifications import NOTIFICATIONS_STREAM
from services.bot.i18n import resolve_language, t
from services.bot.notifier import Notifier

GROUP = "bot-notification-workers"


async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(NOTIFICATIONS_STREAM, GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _language_for_telegram_id(pool: asyncpg.Pool, telegram_id: int) -> str:
    row = await pool.fetchrow("SELECT language FROM users WHERE telegram_id = $1", telegram_id)
    return resolve_language(row["language"] if row else None)


async def process_one(pool: asyncpg.Pool, redis: Redis, notifier: Notifier, *, msg_id: str, fields: dict[str, str]) -> None:
    telegram_id = int(fields["telegram_id"])
    key = fields["key"]
    kwargs: dict[str, Any] = json.loads(fields["kwargs"])
    language = await _language_for_telegram_id(pool, telegram_id)
    # Notifier.send() only enqueues -- the actual Telegram API call happens
    # later, in Notifier's own background worker, subject to its global
    # rate pace and 429 backoff. A code review pass caught that acking
    # right after send() returned meant this stream entry was marked done
    # the instant the notification landed in Notifier's in-memory queue,
    # not when it was actually delivered (or given up on) -- a crash of
    # this process before Notifier's worker got to it lost the
    # notification outright, with no redelivery path left, since the
    # stream entry claiming it was already gone. Awaiting the future
    # send() now returns makes this ack happen only once Notifier reaches
    # a real terminal outcome for this message.
    done = await notifier.send(telegram_id, t(key, language, **kwargs))
    await done
    await redis.xack(NOTIFICATIONS_STREAM, GROUP, msg_id)


def _flatten(streams: Any) -> list[tuple[str, dict[str, str]]]:
    entries: list[tuple[str, dict[str, str]]] = []
    pairs = streams.items() if isinstance(streams, dict) else streams
    for _stream_name, messages in pairs:
        for msg_id, fields in messages:
            entries.append((msg_id, fields))
    return entries


async def process_next(
    pool: asyncpg.Pool, redis: Redis, notifier: Notifier, *, consumer_name: str = "relay-1"
) -> bool:
    """Processes at most one queued notification. Returns False if the
    stream was empty. Built as a single-shot step, same reasoning as
    payout_worker.process_next(): deterministic for tests, and a real
    run_forever() loop is just this called repeatedly.
    """
    await ensure_group(redis)

    pending = await redis.xreadgroup(GROUP, consumer_name, {NOTIFICATIONS_STREAM: "0"}, count=1)
    entries = _flatten(pending)
    if not entries:
        fresh = await redis.xreadgroup(GROUP, consumer_name, {NOTIFICATIONS_STREAM: ">"}, count=1)
        entries = _flatten(fresh)
    if not entries:
        return False

    msg_id, fields = entries[0]
    await process_one(pool, redis, notifier, msg_id=msg_id, fields=fields)
    return True


async def run_forever(
    pool: asyncpg.Pool, redis: Redis, notifier: Notifier, *, consumer_name: str = "relay-1"
) -> None:
    await ensure_group(redis)
    while True:
        pending = await redis.xreadgroup(GROUP, consumer_name, {NOTIFICATIONS_STREAM: "0"}, count=10)
        entries = _flatten(pending)
        if not entries:
            fresh = await redis.xreadgroup(
                GROUP, consumer_name, {NOTIFICATIONS_STREAM: ">"}, count=10, block=5000
            )
            entries = _flatten(fresh)
        for msg_id, fields in entries:
            await process_one(pool, redis, notifier, msg_id=msg_id, fields=fields)
