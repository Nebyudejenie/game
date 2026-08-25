"""End-to-end bot handler tests: a real aiogram Dispatcher/Router, real
Postgres and Redis, and a fake Telegram Bot API session so nothing ever
touches the network. This is what proves the spec's own Prompt 5 test
list: a contact from a different user is rejected, a typed phone number is
rejected with the correct re-prompt, and a duplicate update_id is
processed exactly once.
"""

import itertools
import random
from datetime import datetime, timezone
from decimal import Decimal

import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.send_message import SendMessage
from aiogram.types import Chat, Contact, Message, TelegramObject, Update, User

from packages.core.config import Settings
from packages.core.phone_crypto import decrypt_phone
from services.bot.app import build_dispatcher
from services.bot.notifier import Notifier
from tests.integration.conftest import next_telegram_id

# Randomized start, not itertools.count(1): update_ids feed the dedup
# middleware's Redis keys (seen:tg:{update_id}, 10 minute TTL). A counter
# that restarts from 1 on every pytest invocation would collide with
# still-live dedup keys from a run moments earlier and get silently
# dropped as "already seen" -- exactly the bug this comment is here to
# stop someone from reintroducing.
_id_counter = itertools.count(random.randint(10**8, 2 * 10**8))
_phone_counter = itertools.count(random.randint(10_000_000, 20_000_000))


def unique_phone() -> str:
    # phone_e164 is UNIQUE at the database level -- every test that
    # registers a user needs its own number, the same as it needs its own
    # telegram_id.
    return f"+2519{next(_phone_counter):08d}"


class FakeSession(BaseSession):
    """Records every SendMessage instead of hitting Telegram's real API."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        if isinstance(method, SendMessage):
            self.sent.append(method)
            return Message(
                message_id=next(_id_counter),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        raise NotImplementedError(f"FakeSession doesn't support {type(method).__name__}")

    async def stream_content(self, *args: object, **kwargs: object):
        raise NotImplementedError
        yield b""  # pragma: no cover -- makes this a generator function


def make_text_update(
    telegram_id: int, text: str, *, first_name: str = "Test", language_code: str | None = None
) -> Update:
    user = User(id=telegram_id, is_bot=False, first_name=first_name, language_code=language_code)
    message = Message(
        message_id=next(_id_counter),
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=next(_id_counter), message=message)


def make_contact_update(
    telegram_id: int, *, contact_user_id: int | None, phone: str, first_name: str = "Test"
) -> Update:
    user = User(id=telegram_id, is_bot=False, first_name=first_name)
    contact = Contact(phone_number=phone, first_name=first_name, user_id=contact_user_id)
    message = Message(
        message_id=next(_id_counter),
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=user,
        contact=contact,
    )
    return Update(update_id=next(_id_counter), message=message)


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    bot = Bot(token="123456:FAKE-TEST-TOKEN", session=session)
    return bot, session


async def _settle() -> None:
    import asyncio

    await asyncio.sleep(0.05)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def bot_setup(pool, redis):
    """One Dispatcher for the whole test file/session.

    aiogram's Router can only ever be attached to one Dispatcher for its
    lifetime (services/bot/handlers.py's `router` is a module-level
    singleton, the normal aiogram pattern) -- building a fresh Dispatcher
    per test would try to re-attach the same router repeatedly and aiogram
    correctly refuses that. One shared Dispatcher matches how the bot
    actually runs in production anyway (one long-lived instance).
    """
    settings = Settings(
        telegram_bot_token="123456:FAKE-TEST-TOKEN",
        miniapp_url="",
        telegram_bot_username="jobingo_bot",
    )
    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    dp = build_dispatcher(pool, redis, notifier, settings)
    yield dp, bot, session
    await notifier.stop()


@pytest_asyncio.fixture(loop_scope="session")
async def bot_ctx(bot_setup):
    dp, bot, session = bot_setup
    session.sent.clear()
    return dp, bot, session


async def test_typed_phone_number_is_rejected_with_reprompt(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_text_update(telegram_id, "0912345678")
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    assert "Share Phone Number" in session.sent[0].text or "ስልክ ቁጥር አጋራ" in session.sent[0].text

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert row is None  # typing a number must never register anyone


async def test_contact_from_a_different_user_is_rejected(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    someone_elses_id = next_telegram_id()
    update = make_contact_update(telegram_id, contact_user_id=someone_elses_id, phone=unique_phone())
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    assert "not your own" in session.sent[0].text or "የእርስዎ አይደለም" in session.sent[0].text

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert row is None


async def test_valid_contact_completes_registration(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    phone = unique_phone()
    update = make_contact_update(
        telegram_id, contact_user_id=telegram_id, phone=phone, first_name="Nebyu"
    )
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    assert "Nebyu" in session.sent[0].text

    row = await pool.fetchrow(
        "SELECT phone_e164_encrypted, display_name FROM users WHERE telegram_id = $1", telegram_id
    )
    assert row is not None
    assert decrypt_phone(bytes(row["phone_e164_encrypted"])) == phone
    assert row["display_name"] == "Nebyu"


async def test_referral_credit_survives_a_failed_registration_attempt(pool, bot_ctx):
    # Regression: a real code review pass caught that the pending referral
    # was popped (deleted from Redis) *before* attempting registration,
    # so a retryable failure (contact mismatch, invalid phone -- both
    # explicitly designed to let the user try again) silently lost the
    # referral credit on the very next, successful attempt, with no error
    # surfaced to anyone.
    dp, bot, session = bot_ctx
    referrer_telegram_id = await _register(dp, bot, session)
    referrer_id = await pool.fetchval(
        "SELECT id FROM users WHERE telegram_id = $1", referrer_telegram_id
    )

    new_telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(new_telegram_id, f"/start ref_{referrer_telegram_id}"))
    await _settle()
    session.sent.clear()

    # A mismatched contact -- a retryable failure that must not consume
    # the pending referral.
    someone_elses_id = next_telegram_id()
    await dp.feed_update(
        bot, make_contact_update(new_telegram_id, contact_user_id=someone_elses_id, phone=unique_phone())
    )
    await _settle()
    session.sent.clear()

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", new_telegram_id)
    assert row is None  # the failed attempt must not have registered anyone

    # The real, valid contact -- this must still get credit to the
    # original referrer, not register with no referrer at all.
    await dp.feed_update(
        bot,
        make_contact_update(new_telegram_id, contact_user_id=new_telegram_id, phone=unique_phone()),
    )
    await _settle()

    row = await pool.fetchrow(
        "SELECT referred_by FROM users WHERE telegram_id = $1", new_telegram_id
    )
    assert row is not None
    assert row["referred_by"] == referrer_id


async def test_duplicate_update_id_processed_exactly_once(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
    await dp.feed_update(bot, update)
    await dp.feed_update(bot, update)  # same update_id, fed again
    await _settle()

    assert len(session.sent) == 1  # not 2

    count = await pool.fetchval("SELECT count(*) FROM users WHERE telegram_id = $1", telegram_id)
    assert count == 1


async def test_start_shows_registration_prompt_for_new_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_text_update(telegram_id, "/start")
    await dp.feed_update(bot, update)
    await _settle()

    combined = " ".join(m.text or "" for m in session.sent)
    assert "register" in combined.lower() or "መመዝገብ" in combined or "ስልክ" in combined


async def test_balance_reflects_real_ledger_state(pool, conn, bot_ctx):
    from tests.integration.conftest import fund_user

    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    contact_update = make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
    await dp.feed_update(bot, contact_update)
    await _settle()

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("75.00"))

    session.sent.clear()
    balance_update = make_text_update(telegram_id, "/balance")
    await dp.feed_update(bot, balance_update)
    await _settle()

    assert len(session.sent) == 1
    assert "75.00" in session.sent[0].text


async def _register(dp, bot, session) -> int:
    telegram_id = next_telegram_id()
    contact_update = make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
    await dp.feed_update(bot, contact_update)
    await _settle()
    session.sent.clear()
    return telegram_id


async def test_deposit_command_rate_limited_after_five_in_a_row(pool, bot_ctx, monkeypatch):
    # bot_setup's shared Settings has real-looking (but fake) Chapa
    # credentials -- tests/integration/conftest.py sets them so the
    # payments app's lifespan can construct a provider at all -- so
    # cmd_deposit's own real ChapaProvider(settings.chapa_api_key) would
    # otherwise make a genuine, slowly-timing-out network call (confirmed
    # directly: ConnectTimeout after several seconds). cmd_deposit
    # constructs the provider inline rather than taking it as an injected
    # dependency, so the only way to exercise the real
    # create_deposit_intent() path here without touching the network is
    # to monkeypatch the class itself, the same fake-the-network-boundary
    # discipline every other provider-touching test in this codebase uses.
    #
    # This proves two real things through the actual bot dispatch path,
    # not just through deposits.py directly: cmd_deposit's new
    # `redis: Redis` parameter resolves correctly via aiogram's real
    # dependency injection (a wiring mistake here would raise, not
    # silently no-op), and the spec section 9.2 "deposit 5/hour" rate
    # limit genuinely blocks a 6th rapid attempt end to end.
    class _FakeChapaProvider:
        def __init__(self, api_key: str) -> None:
            self.name = "chapa"

        async def create_checkout(self, *, amount, user_ref, our_ref, return_url):
            from services.payments.provider import CheckoutResult

            return CheckoutResult(
                checkout_url=f"https://pay.test/{our_ref}", provider_ref=our_ref, raw_response={}
            )

    monkeypatch.setattr("services.bot.handlers.ChapaProvider", _FakeChapaProvider)

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    for i in range(5):
        await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
        await _settle()
        assert len(session.sent) == 1, f"attempt {i + 1}: {session.sent}"
        assert "Tap below" in session.sent[0].text or "ከታች ያለውን ይጫኑ" in session.sent[0].text, (
            f"attempt {i + 1} should have succeeded: {session.sent[0].text}"
        )
        session.sent.clear()

    await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
    await _settle()
    assert len(session.sent) == 1
    assert "wait a bit" in session.sent[0].text or "ትንሽ ቆይተው" in session.sent[0].text


async def test_limits_deposit_sets_the_cap_instantly_on_first_use(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 500"))
    await _settle()

    assert len(session.sent) == 1
    assert "500" in session.sent[0].text

    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    cap = await pool.fetchval(
        "SELECT daily_deposit_cap FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert cap == Decimal("500.00")


async def test_limits_deposit_increase_is_deferred_and_says_so(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 100"))
    await _settle()
    session.sent.clear()

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 900"))
    await _settle()

    assert len(session.sent) == 1
    assert "900" in session.sent[0].text

    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    row = await pool.fetchrow(
        "SELECT daily_deposit_cap, pending_daily_deposit_cap FROM responsible_gaming_limits "
        "WHERE user_id = $1",
        user_id,
    )
    assert row["daily_deposit_cap"] == Decimal("100.00")  # unchanged for now
    assert row["pending_daily_deposit_cap"] == Decimal("900.00")


async def test_limits_cooloff_invalid_duration_is_rejected(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits cooloff nextweek"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    row = await pool.fetchrow(
        "SELECT * FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert row is None  # nothing was ever created -- the command never got that far


async def test_limits_cooloff_valid_duration_sets_it(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits cooloff 24h"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    cooloff_until = await pool.fetchval(
        "SELECT cooloff_until FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert cooloff_until is not None


async def test_limits_selfexclude_without_confirm_does_nothing(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits selfexclude"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    status = await pool.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "active"


async def test_limits_selfexclude_confirm_applies_it(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits selfexclude confirm"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    status = await pool.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "self_excluded"
