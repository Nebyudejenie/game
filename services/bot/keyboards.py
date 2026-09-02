"""ReplyKeyboard builders (spec section 7.3)."""

from __future__ import annotations

from enum import Enum

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from services.bot.i18n import t


class MenuAction(Enum):
    """Internal routing tags for main_menu_keyboard()'s buttons -- never
    displayed to a user (the button text itself always comes from
    t("menu.*", language)). Defined here, not in handlers.py, so these
    aren't raw string literals in the one file
    tests/unit/test_bot_no_hardcoded_strings.py holds to "every
    user-facing string comes from i18n.t(...)".
    """

    PLAY = "play"
    BALANCE = "balance"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    INVITE = "invite"
    RULES = "rules"


def registration_keyboard(language: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t("register.button", language), request_contact=True)],
            [KeyboardButton(text=t("register.instructions_button", language))],
        ],
        resize_keyboard=True,
    )


def deposit_checkout_keyboard(language: str, *, checkout_url: str, amount: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("deposit.checkout_button", language, amount=amount), url=checkout_url)]
        ]
    )


def open_wallet_keyboard(language: str, *, miniapp_url: str) -> InlineKeyboardMarkup:
    """P1: when the automatic provider is unavailable, /deposit and
    /withdraw point the player at the Mini App's own wallet screen
    (destination picker, reference input) instead of trying to collect a
    multi-field manual request as bot command args. Only ever called once
    the caller has already confirmed miniapp_url is non-empty -- same
    "never ship a button pointing nowhere" discipline main_menu_keyboard's
    own play_button already follows.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("wallet_open_button", language), web_app=WebAppInfo(url=miniapp_url))]
        ]
    )


def main_menu_keyboard(language: str, *, miniapp_url: str = "") -> ReplyKeyboardMarkup:
    # Telegram requires a valid HTTPS URL for a web_app button -- until the
    # Mini App (Phase 4) is deployed and MINIAPP_URL is configured, Play is
    # a plain button whose handler honestly says the game screen isn't open
    # yet, rather than shipping a button that would error or point nowhere.
    play_button = (
        KeyboardButton(text=t("menu.play", language), web_app=WebAppInfo(url=miniapp_url))
        if miniapp_url
        else KeyboardButton(text=t("menu.play", language))
    )
    return ReplyKeyboardMarkup(
        keyboard=[
            [play_button, KeyboardButton(text=t("menu.balance", language))],
            [
                KeyboardButton(text=t("menu.deposit", language)),
                KeyboardButton(text=t("menu.withdraw", language)),
            ],
            [
                KeyboardButton(text=t("menu.invite", language)),
                KeyboardButton(text=t("menu.rules", language)),
            ],
        ],
        resize_keyboard=True,
    )
