"""Direct tests for services/bot/registration.py, covering edge cases the
end-to-end handler tests (test_bot_handlers.py) don't exercise directly:
phone reuse across accounts, idempotent re-registration, and referral
linkage.
"""

import pytest

from services.bot.registration import (
    ContactMismatch,
    InvalidPhone,
    PhoneAlreadyRegistered,
    get_registered_user,
    register_from_contact,
)
from tests.integration.test_bot_handlers import unique_phone
from tests.integration.conftest import next_telegram_id


async def test_contact_mismatch_raises(pool):
    with pytest.raises(ContactMismatch):
        await register_from_contact(
            pool,
            sender_telegram_id=next_telegram_id(),
            contact_user_id=next_telegram_id(),
            contact_phone=unique_phone(),
            display_name="X",
        )


async def test_invalid_phone_raises(pool):
    telegram_id = next_telegram_id()
    with pytest.raises(InvalidPhone):
        await register_from_contact(
            pool,
            sender_telegram_id=telegram_id,
            contact_user_id=telegram_id,
            contact_phone="not a phone number",
            display_name="X",
        )


async def test_successful_registration_is_new(pool):
    telegram_id = next_telegram_id()
    user = await register_from_contact(
        pool,
        sender_telegram_id=telegram_id,
        contact_user_id=telegram_id,
        contact_phone=unique_phone(),
        display_name="Nebyu",
    )
    assert user.is_new is True
    assert user.telegram_id == telegram_id


async def test_registering_twice_is_idempotent_not_an_error(pool):
    telegram_id = next_telegram_id()
    phone = unique_phone()
    first = await register_from_contact(
        pool, sender_telegram_id=telegram_id, contact_user_id=telegram_id,
        contact_phone=phone, display_name="Nebyu",
    )
    second = await register_from_contact(
        pool, sender_telegram_id=telegram_id, contact_user_id=telegram_id,
        contact_phone=phone, display_name="Nebyu",
    )
    assert first.id == second.id
    assert first.is_new is True
    assert second.is_new is False


async def test_phone_already_used_by_another_account_is_rejected(pool):
    phone = unique_phone()
    first_id = next_telegram_id()
    await register_from_contact(
        pool,
        sender_telegram_id=first_id,
        contact_user_id=first_id,
        contact_phone=phone,
        display_name="First",
    )

    second_id = next_telegram_id()
    with pytest.raises(PhoneAlreadyRegistered):
        await register_from_contact(
            pool,
            sender_telegram_id=second_id,
            contact_user_id=second_id,
            contact_phone=phone,
            display_name="Second",
        )


async def test_referral_links_new_user_to_referrer(pool):
    referrer_id = next_telegram_id()
    await register_from_contact(
        pool, sender_telegram_id=referrer_id, contact_user_id=referrer_id,
        contact_phone=unique_phone(), display_name="Referrer",
    )

    new_user_id = next_telegram_id()
    new_user = await register_from_contact(
        pool,
        sender_telegram_id=new_user_id,
        contact_user_id=new_user_id,
        contact_phone=unique_phone(),
        display_name="Referee",
        referred_by_telegram_id=referrer_id,
    )

    row = await pool.fetchrow("SELECT referred_by FROM users WHERE id = $1", new_user.id)
    referrer_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", referrer_id)
    assert row["referred_by"] == referrer_row["id"]


async def test_unknown_referrer_is_ignored_not_an_error(pool):
    telegram_id = next_telegram_id()
    user = await register_from_contact(
        pool,
        sender_telegram_id=telegram_id,
        contact_user_id=telegram_id,
        contact_phone=unique_phone(),
        display_name="X",
        referred_by_telegram_id=999_999_999_999,  # doesn't exist
    )
    row = await pool.fetchrow("SELECT referred_by FROM users WHERE id = $1", user.id)
    assert row["referred_by"] is None


async def test_get_registered_user_returns_none_for_unknown(pool):
    assert await get_registered_user(pool, 999_999_999_998) is None
