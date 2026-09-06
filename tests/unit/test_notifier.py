"""Tests for services/bot/notifier.py -- the only code path allowed to
call bot.send_message. Uses a mocked Bot so no real Telegram connectivity
is needed; the exceptions constructed here are aiogram's real exception
types, just triggered by a fake bot instead of a real 429 response.
"""

import asyncio
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from services.bot.notifier import Notifier


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=1, text="x"), message="Too Many Requests", retry_after=seconds
    )


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="bot was blocked")


def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=SendMessage(chat_id=1, text="x"), message="Bad Request: can't parse entities"
    )


async def _run_briefly(notifier: Notifier, seconds: float = 0.2) -> None:
    await asyncio.sleep(seconds)


async def test_sends_a_queued_message():
    bot = AsyncMock()
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notifier.send(123, "hello")
        await _run_briefly(notifier)
        bot.send_message.assert_called_once_with(123, "hello")
    finally:
        await notifier.stop()


async def test_retry_after_backs_off_then_eventually_succeeds():
    bot = AsyncMock()
    bot.send_message.side_effect = [_retry_after(1), None]
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notifier.send(123, "hello")
        await asyncio.sleep(1.4)
        assert bot.send_message.call_count == 2
    finally:
        await notifier.stop()


async def test_expired_backoff_entry_is_cleaned_up_not_left_forever():
    # A code review pass caught that _backoff_until never removed an
    # entry once its window passed -- it grew by one entry for every
    # chat_id that had *ever* triggered even a single 429, for the
    # entire life of this long-running process. Once a later message for
    # the same chat is actually dequeued (the common case: a chat that
    # just got 429'd is one this worker is actively sending to), the
    # stale entry must be removed, not just silently ignored.
    bot = AsyncMock()
    bot.send_message.side_effect = [_retry_after(1), None]
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notifier.send(123, "hello")
        await asyncio.sleep(1.4)
        assert bot.send_message.call_count == 2
        assert 123 not in notifier._backoff_until  # noqa: SLF001
    finally:
        await notifier.stop()


async def test_backed_off_chat_does_not_block_other_chats():
    call_order: list[int] = []

    async def fake_send(chat_id: int, text: str, **kwargs: object) -> None:
        call_order.append(chat_id)
        if chat_id == 1 and call_order.count(1) == 1:
            raise _retry_after(5)

    bot = AsyncMock()
    bot.send_message.side_effect = fake_send

    notifier = Notifier(bot)
    notifier.start()
    try:
        await notifier.send(1, "will be rate limited")
        await notifier.send(2, "should still go through promptly")
        await asyncio.sleep(0.3)
        assert 2 in call_order
    finally:
        await notifier.stop()


async def test_forbidden_error_drops_message_without_retry_storm():
    bot = AsyncMock()
    bot.send_message.side_effect = _forbidden()
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notifier.send(123, "hello")
        await asyncio.sleep(0.2)
        assert bot.send_message.call_count == 1
    finally:
        await notifier.stop()


async def test_an_unexpected_send_error_does_not_kill_the_worker():
    # Regression: a real code review pass caught that any exception here
    # other than TelegramRetryAfter/TelegramForbiddenError (e.g.
    # TelegramBadRequest from malformed HTML in an interpolated user
    # string) used to propagate straight out of the worker loop and kill
    # it permanently -- nothing supervises or restarts this task, so
    # every future notification for every user would silently stop.
    bot = AsyncMock()
    bot.send_message.side_effect = [_bad_request(), None]
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notifier.send(123, "message with <malformed> html")
        await notifier.send(456, "a second, unrelated message")
        await asyncio.sleep(0.3)
        # Both sends were attempted -- the worker survived the first
        # message's unexpected failure and went on to process the next
        # one, rather than dying silently after the first.
        assert bot.send_message.call_count == 2
    finally:
        await notifier.stop()


async def test_multiple_messages_are_all_eventually_sent():
    bot = AsyncMock()
    notifier = Notifier(bot)
    notifier.start()
    try:
        for i in range(10):
            await notifier.send(i, f"message {i}")
        await asyncio.sleep(1.0)
        assert bot.send_message.call_count == 10
    finally:
        await notifier.stop()


# --- priority lanes: a bulk/campaign send must never delay an
# interactive reply or a transactional notification queued after it -----


async def test_high_priority_message_is_sent_before_earlier_queued_low_priority_ones():
    call_order: list[int] = []

    async def fake_send(chat_id: int, text: str, **kwargs: object) -> None:
        call_order.append(chat_id)

    bot = AsyncMock()
    bot.send_message.side_effect = fake_send
    notifier = Notifier(bot)
    try:
        # Queue 5 low-priority (campaign-style) sends *before* starting the
        # worker, so they're all genuinely sitting in the queue first --
        # this proves priority order, not just accidental scheduling luck.
        for chat_id in range(100, 105):
            await notifier.send(chat_id, "bulk broadcast", priority="low")
        # A player's own interactive reply, queued last.
        await notifier.send(1, "your balance is 50.00 ETB", priority="high")

        notifier.start()
        await asyncio.sleep(0.3)

        assert bot.send_message.call_count == 6
        # The high-priority message must be the *first* one actually
        # sent, despite being enqueued after all five low-priority ones --
        # this is the real, measurable fix for a busy campaign broadcast
        # otherwise delaying a player's own command reply behind it.
        assert call_order[0] == 1
        assert set(call_order[1:]) == {100, 101, 102, 103, 104}
    finally:
        await notifier.stop()


async def test_low_priority_messages_still_get_sent_with_no_high_priority_traffic():
    # The other direction: a lane with nothing in the other one must not
    # starve -- LOW_PRIORITY_POLL_SECONDS bounds how long a low-priority-
    # only message waits, it never blocks forever.
    bot = AsyncMock()
    notifier = Notifier(bot)
    notifier.start()
    try:
        for chat_id in range(5):
            await notifier.send(chat_id, "bulk broadcast", priority="low")
        await asyncio.sleep(0.5)
        assert bot.send_message.call_count == 5
    finally:
        await notifier.stop()


async def test_a_backed_off_high_priority_chat_does_not_block_low_priority_traffic():
    # A 429 backoff on one specific chat (in the high lane) must not stall
    # unrelated low-priority sends behind it either -- the requeue-to-own-
    # lane fix (_queue_for()) is what keeps this working after adding a
    # second lane.
    call_order: list[int] = []

    async def fake_send(chat_id: int, text: str, **kwargs: object) -> None:
        call_order.append(chat_id)
        if chat_id == 1 and call_order.count(1) == 1:
            raise _retry_after(5)

    bot = AsyncMock()
    bot.send_message.side_effect = fake_send
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notifier.send(1, "will be rate limited", priority="high")
        await notifier.send(999, "unrelated bulk send", priority="low")
        await asyncio.sleep(0.3)
        assert 999 in call_order
    finally:
        await notifier.stop()
