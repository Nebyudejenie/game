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

from packages.core.metrics import telegram_api_duration_seconds

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
    # Which lane this came from -- recorded so a 429-backoff requeue (see
    # _run() below) puts it back on the *same* lane rather than promoting
    # every retried low-priority message to high (or the reverse).
    priority: str = "high"
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
    #
    # Resolves to one of "delivered", "blocked", "gave_up", "failed" --
    # added for the Notification Center's own per-recipient delivery
    # tracking (services/bot/campaign_worker.py), which needs to know
    # *which* terminal outcome happened, not just that one did. Every
    # existing caller keeps working unchanged: they either never look at
    # the resolved value (the ~60 direct replies in handlers.py) or only
    # await it to know a send reached some terminal state
    # (notification_relay.py's own ack-timing), neither of which reads
    # the string itself.
    done: asyncio.Future[str] | None = None


# Launch-readiness audit finding: this was one plain FIFO queue shared by
# every outbound message this codebase ever sends -- a player's own
# interactive command reply (services/bot/handlers.py's ~60 direct
# callers, and notification_relay.py's transactional pushes: deposit
# confirmations, win notifications) competed for the exact same
# GLOBAL_RATE_PER_SECOND=25 budget, in pure arrival order, as a
# Notification Center campaign broadcasting to potentially thousands of
# recipients. A real, measurable "Command Priority Classes" gap (the
# directive's own Section 9): a busy campaign send could genuinely delay
# a player's own /balance reply behind however many broadcast messages
# happened to be queued first, even though the *handler* that produced
# that reply had already finished in milliseconds.
#
# Fixed with two lanes, not a new queueing system -- same Notifier class,
# same _run() worker loop, same per-chat 429 backoff, same retry/error
# handling, same global rate cap (never exceeding Telegram's own real
# limit, priority only changes *whose* message gets that next available
# send slot). "high" (the default -- every interactive reply and every
# transactional notification) always drains before "low" (campaign
# broadcasts, the one call site that explicitly opts in --
# notification_relay.py's own delivery_id check). A lane with nothing
# waiting costs nothing extra; the only added latency is a bounded
# LOW_PRIORITY_POLL_SECONDS on ticks where *only* low-priority work is
# queued, negligible for a bulk send with no single recipient waiting on
# it, and never paid at all when a high-priority message is in flight.
LOW_PRIORITY_POLL_SECONDS = 0.05


class Notifier:
    def __init__(self, bot: Bot, *, max_attempts: int = 5) -> None:
        self._bot = bot
        self._max_attempts = max_attempts
        self._high: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._low: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._backoff_until: dict[int, float] = {}
        self._worker_task: asyncio.Task[None] | None = None

    async def send(
        self, chat_id: int, text: str, *, priority: str = "high", **kwargs: Any
    ) -> asyncio.Future[str]:
        done: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        queue = self._low if priority == "low" else self._high
        await queue.put(OutboundMessage(chat_id, text, kwargs, done=done, priority=priority))
        return done

    def _queue_for(self, message: OutboundMessage) -> asyncio.Queue[OutboundMessage]:
        return self._low if message.priority == "low" else self._high

    async def _requeue_after_delay(self, message: OutboundMessage, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._queue_for(message).put(message)

    async def _get_next(self) -> OutboundMessage:
        while True:
            if not self._high.empty():
                return self._high.get_nowait()
            if not self._low.empty():
                return self._low.get_nowait()
            # Nothing in either lane right now -- wait on "high" with a
            # short timeout rather than a bare blocking get(), so a
            # low-priority message that arrives while we're waiting is
            # never starved indefinitely by a steady trickle of new
            # high-priority ones: every poll tick re-checks both lanes in
            # priority order from the top.
            try:
                return await asyncio.wait_for(
                    self._high.get(), timeout=LOW_PRIORITY_POLL_SECONDS
                )
            except asyncio.TimeoutError:
                continue

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
            message = await self._get_next()
            now = time.monotonic()
            backoff_until = self._backoff_until.get(message.chat_id)

            if backoff_until is not None and now < backoff_until:
                # This chat is still backed off from a previous 429.
                # Launch-readiness audit finding, caught by a real test:
                # putting it straight back onto its own lane and sleeping
                # here (the original single-queue behavior) made
                # _get_next() see that lane as "non-empty" again on the
                # very next iteration -- when this backed-off message is
                # the *only* thing in the high lane, that starves the low
                # lane completely for the rest of the backoff window,
                # since "high has something queued" was never meant to
                # mean "high has something queued that's actually
                # sendable right now". Rescheduled via a delayed task
                # instead: this message is in neither lane while backed
                # off, so _get_next() correctly falls through to whatever
                # else is actually ready (another high-priority chat, or
                # low-priority work) instead of spinning on one that
                # isn't. Fire-and-forget is safe here: the delay's own
                # asyncio.sleep is this message's only remaining state
                # until it re-enters its queue, and stop()'s cancellation
                # of the parent worker task doesn't need to reach it --
                # worst case on a real shutdown, one already-scheduled
                # retry is silently dropped, exactly as acceptable as any
                # other in-flight send being interrupted by a restart.
                asyncio.create_task(
                    self._requeue_after_delay(
                        message, min(backoff_until - now, MAX_BACKOFF_SLEEP_SECONDS)
                    )
                )
                continue
            if backoff_until is not None:
                # The window passed -- a code review pass caught that
                # nothing ever removed this entry once it stopped
                # mattering, so _backoff_until grew by one entry for
                # every chat_id that had *ever* triggered even a single
                # 429, for the entire life of this long-running process.
                # Cleaning it up here closes that for the common case (a
                # chat that got 429'd is, by definition, one this worker
                # is actively sending to, so another message for it
                # dequeuing soon is the expected case) without adding a
                # periodic full-dict sweep for the residual, smaller case
                # of a chat that happens to never send another message
                # again after its one 429 -- not fully unbounded anymore,
                # but not a hard zero either.
                del self._backoff_until[message.chat_id]

            outcome = "delivered"
            api_call_start = time.monotonic()
            try:
                await self._bot.send_message(message.chat_id, message.text, **message.kwargs)
            except TelegramRetryAfter as exc:
                self._backoff_until[message.chat_id] = time.monotonic() + exc.retry_after
                message.attempts += 1
                if message.attempts < self._max_attempts:
                    await self._queue_for(message).put(message)
                    continue  # still in flight -- don't resolve message.done yet
                logger.warning(
                    "notifier_send_gave_up", chat_id=message.chat_id, attempts=message.attempts
                )
                outcome = "gave_up"
            except TelegramForbiddenError:
                outcome = "blocked"  # the user blocked the bot -- nothing to retry
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
                outcome = "failed"
            else:
                await asyncio.sleep(MIN_INTERVAL_SECONDS)
            finally:
                # Every branch above represents a real response from
                # Telegram's own API (or, for the bare Exception case, a
                # local/network failure attempting to reach it) -- timed
                # here, once per actual attempt, including a still-
                # retrying 429 (that `continue` above still runs this
                # finally first), so telegram_api_duration_seconds
                # measures Telegram's own response time specifically,
                # separate from this queue's own pacing/backoff waits.
                telegram_api_duration_seconds.observe(time.monotonic() - api_call_start)

            # Reached only on a terminal outcome (delivered, permanently
            # dropped, or retries exhausted) -- never on a requeue above.
            if message.done is not None and not message.done.done():
                message.done.set_result(outcome)
