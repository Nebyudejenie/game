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
from tests.integration.test_admin_withdrawals import _review_withdrawal

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


async def test_admin_console_rooms_edit_changes_a_real_room_over_a_real_browser(
    admin_server, pool, conn, browser
):
    # An architecture audit found the rooms screen had create + activate/
    # deactivate but no edit UI at all, despite update_room_admin()/
    # _UPDATABLE_ROOM_FIELDS fully supporting it (spec 11: "create/edit
    # stakes, cut, timings, patterns") -- this is the first real-browser
    # coverage of the new edit form, including the window.prompt() reason
    # dialog neither this action nor the pre-existing activate/deactivate
    # button had any Playwright coverage for before.
    from tests.integration.conftest import create_room

    admin_id, username, password, totp_secret = await create_test_admin(pool, role="ops")
    room_id = await create_room(conn, stake=Decimal("10.00"), house_cut_bps=2000)

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    # window.prompt() is auto-dismissed (returns null) with no handler --
    # this is what actually lets the edit form's reason prompt go through
    # rather than the save silently no-op'ing on a cancelled dialog.
    page.on("dialog", lambda dialog: dialog.accept("e2e test: correcting the stake"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="rooms"]')
    room_row = f'tr[data-room-id="{room_id}"]'
    await page.wait_for_selector(room_row, timeout=10000)
    await page.click(f'{room_row} .edit-room-btn')

    await page.wait_for_selector("#edit-room-form", timeout=5000)
    stake_input = page.locator('#edit-room-form input[name="stake"]')
    await stake_input.fill("")
    await stake_input.fill("25.00")
    await page.click('#edit-room-form button[type="submit"]')

    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_function(
        f"""() => {{
            const row = document.querySelector('tr[data-room-id="{room_id}"]');
            return row && row.textContent.includes('25.00');
        }}""",
        timeout=5000,
    )

    updated_stake = await conn.fetchval("SELECT stake FROM rooms WHERE id = $1", room_id)
    assert updated_stake == Decimal("25.00")

    audit_row = await conn.fetchrow(
        "SELECT reason FROM admin_audit_log WHERE admin_id = $1 AND action = 'rooms.update' "
        "ORDER BY id DESC LIMIT 1",
        admin_id,
    )
    assert audit_row is not None
    assert "correcting the stake" in audit_row["reason"]

    assert page_errors == [], f"JS errors during rooms-edit flow: {page_errors}"
    await page.close()


async def test_admin_console_rbac_denial_shows_a_real_message_not_a_blank_screen(
    admin_server, pool, redis, conn, browser
):
    # support has payments:view (sees the screen, the list loads) but not
    # payments:approve -- an *action*-level denial, not a view-level one:
    # the nav-hiding fix below (services/admin/rbac.py's own risk:view
    # gap this test used to exercise) now hides that screen from support
    # entirely, so a real, still-reachable-via-the-actual-nav denial is
    # needed to keep proving the same underlying property this test has
    # always been about -- a real backend 403 renders as a real message,
    # not a blank screen or a silently-succeeded action. js/screens/
    # payments.js's decide() catches its own error and toasts the real
    # backend detail directly.
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="support")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept("e2e test: attempting approval"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="payments"]')
    payment_row = f'tr[data-payment-id="{payment_id}"]'
    await page.wait_for_selector(payment_row, timeout=10000)
    await page.click(f"{payment_row} .approve-btn")

    await page.wait_for_selector("#toast.visible", timeout=5000)
    toast_text = await page.text_content("#toast")
    assert toast_text and "payments:approve" in toast_text

    # The denial has to be real, not just a UI-level message with the
    # action secretly going through anyway.
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "review"

    await page.close()


async def test_admin_console_nav_hides_screens_the_current_role_cant_view(admin_server, pool, browser):
    # An architecture audit caught every screen shown to every role
    # regardless -- a support admin saw "Reports"/"Risk"/"Audit" in the
    # nav despite services/admin/rbac.py granting none of those *:view
    # permissions to that role, discovering the denial only after
    # clicking. Confirms both directions: hidden for the role that
    # genuinely lacks them, and still present for superadmin (who has
    # every permission) -- so this is proven to be real filtering, not a
    # nav that's just broken/empty for everyone.
    support_id, support_user, support_pw, support_totp = await create_test_admin(pool, role="support")
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    support_page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(support_page, admin_server, support_user, support_pw, support_totp)
    await support_page.wait_for_selector(".stat-grid", timeout=10000)
    for hidden_screen in ("reports", "risk", "audit"):
        assert await support_page.query_selector(f'.nav-btn[data-screen="{hidden_screen}"]') is None
    for visible_screen in ("dashboard", "users", "payments", "rounds", "rooms"):
        assert await support_page.query_selector(f'.nav-btn[data-screen="{visible_screen}"]') is not None
    await support_page.close()

    superadmin_page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(superadmin_page, admin_server, username, password, totp_secret)
    await superadmin_page.wait_for_selector(".stat-grid", timeout=10000)
    for screen in ("dashboard", "users", "payments", "rounds", "rooms", "reports", "risk", "audit"):
        assert await superadmin_page.query_selector(f'.nav-btn[data-screen="{screen}"]') is not None
    await superadmin_page.close()


async def test_admin_console_logout_returns_to_the_login_screen(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click("#logout-btn")
    await page.wait_for_selector("#login-screen form", timeout=10000)
    assert await page.is_hidden("#app-shell")

    await page.close()
