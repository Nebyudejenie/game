"""Bot Content admin feature: real Postgres, the real bot_content_sync
refresh loop, and proof that an admin's saved override actually changes
what services/bot/keyboards.py builds -- not just that a row landed in
the database.
"""

import httpx
import pytest

from services.admin import bot_content_queries
from services.bot import bot_content_sync, i18n, keyboards
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin


@pytest.fixture(autouse=True)
async def _reset_i18n_overrides():
    yield
    i18n.set_overrides({})


async def test_set_override_then_refresh_changes_what_the_bot_would_send(pool):
    """The real end-to-end proof: an admin's console action changes an
    actual keyboard the bot builds for a real player, going through the
    exact same refresh path production runs (bot_content_sync.refresh_once),
    never a shortcut that only proves the database row exists.
    """
    admin_id, *_ = await create_test_admin(pool, role="superadmin")

    before_keyboard = keyboards.main_menu_keyboard("am")
    before_label = before_keyboard.keyboard[0][0].text

    await bot_content_queries.set_bot_content_override_admin(
        pool, admin_id=admin_id, key="menu.play", language="am", value="ጨዋታ ጀምር", ip_address=None
    )
    await bot_content_sync.refresh_once(pool)

    after_keyboard = keyboards.main_menu_keyboard("am")
    after_label = after_keyboard.keyboard[0][0].text

    assert after_label == "ጨዋታ ጀምር"
    assert after_label != before_label


async def test_clearing_an_override_reverts_to_the_shipped_default(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    default_value = i18n.default_template("menu.balance", "en")

    await bot_content_queries.set_bot_content_override_admin(
        pool, admin_id=admin_id, key="menu.balance", language="en", value="My Wallet", ip_address=None
    )
    await bot_content_sync.refresh_once(pool)
    assert i18n.t("menu.balance", "en") == "My Wallet"

    cleared = await bot_content_queries.clear_bot_content_override_admin(
        pool, admin_id=admin_id, key="menu.balance", language="en", ip_address=None
    )
    assert cleared is True
    await bot_content_sync.refresh_once(pool)
    assert i18n.t("menu.balance", "en") == default_value


async def test_rejects_an_unknown_key(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(bot_content_queries.UnknownBotContentKey):
        await bot_content_queries.set_bot_content_override_admin(
            pool, admin_id=admin_id, key="not.a.real.key", language="en", value="x", ip_address=None
        )


async def test_rejects_a_value_that_drops_a_required_placeholder(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    # register.success ships with a {name} placeholder every caller relies
    # on -- an override missing it would crash the first real .format(name=...)
    # call downstream instead of failing loudly here, at save time.
    with pytest.raises(bot_content_queries.InvalidBotContentPlaceholders):
        await bot_content_queries.set_bot_content_override_admin(
            pool, admin_id=admin_id, key="register.success", language="en",
            value="Welcome aboard, friend!", ip_address=None,
        )


async def test_rejects_a_value_that_adds_an_unexpected_placeholder(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(bot_content_queries.InvalidBotContentPlaceholders):
        await bot_content_queries.set_bot_content_override_admin(
            pool, admin_id=admin_id, key="menu.play", language="en",
            value="Play, {unexpected_field}!", ip_address=None,
        )


async def test_audit_log_records_the_before_and_after_value(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    await bot_content_queries.set_bot_content_override_admin(
        pool, admin_id=admin_id, key="menu.invite", language="en", value="Invite friends",
        ip_address="10.0.0.9",
    )
    row = await pool.fetchrow(
        "SELECT admin_id, action, target_id, after, ip_address FROM admin_audit_log "
        "WHERE action = 'bot_content.set_override' AND target_id = 'menu.invite:en' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["admin_id"] == admin_id
    assert "Invite friends" in row["after"]
    assert row["ip_address"] == "10.0.0.9"


async def test_list_bot_content_admin_reports_override_status(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    await bot_content_queries.set_bot_content_override_admin(
        pool, admin_id=admin_id, key="menu.withdraw", language="am", value="ገንዘብ አውጣ", ip_address=None
    )
    items = await bot_content_queries.list_bot_content_admin(pool)
    entry = next(i for i in items if i["key"] == "menu.withdraw")
    assert entry["languages"]["am"]["is_overridden"] is True
    assert entry["languages"]["am"]["override_value"] == "ገንዘብ አውጣ"
    assert entry["languages"]["en"]["is_overridden"] is False
    assert entry["languages"]["en"]["current_value"] == entry["languages"]["en"]["default_value"]


# --- RBAC over real HTTP -------------------------------------------------


async def test_finance_and_support_cannot_reach_bot_content_over_http(admin_server, pool):
    for role in ("finance", "support"):
        headers = await _auth_headers(admin_server, pool, role=role)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/bot-content", headers=headers)
        assert response.status_code == 403, f"role {role!r} should not reach /bot-content"


async def test_ops_can_edit_bot_content_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        listing = await client.get(f"{admin_server}/bot-content", headers=headers)
        assert listing.status_code == 200
        assert len(listing.json()) > 0

        update = await client.put(
            f"{admin_server}/bot-content/menu.rules/en", json={"value": "Rules"}, headers=headers
        )
        assert update.status_code == 200

        clear = await client.delete(f"{admin_server}/bot-content/menu.rules/en", headers=headers)
        assert clear.status_code == 200


async def test_invalid_placeholder_over_http_returns_422(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"{admin_server}/bot-content/register.success/en",
            json={"value": "no name placeholder here"},
            headers=headers,
        )
    assert response.status_code == 422
