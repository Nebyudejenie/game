"""Unit tests for services/bot/command_registry.py's in-process cache and
gate middleware -- no real Postgres needed for these (refresh_once()'s
own real-DB behavior is covered by tests/integration/
test_command_registry_admin.py).
"""

from unittest.mock import AsyncMock

from services.bot import command_registry
from services.bot.command_registry import CommandConfig


def _config(handler_name: str, **overrides: object) -> CommandConfig:
    defaults = dict(
        handler_name=handler_name,
        command=handler_name,
        display_name=handler_name,
        description="",
        category="general",
        enabled=True,
        visible=True,
        sort_order=0,
        cooldown_seconds=0,
        rate_limit_per_minute=None,
        content_key=None,
        analytics_key=None,
        admin_managed=True,
    )
    defaults.update(overrides)
    return CommandConfig(**defaults)  # type: ignore[arg-type]


def test_a_handler_with_no_row_defaults_to_enabled():
    command_registry.set_cache({})
    assert command_registry.is_enabled("cmd_never_registered") is True


def test_an_explicitly_disabled_handler_is_reported_disabled():
    command_registry.set_cache({"cmd_balance": _config("cmd_balance", enabled=False)})
    try:
        assert command_registry.is_enabled("cmd_balance") is False
    finally:
        command_registry.set_cache({})


def test_an_explicitly_enabled_handler_is_reported_enabled():
    command_registry.set_cache({"cmd_balance": _config("cmd_balance", enabled=True)})
    try:
        assert command_registry.is_enabled("cmd_balance") is True
    finally:
        command_registry.set_cache({})


async def test_gate_middleware_calls_through_when_enabled():
    command_registry.set_cache({})
    try:
        called = False

        async def cmd_example(event, data):
            nonlocal called
            called = True
            return "ok"

        from types import SimpleNamespace

        data = {"handler": SimpleNamespace(callback=cmd_example)}
        result = await command_registry.command_gate_middleware(cmd_example, object(), data)
        assert called is True
        assert result == "ok"
    finally:
        command_registry.set_cache({})


async def test_gate_middleware_blocks_and_increments_blocked_metric_when_disabled():
    from packages.core import metrics

    command_registry.set_cache({"cmd_example": _config("cmd_example", enabled=False)})
    try:
        called = False

        async def cmd_example(event, data):
            nonlocal called
            called = True
            return "ok"

        from types import SimpleNamespace

        from aiogram.types import Chat, Message, User

        user = User(id=999, is_bot=False, first_name="Test")
        message = Message(
            message_id=1,
            date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            chat=Chat(id=999, type="private"),
            from_user=user,
            text="/example",
        )
        notifier = AsyncMock()
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)  # unregistered -- irrelevant to this test

        data = {"handler": SimpleNamespace(callback=cmd_example), "pool": pool, "notifier": notifier}
        blocked_before = metrics.telegram_command_blocked_total.labels(handler="cmd_example")._value.get()

        result = await command_registry.command_gate_middleware(cmd_example, message, data)

        assert called is False, "a disabled command's real handler must never run"
        assert result is None
        assert notifier.send.await_count == 1
        assert (
            metrics.telegram_command_blocked_total.labels(handler="cmd_example")._value.get()
            == blocked_before + 1
        )
    finally:
        command_registry.set_cache({})
