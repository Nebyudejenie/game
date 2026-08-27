"""Real-browser verification of the admin console frontend (`web/admin/`,
mounted at `/console` by `services/admin/app.py`) -- not a mock DOM, an
actual Chromium tab loading the actual static files, talking to the real
admin API, real Postgres, real Redis. Same discipline
test_miniapp_e2e.py already established for the player-facing frontend:
"start the dev server and use the feature in a browser" means an actual
browser here, not just the API-level tests in test_admin_app.py/test_
admin_queries.py.

This file didn't exist when web/admin/ shipped -- the only verification
at the time was a one-off Playwright script run by hand, never committed,
giving zero regression protection going forward. This is that missing
permanent coverage.
"""

from __future__ import annotations

from decimal import Decimal

import pyotp
import pytest

from packages.core import ledger
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_auth import create_test_admin

pytestmark = pytest.mark.e2e


async def _login(page, admin_server: str, username: str, password: str, totp_secret: str) -> None:
    await page.goto(admin_server + "/console/")
    await page.wait_for_selector("#login-screen form")
    await page.fill('input[name="username"]', username)
    await page.fill('input[name="password"]', password)
    await page.fill('input[name="totp_code"]', pyotp.TOTP(totp_secret).now())
    await page.click('button[type="submit"]')


async def test_admin_console_login_and_dashboard_load(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)

    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)
    await page.wait_for_selector(".stat-grid", timeout=10000)
    # The login card must actually be gone, not just covered -- this is
    # exactly the real bug a first real-browser pass caught before this
    # file existed (a CSS specificity issue left #login-screen visible
    # underneath #app-shell after a successful login; see DECISIONS.md).
    assert await page.is_hidden("#login-screen")

    assert page_errors == [], f"JS errors on load: {page_errors}"
    await page.close()


async def test_admin_console_kyc_action_changes_real_database_state(admin_server, pool, conn, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    row = await conn.fetchrow("SELECT display_name, kyc_level FROM users WHERE id = $1", user_id)
    assert row["kyc_level"] == 0

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="users"]')
    await page.fill("#user-search-input", row["display_name"])
    await page.click('#user-search-form button[type="submit"]')
    await page.click(f'.clickable-row[data-user-id="{user_id}"]')
    await page.wait_for_selector("#kyc-select", timeout=10000)

    await page.select_option("#kyc-select", "2")
    await page.fill("#kyc-reason", "e2e test: ID documents reviewed and verified")
    await page.click("#kyc-submit")

    # The toast is the UI-level confirmation; the database row is the
    # real one -- both must agree, not just the friendlier of the two.
    # users.js's loadDetail() briefly replaces the whole detail panel
    # with a loading placeholder before rebuilding it with fresh values,
    # so this predicate has to tolerate #kyc-select not existing for a
    # moment -- optional chaining, not an assumption it's always there.
    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_function(
        "document.getElementById('kyc-select')?.value === '2'", timeout=5000
    )
    updated = await conn.fetchval("SELECT kyc_level FROM users WHERE id = $1", user_id)
    assert updated == 2

    await page.close()


async def test_admin_console_adjust_balance_credits_a_real_user_over_a_real_browser(
    admin_server, pool, conn, browser
):
    # Real-browser proof for the users.js adjust-balance form specifically
    # (no prior e2e coverage existed for it at all): that crypto.randomUUID()
    # is genuinely available and the request body it builds actually
    # satisfies the backend's now-required request_id field, not just that
    # the idempotency mechanism is correct in isolation (already proven at
    # the API level in test_admin_app.py/test_admin_queries.py -- this test's
    # job is the actual browser wiring, not re-proving that property).
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))
    row = await conn.fetchrow("SELECT display_name FROM users WHERE id = $1", user_id)

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="users"]')
    await page.fill("#user-search-input", row["display_name"])
    await page.click('#user-search-form button[type="submit"]')
    await page.click(f'.clickable-row[data-user-id="{user_id}"]')
    await page.wait_for_selector("#adjust-submit", timeout=10000)

    await page.fill("#adjust-amount", "15.00")
    await page.fill("#adjust-reason", "e2e test: goodwill credit")
    await page.click("#adjust-submit")

    await page.wait_for_selector("#toast.visible", timeout=5000)
    # .field-value is shared by several fields (KYC level, Cash, Bonus,
    # Locked, Net LTV, Last seen) with no distinguishing id/data attribute
    # -- find the one whose sibling .field-label actually reads "Cash"
    # rather than assuming DOM order.
    await page.wait_for_function(
        """() => {
            const label = Array.from(document.querySelectorAll('.field-label'))
                .find(el => el.textContent === 'Cash');
            return label?.nextElementSibling?.textContent.includes('25.00');
        }""",
        timeout=5000,
    )
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("25.00")

    assert page_errors == [], f"JS errors during adjust-balance flow: {page_errors}"
    await page.close()


async def test_admin_console_rbac_denial_shows_a_real_message_not_a_blank_screen(admin_server, pool, browser):
    # support has no risk:view permission (services/admin/rbac.py) --
    # the console must surface that as a real message, the same
    # test_rbac_support_cannot_view_risk_screen_over_http already proves
    # at the API level, but here through the actual screen a support
    # admin would land on. js/screens/risk.js catches its own fetch
    # error and renders the real backend detail directly (js/app.js's
    # own generic "no access" banner is only a fallback for a screen
    # that doesn't handle its own errors) -- the real permission name is
    # what should show up, not a placeholder.
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="support")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="risk"]')
    await page.wait_for_selector(".error-banner", timeout=10000)
    banner_text = await page.text_content(".error-banner")
    assert banner_text and "risk:view" in banner_text

    await page.close()


async def test_admin_console_logout_returns_to_the_login_screen(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click("#logout-btn")
    await page.wait_for_selector("#login-screen form", timeout=10000)
    assert await page.is_hidden("#app-shell")

    await page.close()
