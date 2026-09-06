"""Real-browser verification of the "Stop room" admin UI
(web/admin/js/screens/rooms.js) -- the backend logic is already
thoroughly covered by test_emergency_room_stop.py; this proves the
frontend confirmation flow (reason required, literal "STOP" confirmation
required, real room/round/player/staked-amount preview shown before
committing) actually works end to end in a real Chromium tab, matching
this codebase's own established discipline for every other admin screen.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from packages.core import ledger
from tests.integration.conftest import create_funded_user, create_room
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_console_e2e import _login
from tests.integration.test_round_engine import wait_until

pytestmark = pytest.mark.e2e


async def test_superadmin_stops_a_running_room_over_a_real_browser(
    admin_server, pool, redis, card_pool, conn, browser
):
    import asyncio

    from services.engine.round_engine import RoundEngine, load_room_config

    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    # A generous call_interval_ms, deliberately -- a real browser flow
    # (login, navigate, click, wait for the panel) takes real wall-clock
    # seconds, and this test never has either player win, so a short
    # interval risks the round exhausting all 75 calls and a *second*
    # round already starting mid-test, the same class of timing flake
    # already root-caused elsewhere in this suite (see DECISIONS.md).
    room_id = await create_room(
        conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=5000, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        p2 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        await _login(page, admin_server, username, password, totp_secret)
        await page.wait_for_selector(".stat-grid", timeout=10000)

        await page.click('.nav-btn[data-screen="rooms"]')
        await page.wait_for_selector(f'tr[data-room-id="{room_id}"]', timeout=10000)

        row = f'tr[data-room-id="{room_id}"]'
        await page.click(f"{row} .stop-room-btn")
        await page.wait_for_selector("#stop-room-form", timeout=10000)

        # The real preview data must actually be on the page -- not just
        # that the form exists.
        panel_text = await page.inner_text("#room-edit-panel")
        assert "running" in panel_text
        assert "2" in panel_text  # 2 players
        assert "40.00" in panel_text  # 2 x 20.00 staked

        # Submitting with no confirmation typed must not proceed --
        # required attributes on both inputs enforce this at the DOM
        # level; confirm the form is genuinely still open afterward.
        await page.click('#stop-room-form button[type="submit"]')
        assert await page.is_visible("#stop-room-form")

        await page.fill('#stop-room-form input[name="reason"]', "e2e test: real incident drill")
        await page.fill('#stop-room-form input[name="confirmation"]', "STOP")
        await page.click('#stop-room-form button[type="submit"]')

        await page.wait_for_selector("#stop-room-form", state="detached", timeout=10000)

        assert page_errors == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)

    cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
    cash2 = await ledger.get_or_create_account(conn, p2, "user_cash")
    assert await ledger.balance(conn, cash1.id) == Decimal("50.00")
    assert await ledger.balance(conn, cash2.id) == Decimal("50.00")
    room_row = await conn.fetchrow("SELECT is_active FROM rooms WHERE id = $1", room_id)
    assert room_row["is_active"] is False
