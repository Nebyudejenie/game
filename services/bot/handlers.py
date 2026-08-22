"""Command and message handlers (spec section 7.1-7.3).

Every reply goes through Notifier.send() (never bot.send_message directly)
and every user-facing string goes through i18n.t() (never a hardcoded
literal) -- both are load-bearing invariants the tests check for.
"""

from __future__ import annotations

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from redis.asyncio import Redis

from packages.core import ledger
from packages.core.config import Settings
from services.bot import referral
from services.bot.i18n import SUPPORTED_LANGUAGES, resolve_language, t
from services.bot.keyboards import main_menu_keyboard, registration_keyboard
from services.bot.notifier import Notifier
from services.bot.registration import (
    ContactMismatch,
    InvalidPhone,
    PhoneAlreadyRegistered,
    get_registered_user,
    register_from_contact,
)

router = Router(name="jobingo-bot")

MAX_DISPLAY_NAME_LENGTH = 32


async def _language_for(pool: asyncpg.Pool, telegram_id: int) -> str:
    row = await pool.fetchrow("SELECT language FROM users WHERE telegram_id = $1", telegram_id)
    return resolve_language(row["language"] if row else None)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    pool: asyncpg.Pool,
    redis: Redis,
    notifier: Notifier,
    settings: Settings,
) -> None:
    assert message.from_user is not None
    telegram_id = message.from_user.id
    chat_id = message.chat.id

    referrer_id = referral.parse_referral_code(command.args)
    if referrer_id is not None:
        await referral.store_pending_referral(redis, telegram_id, referrer_id)

    user = await get_registered_user(pool, telegram_id)
    if user is None:
        language = resolve_language(message.from_user.language_code)
        await notifier.send(chat_id, t("welcome.new_user", language))
        await notifier.send(
            chat_id, t("register.prompt", language), reply_markup=registration_keyboard(language)
        )
        return

    language = await _language_for(pool, telegram_id)
    await notifier.send(
        chat_id,
        t("welcome.back", language, name=user.display_name),
        reply_markup=main_menu_keyboard(language, miniapp_url=settings.miniapp_url),
    )


@router.message(F.contact)
async def on_contact(
    message: Message,
    pool: asyncpg.Pool,
    redis: Redis,
    notifier: Notifier,
    settings: Settings,
) -> None:
    assert message.from_user is not None and message.contact is not None
    telegram_id = message.from_user.id
    chat_id = message.chat.id
    language = resolve_language(message.from_user.language_code)

    referrer_id = await referral.pop_pending_referral(redis, telegram_id)

    try:
        user = await register_from_contact(
            pool,
            sender_telegram_id=telegram_id,
            contact_user_id=message.contact.user_id,
            contact_phone=message.contact.phone_number,
            display_name=message.from_user.first_name or str(telegram_id),
            referred_by_telegram_id=referrer_id,
        )
    except ContactMismatch:
        await notifier.send(
            chat_id, t("register.contact_mismatch", language), reply_markup=registration_keyboard(language)
        )
        return
    except InvalidPhone:
        await notifier.send(
            chat_id, t("register.invalid_phone", language), reply_markup=registration_keyboard(language)
        )
        return
    except PhoneAlreadyRegistered:
        await notifier.send(chat_id, t("error.generic", language))
        return

    await notifier.send(
        chat_id,
        t("register.success", language, name=user.display_name),
        reply_markup=main_menu_keyboard(language, miniapp_url=settings.miniapp_url),
    )


@router.message(Command("play"))
async def cmd_play(message: Message, pool: asyncpg.Pool, notifier: Notifier, settings: Settings) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    if not await _require_registered(message, pool, notifier, language):
        return
    if settings.miniapp_url:
        await notifier.send(
            message.chat.id,
            t("play.open", language),
            reply_markup=main_menu_keyboard(language, miniapp_url=settings.miniapp_url),
        )
    else:
        await notifier.send(message.chat.id, t("play.not_available", language))


@router.message(Command("balance"))
async def cmd_balance(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return

    async with pool.acquire() as conn:
        cash = await ledger.get_or_create_account(conn, user.id, "user_cash")
        bonus = await ledger.get_or_create_account(conn, user.id, "user_bonus")
        locked = await ledger.get_or_create_account(conn, user.id, "user_locked")
        cash_balance = await ledger.balance(conn, cash.id)
        bonus_balance = await ledger.balance(conn, bonus.id)
        locked_balance = await ledger.balance(conn, locked.id)

    await notifier.send(
        message.chat.id,
        t(
            "balance.summary",
            language,
            cash=cash_balance,
            bonus=bonus_balance,
            locked=locked_balance,
        ),
    )


@router.message(Command("history"))
async def cmd_history(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return

    rounds = await pool.fetch(
        """
        SELECT rd.seq, rd.stake, rd.ended_at,
               (rw.round_id IS NOT NULL) AS won
        FROM round_entries re
        JOIN rounds rd ON rd.id = re.round_id
        LEFT JOIN round_winners rw ON rw.round_id = re.round_id AND rw.user_id = re.user_id
        WHERE re.user_id = $1 AND rd.status IN ('done', 'voided')
        ORDER BY rd.ended_at DESC NULLS LAST
        LIMIT 10
        """,
        user.id,
    )

    if not rounds:
        await notifier.send(message.chat.id, t("history.empty", language))
        return

    lines = [t("history.header", language)]
    for row in rounds:
        outcome = t("history.outcome_won", language) if row["won"] else t("history.outcome_other", language)
        lines.append(
            t("history.round_line", language, seq=row["seq"], stake=row["stake"], outcome=outcome)
        )
    await notifier.send(message.chat.id, "\n".join(lines))


@router.message(Command("invite"))
async def cmd_invite(
    message: Message, pool: asyncpg.Pool, notifier: Notifier, settings: Settings
) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return

    if not settings.telegram_bot_username:
        await notifier.send(message.chat.id, t("invite.no_username", language))
        return

    count = await pool.fetchval("SELECT count(*) FROM users WHERE referred_by = $1", user.id)
    link = f"https://t.me/{settings.telegram_bot_username}?start=ref_{message.from_user.id}"
    await notifier.send(message.chat.id, t("invite.summary", language, link=link, count=count))


@router.message(Command("rules"))
async def cmd_rules(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    await notifier.send(message.chat.id, t("rules.text", language))


@router.message(Command("support"))
async def cmd_support(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    await notifier.send(message.chat.id, t("support.info", language))


@router.message(Command("deposit"))
async def cmd_deposit(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    if not await _require_registered(message, pool, notifier, language):
        return
    await notifier.send(message.chat.id, t("deposit.not_available", language))


@router.message(Command("withdraw"))
async def cmd_withdraw(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    if not await _require_registered(message, pool, notifier, language):
        return
    await notifier.send(message.chat.id, t("withdraw.not_available", language))


@router.message(Command("limits"))
async def cmd_limits(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    if not await _require_registered(message, pool, notifier, language):
        return
    await notifier.send(message.chat.id, t("limits.not_available", language))


@router.message(Command("language"))
async def cmd_language(message: Message, command: CommandObject, pool: asyncpg.Pool, notifier: Notifier) -> None:
    assert message.from_user is not None
    telegram_id = message.from_user.id
    current_language = await _language_for(pool, telegram_id)

    requested = (command.args or "").strip().lower()
    if not requested:
        await notifier.send(message.chat.id, t("language.prompt", current_language))
        return

    if requested not in SUPPORTED_LANGUAGES:
        await notifier.send(message.chat.id, t("language.invalid", current_language))
        return

    await pool.execute(
        "UPDATE users SET language = $1 WHERE telegram_id = $2", requested, telegram_id
    )
    await notifier.send(message.chat.id, t("language.set", requested))


@router.message(Command("change_username"))
async def cmd_change_username(
    message: Message, command: CommandObject, pool: asyncpg.Pool, notifier: Notifier
) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    if not await _require_registered(message, pool, notifier, language):
        return

    new_name = (command.args or "").strip()
    if not new_name:
        await notifier.send(message.chat.id, t("change_username.usage", language))
        return
    if len(new_name) > MAX_DISPLAY_NAME_LENGTH:
        await notifier.send(message.chat.id, t("change_username.too_long", language))
        return

    await pool.execute(
        "UPDATE users SET display_name = $1 WHERE telegram_id = $2", new_name, message.from_user.id
    )
    await notifier.send(message.chat.id, t("change_username.success", language, name=new_name))


@router.message(F.text)
async def on_unregistered_text(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    """Catch-all, registered last: a typed message from someone who hasn't
    completed registration -- most importantly, someone typing their phone
    number instead of using the Share Phone Number button, which spec
    section 7.2 explicitly requires rejecting (typed numbers are never
    proof of ownership). Registered users sending unrecognized text get no
    reply; nothing in the spec asks for one.
    """
    assert message.from_user is not None
    user = await get_registered_user(pool, message.from_user.id)
    if user is not None:
        return
    language = resolve_language(message.from_user.language_code)
    await notifier.send(
        message.chat.id, t("register.use_button", language), reply_markup=registration_keyboard(language)
    )


async def _require_registered(
    message: Message, pool: asyncpg.Pool, notifier: Notifier, language: str
) -> bool:
    assert message.from_user is not None
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return False
    return True
