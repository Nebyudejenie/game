"""Real-browser verification of the Mini App (spec's own UI document) --
not a mock DOM, an actual Chromium tab loading the actual gateway-served
static files, talking to a real WebSocket, real Postgres, real Redis, and
a real RoundEngine. This is what "start the dev server and use the feature
in a browser before reporting complete" means for a frontend change.

Telegram's `window.Telegram.WebApp` object is stubbed via an init script
(no real Telegram client exists in a test) -- everything downstream of
that (the WebSocket handshake, state_sync, gameplay) is the genuine app
talking to the genuine backend.

Two traps this file's first draft actually fell into, worth naming so they
don't come back:

1. index.html's real `<script src="https://telegram.org/js/telegram-web-app.js">`
   loads after page.add_init_script()'s stub runs, and overwrites
   `window.Telegram.WebApp.initData` back to "" (there's no real Telegram
   environment) -- silently breaking auth. Fixed by blocking that request
   entirely via page.route() so only our stub exists.
2. `#screen-rooms` used to default to `class="active"` in the raw HTML (so
   *something* was visible before JS ran), which made
   `wait_for_selector("#screen-rooms.active")` pass regardless of whether
   auth had actually succeeded -- and the balance placeholder used to be
   "0.00 ETB", which is indistinguishable from a real zero balance. Fixed
   by not defaulting any screen to active, and using "-- ETB" as the
   loading placeholder so a real "0.00 ETB" is unambiguous proof `authed`
   actually arrived.
"""

import asyncio
import json
from decimal import Decimal

import pytest

from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import (
    build_init_data,
    create_funded_user,
    create_room,
    fund_user,
    next_telegram_id,
)

pytestmark = pytest.mark.e2e

TELEGRAM_SDK_URL = "https://telegram.org/js/telegram-web-app.js"


def telegram_stub_script(init_data: str, telegram_id: int, first_name: str) -> str:
    payload = {
        "initData": init_data,
        "initDataUnsafe": {"user": {"id": telegram_id, "first_name": first_name, "language_code": "am"}},
        "themeParams": {},
    }
    return f"""
    window.Telegram = {{
      WebApp: {{
        initData: {json.dumps(payload["initData"])},
        initDataUnsafe: {json.dumps(payload["initDataUnsafe"])},
        themeParams: {json.dumps(payload["themeParams"])},
        ready: function() {{}},
        expand: function() {{}},
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

    # Must be the real Telegram SDK never running at all, not just our
    # stub running "first" -- add_init_script() runs before every page
    # script, but the SDK's own <script> tag would still execute afterward
    # and overwrite window.Telegram.WebApp.initData back to "". A plain
    # `lambda route: route.abort()` creates the coroutine and never awaits
    # or schedules it -- silently a no-op -- hence a real async def here.
    async def block_real_sdk(route):
        await route.abort()

    await page.route(TELEGRAM_SDK_URL, block_real_sdk)
    await page.add_init_script(telegram_stub_script(init_data, telegram_id, first_name))
    return page, console_errors


async def test_miniapp_loads_authenticates_and_shows_balance(gateway_server, browser, conn):
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")

    await page.wait_for_selector("#screen-rooms.active", timeout=10000)
    # "-- ETB" is the loading placeholder; a real "0.00 ETB" only appears
    # once the authed message actually arrives and app.js writes it in.
    await page.wait_for_function(
        "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
    )

    assert console_errors == [], f"JS errors on load: {console_errors}"

    await page.screenshot(path="/tmp/miniapp-rooms.png")
    await page.close()


async def test_room_card_is_reachable_and_activatable_by_keyboard_alone(
    gateway_server, browser, pool, redis, card_pool, conn
):
    # An architecture audit found room cards, the card-selection grid, and
    # the AUTO toggle were plain <div>s with only mouse/touch click
    # listeners -- nothing in the primary "join a game" flow reachable
    # without a pointer. This is the first control in that flow: a real
    # keyboard-only focus + Enter (no page.click() anywhere in this test)
    # must reach the lobby screen exactly the way a tap already does.
    #
    # A live RoundEngine is required here (matching
    # test_miniapp_full_gameplay_flow's own setup) -- state_sync reads
    # whatever round row currently exists for the room, and run_forever()
    # alone doesn't create one: RoundEngine.join() only starts a new round
    # (idle -> lobby) lazily, on the first actual join, so a room with an
    # engine attached but nobody joined yet still has no round at all. A
    # room with no engine attached has no round ever, so no click -- mouse
    # or keyboard -- would reach the lobby screen either way; the first
    # draft of this test learned that the hard way by confirming a plain
    # page.click() control case failed identically. Joining a second,
    # separate player directly through the engine (like the full-gameplay
    # test does) is what actually forces the round into "lobby" before the
    # browser ever loads the page.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, is_active=True,
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

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)

        room_card = page.locator(room_selector)
        assert await room_card.get_attribute("tabindex") == "0"
        assert await room_card.get_attribute("role") == "button"

        await room_card.focus()
        assert await page.evaluate(
            f"document.activeElement === document.querySelector('{room_selector}')"
        )
        await page.keyboard.press("Enter")

        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        assert console_errors == [], f"JS errors during keyboard-only room entry: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_miniapp_language_uses_the_db_value_over_the_telegram_hint(
    gateway_server, browser, pool, conn
):
    # Spec 7.5: "The Mini App reads language_code as a hint but the DB
    # value wins." The stub always reports language_code "am"; after the
    # user row exists we set users.language to "en" directly (the same
    # thing the bot's own /language command would do) and reload -- a real
    # UI string must come back in English, proving the server's value,
    # not the client hint, decided what rendered.
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert user_row is not None, "authed handshake did not create the user row"
    await conn.execute("UPDATE users SET language = 'en' WHERE id = $1", user_row["id"])

    await page.reload()
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    await page.click("#open-wallet-btn")
    await page.wait_for_selector("#screen-wallet.active", timeout=5000)
    wallet_title = await page.text_content('[data-i18n="wallet.title"]')
    assert wallet_title == "Wallet", f"expected the DB's 'en' to win, got {wallet_title!r}"

    assert console_errors == [], f"JS errors on load: {console_errors}"
    await page.close()


async def test_miniapp_language_om_is_accepted_and_falls_back_to_english(
    gateway_server, browser, pool, conn
):
    # web/miniapp/locales/om.json is a deliberate empty stub (mirrors the
    # bot's own om.json/ti.json pattern -- spec 7.5 lists all four
    # languages as supported, but only am/en have real translations so
    # far). Before this, "om" wasn't in i18n.js's SUPPORTED list at all,
    # so a DB value of "om" would have been silently ignored rather than
    # falling through to English -- this proves both that "om" is now
    # accepted and that the fallback chain actually serves English text
    # for every key the stub doesn't define.
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert user_row is not None, "authed handshake did not create the user row"
    await conn.execute("UPDATE users SET language = 'om' WHERE id = $1", user_row["id"])

    await page.reload()
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    await page.click("#open-wallet-btn")
    await page.wait_for_selector("#screen-wallet.active", timeout=5000)
    wallet_title = await page.text_content('[data-i18n="wallet.title"]')
    assert wallet_title == "Wallet", f"expected om's empty stub to fall back to English, got {wallet_title!r}"

    assert console_errors == [], f"JS errors on load: {console_errors}"
    await page.close()


async def test_miniapp_full_gameplay_flow(gateway_server, browser, pool, redis, card_pool, conn):
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

        # Fund the player directly through the ledger (deposits are Phase
        # 5-6, not built) -- then reload so the balance shown reflects it.
        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None, "authed handshake did not create the user row"
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )
        # Target this test's own room specifically -- the dev database
        # accumulates rooms across every previous test run in the session,
        # all at the same 10.00 ETB stake, so `.room-card` alone (the
        # *first* match) is non-deterministic about which room it hits.
        # That was a real, intermittent test bug, not app flakiness: the
        # click could land on some other, already-finished leftover room,
        # whose state_sync never reports status "lobby".
        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)

        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        assert len(cells) == 100

        await cells[9].click()  # card #10
        await page.click("#lobby-cta")
        # Proof the take_card ack actually landed, not just that the click
        # happened: the CTA switches to the "already taken, tap to change"
        # wording once the server confirms.
        await page.wait_for_function(
            "document.getElementById('lobby-cta').textContent.includes('10')", timeout=5000
        )

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        board_cells = await page.query_selector_all(".board-cell")
        assert len(board_cells) == 75
        # Proof this player actually holds a card (not spectating): the
        # your-card section is the one thing enterSpectate() hides.
        assert not await page.is_hidden("#your-card-section")

        # Confirm the actual stake happened -- not just that the UI moved
        # on to the next screen.
        balance_after_stake = await pool.fetchval(
            """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
            """,
            user_row["id"],
        )
        assert balance_after_stake == Decimal("90.00")

        # Wait for at least one live call to actually render.
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )

        await page.screenshot(path="/tmp/miniapp-game.png")

        assert console_errors == [], f"JS errors during gameplay: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_verify_draw_button_shows_a_verified_seed(gateway_server, browser, pool, redis, card_pool, conn):
    """Spec section 14's definition of done: "a player can independently
    verify any round's draw from the published seed." This is that button,
    clicked for real, against a round that actually ran to completion --
    not just a check that /api/rounds/{id}/fairness returns the right JSON
    (test_gateway_rest.py already proves that), but that the UI a player
    would actually use to see it works.
    """
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
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        await page.click("#verify-draw-btn")
        await page.wait_for_selector("#fairness-panel:not(.hidden)", timeout=5000)
        await page.wait_for_function(
            "document.getElementById('fairness-verified').textContent.length > 0", timeout=10000
        )

        verified_text = await page.text_content("#fairness-verified")
        assert verified_text and "✅" in verified_text, f"draw did not verify: {verified_text!r}"

        seed_hex = await page.text_content("#fairness-seed")
        hash_hex = await page.text_content("#fairness-hash")
        assert seed_hex and len(seed_hex.strip()) == 64  # 32 bytes, hex-encoded
        assert hash_hex and len(hash_hex.strip()) == 64  # sha256, hex-encoded

        await page.screenshot(path="/tmp/miniapp-fairness.png")
        assert console_errors == [], f"JS errors during verify-draw flow: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)
