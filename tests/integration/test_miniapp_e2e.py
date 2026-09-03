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

from packages.core.bingo import letter_for
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


async def test_a_genuinely_fresh_room_with_no_round_history_can_still_be_entered(
    gateway_server, browser, conn
):
    """A real production incident: every room in a brand-new deployment
    starts with zero round history -- round_engine.py only creates a
    round row lazily, on the very first take_card. Before that,
    build_state_sync() reports status "idle" (queries.py's own default
    when the round_row lookup finds nothing). The state_sync handler
    used to route "idle" straight back to the room list with no other
    path to the card-selection screen anywhere in the client -- a real
    first player could open a room and then never take a card, ever,
    since nothing else in the app ever requests the lobby UI.

    Every other e2e test in this file pre-seeds the room via a direct
    engine.join() before the browser ever connects, which always leaves
    the room already in "lobby" status by the time a real click lands --
    that's exactly why the whole suite never caught this. This test is
    deliberately the one exception: the browser is the *first and only*
    participant to ever touch this room, reproducing the real incident
    precisely (found live, diagnosed with a real Playwright session
    against the actual production domain, capturing the real WebSocket
    frames -- join sent, state_sync came back status: "idle", and the
    screen simply never left #screen-rooms).
    """
    # Deliberately no RoundEngine running for this room at all -- the WS
    # "join" handler serves state_sync straight from Postgres
    # (services/gateway/queries.py::build_state_sync(), by design: "works
    # correctly even if the room's engine just crashed and hasn't been
    # replaced yet"), so reaching the lobby screen genuinely doesn't
    # depend on a live engine. This also keeps the test honest: no engine
    # means no join() call could have silently pre-seeded a round either.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, is_active=True, max_cards_per_player=1,
    )
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)
    await page.wait_for_function(
        "document.getElementById('balance-amount').textContent.includes('.')", timeout=10000
    )

    room_selector = f'.room-card[data-room-id="{room_id}"]'
    await page.wait_for_selector(room_selector, timeout=10000)
    await page.click(room_selector)

    # The actual bug: this used to stay on #screen-rooms forever, no
    # matter how long you waited, because nothing ever transitioned it.
    await page.wait_for_selector("#screen-lobby.active", timeout=10000)
    cells = await page.query_selector_all(".card-grid-cell")
    assert len(cells) == 432, "the lobby's own card grid must render even with no round yet"

    assert console_errors == [], f"JS errors entering a genuinely fresh room: {console_errors}"
    await page.close()


async def test_a_room_whose_last_round_ended_can_still_be_reentered(gateway_server, browser, conn):
    """A real, currently-live production incident, worse than the "genuinely
    fresh room" one above: RoundEngine._reset_to_idle() (round_engine.py)
    flips the *in-memory* engine back to "idle" the instant a round voids
    or finishes settling, but build_state_sync() reads Postgres, where that
    same round's row keeps its terminal status ('voided'/'done') forever
    until a brand-new round is inserted. The only thing that ever inserts
    one is RoundEngine.join() (via take_card), which only ever runs once a
    player is actually looking at the lobby/card-grid screen -- and the
    state_sync handler routes "voided"/"done" straight back to the room
    list, never to the lobby. Before the fix, that's a permanent deadlock:
    every room goes unplayable forever the moment its first-ever round
    ends, since nobody can ever reach the lobby again to take a card that
    would start the next one. Not caught earlier because no round had ever
    actually finished end-to-end against a real Telegram client until this
    session's has_main_web_app fix let one complete for the first time --
    the very next real player to tap that same room hit this immediately.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, is_active=True, max_cards_per_player=1,
    )
    await conn.execute(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash, ended_at) "
        "VALUES ($1, 1, 'voided', 10.00, 2000, 'test-hash', now())",
        room_id,
    )

    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)
    await page.wait_for_function(
        "document.getElementById('balance-amount').textContent.includes('.')", timeout=10000
    )

    room_selector = f'.room-card[data-room-id="{room_id}"]'
    await page.wait_for_selector(room_selector, timeout=10000)
    await page.click(room_selector)

    # The actual bug: this used to bounce straight back to #screen-rooms
    # (state_sync reporting status "voided" forever) no matter how long you
    # waited or how many times you tapped the room again.
    await page.wait_for_selector("#screen-lobby.active", timeout=10000)
    cells = await page.query_selector_all(".card-grid-cell")
    assert len(cells) == 432, "the lobby's own card grid must render for the next round"

    assert console_errors == [], f"JS errors reentering a room whose last round ended: {console_errors}"
    await page.close()


async def test_a_player_who_took_a_card_in_an_underfilled_round_is_not_left_frozen_at_zero(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A real production incident, caught on video from a real Android
    device: a player takes a card, the lobby countdown ticks normally,
    reaches 0 -- and then just freezes there forever. The countdown
    reaching 0 is a client-local computation from lobby_deadline_ms;
    nothing was ever pushed to an already-connected client telling it the
    round had actually voided (too few players) and a new one had already
    opened server-side. Every OTHER termination path already broadcasts
    something (round_end for a winner or an exhausted round); the
    underfilled-lobby path was the one silent one. min_players=2 and only
    this one browser player ever joining guarantees the round underfills.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=4,
        no_player_next_round_delay_seconds=1, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await conn.execute("UPDATE users SET language = 'en' WHERE id = $1", user_row["id"])
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
        await cells[74].click()  # card #75, matching the real incident

        # Polled, not a single immediate fetch: a tap fires take_card and
        # moves on with no confirm step to wait on, so the async gateway
        # -> engine round trip may still be in flight the instant this runs.
        balance_query = """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
        """
        for _ in range(100):
            balance_after_stake = await pool.fetchval(balance_query, user_row["id"])
            if balance_after_stake == Decimal("90.00"):
                break
            await asyncio.sleep(0.05)
        assert balance_after_stake == Decimal("90.00"), "the stake must actually be charged first"

        # The bug: before the fix, the screen just sat here at "0" forever,
        # no matter how long this waited -- there was nothing to wake it up.
        await page.wait_for_selector("#screen-rooms.active", timeout=15000)

        toast_text = await page.text_content("#toast")
        assert toast_text and "refunded" in toast_text.lower(), (
            f"expected a refund toast explaining why, got: {toast_text!r}"
        )

        for _ in range(100):
            balance_after_refund = await pool.fetchval(
                balance_query,
                user_row["id"],
            )
            if balance_after_refund == Decimal("100.00"):
                break
            await asyncio.sleep(0.05)
        assert balance_after_refund == Decimal("100.00"), "the stake must be refunded back in full"

        assert console_errors == [], f"JS errors during the underfilled-round bounce-back: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_taking_a_card_shows_the_generated_bingo_card_immediately_in_the_lobby(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A real gap, flagged directly against a user's own screen recording:
    tapping a card number only ever turned that grid cell purple -- the
    actual 5x5 Bingo grid a player is about to play never appeared until
    the round transitioned to #screen-game, minutes later once selection
    closed. A player watching only the number grid change color, with no
    confirmation of what card they actually hold, has no way to tell a
    real assignment from a UI glitch. This proves the generated card
    renders in the lobby itself, immediately after the take_card ack --
    no round start, no screen change, no extra action needed.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=30, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await conn.execute("UPDATE users SET language = 'en' WHERE id = $1", user_row["id"])
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        # Not shown before any card is taken.
        assert await page.is_hidden("#lobby-your-cards-section")

        cells = await page.query_selector_all(".card-grid-cell")
        await cells[74].click()  # card #75, matching the real incident

        # Still on the lobby screen throughout -- this is the whole point.
        await page.wait_for_selector("#lobby-your-cards-section:not(.hidden)", timeout=10000)
        assert await page.get_attribute("#screen-lobby", "class") and "active" in (
            await page.get_attribute("#screen-lobby", "class") or ""
        )

        title_text = await page.text_content("#lobby-your-cards-list .your-card-title")
        assert title_text and "75" in title_text, f"expected the card's own number, got {title_text!r}"

        card_cells = await page.query_selector_all("#lobby-your-cards-list .card-cell")
        assert len(card_cells) == 25, "a real 5x5 grid, not a placeholder"

        assert console_errors == [], f"JS errors showing the lobby card preview: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_miniapp_shows_a_retry_banner_instead_of_a_permanent_blank_screen_when_ws_never_connects(
    gateway_server, browser
):
    """A production incident (the Mini App opening to a permanently blank
    screen -- header visible, nothing else) traced to boot()'s own
    `await ws.waitForAuth()` hanging forever when the WebSocket can never
    be established at all (as opposed to opening and then getting a
    terminal close code, which was already handled). No screen in the
    raw HTML defaults to `active` (see this file's own module docstring),
    so a boot() that never completes leaves the page showing nothing but
    its own dark background forever -- exactly the reported symptom.

    Simulates a genuinely broken WS endpoint via Playwright's own
    route_web_socket() with a handler that never calls
    connect_to_server() -- confirmed empirically to make every real
    connection attempt fail immediately with a real (non-terminal)
    close code, the same shape a Cloudflare Tunnel WebSocket-upgrade
    misconfiguration or a firewall would produce from the client's own
    point of view. Runs against the real, unmodified 20s production
    timeout (INITIAL_AUTH_TIMEOUT_MS in ws.js) -- not shortened -- so
    this proves the actual production behavior, not a sped-up stand-in.
    """
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    async def never_connect(ws_route):
        pass  # deliberately never calls ws_route.connect_to_server()

    await page.route_web_socket("**/ws", never_connect)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")

    # The bug this test exists to catch: no screen ever becomes active,
    # and no banner ever appears -- the page just silently stays exactly
    # as blank as it started. Confirm the *opposite* happens instead.
    await page.wait_for_function(
        "document.getElementById('connection-banner').classList.contains('visible')", timeout=25000
    )
    assert await page.evaluate(
        "document.getElementById('connection-banner').classList.contains('actionable')"
    ), "the give-up banner must be tappable to retry, not just informational"
    banner_text = await page.text_content("#connection-banner")
    assert banner_text and banner_text.strip(), "banner must show real text, not stay empty"

    # Never reached "authenticated" or any active game screen -- the whole
    # point is this state is reached *instead of* a working boot, not
    # alongside it.
    assert not await page.evaluate("document.getElementById('screen-rooms').classList.contains('active')")

    await page.screenshot(path="/tmp/miniapp-connect-failed-banner.png")
    await page.close()
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
        assert len(cells) == 432

        # The lobby's own header/status bar (video reference: REFRESH,
        # BALANCE, CONNECTED, Stake, Win) -- real values from state_sync/
        # lobby_tick, not placeholders.
        await page.wait_for_function(
            "document.getElementById('lobby-balance-amount').textContent.includes('100.00')", timeout=5000
        )
        await page.wait_for_function(
            "document.getElementById('lobby-stake-amount').textContent.includes('10.00')", timeout=5000
        )
        assert "connected" in (
            await page.get_attribute("#lobby-connection-pill", "class") or ""
        )
        await page.screenshot(path="/tmp/miniapp-lobby.png")

        # Tapping a card number takes it immediately -- no separate
        # confirm step. #screen-game.active below, reachable only once
        # state_sync/round_start reports a real held card, is itself the
        # proof the take_card ack actually landed, not just that the tap
        # happened -- the balance assertion right after is the other half.
        await cells[9].click()  # card #10

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
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

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


async def test_result_screen_shows_the_winning_card_preview(gateway_server, browser, pool, redis, card_pool, conn):
    """Video reference `20260902093014.mp4`'s winner modal (~t=69s):
    a full card preview, not just a text summary, with the winning
    pattern's cells visibly highlighted. round_engine.py's round_end
    broadcast now carries each winner's own grid (self._card_pool,
    already in memory for settlement -- see the commit adding it), and
    render/card.js's renderStaticCard() draws it. This proves the whole
    path end-to-end against a round that actually completes for real.
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
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        # Whether this player won or the other did, round_end always
        # carries at least one winner's grid -- the panel must show it
        # either way (app.js's `shown = mine || winners[0]` logic).
        await page.wait_for_selector("#result-card-panel:not(.hidden)", timeout=10000)

        card_cells = await page.query_selector_all("#result-card .card-cell")
        assert len(card_cells) == 25, f"expected a full 5x5 card preview, got {len(card_cells)} cells"

        free_cell = await page.query_selector("#result-card .card-cell.free")
        assert free_cell is not None
        assert (await free_cell.text_content()) == "★"

        # The room's default win_patterns (tests/integration/conftest.py's
        # create_room()) includes "corners" alongside row/col/diag -- a
        # real random round can legitimately win on any of them, and
        # corners is the one pattern with only 4 cells, not 5. Derive the
        # expected count from the pattern the result screen itself
        # reports (result.card_row's raw {pattern} interpolation, e.g.
        # "corners" or "row_2") rather than assuming every win is 5 cells.
        result_meta_text = await page.text_content("#result-meta")
        expected_winning_cells = 4 if "corners" in (result_meta_text or "") else 5
        winning_cells = await page.query_selector_all("#result-card .card-cell.winning")
        assert len(winning_cells) == expected_winning_cells, (
            f"expected {expected_winning_cells} cells highlighted for {result_meta_text!r}, "
            f"got {len(winning_cells)}"
        )
        # Every highlighted cell must also actually be marked-called --
        # a winning cell that were somehow not "marked" would mean the
        # gold ring and the column-color fill visibly disagree.
        for cell in winning_cells:
            classes = await cell.get_attribute("class")
            assert "marked" in (classes or ""), f"winning cell not marked: {classes}"

        await page.screenshot(path="/tmp/miniapp-result-card.png")
        assert console_errors == [], f"JS errors during result-screen card preview: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_voice_announcement_requests_the_correct_audio_file_for_a_call(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """The Amharic voice-caller feature's own explicit closing bar --
    "test all 75 numbers and verify... the correct Bingo letter" -- for
    the one piece only a real browser can prove: that a live call
    actually triggers a request for the right
    /audio/calls/{LETTER}_{NN}.mp3 file. No real MP3s exist yet (see
    web/miniapp/audio/calls/README.md) -- this only needs to observe the
    *request* Playwright's own page.on("request") sees, not decode audio,
    so it proves the JS wiring is correct independent of whether real
    audio assets have been recorded.
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

        audio_requests: list[str] = []
        page.on("request", lambda req: audio_requests.append(req.url) if "/audio/calls/" in req.url else None)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        # Visual check of the settings panel this feature adds, not just
        # the request-interception assertions below.
        await page.click("#voice-settings-btn")
        await page.wait_for_selector("#voice-settings-panel:not(.hidden)", timeout=5000)
        await page.screenshot(path="/tmp/miniapp-voice-settings.png")
        await page.click("#voice-settings-btn")
        await page.wait_for_function(
            "document.getElementById('voice-settings-panel').classList.contains('hidden')", timeout=5000
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None, "authed handshake did not create the user row"
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
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )
        # This test's own fast call_interval_ms=15 (real rooms space calls
        # seconds apart) makes voiceCaller's playback queue fall behind the
        # live call stream, since each missing clip only advances the
        # queue after a real network round-trip (a 404) -- so the *current*
        # call badge races ahead of what's actually been requested by now.
        # That's correct queueing behavior under this test's artificially
        # fast pace, not a bug -- wait for a handful of requests to land
        # instead of chasing the latest badge value.
        for _ in range(50):
            if len(audio_requests) >= 5:
                break
            await page.wait_for_timeout(100)

        assert audio_requests, "no /audio/calls/ requests observed during a live call sequence"
        for url in audio_requests:
            filename = url.rsplit("/", 1)[-1]
            stem = filename.removesuffix(".mp3")
            letter, number_str = stem.split("_")
            number = int(number_str)
            assert letter_for(number) == letter, (
                f"{filename}: requested letter {letter!r} but bingo.letter_for({number}) is {letter_for(number)!r}"
            )
        await page.screenshot(path="/tmp/miniapp-voice-game.png")
        assert console_errors == [], f"JS errors during voice announce flow: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_disabling_voice_makes_zero_audio_requests(gateway_server, browser, pool, redis, card_pool, conn):
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

        audio_requests: list[str] = []
        page.on("request", lambda req: audio_requests.append(req.url) if "/audio/calls/" in req.url else None)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        # Turn voice off via the settings panel before ever joining a room.
        await page.click("#voice-settings-btn")
        await page.wait_for_selector("#voice-settings-panel:not(.hidden)", timeout=5000)
        await page.click("#voice-switch-settings")
        assert not await page.evaluate(
            "document.getElementById('voice-switch-settings').classList.contains('on')"
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None, "authed handshake did not create the user row"
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )
        # The setting's own persistence (localStorage) must have survived
        # the reload above -- otherwise this test would prove nothing
        # about the setting actually being off during gameplay.
        assert await page.evaluate("localStorage.getItem('jobingo_voice_enabled')") == "false"

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        # Same proof-of-liveness the other gameplay tests use -- at least
        # one call actually rendered, so silence below is because voice is
        # off, not because no calls happened yet.
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )
        await page.wait_for_timeout(200)

        assert audio_requests == [], f"voice is off but audio was requested: {audio_requests}"
        assert console_errors == [], f"JS errors with voice disabled: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_a_player_can_hold_and_play_several_cards_at_once(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """Multi-card-per-player's own real-browser proof (graceful-snacking-
    quail.md's verification checklist: "take 2 cards, verify both render
    independently"). Selects two cards in the lobby in one batched take
    and confirms the game screen renders two fully independent cards,
    each with its own distinct grid and its own BINGO button -- not one
    card silently clobbered by the other, which is exactly the failure
    mode render/card.js's singleton-to-factory rewrite exists to prevent
    (the old module-level `cells`/`currentGrid` meant a *second*
    buildCard() call would have silently repointed every later mark/claim
    check at whichever card was built last).
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=300,
        is_active=True, max_cards_per_player=3,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 5)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

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
        # Each tap takes the card immediately -- no separate confirm step.
        # The balance assertion right below is the real proof both stakes
        # actually landed, not just that the taps happened.
        await cells[9].click()  # card #10
        await cells[19].click()  # card #20

        # Two separate real stakes actually happened -- not just that the
        # UI moved on to the next screen. Polled, not a single immediate
        # fetch: each tap fires its take_card and moves on with no
        # confirm step to wait on anymore, so the async gateway ->
        # engine round trip may still be in flight the instant this runs.
        balance_query = """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
        """
        for _ in range(100):
            balance_after_stake = await pool.fetchval(balance_query, user_row["id"])
            if balance_after_stake == Decimal("80.00"):
                break
            await asyncio.sleep(0.1)
        assert balance_after_stake == Decimal("80.00"), "expected two separate 10.00 ETB stakes"

        await page.wait_for_selector("#screen-game.active", timeout=25000)

        card_items = await page.query_selector_all(".your-card-item")
        assert len(card_items) == 2, f"expected 2 independently held cards, got {len(card_items)}"

        # Titles must actually distinguish which card is which (language-
        # agnostic check -- game.your_card_no interpolates the raw card
        # number the same way in every locale).
        titles = [await item.query_selector(".your-card-title") for item in card_items]
        title_texts = [(await t.text_content()) or "" for t in titles if t is not None]
        assert any("10" in txt for txt in title_texts), title_texts
        assert any("20" in txt for txt in title_texts), title_texts

        # Each card must have its own full 5x5 grid and its own BINGO
        # button, and the two grids must actually differ -- the concrete
        # proof the factory conversion isolated per-card state correctly.
        grids = []
        for item in card_items:
            grid_cells = await item.query_selector_all(".card-cell")
            assert len(grid_cells) == 25, f"expected a full 5x5 grid, got {len(grid_cells)}"
            grids.append([await c.text_content() for c in grid_cells])
            assert await item.query_selector(".bingo-btn") is not None

        assert grids[0] != grids[1], "both held cards rendered the same grid -- factory isolation broken"

        await page.screenshot(path="/tmp/miniapp-multi-card-game.png", full_page=True)

        # Let the round actually run to completion -- proves round_end's
        # rewritten multi-win handling (summing every one of this
        # player's own winning entries, not just the first) doesn't crash
        # regardless of which of the two players ends up winning.
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        assert console_errors == [], f"JS errors during multi-card gameplay: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_multi_card_session_loss_reports_the_full_amount_not_one_card(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A code review pass caught that round_end's session reality-check
    loss branch subtracted a flat single-card `stake` even when the
    losing player held more than one card -- the untested mirror image
    of the winning-side multi-card bug this same feature already fixed
    (see DECISIONS.md's Phase 4 entry). A player holding 2 cards who
    loses both must see the full 2-card loss on the results screen's
    real-money disclosure, not half of it.

    AUTO is turned off for the browser player directly in the DB (the
    toggle itself only lives on the game screen, reachable after this
    round already starts) so their own two cards can never auto-claim
    regardless of what the draw does to them -- the filler player's own
    auto-claim is then the only way this round can end in a win, making
    "the browser player loses on both cards" a deterministic outcome to
    test against, not a race against their own cards' luck.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True, max_cards_per_player=2,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 5, auto_mark=True)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await conn.execute(
            "UPDATE users SET auto_mark_preference = false WHERE id = $1", user_row["id"]
        )
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
        # Each tap takes the card immediately -- no separate confirm step.
        await cells[9].click()  # card #10
        await cells[19].click()  # card #20

        # Polled, not a single immediate fetch: with no confirm step to
        # wait on anymore, the async gateway -> engine round trip for
        # either tap may still be in flight the instant this runs.
        balance_query = """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
        """
        for _ in range(100):
            balance_after_stake = await pool.fetchval(balance_query, user_row["id"])
            if balance_after_stake == Decimal("80.00"):
                break
            await asyncio.sleep(0.1)
        assert balance_after_stake == Decimal("80.00"), "expected two separate 10.00 ETB stakes"

        await page.wait_for_selector("#screen-result.active", timeout=90000)

        session_text = await page.text_content("#result-session")
        assert "20.00" in (session_text or ""), (
            f"expected the full 2-card 20.00 ETB loss in the session total, got: {session_text!r}"
        )
        assert console_errors == [], f"JS errors during multi-card loss flow: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)
