"""Rate-limited outbound Telegram notification worker.

Nothing in this codebase calls `bot.send_message` directly except this
module -- every outbound message, wherever it originates (a command reply,
a deposit confirmation, a win notification), goes through `Notifier.send()`
so the ~25 msg/s global pace and per-chat 429 backoff are enforced in
exactly one place, per spec section 7.4.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

logger = structlog.get_logger()

GLOBAL_RATE_PER_SECOND = 25.0
MIN_INTERVAL_SECONDS = 1.0 / GLOBAL_RATE_PER_SECOND
MAX_BACKOFF_SLEEP_SECONDS = 0.5


@dataclass
class OutboundMessage:
    chat_id: int
    text: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    # Resolved once this message's handling by _run() reaches a terminal
    # state (delivered, permanently dropped, or retries exhausted) --
    # *not* on every dequeue, since a 429 backoff or a not-yet-exhausted
    # TelegramRetryAfter just requeues the same message for a later
    # attempt. services/bot/notification_relay.py awaits this before
    # acking the Redis Stream entry that produced this send() call, so a
    # notification that's still only sitting in this in-memory queue --
    # not actually delivered, and lost entirely if this process crashes
    # before delivering it -- is never mistaken for "done" and acked away
    # with no redelivery path. Every other caller (services/bot/handlers
    # .py's ~60 direct replies) just awaits send() itself and ignores the
    # returned future, so this stays fire-and-forget for them exactly as
    # before -- nothing here makes an interactive command reply block on
    # actual Telegram delivery or a 429 backoff sleep.
    done: asyncio.Future[None] | None = None


class Notifier:
    def __init__(self, bot: Bot, *, max_attempts: int = 5) -> None:
        self._bot = bot
        self._max_attempts = max_attempts
        self._queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._backoff_until: dict[int, float] = {}
        self._worker_task: asyncio.Task[None] | None = None

    async def send(self, chat_id: int, text: str, **kwargs: Any) -> asyncio.Future[None]:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(OutboundMessage(chat_id, text, kwargs, done=done))
        return done

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

    async def _run(self) -> None:
        while True:
            message = await self._queue.get()
            now = time.monotonic()
            backoff_until = self._backoff_until.get(message.chat_id)

            if backoff_until is not None and now < backoff_until:
                # This chat is still backed off from a previous 429 --
                # put it back and try whatever's behind it first, rather
                # than stalling every other chat's messages behind this
                # one. Sleeping (instead of a bare requeue-and-loop) avoids
                # busy-spinning when this is the only message waiting.
                await self._queue.put(message)
                await asyncio.sleep(min(backoff_until - now, MAX_BACKOFF_SLEEP_SECONDS))
                continue

            try:
                await self._bot.send_message(message.chat_id, message.text, **message.kwargs)
            except TelegramRetryAfter as exc:
                self._backoff_until[message.chat_id] = time.monotonic() + exc.retry_after
                message.attempts += 1
                if message.attempts < self._max_attempts:
                    await self._queue.put(message)
                    continue  # still in flight -- don't resolve message.done yet
                logger.warning(
                    "notifier_send_gave_up", chat_id=message.chat_id, attempts=message.attempts
                )
            except TelegramForbiddenError:
                pass  # the user blocked the bot -- nothing to retry
            except Exception:
                # A code review pass caught that any other exception here
                # (e.g. TelegramBadRequest from malformed HTML in an
                # interpolated string, a network error) used to propagate
                # straight out of this loop and kill the single global
                # notification worker permanently -- nothing supervises
                # or restarts it, so every future deposit/win/withdrawal
                # notification for every user would silently stop until
                # the whole process restarted. Logged and dropped, not
                # retried (most causes here -- malformed content in
                # particular -- would never succeed no matter how many
                # times retried), but the worker itself must keep running
                # for every other queued and future message.
                logger.exception("notifier_send_failed", chat_id=message.chat_id)
            else:
                await asyncio.sleep(MIN_INTERVAL_SECONDS)

            # Reached only on a terminal outcome (delivered, permanently
            # dropped, or retries exhausted) -- never on a requeue above.
            if message.done is not None and not message.done.done():
                message.done.set_result(None)
