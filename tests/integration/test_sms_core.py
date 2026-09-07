"""Domain-level tests for packages/core/sms/* against real Postgres -- no
mocks anywhere in this file. See DECISIONS.md (2026-09-07) for the
Enterprise SMS Control Plane's scoping.
"""

import itertools
import random
from datetime import timedelta, timezone, datetime

import pytest
import pytest_asyncio

from packages.core.sms import audience, campaigns, compliance, messages, nodes, templates
from packages.core.sms.audience import InvalidAudienceFilter
from packages.core.sms.campaigns import InvalidTransition
from packages.core.sms.messages import NotOwnedByNode

_phone_counter = itertools.count(random.randint(30_000_000, 40_000_000))


def unique_sms_phone() -> str:
    return f"+2519{next(_phone_counter):08d}"


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_id(pool):
    """A fresh, throwaway tenant per test -- not the shared 'default' seed
    row. sms_messages.claim_next_message() (by design) claims the oldest
    queued message for a *whole tenant*, so two tests sharing one tenant
    in this never-rolled-back dev database could otherwise claim each
    other's rows (the exact "polluted shared dev database" failure class
    already documented in DECISIONS.md for other suites in this repo).
    Giving each test its own tenant makes that cross-test interference
    structurally impossible, and is itself a real exercise of tenant
    isolation, one of this feature's own non-negotiable properties.
    """
    slug = f"test-{random.randint(1, 10**12)}"
    return await pool.fetchval(
        "INSERT INTO sms_tenants (slug, name) VALUES ($1, $2) RETURNING id", slug, slug
    )


async def _make_contact(conn, tenant_id, *, attributes=None, opted_out=False):
    import json

    row = await conn.fetchrow(
        """
        INSERT INTO sms_contacts (tenant_id, phone_e164, display_name, attributes, opted_out)
        VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id, phone_e164
        """,
        tenant_id, unique_sms_phone(), "Test Contact", json.dumps(attributes or {}), opted_out,
    )
    return row["id"], row["phone_e164"]


async def _make_template(conn, tenant_id, *, body="Hello {{display_name}}!"):
    row = await conn.fetchrow(
        """
        INSERT INTO sms_templates (tenant_id, name, body, variables)
        VALUES ($1, $2, $3, $4) RETURNING id
        """,
        tenant_id, f"tpl-{random.randint(1, 10**9)}", body, templates.extract_variables(body),
    )
    return row["id"]


async def _make_admin_id(conn):
    row = await conn.fetchrow(
        "SELECT id FROM admin_users LIMIT 1"
    )
    if row is not None:
        return row["id"]
    from services.admin import auth

    admin_id, _ = await auth.create_admin_user(
        conn, username=f"sms-test-{random.randint(1, 10**9)}", password="x" * 20, role="superadmin"
    )
    return admin_id


# --- templates -------------------------------------------------------------


def test_extract_variables_finds_each_placeholder_once():
    assert templates.extract_variables("Hi {{name}}, your code is {{code}}. Bye {{name}}") == ["name", "code"]


def test_render_template_substitutes_values():
    assert templates.render_template("Hi {{name}}!", {"name": "Abebe"}) == "Hi Abebe!"


def test_render_template_raises_on_missing_variable():
    with pytest.raises(templates.MissingVariables):
        templates.render_template("Hi {{name}}!", {})


def test_segment_count_gsm7_single_segment_boundary():
    assert templates.count_segments("a" * 160) == 1
    assert templates.count_segments("a" * 161) == 2


def test_segment_count_gsm7_multi_segment_uses_153_per_part():
    # 306 = 2 * 153 exactly -- still 2 segments, not 3.
    assert templates.count_segments("a" * 306) == 2
    assert templates.count_segments("a" * 307) == 3


def test_segment_count_switches_to_ucs2_for_non_gsm7_text():
    # An Amharic character forces UCS2 -- 70/67 limits, not 160/153.
    text = "አ" * 70
    assert templates.count_segments(text) == 1
    assert templates.count_segments(text + "አ") == 2


# --- audience ----------------------------------------------------------


def test_validate_audience_filter_rejects_unknown_keys():
    with pytest.raises(InvalidAudienceFilter):
        audience.validate_audience_filter({"sql": "DROP TABLE users"})


def test_validate_audience_filter_accepts_empty_and_attributes():
    audience.validate_audience_filter({})
    audience.validate_audience_filter({"attributes": {"vip": True}})


async def test_resolve_recipients_matches_attribute_filter(conn, tenant_id):
    matching_id, matching_phone = await _make_contact(conn, tenant_id, attributes={"vip": True})
    await _make_contact(conn, tenant_id, attributes={"vip": False})

    recipients = await audience.resolve_recipients(conn, tenant_id=tenant_id, audience_filter={"attributes": {"vip": True}})
    assert any(r.contact_id == matching_id for r in recipients)
    assert all(r.phone_e164 != "" for r in recipients)


async def test_resolve_recipients_excludes_opted_out_contacts(conn, tenant_id):
    _, phone = await _make_contact(conn, tenant_id, opted_out=True)
    recipients = await audience.resolve_recipients(conn, tenant_id=tenant_id, audience_filter={})
    assert all(r.phone_e164 != phone for r in recipients)


async def test_resolve_recipients_excludes_suppressed_contacts(conn, tenant_id):
    contact_id, phone = await _make_contact(conn, tenant_id)
    await compliance.add_suppression(conn, tenant_id=tenant_id, phone_e164=phone, reason="manual", note=None, created_by_admin_id=None)
    recipients = await audience.resolve_recipients(conn, tenant_id=tenant_id, audience_filter={})
    assert all(r.contact_id != contact_id for r in recipients)


# --- compliance ----------------------------------------------------------


async def test_add_and_remove_suppression_round_trips(conn, tenant_id):
    phone = unique_sms_phone()
    assert await audience.is_suppressed(conn, tenant_id=tenant_id, phone_e164=phone) is False
    await compliance.add_suppression(conn, tenant_id=tenant_id, phone_e164=phone, reason="opt_out", note=None, created_by_admin_id=None)
    assert await audience.is_suppressed(conn, tenant_id=tenant_id, phone_e164=phone) is True
    removed = await compliance.remove_suppression(conn, tenant_id=tenant_id, phone_e164=phone)
    assert removed is True
    assert await audience.is_suppressed(conn, tenant_id=tenant_id, phone_e164=phone) is False


# --- nodes ---------------------------------------------------------------


async def test_create_node_returns_a_working_credential(conn, tenant_id):
    node, raw_token = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    assert node.status == "pending"
    authenticated = await nodes.authenticate_node(conn, raw_token=raw_token)
    assert authenticated is not None
    assert authenticated.id == node.id


async def test_authenticate_node_rejects_wrong_token(conn, tenant_id):
    await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    assert await nodes.authenticate_node(conn, raw_token="not-a-real-token") is None


async def test_a_revoked_node_cannot_authenticate(conn, tenant_id):
    """The directive's own named invariant: 'A revoked node cannot receive
    work' -- enforced here at the strongest possible point, authentication
    itself, not merely at the fetch-job step.
    """
    node, raw_token = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.revoke_node(conn, node_id=node.id)
    assert await nodes.authenticate_node(conn, raw_token=raw_token) is None


async def test_rotate_token_invalidates_the_old_credential(conn, tenant_id):
    node, old_token = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    new_token = await nodes.rotate_token(conn, node_id=node.id)
    assert await nodes.authenticate_node(conn, raw_token=old_token) is None
    authenticated = await nodes.authenticate_node(conn, raw_token=new_token)
    assert authenticated is not None and authenticated.id == node.id


async def test_health_score_is_zero_with_no_heartbeat(conn, tenant_id):
    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    score = await nodes.compute_and_store_health_score(conn, node_id=node.id)
    assert score == 0


async def test_health_score_is_high_after_all_recent_deliveries_succeed(conn, tenant_id):
    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.approve_node(conn, node_id=node.id)
    await nodes.record_heartbeat(conn, node_id=node.id, app_version="1.0", capabilities={})
    for i in range(5):
        msg = await conn.fetchrow(
            "INSERT INTO sms_messages (tenant_id, phone_e164, body, idempotency_key) VALUES ($1, $2, 'x', $3) RETURNING id",
            tenant_id, unique_sms_phone(), f"health-{node.id}-{i}",
        )
        await conn.execute(
            "INSERT INTO sms_delivery_attempts (message_id, node_id, attempt_number, outcome) VALUES ($1, $2, 1, 'delivered')",
            msg["id"], node.id,
        )
    score = await nodes.compute_and_store_health_score(conn, node_id=node.id)
    assert score == 100


# --- campaigns + messages: full lifecycle --------------------------------


async def test_campaign_validate_fails_with_zero_resolvable_recipients(conn, tenant_id):
    admin_id = await _make_admin_id(conn)
    template_id = await _make_template(conn, tenant_id)
    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="empty", template_id=template_id, body_override=None,
        audience_filter={"attributes": {"nonexistent_segment_marker": "nobody-has-this"}}, created_by_admin_id=admin_id,
    )
    result = await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert result.status == "failed"


async def test_campaign_full_lifecycle_creates_and_delivers_messages(conn, tenant_id):
    admin_id = await _make_admin_id(conn)
    template_id = await _make_template(conn, tenant_id)
    marker = f"lifecycle-{random.randint(1, 10**9)}"
    contact_id, phone = await _make_contact(conn, tenant_id, attributes={"segment": marker})

    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="lifecycle", template_id=template_id, body_override=None,
        audience_filter={"attributes": {"segment": marker}}, created_by_admin_id=admin_id,
    )
    assert campaign.status == "draft"

    validated = await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert validated.status == "ready"
    assert validated.recipient_count == 1

    started, created = await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert started.status == "running"
    assert created == 1

    # Idempotent: calling start again creates zero additional messages.
    _, created_again = await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert created_again == 0

    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=admin_id)
    await nodes.approve_node(conn, node_id=node.id)

    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed is not None
    assert claimed.campaign_id == campaign.id
    assert claimed.phone_e164 == phone

    await messages.start_message(conn, message_id=claimed.id, node_id=node.id)
    delivered = await messages.report_result(
        conn, message_id=claimed.id, node_id=node.id, outcome="delivered", error_class=None, raw_provider_response="OK",
    )
    assert delivered.status == "delivered"

    # report_result already completes the campaign itself once every
    # message reaches a terminal state -- no separate call needed.
    completed = await campaigns.get_campaign(conn, campaign_id=campaign.id)
    assert completed.status == "completed"


async def test_invalid_transition_is_rejected(conn, tenant_id):
    admin_id = await _make_admin_id(conn)
    template_id = await _make_template(conn, tenant_id)
    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="bad-transition", template_id=template_id, body_override=None,
        audience_filter={}, created_by_admin_id=admin_id,
    )
    with pytest.raises(InvalidTransition):
        # draft -> running directly is not a legal transition; must go
        # through validate/ready first.
        await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)


async def test_cancel_campaign_cancels_only_still_queued_messages(conn, tenant_id):
    admin_id = await _make_admin_id(conn)
    template_id = await _make_template(conn, tenant_id)
    marker = f"cancel-{random.randint(1, 10**9)}"
    await _make_contact(conn, tenant_id, attributes={"segment": marker})
    await _make_contact(conn, tenant_id, attributes={"segment": marker})

    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="cancel-me", template_id=template_id, body_override=None,
        audience_filter={"attributes": {"segment": marker}}, created_by_admin_id=admin_id,
    )
    await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)

    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=admin_id)
    await nodes.approve_node(conn, node_id=node.id)
    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed is not None  # one of the two messages is now 'assigned'

    cancelled = await campaigns.cancel_campaign(conn, campaign_id=campaign.id, admin_id=admin_id, reason="test cancel")
    assert cancelled.status == "cancelled"

    rows = await conn.fetch("SELECT status FROM sms_messages WHERE campaign_id = $1 ORDER BY id", campaign.id)
    statuses = [r["status"] for r in rows]
    assert "cancelled" in statuses
    assert "assigned" in statuses  # the already-claimed one is left alone


async def test_paused_campaign_messages_are_not_claimable(conn, tenant_id):
    admin_id = await _make_admin_id(conn)
    template_id = await _make_template(conn, tenant_id)
    marker = f"pause-{random.randint(1, 10**9)}"
    await _make_contact(conn, tenant_id, attributes={"segment": marker})

    campaign = await campaigns.create_campaign(
        conn, tenant_id=tenant_id, name="pause-me", template_id=template_id, body_override=None,
        audience_filter={"attributes": {"segment": marker}}, created_by_admin_id=admin_id,
    )
    await campaigns.validate_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    await campaigns.start_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    await campaigns.pause_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)

    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=admin_id)
    await nodes.approve_node(conn, node_id=node.id)
    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed is None  # queue state preserved, just not offered while paused

    resumed = await campaigns.resume_campaign(conn, campaign_id=campaign.id, admin_id=admin_id)
    assert resumed.status == "running"
    claimed_after_resume = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed_after_resume is not None


async def test_claim_next_message_respects_priority_order(conn, tenant_id):
    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.approve_node(conn, node_id=node.id)
    low_key = f"prio-low-{random.randint(1, 10**9)}"
    crit_key = f"prio-crit-{random.randint(1, 10**9)}"
    await conn.execute(
        "INSERT INTO sms_messages (tenant_id, phone_e164, body, priority, idempotency_key) VALUES ($1, $2, 'x', 'low', $3)",
        tenant_id, unique_sms_phone(), low_key,
    )
    await conn.execute(
        "INSERT INTO sms_messages (tenant_id, phone_e164, body, priority, idempotency_key) VALUES ($1, $2, 'x', 'critical', $3)",
        tenant_id, unique_sms_phone(), crit_key,
    )
    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed is not None
    assert claimed.priority == "critical"


async def test_report_result_requeues_a_retryable_failure(conn, tenant_id):
    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.approve_node(conn, node_id=node.id)
    key = f"retry-{random.randint(1, 10**9)}"
    await conn.execute(
        "INSERT INTO sms_messages (tenant_id, phone_e164, body, idempotency_key) VALUES ($1, $2, 'x', $3)",
        tenant_id, unique_sms_phone(), key,
    )
    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed is not None
    result = await messages.report_result(
        conn, message_id=claimed.id, node_id=node.id, outcome="failed", error_class="network", raw_provider_response=None,
    )
    assert result.status == "queued"
    assert result.assigned_node_id is None


async def test_report_result_dead_letters_a_permanent_failure(conn, tenant_id):
    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.approve_node(conn, node_id=node.id)
    key = f"permfail-{random.randint(1, 10**9)}"
    await conn.execute(
        "INSERT INTO sms_messages (tenant_id, phone_e164, body, idempotency_key) VALUES ($1, $2, 'x', $3)",
        tenant_id, unique_sms_phone(), key,
    )
    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed is not None
    result = await messages.report_result(
        conn, message_id=claimed.id, node_id=node.id, outcome="failed", error_class="permanent", raw_provider_response=None,
    )
    assert result.status == "dead_letter"


async def test_report_result_dead_letters_after_max_attempts_exhausted(conn, tenant_id):
    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.approve_node(conn, node_id=node.id)
    key = f"exhaust-{random.randint(1, 10**9)}"
    await conn.execute(
        "INSERT INTO sms_messages (tenant_id, phone_e164, body, idempotency_key) VALUES ($1, $2, 'x', $3)",
        tenant_id, unique_sms_phone(), key,
    )
    message_id = None
    for attempt in range(messages.MAX_DELIVERY_ATTEMPTS):
        claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
        assert claimed is not None, f"expected a claimable message on attempt {attempt + 1}"
        message_id = claimed.id
        result = await messages.report_result(
            conn, message_id=claimed.id, node_id=node.id, outcome="failed", error_class="network", raw_provider_response=None,
        )
    assert result.status == "dead_letter"
    assert result.attempt_count == messages.MAX_DELIVERY_ATTEMPTS


async def test_report_result_rejects_a_report_from_a_non_owning_node(conn, tenant_id):
    node_a, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-a-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    node_b, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-b-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.approve_node(conn, node_id=node_a.id)
    await nodes.approve_node(conn, node_id=node_b.id)
    key = f"ownership-{random.randint(1, 10**9)}"
    await conn.execute(
        "INSERT INTO sms_messages (tenant_id, phone_e164, body, idempotency_key) VALUES ($1, $2, 'x', $3)",
        tenant_id, unique_sms_phone(), key,
    )
    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node_a.id)
    assert claimed is not None
    with pytest.raises(NotOwnedByNode):
        await messages.report_result(
            conn, message_id=claimed.id, node_id=node_b.id, outcome="delivered", error_class=None, raw_provider_response=None,
        )


async def test_reconcile_stale_in_flight_moves_a_silent_message_to_unknown_then_requeues(conn, tenant_id):
    node, _ = await nodes.create_node(conn, tenant_id=tenant_id, name=f"node-{random.randint(1, 10**9)}", fleet_group="default", created_by_admin_id=None)
    await nodes.approve_node(conn, node_id=node.id)
    key = f"stale-{random.randint(1, 10**9)}"
    await conn.execute(
        "INSERT INTO sms_messages (tenant_id, phone_e164, body, idempotency_key) VALUES ($1, $2, 'x', $3)",
        tenant_id, unique_sms_phone(), key,
    )
    claimed = await messages.claim_next_message(conn, tenant_id=tenant_id, node_id=node.id)
    assert claimed is not None
    # Simulate the node going silent: backdate assigned_at well past the
    # in-flight timeout without ever calling start/report_result.
    await conn.execute(
        "UPDATE sms_messages SET assigned_at = now() - interval '1 hour' WHERE id = $1", claimed.id
    )
    reconciled = await messages.reconcile_stale_in_flight(conn, tenant_id=tenant_id, stale_after=timedelta(minutes=5))
    assert reconciled >= 1

    row = await conn.fetchrow("SELECT status, last_error_class FROM sms_messages WHERE id = $1", claimed.id)
    assert row["status"] == "queued"
    assert row["last_error_class"] == "timeout"

    attempt = await conn.fetchrow(
        "SELECT outcome, error_class FROM sms_delivery_attempts WHERE message_id = $1 ORDER BY attempt_number DESC LIMIT 1",
        claimed.id,
    )
    assert attempt["outcome"] == "unknown"
    assert attempt["error_class"] == "timeout"
