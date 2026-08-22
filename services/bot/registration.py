"""Registration: contact validation, phone normalization, user creation.

The one rule that matters most here (spec section 7.2): Telegram lets any
user forward any contact card, verified or not. A shared contact only
proves phone ownership if its `user_id` matches the sender who shared it --
anything else is not proof of anything and must be rejected outright.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from services.bot.phone import normalize_ethiopian_phone


class ContactMismatch(Exception):
    """The shared contact belongs to someone other than the sender."""


class InvalidPhone(Exception):
    """The contact's phone number isn't a recognizable Ethiopian number."""


class PhoneAlreadyRegistered(Exception):
    """This phone number is already tied to a different account."""


@dataclass(frozen=True)
class RegisteredUser:
    id: int
    telegram_id: int
    display_name: str
    phone_e164: str
    is_new: bool


async def register_from_contact(
    pool: asyncpg.Pool,
    *,
    sender_telegram_id: int,
    contact_user_id: int | None,
    contact_phone: str,
    display_name: str,
    referred_by_telegram_id: int | None = None,
) -> RegisteredUser:
    if contact_user_id != sender_telegram_id:
        raise ContactMismatch()

    phone = normalize_ethiopian_phone(contact_phone)
    if phone is None:
        raise InvalidPhone()

    existing = await pool.fetchrow(
        "SELECT id, display_name, phone_e164 FROM users WHERE telegram_id = $1",
        sender_telegram_id,
    )
    if existing is not None:
        return RegisteredUser(
            existing["id"], sender_telegram_id, existing["display_name"], existing["phone_e164"], is_new=False
        )

    referred_by_id: int | None = None
    if referred_by_telegram_id is not None:
        referrer = await pool.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1", referred_by_telegram_id
        )
        if referrer is not None:
            referred_by_id = referrer["id"]

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (telegram_id, display_name, phone_e164, referred_by)
            VALUES ($1, $2, $3, $4)
            RETURNING id, display_name, phone_e164
            """,
            sender_telegram_id,
            display_name,
            phone,
            referred_by_id,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        if exc.constraint_name and "phone_e164" in exc.constraint_name:
            raise PhoneAlreadyRegistered() from exc
        # telegram_id conflict: a concurrent /start + contact-share race
        # already created this user microseconds earlier -- read it back.
        row = await pool.fetchrow(
            "SELECT id, display_name, phone_e164 FROM users WHERE telegram_id = $1",
            sender_telegram_id,
        )
        assert row is not None
        return RegisteredUser(
            row["id"], sender_telegram_id, row["display_name"], row["phone_e164"], is_new=False
        )

    assert row is not None
    return RegisteredUser(row["id"], sender_telegram_id, row["display_name"], row["phone_e164"], is_new=True)


async def get_registered_user(pool: asyncpg.Pool, telegram_id: int) -> RegisteredUser | None:
    row = await pool.fetchrow(
        "SELECT id, display_name, phone_e164 FROM users WHERE telegram_id = $1", telegram_id
    )
    if row is None:
        return None
    return RegisteredUser(row["id"], telegram_id, row["display_name"], row["phone_e164"], is_new=False)
