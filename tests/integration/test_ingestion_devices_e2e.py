"""Real-browser verification of the Ingestion Devices admin screen (`web/
admin/js/screens/ingestion_devices.js`) -- the backend logic is already
thoroughly covered by tests/integration/test_ingestion_devices.py; this
file's job is proving the frontend wiring itself works end to end in a
real Chromium tab, matching test_admin_manual_payments_e2e.py's own
established discipline for every other admin screen: register -> see the
one-time token -> revoke -> reactivate -> rotate -> confirm the console
never displays a device's token outside the create/rotate response panel.
"""

from __future__ import annotations

import itertools
import random

import pytest

from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_console_e2e import _login

pytestmark = pytest.mark.e2e

_device_counter = itertools.count(random.randint(1, 10**6))


def _next_device_id() -> str:
    return f"e2e-device-{next(_device_counter)}"


async def test_superadmin_registers_revokes_reactivates_and_rotates_a_device_over_a_real_browser(
    admin_server, pool, conn, browser
):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    device_id = _next_device_id()

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("dialog", lambda dialog: dialog.accept())

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="ingestion_devices"]')
    await page.wait_for_selector("#create-device-form", timeout=10000)

    # --- register ------------------------------------------------------
    await page.fill('input[name="device_id"]', device_id)
    await page.fill('input[name="device_name"]', "E2E test phone")
    await page.click('#create-device-form button[type="submit"]')

    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_selector("#new-token-panel pre.code-block", timeout=5000)
    shown_token = await page.inner_text("#new-token-panel pre.code-block")
    assert len(shown_token.strip()) > 20

    row = await conn.fetchrow(
        "SELECT id, token_hash FROM ingestion_devices WHERE device_id = $1", device_id
    )
    assert row is not None
    device_pk = row["id"]
    from packages.core.device_auth import hash_device_token

    assert hash_device_token(shown_token.strip()) == row["token_hash"]

    # The row itself never appears with any token/hash column visible in
    # the DOM outside that one-time panel -- confirm the table row for
    # this device carries no trace of the secret.
    await page.wait_for_selector(f'tr[data-device-pk="{device_pk}"]', timeout=5000)
    row_text = await page.inner_text(f'tr[data-device-pk="{device_pk}"]')
    assert shown_token.strip() not in row_text
    assert row["token_hash"] not in row_text

    # --- revoke, verify the row reflects it -----------------------------
    await page.click(f'tr[data-device-pk="{device_pk}"] .toggle-status-btn')
    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_selector(f'tr[data-device-pk="{device_pk}"] .badge-revoked', timeout=5000)

    status = await conn.fetchval("SELECT status FROM ingestion_devices WHERE id = $1", device_pk)
    assert status == "revoked"

    # --- reactivate ------------------------------------------------------
    await page.click(f'tr[data-device-pk="{device_pk}"] .toggle-status-btn')
    await page.wait_for_selector("#toast.visible", timeout=5000)

    status = await conn.fetchval("SELECT status FROM ingestion_devices WHERE id = $1", device_pk)
    assert status == "active"

    # --- rotate token: old hash gone, new one shown and persisted -------
    old_hash = row["token_hash"]
    await page.click(f'tr[data-device-pk="{device_pk}"] .rotate-token-btn')
    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_selector("#new-token-panel pre.code-block", timeout=5000)
    rotated_token = await page.inner_text("#new-token-panel pre.code-block")
    assert rotated_token.strip() != shown_token.strip()

    new_hash = await conn.fetchval("SELECT token_hash FROM ingestion_devices WHERE id = $1", device_pk)
    assert new_hash != old_hash
    assert hash_device_token(rotated_token.strip()) == new_hash

    assert page_errors == [], f"JS errors during device lifecycle flow: {page_errors}"
    await page.close()


async def test_support_role_sees_the_screen_but_cannot_register_a_device_over_a_real_browser(
    admin_server, pool, browser
):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="support")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="ingestion_devices"]')
    await page.wait_for_selector("#create-device-form", timeout=10000)

    await page.fill('input[name="device_id"]', _next_device_id())
    await page.fill('input[name="device_name"]', "Should be forbidden")
    await page.click('#create-device-form button[type="submit"]')

    # A real 403 from the backend RBAC check, surfaced as a toast -- the
    # console never even shows the button as disabled client-side, since
    # the real enforcement (and the real test) is server-side.
    await page.wait_for_selector("#toast.visible.toast-error", timeout=5000)
    await page.close()
