"""Real-browser verification of the Mini App's wallet screen additions:
the deposit and withdraw tabs (previously placeholder "launching soon"
panes), the transaction history tab, and the reality-check net-position
line on the results screen (spec section 12). Same discipline as
test_miniapp_e2e.py -- a real Chromium tab against the real gateway, real
Postgres, real Redis, real RoundEngine.

Deposit/withdraw creation goes through services.gateway.app's real
/api/deposit and /api/withdraw routes, which read their provider from
app.state.chapa -- swapped for a fake here the same way
test_gateway_rest.py does at the HTTP layer, so this suite drives the real
UI and the real backend logic without ever making a live Chapa network
call.
"""

import asyncio
import json
from decimal import Decimal

import pytest

from services.engine.round_engine import RoundEngine, load_room_config
from services.gateway.app import app as gateway_app
from tests.integration.conftest import build_init_data, create_funded_user, create_room, fund_user, next_telegram_id
from tests.integration.test_miniapp_e2e import TELEGRAM_SDK_URL
from tests.integration.test_payments_deposits import FakePaymentProvider
from tests.integration.test_payout_worker import FakePayoutProvider

pytestmark = pytest.mark.e2e


def telegram_stub_script(init_data: str, telegram_id: int, first_name: str) -> str:
    payload = {
        "initData": init_data,
        "initDataUnsafe": {"user": {"id": telegram_id, "first_name": first_name, "language_code": "am"}},
        "themeParams": {},
    }
    return f"""
    window.__openedLinks = [];
    window.Telegram = {{
      WebApp: {{
        initData: {json.dumps(payload["initData"])},
        initDataUnsafe: {json.dumps(payload["initDataUnsafe"])},
        themeParams: {json.dumps(payload["themeParams"])},
        ready: function() {{}},
        expand: function() {{}},
        openLink: function(url) {{ window.__openedLinks.push(url); }},
        HapticFeedback: {{
          impactOccurred: function() {{}},
          notificationOccurred: function() {{}}
        }},
        BackButton: {{
          show: function() {{}},
          hide: function() {{}},
          onClick: function() {{}}
        }}
      }}
    }};
    """


async def prepare_page(browser, telegram_id: int, first_name: str = "Nebyu"):
    init_data = build_init_data(telegram_id, first_name=first_name)
    page = await browser.new_page(viewport={"width": 390, "height": 780})
    console_errors: list[str] = []
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))

    async def block_real_sdk(route):
        await route.abort()

    await page.route(TELEGRAM_SDK_URL, block_real_sdk)
    await page.add_init_script(telegram_stub_script(init_data, telegram_id, first_name))
    return page, console_errors


async def _open_wallet_tab(page, tab: str) -> None:
    await page.click("#open-wallet-btn")
    await page.wait_for_selector("#screen-wallet.active", timeout=10000)
    await page.click(f'.wallet-tab[data-tab="{tab}"]')
    await page.wait_for_selector(f"#wallet-pane-{tab}:not(.hidden)", timeout=5000)


async def test_deposit_flow_opens_a_checkout_link(gateway_server, browser, pool, conn):
    original_provider = gateway_app.state.chapa
    gateway_app.state.chapa = FakePaymentProvider()
    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await conn.execute(
            "UPDATE users SET phone_e164 = $2 WHERE id = $1", user_row["id"], f"+2519{telegram_id % 100_000_000:08d}"
        )

        await _open_wallet_tab(page, "deposit")
        await page.click('.amount-chip[data-amount="100"]')
        await page.click("#deposit-submit-btn")

        await page.wait_for_function("window.__openedLinks.length > 0", timeout=10000)
        opened = await page.evaluate("window.__openedLinks[0]")
        assert opened.startswith("https://pay.test/DEP-")

        row = await pool.fetchrow(
            "SELECT status, amount FROM payments WHERE user_id = $1 ORDER BY id DESC LIMIT 1", user_row["id"]
        )
        assert row["status"] == "processing"
        assert row["amount"] == Decimal("100.00")

        assert console_errors == [], f"JS errors during deposit flow: {console_errors}"
        await page.screenshot(path="/tmp/miniapp-deposit.png")
        await page.close()
    finally:
        gateway_app.state.chapa = original_provider


async def test_deposit_flow_shows_a_translated_error(gateway_server, browser, pool, conn):
    original_provider = gateway_app.state.chapa
    gateway_app.state.chapa = FakePaymentProvider()
    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )
        # Deliberately never set a phone -- the real "phone_required" gate.

        await _open_wallet_tab(page, "deposit")
        await page.fill("#deposit-amount-input", "100")
        await page.click("#deposit-submit-btn")

        # "error"/"success" is only ever added once setWalletStatus() lands
        # the *final* outcome -- the "opening..." intermediate message also
        # has non-empty text, so waiting on text length alone would race.
        await page.wait_for_selector("#deposit-status.error, #deposit-status.success", timeout=10000)
        status_text = await page.text_content("#deposit-status")
        assert status_text and status_text.strip() != ""
        assert "launch" not in status_text.lower()  # not the generic "not available" copy

        assert console_errors == [], f"JS errors during deposit error flow: {console_errors}"
        await page.close()
    finally:
        gateway_app.state.chapa = original_provider


async def test_withdraw_flow_shows_a_real_outcome(gateway_server, browser, pool, conn):
    original_provider = gateway_app.state.chapa
    gateway_app.state.chapa = FakePayoutProvider()
    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("500.00"))

        await _open_wallet_tab(page, "withdraw")
        await page.fill("#withdraw-amount-input", "200")
        await page.fill("#withdraw-account-input", "0911223344")
        await page.fill("#withdraw-name-input", "Test Holder")
        await page.click("#withdraw-submit-btn")

        # The wait itself is the real assertion: reaching the "success"
        # class (rather than "error") proves the real request_withdrawal()
        # call actually succeeded, auto-approved or landed in review either
        # way -- both are success outcomes from the player's perspective.
        await page.wait_for_selector("#withdraw-status.success", timeout=10000)
        status_text = await page.text_content("#withdraw-status")
        assert status_text and status_text.strip() != ""

        row = await pool.fetchrow(
            "SELECT direction, our_ref FROM payments WHERE user_id = $1 ORDER BY id DESC LIMIT 1", user_row["id"]
        )
        assert row["direction"] == "out"
        assert row["our_ref"].startswith("WD-")

        assert console_errors == [], f"JS errors during withdraw flow: {console_errors}"
        await page.screenshot(path="/tmp/miniapp-withdraw.png")
        await page.close()
    finally:
        gateway_app.state.chapa = original_provider


async def test_history_tab_shows_a_completed_round(gateway_server, browser, pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        await cells[9].click()  # card #10
        await page.click("#lobby-cta")
        await page.wait_for_function(
            "document.getElementById('lobby-cta').textContent.includes('10')", timeout=5000
        )

        await page.wait_for_selector("#screen-game.active", timeout=25000)

        # No winner is guaranteed (auto-mark, both real players, exhausted
        # calls) -- either way the round reaches a terminal state and
        # /api/history should list it. Wait for the result screen, whatever
        # the outcome, then go check history.
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        # Reality check: the net-position line must actually be populated.
        session_text = await page.text_content("#result-session")
        assert session_text and session_text.strip() != ""
        await page.screenshot(path="/tmp/miniapp-reality-check.png")

        # #open-wallet-btn only lives in the room-list header -- the result
        # screen has no path back to it in this stub (the Telegram
        # BackButton stub is a no-op, same as prepare_page()'s other
        # stubs), so reload the same way test_miniapp_e2e.py's own flow
        # does to pick a fresh state_sync back up from "rooms".
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        await _open_wallet_tab(page, "history")
        await page.wait_for_function(
            "document.getElementById('history-list').children.length > 0", timeout=10000
        )

        assert console_errors == [], f"JS errors during history flow: {console_errors}"
        await page.screenshot(path="/tmp/miniapp-history.png")
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)
