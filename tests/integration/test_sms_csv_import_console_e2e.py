"""Real-browser verification of the Bulk Import screen (web/sms/js/
screens/import.js): an actual Chromium tab uploading a real file through
a real <input type="file">, reading the real validation summary and error
table back from the DOM, and creating a real campaign from it -- the
exact workflow the production directive's own diagram describes (login ->
dashboard -> upload CSV -> validation -> preview -> create campaign ->
review -> confirm). The node-claim/delivery half of this same flow is
already proven over real HTTP in test_sms_csv_import_app.py; this file's
job is the browser/DOM half specifically.
"""

from __future__ import annotations

import random

import httpx
import pyotp
import pytest

from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_sms_console_e2e import _login

pytestmark = pytest.mark.e2e


async def test_bulk_import_upload_preview_and_create_campaign_through_the_real_ui(
    sms_server, pool, browser, tmp_path
):
    """required_fleet_group is deliberately set to a value unique to this
    test (not left blank/any-fleet) -- a real gap this closes: an earlier
    version of this test left its own started campaign's message sitting
    'queued' with required_fleet_group=NULL (any node eligible) forever,
    since the browser flow itself only clicks through to Start and never
    drives the message to a terminal state. In this shared, session
    -scoped sms_server's one 'default' tenant, that orphaned message
    stayed claimable by *any other test's* default-fleet node indefinitely
    -- including across separate pytest invocations, since nothing
    truncates this dev database between runs -- and broke
    test_sms_app.py's own unrelated delivery-flow test by handing its
    node this test's stale message instead of its own. Scoping to a
    private fleet group prevents that class of interference outright, and
    this test still drains its own message via the real node protocol
    below so nothing is left dangling regardless.
    """
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    marker_body = f"e2e-csv-{random.randint(1, 10**9)}"
    campaign_name = f"csv-ui-campaign-{random.randint(1, 10**9)}"
    fleet = f"e2e-csv-fleet-{random.randint(1, 10**9)}"

    csv_path = tmp_path / "bulk.csv"
    csv_path.write_text(
        "phone_number,message\n"
        f"0911111111,{marker_body}\n"
        "0512345678,bad number\n"
    )

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    await _login(page, sms_server, username, password, totp_secret)
    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)

    await page.click('button[data-screen="import"]')
    await page.wait_for_selector('#upload-form input[type="file"]', timeout=10000)

    # Default format is already phone_message (matches this CSV's own
    # header) -- only the file needs setting.
    await page.set_input_files('#upload-form input[type="file"]', str(csv_path))
    await page.click("#upload-btn")

    await page.wait_for_selector("#import-result .stat-grid", timeout=10000)
    stat_values = await page.locator("#import-result .stat-card .stat-value").all_text_contents()
    # Total, Valid, Invalid, Duplicates, Suppressed, in that literal order.
    assert stat_values[:3] == ["2", "1", "1"], stat_values

    # The invalid row is inspectable, not just counted.
    await page.click('#error-rows button[data-status="invalid"]')
    await page.wait_for_selector('#error-rows-table td:has-text("0512345678")', timeout=10000)

    # Create the campaign from this import -- no template/body needed,
    # this is the phone_message format (each row already has its own text).
    await page.fill('#create-campaign-from-import-form input[name="name"]', campaign_name)
    await page.fill('#create-campaign-from-import-form input[name="required_fleet_group"]', fleet)
    await page.click('#create-campaign-from-import-form button[type="submit"]')
    # Not a bare ".toast.visible" wait -- the upload step's own earlier
    # "CSV validated" toast can still be mid-fade (ui.js's own 3.5s
    # timer) when this second toast fires, so a class-presence wait can
    # resolve against the *first* toast instead of waiting for this
    # action's own. Waiting on the toast's actual text is what actually
    # proves this specific action's own success message landed.
    await page.wait_for_selector('#toast:has-text("created")', timeout=10000)

    assert page_errors == [], f"JS errors during the import flow: {page_errors}"

    # The created campaign is an ordinary draft from here on -- confirmed
    # by driving it through the *existing*, already-e2e-tested Campaigns
    # screen with zero import-specific UI of its own.
    await page.click('button[data-screen="campaigns"]')
    await page.wait_for_selector(f'tr:has-text("{campaign_name}")', timeout=10000)
    await page.click(f'tr:has-text("{campaign_name}")')
    await page.wait_for_selector('button[data-action="validate"]', timeout=10000)
    await page.click('button[data-action="validate"]')
    await page.wait_for_selector('button[data-action="start"]', timeout=10000)
    await page.click('button[data-action="start"]')
    await page.wait_for_selector('#campaign-detail .badge-running', timeout=10000)

    await page.close()

    # Drive the message to a real terminal state through the actual node
    # protocol (a physical device, not this admin browser tab, would call
    # these in production) -- nothing this test created is left sitting
    # 'queued' forever once it ends, regardless of the fleet-group
    # scoping above.
    async with httpx.AsyncClient(timeout=10.0) as client:
        login = await client.post(
            f"{sms_server}/auth/login",
            json={"username": username, "password": password, "totp_code": pyotp.TOTP(totp_secret).now()},
        )
        admin_headers = {"Authorization": f"Bearer {login.json()['token']}"}

        node = await client.post(
            f"{sms_server}/nodes", headers=admin_headers,
            json={"name": f"e2e-csv-drain-node-{random.randint(1, 10**9)}", "fleet_group": fleet},
        )
        node_headers = {"Authorization": f"Bearer {node.json()['token']}"}
        await client.post(f"{sms_server}/nodes/{node.json()['id']}/approve", headers=admin_headers)

        fetch = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
        job = fetch.json()["job"]
        assert job is not None
        assert job["body"] == marker_body
        await client.post(
            f"{sms_server}/v1/nodes/jobs/{job['message_id']}/result",
            headers=node_headers, json={"outcome": "delivered"},
        )
