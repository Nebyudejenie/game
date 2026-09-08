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
    START = "start"
    SUPPORT = "support"


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


def main_menu_keyboard(language: str) -> ReplyKeyboardMarkup:
    # Launching the Mini App no longer lives on this keyboard at all: the
    # bot's own chat-menu button (services/bot/verify_menu_button.py) and
    # the /play command are the real launch surfaces now, so this row's
    # first button is a plain text button, same shape as every other
    # button here.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t("menu.start", language)), KeyboardButton(text=t("menu.balance", language))],
            [
                KeyboardButton(text=t("menu.deposit", language)),
                KeyboardButton(text=t("menu.support", language)),
            ],
            [
                KeyboardButton(text=t("menu.invite", language)),
                KeyboardButton(text=t("menu.rules", language)),
            ],
        ],
        resize_keyboard=True,
    )
