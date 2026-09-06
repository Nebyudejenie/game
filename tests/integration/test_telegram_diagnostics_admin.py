"""GET /telegram/webhook-health -- real HTTP against the real admin app,
Bot.get_webhook_info() faked at the aiogram class level (the same
approach every getWebhookInfo caller in this codebase would need, since
no real Telegram bot token/network access exists in this test
environment) rather than mocking anything admin-specific.
"""

from datetime import datetime, timezone

import httpx
from aiogram import Bot
from aiogram.types import WebhookInfo

from packages.core.config import get_settings
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin

# conftest.py's own global TELEGRAM_BOT_TOKEN default ("test-bot-token-
# for-suite") does not match Telegram's real numeric-id:hash token shape
# -- fine for every other test in this suite (nothing else ever
# constructs a real aiogram Bot from it directly the way this endpoint
# does), but aiogram.Bot's own __init__ validates token format before any
# patched get_webhook_info() is ever reached, so a genuinely well-formed
# fake token is needed here specifically. Matches test_bot_handlers.py's
# own make_bot() convention exactly.
_WELL_FORMED_FAKE_TOKEN = "123456:FAKE-TEST-TOKEN"


def _fake_webhook_info(**overrides: object) -> WebhookInfo:
    defaults: dict[str, object] = {
        "url": "https://bot.test/webhook",
        "has_custom_certificate": False,
        "pending_update_count": 0,
        "ip_address": "203.0.113.1",
        "last_error_date": None,
        "last_error_message": None,
        "last_synchronization_error_date": None,
        "max_connections": 40,
        "allowed_updates": None,
    }
    defaults.update(overrides)
    return WebhookInfo(**defaults)  # type: ignore[arg-type]


async def test_healthy_webhook_reports_status_healthy_over_http(admin_server, pool, monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", _WELL_FORMED_FAKE_TOKEN)

    async def fake_get_webhook_info(self: Bot) -> WebhookInfo:
        return _fake_webhook_info(pending_update_count=3)

    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)

    headers = await _auth_headers(admin_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/telegram/webhook-health", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["pending_update_count"] == 3
    assert body["status"] == "healthy"
    assert body["last_error_message"] is None


async def test_large_backlog_reports_status_critical_over_http(admin_server, pool, monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", _WELL_FORMED_FAKE_TOKEN)

    async def fake_get_webhook_info(self: Bot) -> WebhookInfo:
        return _fake_webhook_info(
            pending_update_count=500,
            last_error_message="Wrong response from the webhook: 502 Bad Gateway",
            last_error_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)

    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/telegram/webhook-health", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "critical"
    assert "502 Bad Gateway" in body["last_error_message"]
    assert body["last_error_date"] is not None


async def test_every_admin_role_can_view_telegram_health_over_http(admin_server, pool, monkeypatch):
    # telegram:view_health is deliberately as broad as dashboard:view --
    # support triaging a "the bot isn't replying" ticket needs this just
    # as much as ops/finance/superadmin do.
    monkeypatch.setattr(get_settings(), "telegram_bot_token", _WELL_FORMED_FAKE_TOKEN)

    async def fake_get_webhook_info(self: Bot) -> WebhookInfo:
        return _fake_webhook_info()

    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)

    for role in ("support", "finance", "ops", "superadmin"):
        headers = await _auth_headers(admin_server, pool, role=role)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/telegram/webhook-health", headers=headers)
        assert response.status_code == 200, f"role {role} was unexpectedly denied"


async def test_unauthenticated_request_is_rejected_over_http(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/telegram/webhook-health")
    assert response.status_code in (401, 403)


async def test_missing_bot_token_returns_503_not_a_raw_exception(admin_server, pool, monkeypatch):
    # get_settings() is lru_cache'd (packages/core/config.py) -- app.py's
    # own handler calls get_settings() itself, which returns this same
    # cached instance, so monkeypatching the instance's attribute directly
    # (rather than trying to bust the cache) is what actually takes effect.
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "")

    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/telegram/webhook-health", headers=headers)

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]
