"""Command and message handlers (spec section 7.1-7.3).

Every reply goes through Notifier.send() (never bot.send_message directly)
and every user-facing string goes through i18n.t() (never a hardcoded
literal) -- both are load-bearing invariants the tests check for.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, ReplyKeyboardRemove
from redis.asyncio import Redis

from packages.core import ledger, responsible_gaming
from packages.core.config import Settings
from services.bot import referral
from services.bot.i18n import SUPPORTED_LANGUAGES, resolve_language, t
from services.bot.keyboards import (
    MenuAction,
    deposit_checkout_keyboard,
    main_menu_keyboard,
    open_wallet_keyboard,
    registration_keyboard,
)
from services.bot.notifier import Notifier
from services.bot.registration import (
    ContactMismatch,
    InvalidPhone,
    PhoneAlreadyRegistered,
    get_registered_user,
    register_from_contact,
)
from services.payments import availability, deposits, manual, withdrawals
from services.payments.chapa import ChapaProvider
from services.payments.manual_provider import ManualProvider
from services.payments.provider import PaymentProvider
from services.payments.telebirr_ingest import (
    SOURCE_TELEGRAM_AGENT,
    STATUS_CONFLICTING_DUPLICATE,
    STATUS_DUPLICATE,
    STATUS_INGESTED_AVAILABLE,
    STATUS_INGESTED_REJECTED,
    STATUS_UNPARSEABLE,
    ingest_sms_evidence,
)

router = Router(name="jobingo-bot")

MAX_DISPLAY_NAME_LENGTH = 32


async def _language_for(pool: asyncpg.Pool, telegram_id: int) -> str:
    row = await pool.fetchrow("SELECT language FROM users WHERE telegram_id = $1", telegram_id)
    return resolve_language(row["language"] if row else None)


def _miniapp_direct_link(settings: Settings) -> str | None:
    """A t.me/<bot>/<short_name> direct link -- a second, independent
    launch surface alongside the web_app button both main_menu_keyboard()
    calls already send. A real production incident found the persistent
    menu button and keyboard button (both the same raw setChatMenuButton/
    web_app API mechanism) delivered no initData at all for some clients
    until the Mini App was also registered via BotFather's /newapp; this
    direct link uses that same registered app and is confirmed to work
    even when a button-based launch doesn't. None when the short name
    isn't configured -- never construct a link nobody set up.
    """
    if not settings.telegram_bot_username or not settings.telegram_miniapp_short_name:
        return None
    return f"https://t.me/{settings.telegram_bot_username}/{settings.telegram_miniapp_short_name}"


async def _send_refreshed_main_menu(
    notifier: Notifier, chat_id: int, text: str, language: str, settings: Settings
) -> None:
    """A real, live production incident: a player's persistent keyboard
    kept showing a plain "Play" button that just sent text (routing to
    on_menu_text() -> cmd_play() again, an unhelpful loop) instead of
    opening the Mini App directly -- even after MINIAPP_URL was fully
    configured and this exact chat had been sent a fresh
    main_menu_keyboard() with a real web_app button attached. Telegram's
    own client only ever redraws a chat's persistent ReplyKeyboardMarkup
    when it decides one is needed; a keyboard that already looks the
    same (identical button labels) to one already on screen isn't
    guaranteed to be replaced even though the web_app attribute
    underneath actually changed -- this player's client had been holding
    onto the very first keyboard this bot ever sent it, from before
    MINIAPP_URL existed at all, when Play truly was a plain text button.
    An explicit ReplyKeyboardRemove sent first forces the client to
    genuinely discard whatever it was showing before the real keyboard
    (with its real web_app button) replaces it, rather than trusting the
    client to notice the difference on its own.
    """
    await notifier.send(chat_id, text, reply_markup=ReplyKeyboardRemove())
    await notifier.send(
        chat_id, text, reply_markup=main_menu_keyboard(language, miniapp_url=settings.miniapp_url)
    )


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
    await _send_refreshed_main_menu(
        notifier, chat_id, t("welcome.back", language, name=user.display_name), language, settings
    )
    direct_link = _miniapp_direct_link(settings)
    if direct_link:
        await notifier.send(chat_id, t("play.direct_link", language, link=direct_link))


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

    # Peeked, not popped, here -- a real bug a code review pass caught:
    # popping (deleting) the pending referral before attempting
    # registration meant a retryable failure (ContactMismatch,
    # InvalidPhone -- both explicitly designed to let the user try again)
    # silently lost the referral credit on the user's next, successful
    # attempt, with no error surfaced to anyone. Only cleared once
    # registration has actually recorded it in users.referred_by.
    referrer_id = await referral.peek_pending_referral(redis, telegram_id)

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

    if referrer_id is not None:
        await referral.clear_pending_referral(redis, telegram_id)

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
        await _send_refreshed_main_menu(
            notifier, message.chat.id, t("play.open", language), language, settings
        )
        direct_link = _miniapp_direct_link(settings)
        if direct_link:
            await notifier.send(message.chat.id, t("play.direct_link", language, link=direct_link))
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

    # See services/gateway/queries.py::user_history()'s own comment --
    # this is the same query, duplicated here for the bot's /history
    # command, and needs the same fix: scope the winner join to the
    # entry's own card_no (a player can hold several cards in one round
    # now) and group by round so one round is one line, not one line per
    # card or a multiplicative blowup when several of a player's own
    # cards won the same round.
    rounds = await pool.fetch(
        """
        SELECT rd.seq, rd.stake, rd.ended_at,
               count(rw.round_id) > 0 AS won
        FROM round_entries re
        JOIN rounds rd ON rd.id = re.round_id
        LEFT JOIN round_winners rw
            ON rw.round_id = re.round_id AND rw.user_id = re.user_id AND rw.card_no = re.card_no
        WHERE re.user_id = $1 AND rd.status IN ('done', 'voided')
        GROUP BY rd.id, rd.seq, rd.stake, rd.ended_at
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
async def cmd_deposit(
    message: Message,
    command: CommandObject,
    pool: asyncpg.Pool,
    redis: Redis,
    notifier: Notifier,
    settings: Settings,
) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return

    # P1: keep taking deposits when Chapa is unavailable/not configured --
    # availability.get_payment_availability() is the single source of
    # truth the Mini App itself reads too (GET /api/payment-methods), so
    # this bot gate and the Mini App's own dynamic show/hide can never
    # silently disagree about what's actually live.
    methods = await availability.get_payment_availability(pool, settings)
    if ChapaProvider.name not in methods["deposit"]:
        if ManualProvider.name in methods["deposit"] and settings.miniapp_url:
            await notifier.send(
                message.chat.id,
                t("deposit.manual_only_available", language),
                reply_markup=open_wallet_keyboard(language, miniapp_url=settings.miniapp_url),
            )
        else:
            await notifier.send(message.chat.id, t("deposit.not_available", language))
        return

    raw_amount = (command.args or "").strip()
    if not raw_amount:
        await notifier.send(message.chat.id, t("deposit.usage", language, min=str(settings.min_deposit_etb)))
        return
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        amount = None
    if amount is None or amount <= 0:
        await notifier.send(message.chat.id, t("deposit.invalid_amount", language))
        return

    provider = ChapaProvider(settings.chapa_api_key)
    try:
        intent = await deposits.create_deposit_intent(
            pool,
            redis,
            provider,
            user_id=user.id,
            amount=amount,
            phone_e164=user.phone_e164,
            return_url=settings.miniapp_url,
            callback_url=f"{settings.payments_public_base_url}/webhooks/chapa",
            min_deposit=settings.min_deposit_etb,
            daily_cap=settings.daily_deposit_cap_etb,
        )
    except deposits.DepositRateLimited:
        await notifier.send(message.chat.id, t("deposit.rate_limited", language))
        return
    except deposits.BelowMinimumDeposit:
        await notifier.send(
            message.chat.id, t("deposit.below_minimum", language, min=str(settings.min_deposit_etb))
        )
        return
    except deposits.DailyDepositCapExceeded:
        await notifier.send(message.chat.id, t("deposit.daily_cap_exceeded", language))
        return
    except deposits.DepositorSelfExcluded:
        await notifier.send(message.chat.id, t("deposit.self_excluded", language))
        return
    except deposits.DepositorCoolingOff:
        await notifier.send(message.chat.id, t("deposit.cooloff_active", language))
        return
    except (deposits.DepositorBanned, deposits.UnknownDepositor, deposits.DepositProviderError):
        await notifier.send(message.chat.id, t("deposit.provider_error", language))
        return

    await notifier.send(
        message.chat.id,
        t("deposit.checkout_ready", language, amount=str(amount)),
        reply_markup=deposit_checkout_keyboard(language, checkout_url=intent.checkout_url, amount=str(amount)),
    )


@router.message(Command("withdraw"))
async def cmd_withdraw(
    message: Message,
    command: CommandObject,
    pool: asyncpg.Pool,
    redis: Redis,
    notifier: Notifier,
    settings: Settings,
) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return

    # P1: keep paying out withdrawals when Chapa is unavailable. Unlike
    # /deposit, this never needs a Mini-App redirect -- a manual
    # withdrawal needs nothing this command doesn't already collect (no
    # destination picker, no reference number up front; an admin adds
    # the real reference at settlement time). It just runs the exact
    # same flow through ManualProvider() with force_review=True instead.
    methods = await availability.get_payment_availability(pool, settings)
    use_manual = ChapaProvider.name not in methods["withdraw"]
    if use_manual and ManualProvider.name not in methods["withdraw"]:
        await notifier.send(message.chat.id, t("withdraw.not_available", language))
        return

    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 3:
        await notifier.send(
            message.chat.id, t("withdraw.usage", language, min=str(settings.min_withdraw_etb))
        )
        return
    raw_amount, account_ref, holder_name = parts
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        amount = None
    if amount is None or amount <= 0:
        await notifier.send(message.chat.id, t("withdraw.invalid_amount", language))
        return

    provider: PaymentProvider = ManualProvider() if use_manual else ChapaProvider(settings.chapa_api_key)
    try:
        intent = await withdrawals.request_withdrawal(
            pool,
            redis,
            provider,
            user_id=user.id,
            amount=amount,
            method_kind=withdrawals.DEFAULT_METHOD_KIND,
            account_ref=account_ref,
            holder_name=holder_name,
            min_withdraw=settings.min_withdraw_etb,
            auto_approve_limit=settings.auto_approve_withdraw_etb,
            kyc_threshold=settings.kyc_required_above_etb,
            chargeback_window_minutes=settings.withdraw_chargeback_window_minutes,
            max_withdrawals_per_day=settings.max_withdrawals_per_day,
            force_review=use_manual,
        )
    except withdrawals.BelowMinimumWithdrawal:
        await notifier.send(
            message.chat.id, t("withdraw.below_minimum", language, min=str(settings.min_withdraw_etb))
        )
        return
    except withdrawals.InsufficientAvailableBalance:
        await notifier.send(message.chat.id, t("wallet.insufficient", language))
        return
    except withdrawals.KycLevelTooLow:
        await notifier.send(message.chat.id, t("withdraw.kyc_required", language))
        return
    except withdrawals.RecentReversibleDeposit:
        await notifier.send(message.chat.id, t("withdraw.recent_deposit", language))
        return
    except withdrawals.UnknownWithdrawer:
        await notifier.send(message.chat.id, t("error.generic", language))
        return

    if intent.status == withdrawals.STATUS_APPROVED:
        await notifier.send(message.chat.id, t("withdraw.requested_approved", language, amount=str(amount)))
    else:
        await notifier.send(message.chat.id, t("withdraw.requested_review", language, amount=str(amount)))


@router.message(Command("limits"))
async def cmd_limits(
    message: Message, command: CommandObject, pool: asyncpg.Pool, notifier: Notifier
) -> None:
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return

    parsed = responsible_gaming.parse_limits_command(command.args or "")
    if parsed.action is None:
        await notifier.send(message.chat.id, t("limits.usage", language))
        return
    assert parsed.value is not None

    if parsed.action is responsible_gaming.LimitsAction.SET_DEPOSIT:
        try:
            amount = Decimal(parsed.value)
        except InvalidOperation:
            await notifier.send(message.chat.id, t("limits.invalid_amount", language))
            return
        if amount <= 0:
            await notifier.send(message.chat.id, t("limits.invalid_amount", language))
            return
        async with pool.acquire() as conn:
            applied_now = await responsible_gaming.set_deposit_limit(conn, user.id, amount)
        if applied_now:
            await notifier.send(message.chat.id, t("limits.deposit_set", language, amount=str(amount)))
        else:
            await notifier.send(
                message.chat.id, t("limits.deposit_set_pending", language, amount=str(amount))
            )
        return

    if parsed.action is responsible_gaming.LimitsAction.SET_LOSS:
        try:
            amount = Decimal(parsed.value)
        except InvalidOperation:
            await notifier.send(message.chat.id, t("limits.invalid_amount", language))
            return
        if amount <= 0:
            await notifier.send(message.chat.id, t("limits.invalid_amount", language))
            return
        async with pool.acquire() as conn:
            applied_now = await responsible_gaming.set_loss_limit(conn, user.id, amount)
        if applied_now:
            await notifier.send(message.chat.id, t("limits.loss_set", language, amount=str(amount)))
        else:
            await notifier.send(
                message.chat.id, t("limits.loss_set_pending", language, amount=str(amount))
            )
        return

    if parsed.action is responsible_gaming.LimitsAction.COOL_OFF:
        hours = responsible_gaming.COOLOFF_DURATIONS_HOURS.get(parsed.value.lower())
        if hours is None:
            await notifier.send(message.chat.id, t("limits.cooloff_invalid_duration", language))
            return
        async with pool.acquire() as conn:
            await responsible_gaming.cool_off(conn, user.id, hours)
        await notifier.send(message.chat.id, t("limits.cooloff_set", language, hours=hours))
        return

    if parsed.action is responsible_gaming.LimitsAction.SELF_EXCLUDE:
        if parsed.value.lower() != responsible_gaming.SELF_EXCLUDE_CONFIRMATION_TOKEN:
            await notifier.send(message.chat.id, t("limits.selfexclude_confirm", language))
            return
        await responsible_gaming.self_exclude(pool, user.id)
        await notifier.send(
            message.chat.id,
            t(
                "limits.selfexclude_done",
                language,
                days=responsible_gaming.SELF_EXCLUSION_MINIMUM_DAYS,
            ),
        )


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


@router.message(F.photo)
async def on_photo(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    """Optional receipt-proof mechanism for a manual deposit (P1: keep
    taking deposits when Chapa is unavailable) -- Telegram-native, no new
    object storage needed, since the photo already lives on Telegram's
    own servers the moment it's sent here. No conversational state
    required: correlates to whichever manual deposit request this player
    most recently submitted that's still awaiting review with no receipt
    attached yet (manual.attach_receipt_to_latest_pending_deposit).
    Registered users only -- an unregistered sender has no deposit to
    attach anything to in the first place.
    """
    assert message.from_user is not None and message.photo is not None
    language = await _language_for(pool, message.from_user.id)
    user = await get_registered_user(pool, message.from_user.id)
    if user is None:
        return

    # Telegram sends the same photo at several resolutions; the last
    # entry is always the highest-resolution one.
    file_id = message.photo[-1].file_id
    attached_to = await manual.attach_receipt_to_latest_pending_deposit(
        pool, user_id=user.id, telegram_file_id=file_id
    )
    if attached_to is None:
        await notifier.send(message.chat.id, t("manual_deposit.no_pending_request", language))
        return
    await notifier.send(message.chat.id, t("manual_deposit.receipt_received", language))


async def _is_active_payment_agent(message: Message, pool: asyncpg.Pool) -> bool:
    if message.from_user is None:
        return False
    row = await pool.fetchval(
        "SELECT 1 FROM payment_agents WHERE telegram_user_id = $1 AND is_active", message.from_user.id
    )
    return row is not None


@router.message(F.text, _is_active_payment_agent)
async def on_agent_sms(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    """Telegram payment-agent SMS-forwarding channel (CTO directive
    section 115) -- a thin adapter: the filter above authenticates the
    sender against payment_agents (empty today -- ships with zero
    authorized agents), then this just hands the raw text to the one real
    ingestion pipeline (telebirr_ingest.ingest_sms_evidence) and reports
    back the parse outcome only. No financial/parsing logic lives here --
    MacroDroid's HTTP route (services/payments/app.py) enforces the exact
    same rules through the exact same function, never a second copy.

    Registered as a (F.text, filter) pair rather than a bare F.text so
    aiogram's own routing falls through to on_menu_text below for every
    non-agent sender, instead of this handler swallowing ordinary
    players' menu-button presses.
    """
    assert message.from_user is not None
    language = await _language_for(pool, message.from_user.id)
    raw_sms = (message.text or "").strip()

    outcome = await ingest_sms_evidence(
        pool, raw_sms=raw_sms, source=SOURCE_TELEGRAM_AGENT, source_ref=str(message.from_user.id)
    )
    reference = outcome.external_reference or ""
    reason = outcome.reason or ""

    if outcome.status == STATUS_UNPARSEABLE:
        await notifier.send(message.chat.id, t("agent.ingest_unparseable", language, reason=reason))
    elif outcome.status == STATUS_INGESTED_AVAILABLE:
        await notifier.send(message.chat.id, t("agent.ingest_available", language, reference=reference))
    elif outcome.status == STATUS_INGESTED_REJECTED:
        await notifier.send(
            message.chat.id, t("agent.ingest_rejected", language, reference=reference, reason=reason)
        )
    elif outcome.status == STATUS_DUPLICATE:
        await notifier.send(message.chat.id, t("agent.ingest_duplicate", language, reference=reference))
    elif outcome.status == STATUS_CONFLICTING_DUPLICATE:
        await notifier.send(message.chat.id, t("agent.ingest_conflicting", language, reference=reference))


@router.message(F.text)
async def on_menu_text(message: Message, pool: asyncpg.Pool, redis: Redis, notifier: Notifier, settings: Settings) -> None:
    """Handle localized ReplyKeyboard button presses without relying on
    exact static text matching. Telegram delivers the button text in the
    user's own language, so we resolve the sender's language and compare
    against the known menu labels for that locale."""
    assert message.from_user is not None
    text = (message.text or "").strip()
    language = await _language_for(pool, message.from_user.id)
    registered = await get_registered_user(pool, message.from_user.id) is not None

    mapping = {
        t("menu.play", language): (MenuAction.PLAY, True),
        t("menu.balance", language): (MenuAction.BALANCE, False),
        t("menu.deposit", language): (MenuAction.DEPOSIT, True),
        t("menu.withdraw", language): (MenuAction.WITHDRAW, True),
        t("menu.invite", language): (MenuAction.INVITE, False),
        t("menu.rules", language): (MenuAction.RULES, False),
    }

    matched = mapping.get(text)
    if not matched:
        if not registered:
            await notifier.send(message.chat.id, t("register.use_button", language), reply_markup=registration_keyboard(language))
        return

    action, needs_registration = matched
    if needs_registration and not registered:
        await notifier.send(message.chat.id, t("error.not_registered", language))
        return

    if action == MenuAction.PLAY:
        await cmd_play(message, pool, notifier, settings)
    elif action == MenuAction.BALANCE:
        await cmd_balance(message, pool, notifier)
    elif action == MenuAction.DEPOSIT:
        empty_command = CommandObject(command="deposit", args="")
        await cmd_deposit(message, empty_command, pool, redis, notifier, settings)
    elif action == MenuAction.WITHDRAW:
        empty_command = CommandObject(command="withdraw", args="")
        await cmd_withdraw(message, empty_command, pool, redis, notifier, settings)
    elif action == MenuAction.INVITE:
        await cmd_invite(message, pool, notifier, settings)
    elif action == MenuAction.RULES:
        await cmd_rules(message, pool, notifier)


@router.message()
async def on_unhandled_message(message: Message, pool: asyncpg.Pool, notifier: Notifier) -> None:
    """Final catch-all: ignore everything we do not explicitly handle.
    Registered users get silent drop for unknown text/media; unregistered
    users get the registration prompt."""
    assert message.from_user is not None
    user = await get_registered_user(pool, message.from_user.id)
    if user is not None:
        return
    language = await _language_for(pool, message.from_user.id)
    await notifier.send(
        message.chat.id,
        t("register.use_button", language),
        reply_markup=registration_keyboard(language),
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
