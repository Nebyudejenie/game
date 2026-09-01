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

import asyncio
import json
from typing import Any

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core.notifications import NOTIFICATIONS_STREAM
from services.bot.i18n import resolve_language, t
from services.bot.notifier import Notifier

logger = structlog.get_logger()

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


async def _drain_one_user(
    pool: asyncpg.Pool, redis: Redis, notifier: Notifier, entries: list[tuple[str, dict[str, str]]]
) -> None:
    # A code-review pass caught two related gaps here: db_pool.py's new
    # bounded pool.acquire() turns sustained-load pool exhaustion into a
    # real TimeoutError (previously an indefinite hang), and this
    # function's caller runs every user's own _drain_one_user()
    # concurrently via asyncio.gather() -- without a try/except here, one
    # user's failure would propagate into that gather() and (its default
    # behavior, no return_exceptions=True) cancel every other user's
    # still-in-flight delivery in the same batch too, not just skip the
    # one that failed. process_one() only acks on a normal exit, so a
    # message that raises here is simply picked back up by this same
    # consumer's own pending-entries re-read next iteration -- the exact
    # redelivery-on-crash guarantee this module's own docstring already
    # promises, just without actually crashing anything.
    for msg_id, fields in entries:
        try:
            await process_one(pool, redis, notifier, msg_id=msg_id, fields=fields)
        except Exception:
            logger.exception("notification_relay_process_one_failed", msg_id=msg_id)


async def _process_batch(
    pool: asyncpg.Pool, redis: Redis, notifier: Notifier, entries: list[tuple[str, dict[str, str]]]
) -> None:
    # A code review pass caught that awaiting process_one() for each entry
    # in turn made this a head-of-line-blocking loop: process_one() awaits
    # notifier.send()'s returned future all the way to a terminal outcome,
    # which for a chat currently in a Telegram 429 backoff can mean several
    # retry/sleep cycles (see Notifier._run()). One backed-off chat_id in
    # the batch used to stall delivery to every other, unrelated user in
    # it for the whole backoff duration. Grouping by telegram_id and
    # running each user's entries concurrently fixes that while still
    # keeping a single user's own notifications in their original stream
    # order (so a 429 on their first message can't let their second one
    # jump ahead and arrive out of sequence).
    by_user: dict[int, list[tuple[str, dict[str, str]]]] = {}
    for msg_id, fields in entries:
        by_user.setdefault(int(fields["telegram_id"]), []).append((msg_id, fields))
    await asyncio.gather(
        *(_drain_one_user(pool, redis, notifier, user_entries) for user_entries in by_user.values())
    )


async def run_forever(
    pool: asyncpg.Pool, redis: Redis, notifier: Notifier, *, consumer_name: str = "relay-1"
) -> None:
    await ensure_group(redis)
    while True:
        # Read-phase failures (Redis itself, say) get isolated the same
        # way as services/payments/payout_worker.py's run_forever() --
        # back off and retry the whole iteration rather than letting an
        # exception escape this loop and silently kill the fire-and-forget
        # relay_task with no automatic restart (services/bot/app.py only
        # ever cancels it at shutdown, never checks its health in between).
        try:
            pending = await redis.xreadgroup(GROUP, consumer_name, {NOTIFICATIONS_STREAM: "0"}, count=10)
            entries = _flatten(pending)
            if not entries:
                fresh = await redis.xreadgroup(
                    GROUP, consumer_name, {NOTIFICATIONS_STREAM: ">"}, count=10, block=5000
                )
                entries = _flatten(fresh)
        except Exception:
            logger.exception("notification_relay_read_failed")
            await asyncio.sleep(1)
            continue

        await _process_batch(pool, redis, notifier, entries)
