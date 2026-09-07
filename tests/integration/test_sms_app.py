"""HTTP-layer tests for services/sms/app.py: real login against the exact
same admin accounts/session store the Bingo admin console uses, RBAC
enforced through the actual dependency chain, the generic node protocol
end to end, and compliance suppression enforcement -- see DECISIONS.md
(2026-09-07) for why this is a genuinely separate FastAPI app rather than
new routes bolted onto services/admin/app.py.
"""

import random

import httpx
import pyotp
import pytest

from tests.integration.test_admin_auth import create_test_admin


async def _login(sms_server: str, username: str, password: str, totp_secret: str) -> str:
    code = pyotp.TOTP(totp_secret).now()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{sms_server}/auth/login",
            json={"username": username, "password": password, "totp_code": code},
        )
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


async def _auth_headers(sms_server: str, pool, role: str = "superadmin") -> dict[str, str]:
    admin_id, username, password, totp_secret = await create_test_admin(pool, role=role)
    token = await _login(sms_server, username, password, totp_secret)
    return {"Authorization": f"Bearer {token}"}


def _unique(prefix: str) -> str:
    return f"{prefix}-{random.randint(1, 10**12)}"


async def test_login_works_with_the_same_admin_account_as_the_bingo_console(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{sms_server}/overview", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert "messages_by_status" in body and "nodes_by_status" in body and "campaigns_by_status" in body


async def test_protected_route_requires_bearer_token(sms_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{sms_server}/overview")
    assert response.status_code == 401


async def test_support_role_can_view_but_not_create_a_campaign(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        view = await client.get(f"{sms_server}/campaigns", headers=headers)
        assert view.status_code == 200
        create = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={"name": _unique("rbac"), "body_override": "hi", "audience_filter": {}},
        )
    assert create.status_code == 403


async def test_ops_role_can_create_but_not_start_a_campaign(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={"name": _unique("rbac-ops"), "body_override": "hi", "audience_filter": {}},
        )
        assert create.status_code == 200, create.text
        campaign_id = create.json()["id"]
        validate = await client.post(f"{sms_server}/campaigns/{campaign_id}/validate", headers=headers)
        assert validate.status_code == 200
        start = await client.post(f"{sms_server}/campaigns/{campaign_id}/start", headers=headers)
    assert start.status_code == 403


async def test_node_registration_requires_admin_and_returns_a_usable_token(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{sms_server}/nodes", headers=headers, json={"name": _unique("node"), "fleet_group": "default"}
        )
        assert create.status_code == 200, create.text
        body = create.json()
        assert body["status"] == "pending"
        node_token = body["token"]

        # A pending node authenticates but gets no work.
        fetch_pending = await client.post(
            f"{sms_server}/v1/nodes/fetch-job", headers={"Authorization": f"Bearer {node_token}"}
        )
        assert fetch_pending.status_code == 200
        assert fetch_pending.json()["job"] is None

        approve = await client.post(f"{sms_server}/nodes/{body['id']}/approve", headers=headers)
        assert approve.status_code == 200

        heartbeat = await client.post(
            f"{sms_server}/v1/nodes/heartbeat", headers={"Authorization": f"Bearer {node_token}"},
            json={"app_version": "1.0.0", "capabilities": {"max_concurrency": 1}},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["status"] == "active"


async def test_a_revoked_node_gets_401_from_the_node_protocol(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        create = await client.post(f"{sms_server}/nodes", headers=headers, json={"name": _unique("node-revoke")})
        node = create.json()
        await client.post(f"{sms_server}/nodes/{node['id']}/revoke", headers=headers)
        fetch = await client.post(
            f"{sms_server}/v1/nodes/fetch-job", headers={"Authorization": f"Bearer {node['token']}"}
        )
    assert fetch.status_code == 401


async def test_full_campaign_to_delivery_flow_over_real_http(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    phone = f"+2519{random.randint(10_000_000, 99_999_999)}"
    marker = _unique("segment")

    async with httpx.AsyncClient(timeout=10.0) as client:
        contact = await client.post(
            f"{sms_server}/contacts", headers=headers,
            json={"phone_e164": phone, "display_name": "E2E Contact", "attributes": {"segment": marker}},
        )
        assert contact.status_code == 200, contact.text

        campaign = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={
                "name": _unique("e2e-campaign"), "body_override": "Hello {{display_name}}!",
                "audience_filter": {"attributes": {"segment": marker}},
            },
        )
        assert campaign.status_code == 200, campaign.text
        campaign_id = campaign.json()["id"]

        validate = await client.post(f"{sms_server}/campaigns/{campaign_id}/validate", headers=headers)
        assert validate.status_code == 200, validate.text
        assert validate.json()["status"] == "ready"
        assert validate.json()["recipient_count"] == 1

        start = await client.post(f"{sms_server}/campaigns/{campaign_id}/start", headers=headers)
        assert start.status_code == 200, start.text
        assert start.json()["status"] == "running"
        assert start.json()["messages_created"] == 1

        node = await client.post(f"{sms_server}/nodes", headers=headers, json={"name": _unique("e2e-node")})
        node_headers = {"Authorization": f"Bearer {node.json()['token']}"}
        approve = await client.post(f"{sms_server}/nodes/{node.json()['id']}/approve", headers=headers)
        assert approve.status_code == 200

        fetch = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
        assert fetch.status_code == 200
        job = fetch.json()["job"]
        assert job is not None
        assert job["body"] == "Hello E2E Contact!"
        message_id = job["message_id"]

        started = await client.post(f"{sms_server}/v1/nodes/jobs/{message_id}/start", headers=node_headers)
        assert started.status_code == 200

        result = await client.post(
            f"{sms_server}/v1/nodes/jobs/{message_id}/result", headers=node_headers,
            json={"outcome": "delivered"},
        )
        assert result.status_code == 200
        assert result.json()["status"] == "delivered"

        campaign_after = await client.get(f"{sms_server}/campaigns/{campaign_id}", headers=headers)
        assert campaign_after.json()["status"] == "completed"

        messages_list = await client.get(f"{sms_server}/campaigns/{campaign_id}/messages", headers=headers)
        assert messages_list.status_code == 200
        assert messages_list.json()[0]["status"] == "delivered"


async def test_suppressed_contact_never_gets_a_deliverable_message(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    phone = f"+2519{random.randint(10_000_000, 99_999_999)}"
    marker = _unique("suppressed-segment")

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{sms_server}/contacts", headers=headers,
            json={"phone_e164": phone, "display_name": "Suppressed", "attributes": {"segment": marker}},
        )
        add_suppression = await client.post(
            f"{sms_server}/suppressions", headers=headers, json={"phone_e164": phone, "reason": "opt_out"}
        )
        assert add_suppression.status_code == 200

        campaign = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={
                "name": _unique("suppressed-campaign"), "body_override": "Hi {{display_name}}",
                "audience_filter": {"attributes": {"segment": marker}},
            },
        )
        campaign_id = campaign.json()["id"]
        validate = await client.post(f"{sms_server}/campaigns/{campaign_id}/validate", headers=headers)
        # The suppressed contact is excluded from resolve_recipients entirely
        # (audience.py), so there is nobody left to validate against.
        assert validate.json()["status"] == "failed"


async def test_node_cannot_report_a_result_for_a_message_it_does_not_own(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    phone = f"+2519{random.randint(10_000_000, 99_999_999)}"
    marker = _unique("ownership-segment")

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{sms_server}/contacts", headers=headers,
            json={"phone_e164": phone, "attributes": {"segment": marker}},
        )
        campaign = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={"name": _unique("own-campaign"), "body_override": "hi", "audience_filter": {"attributes": {"segment": marker}}},
        )
        campaign_id = campaign.json()["id"]
        await client.post(f"{sms_server}/campaigns/{campaign_id}/validate", headers=headers)
        await client.post(f"{sms_server}/campaigns/{campaign_id}/start", headers=headers)

        node_a = await client.post(f"{sms_server}/nodes", headers=headers, json={"name": _unique("node-a")})
        node_b = await client.post(f"{sms_server}/nodes", headers=headers, json={"name": _unique("node-b")})
        await client.post(f"{sms_server}/nodes/{node_a.json()['id']}/approve", headers=headers)
        await client.post(f"{sms_server}/nodes/{node_b.json()['id']}/approve", headers=headers)

        fetch = await client.post(
            f"{sms_server}/v1/nodes/fetch-job", headers={"Authorization": f"Bearer {node_a.json()['token']}"}
        )
        message_id = fetch.json()["job"]["message_id"]

        result = await client.post(
            f"{sms_server}/v1/nodes/jobs/{message_id}/result",
            headers={"Authorization": f"Bearer {node_b.json()['token']}"},
            json={"outcome": "delivered"},
        )
    assert result.status_code == 409


# --- Phase 2: node lifecycle, capacity, fleet-group eligibility ---------


async def test_node_heartbeat_reports_capacity_and_protocol_version_over_http(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        create = await client.post(f"{sms_server}/nodes", headers=headers, json={"name": _unique("cap-node")})
        node = create.json()
        node_headers = {"Authorization": f"Bearer {node['token']}"}
        heartbeat = await client.post(
            f"{sms_server}/v1/nodes/heartbeat", headers=node_headers,
            json={"app_version": "2.0", "max_concurrent_jobs": 3, "protocol_version": 2},
        )
        assert heartbeat.status_code == 200

        listing = await client.get(f"{sms_server}/nodes", headers=headers)
    row = next(n for n in listing.json() if n["id"] == node["id"])
    assert row["max_concurrent_jobs"] == 3
    assert row["protocol_version"] == 2


async def test_node_maintenance_route_refuses_new_work_with_an_honest_reason(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        create = await client.post(f"{sms_server}/nodes", headers=headers, json={"name": _unique("maint-node")})
        node = create.json()
        await client.post(f"{sms_server}/nodes/{node['id']}/approve", headers=headers)
        maint = await client.post(f"{sms_server}/nodes/{node['id']}/maintenance", headers=headers)
        assert maint.status_code == 200

        listing = await client.get(f"{sms_server}/nodes", headers=headers)
        row = next(n for n in listing.json() if n["id"] == node["id"])
        assert row["status"] == "maintenance"

        fetch = await client.post(
            f"{sms_server}/v1/nodes/fetch-job", headers={"Authorization": f"Bearer {node['token']}"}
        )
    assert fetch.json()["job"] is None
    assert "maintenance" in fetch.json()["reason"]


async def test_node_capacity_is_enforced_over_http(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        create = await client.post(f"{sms_server}/nodes", headers=headers, json={"name": _unique("cap-limit-node")})
        node = create.json()
        node_headers = {"Authorization": f"Bearer {node['token']}"}
        await client.post(f"{sms_server}/nodes/{node['id']}/approve", headers=headers)
        await client.post(
            f"{sms_server}/v1/nodes/heartbeat", headers=node_headers, json={"max_concurrent_jobs": 1}
        )

        phone = f"+2519{random.randint(10_000_000, 99_999_999)}"
        marker = _unique("cap-segment")
        await client.post(
            f"{sms_server}/contacts", headers=headers,
            json={"phone_e164": phone, "attributes": {"segment": marker}},
        )
        campaign = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={"name": _unique("cap-campaign"), "body_override": "hi", "audience_filter": {"attributes": {"segment": marker}}},
        )
        campaign_id = campaign.json()["id"]
        await client.post(f"{sms_server}/campaigns/{campaign_id}/validate", headers=headers)
        await client.post(f"{sms_server}/campaigns/{campaign_id}/start", headers=headers)

        first = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
        assert first.json()["job"] is not None
        # max_concurrent_jobs=1 and this node already holds one in-flight
        # message -- a second fetch must be refused regardless of what
        # else might be queued for this tenant.
        second = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
    assert second.json()["job"] is None
    assert "max_concurrent_jobs" in second.json()["reason"]


async def test_campaign_required_fleet_group_restricts_delivery_over_http(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    phone = f"+2519{random.randint(10_000_000, 99_999_999)}"
    marker = _unique("vip-segment")

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{sms_server}/contacts", headers=headers,
            json={"phone_e164": phone, "attributes": {"segment": marker}},
        )
        campaign = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={
                "name": _unique("vip-campaign"), "body_override": "hi",
                "audience_filter": {"attributes": {"segment": marker}}, "required_fleet_group": "vip",
            },
        )
        campaign_id = campaign.json()["id"]
        assert campaign.json()["required_fleet_group"] == "vip"
        await client.post(f"{sms_server}/campaigns/{campaign_id}/validate", headers=headers)
        await client.post(f"{sms_server}/campaigns/{campaign_id}/start", headers=headers)

        default_node = await client.post(
            f"{sms_server}/nodes", headers=headers, json={"name": _unique("default-node")}
        )
        vip_node = await client.post(
            f"{sms_server}/nodes", headers=headers, json={"name": _unique("vip-node"), "fleet_group": "vip"}
        )
        await client.post(f"{sms_server}/nodes/{default_node.json()['id']}/approve", headers=headers)
        await client.post(f"{sms_server}/nodes/{vip_node.json()['id']}/approve", headers=headers)

        # A DB-level check, not "did the default node receive nothing at
        # all" -- the shared 'default' tenant this server operates on can
        # carry unrelated stray queued messages from other tests, so the
        # robust assertion is that *this* message specifically was never
        # claimed by the ineligible node, not that fetch-job returned
        # nothing globally.
        await client.post(
            f"{sms_server}/v1/nodes/fetch-job", headers={"Authorization": f"Bearer {default_node.json()['token']}"}
        )
        our_message = await pool.fetchrow(
            "SELECT id, status, assigned_node_id FROM sms_messages WHERE campaign_id = $1", campaign_id
        )
        assert our_message["status"] == "queued"
        assert our_message["assigned_node_id"] is None

        # A node's own fleet_group restricts it to *matching or unrestricted*
        # campaigns, not exclusively to campaigns that require its group --
        # a real 'vip' node is also eligible for any older, unrelated
        # required_fleet_group=NULL message already sitting in this shared
        # 'default' tenant. Drain those first (same discipline as
        # test_sms_console_e2e.py's own drain step) so the eventual real
        # fetch is guaranteed to reach this test's own message, not stray
        # debris -- but stop as soon as *our* message itself comes back,
        # rather than dead-lettering it along with everything else.
        vip_headers = {"Authorization": f"Bearer {vip_node.json()['token']}"}
        job = None
        for _ in range(50):
            fetch = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=vip_headers)
            candidate = fetch.json()["job"]
            if candidate is None:
                break
            if candidate["message_id"] == our_message["id"]:
                job = candidate
                break
            await client.post(
                f"{sms_server}/v1/nodes/jobs/{candidate['message_id']}/result",
                headers=vip_headers, json={"outcome": "failed", "error_class": "permanent"},
            )
    assert job is not None
    assert job["message_id"] == our_message["id"]
