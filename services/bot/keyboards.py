"""ReplyKeyboard builders (spec section 7.3)."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from services.bot.i18n import t


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
