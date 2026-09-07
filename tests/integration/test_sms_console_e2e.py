"""Real-browser verification of the SMS Control Plane frontend
(`web/sms/`, mounted at `/console` by `services/sms/app.py`) -- an actual
Chromium tab loading the actual static files, talking to the real SMS
API, real Postgres, real Redis. Same discipline as
test_admin_console_e2e.py.
"""

from __future__ import annotations

import random

import httpx
import pyotp
import pytest

from tests.integration.test_admin_auth import create_test_admin

pytestmark = pytest.mark.e2e


async def _login(page, sms_server: str, username: str, password: str, totp_secret: str) -> None:
    await page.goto(sms_server + "/console/")
    await page.wait_for_selector("#login-screen form")
    await page.fill('input[name="username"]', username)
    await page.fill('input[name="password"]', password)
    await page.fill('input[name="totp_code"]', pyotp.TOTP(totp_secret).now())
    await page.click('button[type="submit"]')


async def test_sms_console_login_and_overview_loads(sms_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, sms_server, username, password, totp_secret)

    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)
    await page.wait_for_selector(".stat-grid", timeout=10000)
    assert await page.is_hidden("#login-screen")
    assert page_errors == [], f"JS errors on load: {page_errors}"
    await page.close()


async def test_sms_console_full_campaign_flow_through_the_real_ui(sms_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    marker = f"e2e-ui-{random.randint(1, 10**9)}"
    phone = f"+2519{random.randint(10_000_000, 99_999_999)}"

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    await _login(page, sms_server, username, password, totp_secret)
    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)

    # Contacts: add one real contact tagged with a unique segment marker.
    await page.click('button[data-screen="contacts"]')
    await page.wait_for_selector('#create-contact-form')
    await page.fill('input[name="phone_e164"]', phone)
    await page.fill('input[name="display_name"]', "UI Test Contact")
    await page.fill('input[name="attribute_key"]', "segment")
    await page.fill('input[name="attribute_value"]', marker)
    await page.click('#create-contact-form button[type="submit"]')
    await page.wait_for_selector(f'td:has-text("{phone}")', timeout=10000)

    # Nodes: register one, capture its one-time token from the UI panel.
    await page.click('button[data-screen="nodes"]')
    await page.wait_for_selector('#create-node-form')
    node_name = f"ui-node-{random.randint(1, 10**9)}"
    await page.fill('input[name="name"]', node_name)
    await page.click('#create-node-form button[type="submit"]')
    await page.wait_for_selector('#new-token-panel pre.code-block', timeout=10000)
    node_token = (await page.text_content('#new-token-panel pre.code-block')).strip()
    await page.wait_for_selector(f'tr:has-text("{node_name}") button:has-text("Approve")', timeout=10000)
    await page.click(f'tr:has-text("{node_name}") button:has-text("Approve")')
    await page.wait_for_selector(f'tr:has-text("{node_name}") .badge-active', timeout=10000)

    # claim_next_message() correctly claims the oldest queued message for
    # the *whole tenant* (any healthy node may pick up any tenant's oldest
    # job -- real, correct production behavior) -- but this shared,
    # never-truncated 'default' tenant can carry stray queued messages
    # left by other tests. Drain them first (dead-lettering each, a real
    # terminal state, not misrepresenting them as delivered) so the fetch-
    # job call later in this test is guaranteed to return *this* test's
    # own message, not someone else's leftover one -- the same "polluted
    # shared dev database" class of issue this whole project's test suite
    # already contends with elsewhere (see DECISIONS.md).
    node_headers = {"Authorization": f"Bearer {node_token}"}
    async with httpx.AsyncClient() as client:
        for _ in range(50):
            drain = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
            stray_job = drain.json()["job"]
            if stray_job is None:
                break
            await client.post(
                f"{sms_server}/v1/nodes/jobs/{stray_job['message_id']}/result",
                headers=node_headers, json={"outcome": "failed", "error_class": "permanent"},
            )

    # Campaigns: create a draft targeting only the contact just added.
    await page.click('button[data-screen="campaigns"]')
    await page.wait_for_selector('#create-campaign-form')
    campaign_name = f"ui-campaign-{random.randint(1, 10**9)}"
    await page.fill('input[name="name"]', campaign_name)
    await page.fill('input[name="body_override"]', "Hello {{display_name}}, UI test!")
    await page.fill('input[name="attribute_key"]', "segment")
    await page.fill('input[name="attribute_value"]', marker)
    await page.click('#create-campaign-form button[type="submit"]')
    await page.wait_for_selector(f'tr:has-text("{campaign_name}")', timeout=10000)

    await page.click(f'tr:has-text("{campaign_name}")')
    await page.wait_for_selector('button[data-action="validate"]', timeout=10000)
    await page.click('button[data-action="validate"]')
    await page.wait_for_selector('button[data-action="start"]', timeout=10000)
    await page.click('button[data-action="start"]')
    # Scoped to #campaign-detail, not a bare ".badge-running" -- the
    # campaigns list on the same page can already contain an unrelated
    # 'running' campaign left behind by another test sharing this same
    # 'default' tenant (e.g. test_sms_app.py's node-ownership test, which
    # deliberately never resolves its own campaign), which would let a
    # page-wide selector resolve instantly against the wrong row before
    # this campaign's own start request has actually completed.
    await page.wait_for_selector('#campaign-detail .badge-running', timeout=10000)

    assert page_errors == [], f"JS errors during flow: {page_errors}"

    # Drive the rest of the delivery through the real node protocol
    # directly (a physical device, not this admin browser tab, is what
    # would call these in production) and confirm the UI reflects it. The
    # queue was drained above, so this is guaranteed to be *this* test's
    # own message.
    async with httpx.AsyncClient() as client:
        fetch = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
        job = fetch.json()["job"]
        assert job is not None
        assert job["body"] == "Hello UI Test Contact, UI test!"
        message_id = job["message_id"]
        result = await client.post(
            f"{sms_server}/v1/nodes/jobs/{message_id}/result",
            headers=node_headers, json={"outcome": "delivered"},
        )
        assert result.status_code == 200

    await page.click(f'tr:has-text("{campaign_name}")')
    await page.wait_for_selector('#campaign-detail .badge-completed', timeout=10000)
    await page.wait_for_selector('#campaign-detail .badge-delivered', timeout=10000)

    await page.close()
