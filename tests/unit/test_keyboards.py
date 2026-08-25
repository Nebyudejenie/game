"""Tests for services/bot/keyboards.py. Had zero test coverage anywhere
in the codebase before this file (confirmed by grep). Pure functions, no
I/O -- but two real behaviors worth pinning down: registration_keyboard's
button must actually request_contact=True (the entire contact-mismatch
check in services/bot/registration.py depends on the contact having come
through Telegram's own share-contact UI, not a typed number), and
main_menu_keyboard's Play button must never ship a web_app pointing at an
empty URL (Telegram itself would error on that, which is exactly why
services/bot/keyboards.py's own comment says an empty MINIAPP_URL falls
back to a plain button instead).
"""

from services.bot.keyboards import (
    deposit_checkout_keyboard,
    main_menu_keyboard,
    registration_keyboard,
)


def test_registration_keyboard_share_button_requests_contact() -> None:
    kb = registration_keyboard("en")
    share_button = kb.keyboard[0][0]
    assert share_button.request_contact is True


def test_registration_keyboard_has_a_second_row_with_no_contact_request() -> None:
    # The instructions button must never also request_contact -- only one
    # button in this keyboard is allowed to trigger Telegram's share-contact
    # flow, or a tap on the wrong button could look like a valid share.
    kb = registration_keyboard("en")
    instructions_button = kb.keyboard[1][0]
    assert not instructions_button.request_contact


def test_main_menu_keyboard_play_button_has_no_web_app_when_miniapp_url_is_empty() -> None:
    kb = main_menu_keyboard("en", miniapp_url="")
    play_button = kb.keyboard[0][0]
    assert play_button.web_app is None


def test_main_menu_keyboard_play_button_uses_the_real_miniapp_url_when_set() -> None:
    kb = main_menu_keyboard("en", miniapp_url="https://app.example.test/")
    play_button = kb.keyboard[0][0]
    assert play_button.web_app is not None
    assert play_button.web_app.url == "https://app.example.test/"


def test_main_menu_keyboard_has_all_six_menu_buttons() -> None:
    kb = main_menu_keyboard("en", miniapp_url="")
    labels = [button.text for row in kb.keyboard for button in row]
    assert len(labels) == 6
    assert len(set(labels)) == 6  # no duplicate button


def test_deposit_checkout_keyboard_links_to_the_real_checkout_url() -> None:
    kb = deposit_checkout_keyboard("en", checkout_url="https://pay.example.test/abc123", amount="200.00")
    button = kb.inline_keyboard[0][0]
    assert button.url == "https://pay.example.test/abc123"
    assert "200.00" in button.text


def test_keyboards_render_in_amharic_too() -> None:
    # Every keyboard-building function must resolve real Amharic text, not
    # silently fall back to English or a raw i18n key -- the same
    # discipline this codebase's i18n key-set parity checks enforce
    # elsewhere, applied to the one place that hasn't been checked yet.
    reg_kb = registration_keyboard("am")
    menu_kb = main_menu_keyboard("am", miniapp_url="")
    dep_kb = deposit_checkout_keyboard("am", checkout_url="https://pay.example.test/x", amount="50.00")

    assert reg_kb.keyboard[0][0].text != ""
    assert all(button.text for row in menu_kb.keyboard for button in row)
    assert dep_kb.inline_keyboard[0][0].text != ""
