"""Registration: contact validation, phone normalization, user creation.

The one rule that matters most here (spec section 7.2): Telegram lets any
user forward any contact card, verified or not. A shared contact only
proves phone ownership if its `user_id` matches the sender who shared it --
anything else is not proof of anything and must be rejected outright.

Phone numbers are stored encrypted (spec section 9.2) -- see
packages/core/phone_crypto.py for why that's two columns
(`phone_e164_encrypted` for confidentiality, `phone_lookup_hash` for the
UNIQUE constraint and exact-match lookups a random-nonce ciphertext can't
support). Every read here decrypts; every write encrypts and hashes.
`RegisteredUser.phone_e164` still carries the plain E.164 string, same as
before -- callers elsewhere in the bot never need to know storage changed.

Not every `users` row has a phone on file, though: services/gateway
/queries.py's get_or_create_user_by_telegram_id() lazily creates a row
with just a telegram_id for anyone who opens the Mini App before ever
messaging the bot. This module treats that as "not actually registered
yet" (spec section 7.2's registration is the contact-share flow, not
mere row existence) -- get_registered_user() returns None for it, and
register_from_contact() completes registration in place by attaching the
just-validated contact's phone to that same row, rather than creating a
duplicate account or crashing on the missing phone.

Spec section 12's age gate ("18+ declaration at registration") is recorded
here too: `users.age_confirmed_at` is set the moment registration first
completes, on the theory that the declaration text shown alongside the
share-contact prompt (services/bot/keyboards.py's registration_keyboard,
services/bot/locales/*.json's `register.prompt`) and the act of actually
sharing a real, matching contact together constitute the declaration --
the same "by continuing you confirm..." pattern most consent flows use,
not a separate confirmation step. `COALESCE`d in both write paths below
so a user re-registering (already has a phone) or completing a
previously-phoneless row never has their original declaration timestamp
overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from packages.core.phone_crypto import decrypt_phone, encrypt_phone, phone_lookup_hash
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


async def _attach_phone_to_existing_user(
    pool: asyncpg.Pool, user_id: int, phone: str, referred_by_id: int | None
) -> None:
    """Attaches a freshly-validated contact's phone to a user row that
    already exists but has none on file -- see this module's own
    docstring for why such a row can exist at all.

    Also records a pending referral if one applies. A code review pass
    caught that this path never touched `referred_by` at all -- silently
    dropping referral credit for anyone whose `users` row predated their
    contact share (e.g. the gateway's own lazy
    get_or_create_user_by_telegram_id() row from opening the Mini App
    first), even though handlers.py's on_contact() still unconditionally
    cleared their pending referral on any non-exception return.
    `COALESCE` keeps whatever `referred_by` this row already has rather
    than overwriting it -- it should always be NULL the first time a row
    reaches here (it's only ever set here or at INSERT time, never
    cleared), but not overwriting an already-attributed referral is the
    safe default regardless.
    """
    try:
        await pool.execute(
            """
            UPDATE users
            SET phone_e164_encrypted = $2, phone_lookup_hash = $3, referred_by = COALESCE(referred_by, $4),
                age_confirmed_at = COALESCE(age_confirmed_at, now())
            WHERE id = $1
            """,
            user_id,
            encrypt_phone(phone),
            phone_lookup_hash(phone),
            referred_by_id,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        if exc.constraint_name and "phone_lookup_hash" in exc.constraint_name:
            raise PhoneAlreadyRegistered() from exc
        raise


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

    # Resolved once, up front, and reused by every path below that can
    # end up recording a referral (a brand-new INSERT, or attaching a
    # phone to an existing phoneless row) -- a single computation point
    # rather than duplicating it per-branch is what closes off the whole
    # class of "this path forgot to handle referred_by" bug.
    referred_by_id: int | None = None
    if referred_by_telegram_id is not None:
        referrer = await pool.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1", referred_by_telegram_id
        )
        if referrer is not None:
            referred_by_id = referrer["id"]

    existing = await pool.fetchrow(
        "SELECT id, display_name, phone_e164_encrypted FROM users WHERE telegram_id = $1",
        sender_telegram_id,
    )
    if existing is not None:
        existing_phone_blob = existing["phone_e164_encrypted"]
        if existing_phone_blob is None:
            # A row with no phone -- created by the gateway's own lazy
            # get_or_create_user_by_telegram_id() for someone who opened
            # the Mini App before ever messaging the bot. The contact
            # just shared and validated above completes registration for
            # real, in place, rather than crashing on the missing phone.
            await _attach_phone_to_existing_user(pool, existing["id"], phone, referred_by_id)
            return RegisteredUser(
                existing["id"], sender_telegram_id, existing["display_name"], phone, is_new=False
            )
        return RegisteredUser(
            existing["id"],
            sender_telegram_id,
            existing["display_name"],
            decrypt_phone(bytes(existing_phone_blob)),
            is_new=False,
        )

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users
                (telegram_id, display_name, phone_e164_encrypted, phone_lookup_hash, referred_by, age_confirmed_at)
            VALUES ($1, $2, $3, $4, $5, now())
            RETURNING id, display_name
            """,
            sender_telegram_id,
            display_name,
            encrypt_phone(phone),
            phone_lookup_hash(phone),
            referred_by_id,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        if exc.constraint_name and "phone_lookup_hash" in exc.constraint_name:
            raise PhoneAlreadyRegistered() from exc
        # telegram_id conflict: either a concurrent /start + contact-share
        # race already created this user microseconds earlier (that row
        # will have a phone -- its own INSERT set one), or we collided
        # with a gateway-lazy-created phoneless row instead (same fix as
        # the existing-user branch above).
        row = await pool.fetchrow(
            "SELECT id, display_name, phone_e164_encrypted FROM users WHERE telegram_id = $1",
            sender_telegram_id,
        )
        assert row is not None
        row_phone_blob = row["phone_e164_encrypted"]
        if row_phone_blob is None:
            await _attach_phone_to_existing_user(pool, row["id"], phone, referred_by_id)
            return RegisteredUser(row["id"], sender_telegram_id, row["display_name"], phone, is_new=False)
        return RegisteredUser(
            row["id"],
            sender_telegram_id,
            row["display_name"],
            decrypt_phone(bytes(row_phone_blob)),
            is_new=False,
        )

    assert row is not None
    return RegisteredUser(row["id"], sender_telegram_id, row["display_name"], phone, is_new=True)


async def get_registered_user(pool: asyncpg.Pool, telegram_id: int) -> RegisteredUser | None:
    row = await pool.fetchrow(
        "SELECT id, display_name, phone_e164_encrypted FROM users WHERE telegram_id = $1", telegram_id
    )
    if row is None or row["phone_e164_encrypted"] is None:
        # No phone on file means registration (spec section 7.2's
        # contact-share flow) was never actually completed -- including a
        # row the gateway lazily created for someone who opened the Mini
        # App before ever messaging the bot. Treat it the same as "no
        # row at all" rather than crashing on the missing phone.
        return None
    return RegisteredUser(
        row["id"], telegram_id, row["display_name"], decrypt_phone(bytes(row["phone_e164_encrypted"])), is_new=False
    )
