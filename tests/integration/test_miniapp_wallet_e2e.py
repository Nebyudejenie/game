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

from packages.core.phone_crypto import encrypt_phone, phone_lookup_hash
from services.admin import queries as admin_queries
from services.engine.round_engine import RoundEngine, load_room_config
from services.gateway.app import app as gateway_app
from services.payments import deposits
from tests.integration.conftest import build_init_data, create_funded_user, create_room, fund_user, next_telegram_id
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_miniapp_e2e import TELEGRAM_SDK_URL
from tests.integration.test_payments_deposits import FakePaymentProvider, _webhook
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
        phone = f"+2519{telegram_id % 100_000_000:08d}"
        await conn.execute(
            "UPDATE users SET phone_e164_encrypted = $2, phone_lookup_hash = $3 WHERE id = $1",
            user_row["id"],
            encrypt_phone(phone),
            phone_lookup_hash(phone),
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


async def test_deposit_flow_shows_confirming_then_confirms_on_real_completion(
    gateway_server, browser, pool, redis, conn
):
    # Spec 2.6: "On return: 'Confirming your deposit…' with live polling,
    # never a premature success." Opening the checkout link is not proof
    # of payment -- the player hasn't paid yet at that point -- so the
    # status right after must be a neutral "confirming" state, not
    # "success" (the bug this fix closes: the old copy was already styled
    # "success" the instant the checkout link opened, before any money
    # had actually moved). Only a real ledger credit -- driven through
    # services.payments.deposits.handle_webhook(), the same real
    # completion path test_payments_deposits.py exercises directly, not a
    # synthetic balance push -- should ever flip it to "confirmed".
    original_provider = gateway_app.state.chapa
    provider = FakePaymentProvider()
    gateway_app.state.chapa = provider
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
        phone = f"+2519{telegram_id % 100_000_000:08d}"
        await conn.execute(
            "UPDATE users SET phone_e164_encrypted = $2, phone_lookup_hash = $3 WHERE id = $1",
            user_row["id"],
            encrypt_phone(phone),
            phone_lookup_hash(phone),
        )

        await _open_wallet_tab(page, "deposit")
        await page.click('.amount-chip[data-amount="100"]')
        await page.click("#deposit-submit-btn")

        await page.wait_for_function("window.__openedLinks.length > 0", timeout=10000)

        # The checkout link is open but nothing has been paid yet -- the
        # status must be showing the neutral "confirming" copy, not
        # "success". Text alone isn't proof (the earlier "opening
        # checkout…" message is also non-empty); the class list is what
        # actually distinguishes a premature claim from an honest one.
        status_classes = await page.get_attribute("#deposit-status", "class")
        assert "success" not in status_classes
        assert "error" not in status_classes
        confirming_text = await page.text_content("#deposit-status")
        assert confirming_text and confirming_text.strip() != ""

        payment_row = await pool.fetchrow(
            "SELECT our_ref FROM payments WHERE user_id = $1 ORDER BY id DESC LIMIT 1", user_row["id"]
        )
        assert payment_row is not None
        headers, body = _webhook(
            event_id=f"evt-{payment_row['our_ref']}",
            our_ref=payment_row["our_ref"],
            status="succeeded",
            amount="100.00",
        )
        outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
        assert outcome == "credited"

        # The real ledger credit above published a genuine balance_update
        # over this user's own already-open WS connection -- proof the
        # confirmation is wired to the actual completion signal, not a
        # timer or a guess.
        await page.wait_for_selector("#deposit-status.success", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        assert console_errors == [], f"JS errors during deposit confirmation flow: {console_errors}"
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
    # is_active=True: the gateway's own room list (what the miniapp UI
    # browses) reads WHERE is_active = true, unlike every other test using
    # create_room() (see its own docstring for why False is the default).
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
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


async def test_history_tab_filters_by_won_and_lost(gateway_server, browser, pool, conn):
    # Spec 2.6: "History: rounds and transactions, filterable, each
    # linking to its detail." Seeding two already-completed rounds
    # directly (one won, one lost) rather than playing a live round to
    # completion -- the outcome test above already covers the live path,
    # and its own auto-mark-vs-auto-mark setup can't guarantee a specific
    # winner, which this test needs to prove the filter actually filters.
    room_id = await create_room(conn)

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
    user_id = user_row["id"]

    won_round = await conn.fetchrow(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash, ended_at) "
        "VALUES ($1, 101, 'done', 20.00, 2000, 'test-hash', now()) RETURNING id",
        room_id,
    )
    await conn.execute(
        "INSERT INTO round_entries (round_id, card_no, user_id) VALUES ($1, 1, $2)",
        won_round["id"],
        user_id,
    )
    await conn.execute(
        "INSERT INTO round_winners (round_id, user_id, card_no, pattern, won_on_call, amount) "
        "VALUES ($1, $2, 1, 'row', 10, 32.00)",
        won_round["id"],
        user_id,
    )

    lost_round = await conn.fetchrow(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash, ended_at) "
        "VALUES ($1, 102, 'done', 20.00, 2000, 'test-hash', now()) RETURNING id",
        room_id,
    )
    await conn.execute(
        "INSERT INTO round_entries (round_id, card_no, user_id) VALUES ($1, 2, $2)",
        lost_round["id"],
        user_id,
    )

    await _open_wallet_tab(page, "history")
    await page.wait_for_function(
        "document.getElementById('history-list').children.length > 0", timeout=10000
    )
    assert await page.locator("#history-list .history-row").count() == 2

    # The stub's language_code is "am" -- assert on the interpolated round
    # number ("#101"), not English wording, so this holds regardless of
    # which locale actually rendered.
    await page.click('#history-filter-chips .amount-chip[data-filter="won"]')
    await page.wait_for_function(
        "document.getElementById('history-list').children.length === 1", timeout=5000
    )
    assert "#101" in (await page.text_content("#history-list"))

    await page.click('#history-filter-chips .amount-chip[data-filter="lost"]')
    await page.wait_for_function(
        "document.getElementById('history-list').children.length === 1", timeout=5000
    )
    assert "#102" in (await page.text_content("#history-list"))

    await page.click('#history-filter-chips .amount-chip[data-filter="all"]')
    await page.wait_for_function(
        "document.getElementById('history-list').children.length === 2", timeout=5000
    )

    assert console_errors == [], f"JS errors during history filtering: {console_errors}"
    await page.close()


async def test_manual_deposit_flow_submits_a_real_review_request(gateway_server, browser, pool, conn):
    # P1: keep taking deposits when the automatic provider is
    # unavailable. Deliberately doesn't touch gateway_app.state.chapa at
    # all -- the manual rail is provider-independent by design (the whole
    # point of this feature), so this proves the manual panel works
    # regardless of Chapa's own state.
    destination_row = await conn.fetchrow(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, instructions) "
        "VALUES ('telebirr', '0911000000', 'Jo Bingo PLC', 'Reference your player id') RETURNING id"
    )

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

    await _open_wallet_tab(page, "deposit")
    await page.click("#deposit-manual-toggle-btn")
    await page.wait_for_selector("#deposit-manual-destination-select", timeout=10000)
    # The dropdown lists every active destination (shared, session-wide
    # data other tests also insert into), ordered by method_kind then id
    # -- not necessarily this test's own newest row -- so select it
    # explicitly rather than assuming it's the default option.
    await page.select_option("#deposit-manual-destination-select", str(destination_row["id"]))

    await page.fill("#deposit-manual-amount-input", "300")
    await page.fill("#deposit-manual-reference-input", "FT-E2E-MINIAPP-1")
    await page.click("#deposit-manual-submit-btn")

    await page.wait_for_selector("#deposit-manual-status.success", timeout=10000)

    row = await pool.fetchrow(
        "SELECT status, provider, amount, provider_ref, manual_destination_id FROM payments "
        "WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
        user_row["id"],
    )
    assert row["status"] == "review"
    assert row["provider"] == "manual"
    assert row["amount"] == Decimal("300.00")
    assert row["provider_ref"] == "FT-E2E-MINIAPP-1"
    assert row["manual_destination_id"] == destination_row["id"]

    # No premature credit -- the whole point of "PENDING REVIEW".
    balance_text = await page.text_content("#balance-amount")
    assert balance_text and "0.00" in balance_text

    assert console_errors == [], f"JS errors during manual deposit flow: {console_errors}"
    await page.screenshot(path="/tmp/miniapp-manual-deposit.png")
    await page.close()


async def test_manual_withdraw_checkbox_forces_a_real_review_status(gateway_server, browser, pool, conn):
    # Proves the checkbox actually changes server-side behavior, not just
    # UI copy: a small amount that would normally auto-approve over the
    # automatic rail must still land in 'review' with provider='manual'
    # once the checkbox is checked -- a human must act on every manual
    # withdrawal regardless of amount (see request_withdrawal's own
    # force_review parameter).
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
    await page.fill("#withdraw-amount-input", "100")
    await page.fill("#withdraw-account-input", "0911223344")
    await page.fill("#withdraw-name-input", "Test Holder")
    await page.check("#withdraw-manual-checkbox")
    await page.click("#withdraw-submit-btn")

    # data.status is always "review" for a force_review request (never
    # "approved"), so wallet.withdraw_review is the only copy that can
    # render here -- checked below via the real DB row instead of the
    # rendered text, since the stub's language isn't pinned in this file
    # the way test_miniapp_e2e.py's own stub pins "am".
    await page.wait_for_selector("#withdraw-status.success", timeout=10000)
    status_text = await page.text_content("#withdraw-status")
    assert status_text and status_text.strip() != ""

    row = await pool.fetchrow(
        "SELECT status, provider FROM payments WHERE user_id = $1 ORDER BY id DESC LIMIT 1", user_row["id"]
    )
    assert row["status"] == "review"
    assert row["provider"] == "manual"

    assert console_errors == [], f"JS errors during manual withdraw flow: {console_errors}"
    await page.close()


async def _toggle_chapa(conn, *, direction: str, enabled: bool) -> None:
    await conn.execute(
        "UPDATE payment_provider_availability SET enabled = $1 WHERE provider = 'chapa' AND direction = $2",
        enabled,
        direction,
    )


async def test_wallet_shows_only_manual_when_chapa_deposit_is_disabled(gateway_server, browser, pool, conn):
    # The backend, not this file, decides -- payment_provider_availability
    # is real, shared state; this proves the Mini App genuinely reads it
    # live rather than hardcoding "Chapa is always an option."
    await _toggle_chapa(conn, direction="in", enabled=False)
    try:
        destination_row = await conn.fetchrow(
            "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name) "
            "VALUES ('telebirr', '0911000000', 'Jo Bingo PLC') RETURNING id"
        )

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        await _open_wallet_tab(page, "deposit")
        # The manual panel shows up on its own -- no toggle click needed,
        # since there's nothing else to toggle from.
        await page.wait_for_selector("#deposit-manual-section:not(.hidden)", timeout=10000)
        assert await page.is_hidden("#deposit-automatic-section")
        assert await page.is_hidden("#deposit-manual-toggle-btn")

        assert console_errors == [], f"JS errors while chapa deposit was disabled: {console_errors}"
        await page.close()
    finally:
        await _toggle_chapa(conn, direction="in", enabled=True)


async def test_wallet_locks_withdraw_to_manual_when_chapa_withdraw_is_disabled(gateway_server, browser, conn):
    await _toggle_chapa(conn, direction="out", enabled=False)
    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        await _open_wallet_tab(page, "withdraw")
        await page.wait_for_function(
            "document.getElementById('withdraw-manual-toggle-row').classList.contains('hidden')",
            timeout=10000,
        )
        assert await page.is_checked("#withdraw-manual-checkbox")
        assert await page.is_disabled("#withdraw-manual-checkbox")

        assert console_errors == [], f"JS errors while chapa withdraw was disabled: {console_errors}"
        await page.close()
    finally:
        await _toggle_chapa(conn, direction="out", enabled=True)


async def test_full_lifecycle_registration_through_withdrawal_using_the_manual_rail(
    gateway_server, browser, pool, redis, card_pool, conn
):
    # The product directive's own final acceptance bar, verbatim:
    # "verify that a Telegram player can complete: Registration ->
    # Deposit -> Wallet credit -> Play -> Win -> Payout -> Withdrawal"
    # using either automatic or manual payment. Every individual link in
    # this chain already has its own dedicated, focused test elsewhere;
    # this is the one test proving they compose correctly as a single
    # continuous player session that survives crossing both payment
    # system boundaries -- deposit review and withdrawal settlement --
    # without ever touching Chapa. The admin-side actions (approve the
    # deposit, approve+settle the withdrawal) are called directly rather
    # than driven through the admin console's own UI, since that UI path
    # is already independently proven in test_admin_manual_payments_e2e
    # .py -- this test's own job is the PLAYER's continuous journey.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        destination_row = await conn.fetchrow(
            "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name) "
            "VALUES ('telebirr', '0911000000', 'Jo Bingo PLC') RETURNING id"
        )
        admin_id, *_ = await create_test_admin(pool)

        # --- Registration ---
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

        # --- Deposit (manual, real browser submission) ---
        await _open_wallet_tab(page, "deposit")
        await page.click("#deposit-manual-toggle-btn")
        await page.wait_for_selector("#deposit-manual-destination-select", timeout=10000)
        await page.select_option("#deposit-manual-destination-select", str(destination_row["id"]))
        await page.fill("#deposit-manual-amount-input", "100")
        await page.fill("#deposit-manual-reference-input", "FT-E2E-LIFECYCLE-1")
        await page.click("#deposit-manual-submit-btn")
        await page.wait_for_selector("#deposit-manual-status.success", timeout=10000)

        deposit_payment_id = await pool.fetchval(
            "SELECT id FROM payments WHERE user_id = $1 AND direction = 'in' ORDER BY id DESC LIMIT 1",
            user_row["id"],
        )

        # --- Wallet credit (admin approves; the credit must reach this
        # already-open tab live, over the same WS connection) ---
        approved = await admin_queries.approve_manual_deposit_admin(
            pool, redis, admin_id=admin_id, payment_id=deposit_payment_id, reason="verified externally",
            ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
        )
        assert approved == "credited"
        await page.wait_for_function(
            "document.getElementById('wallet-cash').textContent.includes('100.00')", timeout=10000
        )
        # request_withdrawal()'s chargeback-window gate (30 real minutes
        # in this environment's settings) correctly treats a just-
        # -succeeded deposit as reversible regardless of rail -- a real,
        # already-existing protection this test ran straight into on its
        # first pass, not something to special-case around in the
        # product code. Backdating here is the test's own concern:
        # simulating that the window has genuinely elapsed, the same
        # "age a row via direct SQL" technique test_admin_withdrawals.py
        # already uses for its own stuck-payout test.
        await conn.execute(
            "UPDATE payments SET created_at = now() - interval '1 hour' WHERE id = $1", deposit_payment_id
        )

        # --- Play ---
        await page.click("#wallet-back-btn")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

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

        # --- Win / Payout (whatever the real, unrigged outcome is --
        # matching this suite's own established precedent of never
        # forcing a specific winner; either outcome reaches a terminal
        # state and this test's own job is what happens to the balance
        # afterward, not which player happened to win) ---
        await page.wait_for_selector("#screen-result.active", timeout=90000)
        cash_after_round = await pool.fetchval(
            """
            SELECT balance FROM account_balances b JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
            """,
            user_row["id"],
        )
        assert cash_after_round is not None and cash_after_round >= Decimal("0.00")

        # --- Withdrawal (manual, real browser submission, of whatever
        # remains after the stake and possible win) ---
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await _open_wallet_tab(page, "withdraw")
        await page.fill("#withdraw-amount-input", str(cash_after_round))
        await page.fill("#withdraw-account-input", "0911223344")
        await page.fill("#withdraw-name-input", "Test Holder")
        await page.check("#withdraw-manual-checkbox")
        await page.click("#withdraw-submit-btn")
        await page.wait_for_selector("#withdraw-status.success", timeout=10000)

        withdrawal = await pool.fetchrow(
            "SELECT id, status, provider, amount FROM payments WHERE user_id = $1 AND direction = 'out' "
            "ORDER BY id DESC LIMIT 1",
            user_row["id"],
        )
        assert withdrawal["status"] == "review"
        assert withdrawal["provider"] == "manual"
        assert withdrawal["amount"] == cash_after_round

        # --- Admin approves and settles the withdrawal ---
        approved_wd = await admin_queries.approve_manual_withdrawal_admin(
            pool, redis, admin_id=admin_id, payment_id=withdrawal["id"], reason="verified identity",
            ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
        )
        assert approved_wd == "approved"
        settled = await admin_queries.settle_manual_withdrawal_admin(
            pool, redis, admin_id=admin_id, payment_id=withdrawal["id"], external_reference="TXN-E2E-LIFECYCLE",
            reason="sent via Telebirr", ip_address="10.0.0.1",
        )
        assert settled is True

        final_status = await conn.fetchrow(
            "SELECT status, provider_ref FROM payments WHERE id = $1", withdrawal["id"]
        )
        assert final_status["status"] == "succeeded"
        assert final_status["provider_ref"] == "TXN-E2E-LIFECYCLE"

        locked_balance = await pool.fetchval(
            """
            SELECT balance FROM account_balances b JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_locked'
            """,
            user_row["id"],
        )
        assert locked_balance == Decimal("0.00")

        assert console_errors == [], f"JS errors during the full lifecycle: {console_errors}"
        await page.screenshot(path="/tmp/miniapp-full-lifecycle.png")
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)
