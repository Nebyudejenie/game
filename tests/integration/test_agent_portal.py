"""Integration tests for the Payment Agent Portal (services/payments/
agent_auth.py + the /agent-portal/* routes in services/payments/app.py).

Real Postgres, real Redis, and the real HTTP route (payments_server) for
the same reason test_telebirr_ingest.py's own MacroDroid-route tests are
HTTP, not in-process calls: this is the one surface an agent's browser
genuinely reaches. The one-time login token itself is minted directly via
agent_auth.generate_login_link() in these tests (simulating exactly what
the bot's /portal command does -- that command itself is covered
separately in test_bot_handlers.py) rather than going through a real
Telegram round trip, which is already proven to work correctly there.
"""

import itertools
import random

import httpx

from services.payments import agent_auth

_id_counter = itertools.count(random.randint(10**8, 2 * 10**8))
_ref_counter = itertools.count(random.randint(10**7, 2 * 10**7))


def _next_telegram_id() -> int:
    return next(_id_counter)


def _next_reference() -> str:
    return f"DI{next(_ref_counter):08d}"


async def _create_agent(pool, *, is_active: bool = True) -> int:
    telegram_id = _next_telegram_id()
    await pool.execute(
        "INSERT INTO payment_agents (telegram_user_id, display_name, is_active) VALUES ($1, $2, $3)",
        telegram_id,
        f"Agent {telegram_id}",
        is_active,
    )
    return telegram_id


async def _insert_evidence(
    pool, *, source: str, source_ref: str, status: str = "available", amount: str = "25.00"
) -> str:
    reference = _next_reference()
    await pool.execute(
        """
        INSERT INTO payment_evidence
            (source, source_ref, raw_sms, evidence_hash, external_reference, raw_reference,
             amount, payer_name, payer_phone, status, parser_version)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'test-v1')
        """,
        source,
        source_ref,
        f"raw sms body for {reference} -- must never reach the agent portal response",
        f"hash-{reference}",
        reference,
        reference,
        amount,
        "Some Payer Name",
        "2519****0000",
        status,
    )
    return reference


async def test_login_with_valid_token_returns_a_session(payments_server, pool, redis):
    telegram_id = await _create_agent(pool)
    login_url = await agent_auth.generate_login_link(
        redis, telegram_user_id=telegram_id, portal_base_url="https://agent.test"
    )
    token = login_url.split("token=")[1]

    async with httpx.AsyncClient() as client:
        response = await client.post(f"{payments_server}/agent-portal/login", json={"token": token})
    assert response.status_code == 200
    assert response.json()["session_token"]


async def test_login_token_is_single_use(payments_server, pool, redis):
    telegram_id = await _create_agent(pool)
    login_url = await agent_auth.generate_login_link(
        redis, telegram_user_id=telegram_id, portal_base_url="https://agent.test"
    )
    token = login_url.split("token=")[1]

    async with httpx.AsyncClient() as client:
        first = await client.post(f"{payments_server}/agent-portal/login", json={"token": token})
        assert first.status_code == 200

        second = await client.post(f"{payments_server}/agent-portal/login", json={"token": token})
    assert second.status_code == 401


async def test_login_rejects_garbage_token(payments_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/agent-portal/login", json={"token": "not-a-real-token"}
        )
    assert response.status_code == 401


async def test_login_rejects_a_deactivated_agent(payments_server, pool, redis):
    telegram_id = await _create_agent(pool, is_active=False)
    login_url = await agent_auth.generate_login_link(
        redis, telegram_user_id=telegram_id, portal_base_url="https://agent.test"
    )
    token = login_url.split("token=")[1]

    async with httpx.AsyncClient() as client:
        response = await client.post(f"{payments_server}/agent-portal/login", json={"token": token})
    assert response.status_code == 401


async def test_me_and_submissions_reject_missing_or_garbage_auth(payments_server):
    async with httpx.AsyncClient() as client:
        no_auth = await client.get(f"{payments_server}/agent-portal/me")
        garbage = await client.get(
            f"{payments_server}/agent-portal/submissions",
            headers={"Authorization": "Bearer garbage-session-token"},
        )
    assert no_auth.status_code == 401
    assert garbage.status_code == 401


async def _login(payments_server, redis, telegram_id: int) -> str:
    login_url = await agent_auth.generate_login_link(
        redis, telegram_user_id=telegram_id, portal_base_url="https://agent.test"
    )
    token = login_url.split("token=")[1]
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{payments_server}/agent-portal/login", json={"token": token})
    return response.json()["session_token"]


async def test_me_reports_the_correct_identity(payments_server, pool, redis):
    telegram_id = await _create_agent(pool)
    session_token = await _login(payments_server, redis, telegram_id)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{payments_server}/agent-portal/me", headers={"Authorization": f"Bearer {session_token}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["telegram_user_id"] == telegram_id
    assert body["display_name"] == f"Agent {telegram_id}"


async def test_submissions_only_shows_this_agents_own_telegram_agent_rows(payments_server, pool, redis):
    """The actual security property under test: agent A's session must
    never see agent B's submissions, and must never see a macrodroid
    -sourced row at all (macrodroid submissions aren't tied to any
    telegram-agent identity in the first place).
    """
    agent_a = await _create_agent(pool)
    agent_b = await _create_agent(pool)

    ref_a = await _insert_evidence(pool, source="telegram_agent", source_ref=str(agent_a))
    ref_b = await _insert_evidence(pool, source="telegram_agent", source_ref=str(agent_b))
    ref_macrodroid = await _insert_evidence(pool, source="macrodroid", source_ref="some-device-id")

    session_token = await _login(payments_server, redis, agent_a)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{payments_server}/agent-portal/submissions",
            headers={"Authorization": f"Bearer {session_token}"},
        )
    assert response.status_code == 200
    references = {row["reference"] for row in response.json()}
    assert ref_a in references
    assert ref_b not in references
    assert ref_macrodroid not in references


async def test_submissions_never_include_raw_sms_or_payer_information(payments_server, pool, redis):
    agent_id = await _create_agent(pool)
    await _insert_evidence(pool, source="telegram_agent", source_ref=str(agent_id))
    session_token = await _login(payments_server, redis, agent_id)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{payments_server}/agent-portal/submissions",
            headers={"Authorization": f"Bearer {session_token}"},
        )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 1
    for row in rows:
        assert "raw_sms" not in row
        assert "payer_name" not in row
        assert "payer_phone" not in row
        assert "recipient_name" not in row
        assert "recipient_phone" not in row
        assert set(row.keys()) == {"reference", "amount", "status", "reject_reason", "received_at"}


async def test_deactivating_an_agent_immediately_invalidates_their_live_session(payments_server, pool, redis):
    """Mirrors services/admin/auth.py::resolve_session's own tested
    guarantee: an already-issued session must stop working the moment the
    account is deactivated, not merely after its TTL expires.
    """
    telegram_id = await _create_agent(pool)
    session_token = await _login(payments_server, redis, telegram_id)

    await pool.execute(
        "UPDATE payment_agents SET is_active = false WHERE telegram_user_id = $1", telegram_id
    )

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{payments_server}/agent-portal/me", headers={"Authorization": f"Bearer {session_token}"}
        )
    assert response.status_code == 401


async def test_logout_invalidates_the_session(payments_server, pool, redis):
    telegram_id = await _create_agent(pool)
    session_token = await _login(payments_server, redis, telegram_id)

    async with httpx.AsyncClient() as client:
        logout_response = await client.post(
            f"{payments_server}/agent-portal/logout",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        assert logout_response.status_code == 200

        me_response = await client.get(
            f"{payments_server}/agent-portal/me", headers={"Authorization": f"Bearer {session_token}"}
        )
    assert me_response.status_code == 401
