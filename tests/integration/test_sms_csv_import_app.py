"""HTTP-layer tests for the CSV bulk-import routes in services/sms/app.py:
RBAC through the real dependency chain, the full upload -> preview ->
create-campaign -> start -> node-claim flow over real HTTP, and the
upload-safety properties (idempotency, oversized files) a real operator
console actually exercises. Mirrors test_sms_app.py's own conventions
exactly (same sms_server/_auth_headers fixtures, same shared 'default'
tenant this whole file's own server fixture uses).
"""

import random

import httpx
import pytest

from tests.integration.test_sms_app import _auth_headers, _unique


def _csv_files(content: bytes, filename: str = "bulk.csv") -> dict[str, tuple[str, bytes, str]]:
    return {"file": (filename, content, "text/csv")}


async def test_rbac_support_cannot_upload_csv(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{sms_server}/import/csv",
            headers=headers,
            files=_csv_files(b"phone_number,message\n0911111111,hi\n"),
            data={"format": "phone_message", "idempotency_key": _unique("key")},
        )
    assert response.status_code == 403


async def test_rbac_ops_can_upload_csv(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{sms_server}/import/csv",
            headers=headers,
            files=_csv_files(b"phone_number,message\n0911111111,hi\n"),
            data={"format": "phone_message", "idempotency_key": _unique("key")},
        )
    assert response.status_code == 200, response.text
    assert response.json()["valid_rows"] == 1


async def test_upload_requires_authentication(sms_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{sms_server}/import/csv",
            files=_csv_files(b"phone_number,message\n0911111111,hi\n"),
            data={"format": "phone_message", "idempotency_key": _unique("key")},
        )
    assert response.status_code == 401


async def test_upload_rejects_missing_idempotency_key(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{sms_server}/import/csv",
            headers=headers,
            files=_csv_files(b"phone_number,message\n0911111111,hi\n"),
            data={"format": "phone_message", "idempotency_key": "   "},
        )
    assert response.status_code == 422


async def test_upload_rejects_malformed_csv_with_a_clean_error_not_a_500(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{sms_server}/import/csv",
            headers=headers,
            files=_csv_files(b"totally_wrong_header\nsomething\n"),
            data={"format": "phone_message", "idempotency_key": _unique("key")},
        )
    assert response.status_code == 422
    assert "missing required column" in response.json()["detail"]


async def test_oversized_upload_is_rejected_without_reading_it_all_into_memory(sms_server, pool):
    """A real 413, not a hang or an unbounded memory read -- proves
    _read_upload_bounded's own chunked-abort behavior over a real HTTP
    request, not just the byte-count check inside csv_import.py.
    """
    from packages.core.sms.csv_import import MAX_CSV_SIZE_BYTES

    headers = await _auth_headers(sms_server, pool, role="ops")
    oversized = b"phone_number,message\n" + (b"0911111111,x\n" * ((MAX_CSV_SIZE_BYTES // 13) + 1000))
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{sms_server}/import/csv",
            headers=headers,
            files=_csv_files(oversized),
            data={"format": "phone_message", "idempotency_key": _unique("key")},
        )
    assert response.status_code == 413


async def test_repeated_upload_over_http_with_same_key_returns_the_same_job(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="ops")
    key = _unique("dup-key")
    body = _csv_files(b"phone_number,message\n0911111111,hi\n")
    async with httpx.AsyncClient() as client:
        first = await client.post(
            f"{sms_server}/import/csv", headers=headers, files=body,
            data={"format": "phone_message", "idempotency_key": key},
        )
        second = await client.post(
            f"{sms_server}/import/csv", headers=headers, files=body,
            data={"format": "phone_message", "idempotency_key": key},
        )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]


async def test_get_import_job_and_list_rows_over_http(sms_server, pool):
    headers = await _auth_headers(sms_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        upload = await client.post(
            f"{sms_server}/import/csv", headers=headers,
            files=_csv_files(b"phone_number,message\n0911111111,hi\n0512345678,bad\n"),
            data={"format": "phone_message", "idempotency_key": _unique("key")},
        )
        job_id = upload.json()["job_id"]

        job = await client.get(f"{sms_server}/import/{job_id}", headers=headers)
        assert job.status_code == 200
        assert job.json()["valid_rows"] == 1
        assert job.json()["invalid_rows"] == 1

        invalid_rows = await client.get(f"{sms_server}/import/{job_id}/rows?status=invalid", headers=headers)
        assert invalid_rows.status_code == 200
        assert len(invalid_rows.json()) == 1
        assert invalid_rows.json()[0]["raw_phone"] == "0512345678"


async def test_full_csv_campaign_flow_over_real_http_including_node_claim(sms_server, pool):
    """The one thing that actually matters end to end: a CSV-created
    message reaches a real node through the exact same generic
    fetch-job/start/result protocol every other campaign already uses --
    no separate queue, no separate claim path. required_fleet_group is
    set to a random value unique to this test so this shared session
    -scoped tenant's other tests' own campaigns/nodes can never claim (or
    be claimed by) this one (same isolation technique test_sms_app.py's
    own required_fleet_group test already uses).
    """
    headers = await _auth_headers(sms_server, pool, role="superadmin")
    fleet = _unique("csv-fleet")
    marker_text = _unique("csv-body")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Registered and drained *before* this test creates its own
        # message -- required_fleet_group alone only stops *other*
        # tests' nodes from claiming *this* test's message; it does
        # nothing to stop *this* node from claiming an older, still
        # -queued message some other test left behind with
        # required_fleet_group=NULL (any node eligible, including one
        # scoped to a private fleet). Same "polluted shared dev
        # database" class of issue test_sms_console_e2e.py's own comment
        # already documents and drains for exactly this reason.
        node = await client.post(
            f"{sms_server}/nodes", headers=headers, json={"name": _unique("csv-node"), "fleet_group": fleet}
        )
        node_headers = {"Authorization": f"Bearer {node.json()['token']}"}
        await client.post(f"{sms_server}/nodes/{node.json()['id']}/approve", headers=headers)
        for _ in range(50):
            drain = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
            stray = drain.json()["job"]
            if stray is None:
                break
            await client.post(
                f"{sms_server}/v1/nodes/jobs/{stray['message_id']}/result",
                headers=node_headers, json={"outcome": "failed", "error_class": "permanent"},
            )

        upload = await client.post(
            f"{sms_server}/import/csv", headers=headers,
            files=_csv_files(f"phone_number,message\n0911111111,{marker_text}\n".encode()),
            data={"format": "phone_message", "idempotency_key": _unique("key")},
        )
        assert upload.status_code == 200, upload.text
        job_id = upload.json()["job_id"]
        assert upload.json()["will_send"] == 1

        campaign = await client.post(
            f"{sms_server}/campaigns", headers=headers,
            json={
                "name": _unique("csv-e2e-campaign"), "audience_filter": {},
                "import_job_id": job_id, "required_fleet_group": fleet,
            },
        )
        assert campaign.status_code == 200, campaign.text
        campaign_id = campaign.json()["id"]
        assert campaign.json()["import_job_id"] == job_id

        validate = await client.post(f"{sms_server}/campaigns/{campaign_id}/validate", headers=headers)
        assert validate.status_code == 200, validate.text
        assert validate.json()["recipient_count"] == 1

        start = await client.post(f"{sms_server}/campaigns/{campaign_id}/start", headers=headers)
        assert start.status_code == 200, start.text
        assert start.json()["messages_created"] == 1

        # Reuses the same node registered and drained above -- the
        # queue was empty a moment ago, so this is guaranteed to be
        # *this* test's own message.
        fetch = await client.post(f"{sms_server}/v1/nodes/fetch-job", headers=node_headers)
        assert fetch.status_code == 200
        job = fetch.json()["job"]
        assert job is not None
        assert job["body"] == marker_text
        assert job["phone_e164"] == "+251911111111"

        report = await client.post(
            f"{sms_server}/v1/nodes/jobs/{job['message_id']}/result",
            headers=node_headers, json={"outcome": "delivered"},
        )
        assert report.status_code == 200

        messages = await client.get(f"{sms_server}/campaigns/{campaign_id}/messages", headers=headers)
        assert messages.json()[0]["status"] == "delivered"
