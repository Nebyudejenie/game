"""Telegram Command Center admin operations: real Postgres, real RBAC
over real HTTP, a real end-to-end dispatcher test proving a disabled
command actually degrades gracefully (not a crash), and a real
notification-relay consumption proving "send test" delivers through the
same pipeline every other admin-originated message uses.
"""

import httpx
import pytest

from services.admin import command_registry_queries
from services.bot import command_registry, notification_relay
from services.bot.notifier import Notifier
from tests.integration.conftest import next_telegram_id
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_bot_handlers import make_bot


@pytest.fixture(autouse=True)
async def _reset_registry_cache():
    """Every test in this file either seeds its own cache state or relies
    on the "no row -> enabled" default -- never leaking a previous test's
    disabled-command state into the next one.
    """
    command_registry.set_cache({})
    yield
    command_registry.set_cache({})


async def test_refresh_once_loads_real_seeded_rows(pool):
    await command_registry.refresh_once(pool)
    config = command_registry.get_config("cmd_balance")
    assert config is not None
    assert config.command == "balance"
    assert config.content_key == "balance.summary"
    assert config.admin_managed is True

    start_config = command_registry.get_config("cmd_start")
    assert start_config is not None
    assert start_config.admin_managed is False


# The real end-to-end "a disabled command degrades gracefully through the
# real dispatcher" test lives in tests/integration/test_bot_handlers.py
# (test_disabling_a_command_makes_a_real_dispatcher_call_degrade_gracefully)
# instead of here -- it needs test_bot_handlers.py's own session-scoped
# bot_ctx/bot_setup fixtures, and importing a session-scoped async fixture
# across test modules is a real, confirmed pytest gotcha: pytest
# instantiated bot_setup a *second* time when it was reachable via both
# its native module and an import here, and aiogram permanently refuses a
# second Dispatcher attachment for the same Router for the rest of the
# process once that happens ("RuntimeError: Router is already attached to
# <Dispatcher ...>"), poisoning every later test in a combined run.
# Keeping dispatcher-dependent tests inside the file that owns those
# fixtures avoids the whole class of bug.


async def test_admin_managed_false_command_cannot_be_disabled(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(command_registry_queries.CommandNotAdminManaged):
        await command_registry_queries.update_command_admin(
            pool, admin_id=admin_id, handler_name="cmd_start", changes={"enabled": False}, reason=None, ip_address=None
        )


async def test_unknown_field_is_rejected(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(command_registry_queries.InvalidCommandField):
        await command_registry_queries.update_command_admin(
            pool, admin_id=admin_id, handler_name="cmd_balance", changes={"handler_name": "cmd_evil"}, reason=None, ip_address=None
        )


async def test_update_produces_a_real_audit_record(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    await command_registry_queries.update_command_admin(
        pool, admin_id=admin_id, handler_name="cmd_rules", changes={"description": "Updated by test"},
        reason="testing audit trail", ip_address="127.0.0.1",
    )
    row = await conn.fetchrow(
        "SELECT * FROM admin_audit_log WHERE admin_id = $1 AND action = 'telegram_commands.update' "
        "ORDER BY id DESC LIMIT 1",
        admin_id,
    )
    assert row is not None
    assert row["target_id"] == "cmd_rules"
    assert row["reason"] == "testing audit trail"
    assert "Updated by test" in row["after"]

    # Cleanup -- shared, persistent row.
    await command_registry_queries.update_command_admin(
        pool, admin_id=admin_id, handler_name="cmd_rules", changes={"description": "Static Bingo rules text"},
        reason="test cleanup", ip_address=None,
    )


async def test_preview_renders_sample_placeholders_not_real_values(pool):
    preview = await command_registry_queries.preview_command_content_admin(
        pool, handler_name="cmd_balance", language="en"
    )
    assert preview["content_key"] == "balance.summary"
    assert set(preview["placeholders"]) == {"cash", "bonus", "locked"}
    assert "[cash]" in preview["rendered_preview"]
    assert "[bonus]" in preview["rendered_preview"]
    assert "[locked]" in preview["rendered_preview"]


async def test_preview_with_no_placeholders_renders_verbatim(pool):
    preview = await command_registry_queries.preview_command_content_admin(
        pool, handler_name="cmd_rules", language="en"
    )
    assert preview["placeholders"] == []
    assert preview["rendered_preview"] == preview["template"]


async def test_preview_unknown_handler_raises(pool):
    with pytest.raises(command_registry_queries.UnknownBotCommand):
        await command_registry_queries.preview_command_content_admin(
            pool, handler_name="cmd_does_not_exist", language="en"
        )


async def test_preview_missing_content_key_raises(pool):
    with pytest.raises(command_registry_queries.MissingContentKey):
        await command_registry_queries.preview_command_content_admin(
            pool, handler_name="cmd_deposit", language="en"
        )


async def test_send_test_delivers_through_the_real_relay_to_only_the_named_recipient(pool, redis):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    target_telegram_id = next_telegram_id()

    await command_registry_queries.send_test_command_admin(
        pool, redis, admin_id=admin_id, handler_name="cmd_rules", target_telegram_id=target_telegram_id,
        language="en", ip_address="127.0.0.1",
    )

    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    try:
        delivered = await notification_relay.process_next(pool, redis, notifier, consumer_name="cmd-test-send")
        assert delivered is True
        import asyncio

        await asyncio.sleep(0.1)
        assert len(session.sent) == 1
        assert session.sent[0].chat_id == target_telegram_id
        assert "[TEST" in session.sent[0].text
    finally:
        await notifier.stop()


async def test_list_commands_admin_reports_no_data_for_a_handler_with_no_metrics_url(pool):
    commands = await command_registry_queries.list_commands_admin(pool, bot_metrics_url="")
    assert len(commands) == 18
    for c in commands:
        assert c["metrics"] is None


# --- RBAC boundary, over real HTTP -----------------------------------


async def test_support_can_view_commands_but_not_manage_them_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        view = await client.get(f"{admin_server}/telegram/commands", headers=headers)
        assert view.status_code == 200

        manage = await client.patch(
            f"{admin_server}/telegram/commands/cmd_rules",
            headers=headers,
            json={"changes": {"description": "hacked"}},
        )
        assert manage.status_code == 403


async def test_ops_can_manage_commands_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"{admin_server}/telegram/commands/cmd_support",
            headers=headers,
            json={"changes": {"description": "Static support contact info"}, "reason": "no-op test"},
        )
        assert response.status_code == 200


async def test_disabling_a_structural_command_over_http_returns_422(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"{admin_server}/telegram/commands/cmd_start",
            headers=headers,
            json={"changes": {"enabled": False}},
        )
        assert response.status_code == 422


async def test_unauthenticated_requests_are_rejected_over_http(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/telegram/commands")
        assert response.status_code in (401, 403)
