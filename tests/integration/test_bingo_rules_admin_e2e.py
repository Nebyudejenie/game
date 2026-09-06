"""Real-browser verification of the new "winning condition" configuration
UI in web/admin/js/screens/rooms.js -- the backend logic is already
thoroughly covered by test_admin_queries.py and test_round_engine.py;
this proves the create/edit forms and their live preview actually work
end to end in a real Chromium tab, matching this codebase's own
established discipline for every other admin screen.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import create_room
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_console_e2e import _login

pytestmark = pytest.mark.e2e


async def test_create_room_shows_and_submits_the_winning_condition(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="rooms"]')
    await page.wait_for_selector("#create-room-form", timeout=10000)

    # Real default preview, no interaction yet.
    preview = await page.inner_text("#create-room-form .winning-condition-text")
    assert "2 completed lines" in preview
    assert "row" in preview and "column" in preview and "diagonal" in preview

    # Change to 1 required line, uncheck "diagonal" -- the preview must
    # update live, not just on submit.
    await page.fill('#create-room-form [name="min_winning_lines"]', "1")
    await page.uncheck('#create-room-form [name="win_patterns"][value="diag"]')
    preview_after = await page.inner_text("#create-room-form .winning-condition-text")
    assert "1 completed line" in preview_after  # singular, not "1 completed lines"
    assert "diagonal" not in preview_after

    code = f"e2e-rules-{admin_id}"
    await page.fill('#create-room-form [name="code"]', code)
    await page.fill('#create-room-form [name="stake"]', "15.00")
    await page.click('#create-room-form button[type="submit"]')

    await page.wait_for_selector(f'tr[data-room-id]:has-text("{code}")', timeout=10000)
    row_text = await page.inner_text(f'table.data-table tbody tr:has-text("{code}")')
    assert "1 completed line" in row_text

    assert page_errors == []


async def test_edit_room_preserves_and_updates_the_winning_condition(
    admin_server, pool, conn, browser
):
    from decimal import Decimal

    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, min_winning_lines=2, is_active=True,
    )

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("dialog", lambda dialog: dialog.accept("e2e test: switching this room's rule"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="rooms"]')
    await page.wait_for_selector(f'tr[data-room-id="{room_id}"]', timeout=10000)

    await page.click(f'tr[data-room-id="{room_id}"] .edit-room-btn')
    await page.wait_for_selector("#edit-room-form", timeout=10000)

    # The edit form must open pre-filled with this room's *real* current
    # value, not a fresh default.
    existing_value = await page.input_value('#edit-room-form [name="min_winning_lines"]')
    assert existing_value == "2"

    await page.fill('#edit-room-form [name="min_winning_lines"]', "3")
    await page.click('#edit-room-form button[type="submit"]')

    await page.wait_for_selector("#edit-room-form", state="detached", timeout=10000)

    row_text = await page.inner_text(f'tr[data-room-id="{room_id}"]')
    assert "3 completed lines" in row_text

    assert page_errors == []

    row = await conn.fetchrow("SELECT min_winning_lines FROM rooms WHERE id = $1", room_id)
    assert row["min_winning_lines"] == 3
