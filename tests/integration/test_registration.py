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


async def _create_phoneless_user(pool, telegram_id: int) -> int:
    # Mirrors services/gateway/queries.py's get_or_create_user_by_telegram_id()
    # exactly -- the real path that creates a users row with no phone at
    # all, for anyone who opens the Mini App before ever messaging the bot.
    row = await pool.fetchrow(
        "INSERT INTO users (telegram_id, display_name) VALUES ($1, $2) RETURNING id",
        telegram_id,
        "Mini App User",
    )
    assert row is not None
    return int(row["id"])


async def test_get_registered_user_returns_none_for_a_phoneless_row(pool):
    # A real regression: this used to crash with TypeError
    # ("cannot convert 'NoneType' object to bytes") instead of correctly
    # reporting "not registered" for a Mini-App-first user who then
    # messages the bot (e.g. /start, /balance, /deposit -- every handler
    # that calls get_registered_user).
    telegram_id = next_telegram_id()
    await _create_phoneless_user(pool, telegram_id)
    assert await get_registered_user(pool, telegram_id) is None


async def test_register_from_contact_completes_registration_for_a_phoneless_row(pool):
    # Same real regression, the other call site: a Mini-App-first user
    # who then shares their contact with the bot to actually register
    # must have their phone attached to the existing row, not crash.
    telegram_id = next_telegram_id()
    user_id = await _create_phoneless_user(pool, telegram_id)
    phone = unique_phone()

    user = await register_from_contact(
        pool,
        sender_telegram_id=telegram_id,
        contact_user_id=telegram_id,
        contact_phone=phone,
        display_name="Mini App User",
    )

    assert user.id == user_id  # the same row, not a duplicate account
    assert user.phone_e164 == phone
    assert user.is_new is False

    # And it's really persisted, readable back through the normal path.
    fetched = await get_registered_user(pool, telegram_id)
    assert fetched is not None
    assert fetched.phone_e164 == phone


async def test_register_from_contact_records_referral_for_a_phoneless_row(pool):
    # A code review pass caught that _attach_phone_to_existing_user() --
    # the path a Mini-App-first user's contact-share goes through -- never
    # touched referred_by at all, silently dropping referral credit for
    # anyone whose users row predated their contact share, even though
    # handlers.py's on_contact() still unconditionally cleared their
    # pending referral once registration returned without raising.
    referrer_id = next_telegram_id()
    await register_from_contact(
        pool, sender_telegram_id=referrer_id, contact_user_id=referrer_id,
        contact_phone=unique_phone(), display_name="Referrer",
    )

    telegram_id = next_telegram_id()
    user_id = await _create_phoneless_user(pool, telegram_id)

    user = await register_from_contact(
        pool,
        sender_telegram_id=telegram_id,
        contact_user_id=telegram_id,
        contact_phone=unique_phone(),
        display_name="Mini App User",
        referred_by_telegram_id=referrer_id,
    )
    assert user.id == user_id

    row = await pool.fetchrow("SELECT referred_by FROM users WHERE id = $1", user_id)
    referrer_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", referrer_id)
    assert row["referred_by"] == referrer_row["id"]


async def test_new_registration_records_an_age_confirmation_timestamp(pool):
    # spec section 12: "Age gate: 18+ declaration at registration." The
    # declaration itself is the prompt text shown alongside the
    # share-contact button (services/bot/locales/*.json's register.prompt)
    # -- this is the durable record that it was shown and acted on.
    telegram_id = next_telegram_id()
    user = await register_from_contact(
        pool, sender_telegram_id=telegram_id, contact_user_id=telegram_id,
        contact_phone=unique_phone(), display_name="Nebyu",
    )
    row = await pool.fetchrow("SELECT age_confirmed_at FROM users WHERE id = $1", user.id)
    assert row["age_confirmed_at"] is not None


async def test_completing_a_phoneless_row_also_records_age_confirmation(pool):
    # The other write path -- a Mini-App-first user completing
    # registration by attaching a phone to their already-existing,
    # phoneless row -- must record the same declaration, not just the
    # brand-new-INSERT path above.
    telegram_id = next_telegram_id()
    user_id = await _create_phoneless_user(pool, telegram_id)

    await register_from_contact(
        pool, sender_telegram_id=telegram_id, contact_user_id=telegram_id,
        contact_phone=unique_phone(), display_name="Mini App User",
    )

    row = await pool.fetchrow("SELECT age_confirmed_at FROM users WHERE id = $1", user_id)
    assert row["age_confirmed_at"] is not None


async def test_re_registering_does_not_reset_the_original_age_confirmation_timestamp(pool):
    telegram_id = next_telegram_id()
    phone = unique_phone()
    await register_from_contact(
        pool, sender_telegram_id=telegram_id, contact_user_id=telegram_id,
        contact_phone=phone, display_name="Nebyu",
    )
    first = await pool.fetchrow(
        "SELECT id, age_confirmed_at FROM users WHERE telegram_id = $1", telegram_id
    )

    await register_from_contact(
        pool, sender_telegram_id=telegram_id, contact_user_id=telegram_id,
        contact_phone=phone, display_name="Nebyu",
    )
    second = await pool.fetchrow(
        "SELECT id, age_confirmed_at FROM users WHERE telegram_id = $1", telegram_id
    )

    assert first["id"] == second["id"]
    assert first["age_confirmed_at"] == second["age_confirmed_at"]


async def test_register_from_contact_still_rejects_a_phone_already_used_elsewhere_for_a_phoneless_row(pool):
    existing_phone = unique_phone()
    other_id = next_telegram_id()
    await register_from_contact(
        pool, sender_telegram_id=other_id, contact_user_id=other_id,
        contact_phone=existing_phone, display_name="Other",
    )

    telegram_id = next_telegram_id()
    await _create_phoneless_user(pool, telegram_id)

    with pytest.raises(PhoneAlreadyRegistered):
        await register_from_contact(
            pool,
            sender_telegram_id=telegram_id,
            contact_user_id=telegram_id,
            contact_phone=existing_phone,
            display_name="Mini App User",
        )
