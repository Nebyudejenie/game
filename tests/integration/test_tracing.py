"""Real OpenTelemetry traces (spec section 10.4: "deposit and payout
paths end to end") -- proven by actually configuring a real SDK
TracerProvider (not the API's own no-op default) and reading back real
exported spans with real parent/child relationships and real attributes,
the same "prove it happened, don't just assert config" discipline as
every other observability piece built this session.

Tracing configuration is process-global and can only be set once --
OpenTelemetry's own API silently ignores a second set_tracer_provider()
call (confirmed by actually calling it twice and checking which provider
stayed active, not assumed from docs). This file configures it exactly
once, at import time, with a real in-memory exporter. From the point this
file is first collected onward, every deposit/withdrawal/payout call
anywhere else in this pytest session becomes genuinely traced too -- a
deliberate, low-cost tradeoff (span objects are cheap; this process
already runs far heavier concurrent-load and chaos work elsewhere)
rather than building a separate subprocess harness just to isolate it.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services.payments import deposits, payout_worker
from services.payments.provider import CheckoutResult, PayoutResult
from services.payments.withdrawals import request_withdrawal
from tests.integration.conftest import create_funded_user

_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "test"}))
_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture(autouse=True)
def _clear_spans():
    _EXPORTER.clear()
    yield


@dataclass
class _FakeProvider:
    name: str = "chapa"

    async def create_checkout(self, *, amount, user_ref, our_ref, return_url):
        return CheckoutResult(
            checkout_url=f"https://pay.test/{our_ref}", provider_ref=our_ref, raw_response={}
        )

    def verify_webhook(self, headers, raw_body):
        raise NotImplementedError

    async def fetch_status(self, our_ref):
        raise NotImplementedError

    async def create_payout(self, *, method, amount, our_ref):
        return PayoutResult(provider_ref=f"chapa-{our_ref}", status="succeeded", raw_response={})


async def test_create_deposit_intent_produces_real_nested_spans(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("0.00"))
    await deposits.create_deposit_intent(
        pool,
        redis,
        _FakeProvider(),
        user_id=user_id,
        amount=Decimal("25.00"),
        phone_e164="+251911000222",
        return_url="https://example.test/return",
        min_deposit=Decimal("10.00"),
        daily_cap=Decimal("50000.00"),
    )

    spans = _EXPORTER.get_finished_spans()
    names = [s.name for s in spans]
    assert "deposit.create_intent" in names
    assert "deposit.provider_checkout" in names

    outer = next(s for s in spans if s.name == "deposit.create_intent")
    inner = next(s for s in spans if s.name == "deposit.provider_checkout")
    # A real parent/child relationship, not just two unrelated spans that
    # happen to share a test -- proves start_as_current_span() nesting
    # actually works across the await inside create_checkout(), not only
    # in a synchronous toy example.
    assert inner.parent is not None
    assert inner.parent.span_id == outer.context.span_id
    assert dict(outer.attributes)["user_id"] == user_id


async def test_apply_confirmed_status_span_records_the_real_outcome(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("0.00"))
    intent = await deposits.create_deposit_intent(
        pool,
        redis,
        _FakeProvider(),
        user_id=user_id,
        amount=Decimal("30.00"),
        phone_e164="+251911000333",
        return_url="https://example.test/return",
        min_deposit=Decimal("10.00"),
        daily_cap=Decimal("50000.00"),
    )
    _EXPORTER.clear()

    outcome = await deposits._apply_confirmed_status(
        pool,
        redis,
        our_ref=intent.our_ref,
        event_id=f"evt-tracing-test-{uuid.uuid4()}",
        provider_name="chapa",
        status="succeeded",
        amount=Decimal("30.00"),
        provider_ref="prov-ref-tracing-test",
        raw={},
    )
    assert outcome == "credited"

    span = next(
        s for s in _EXPORTER.get_finished_spans() if s.name == "deposit.apply_confirmed_status"
    )
    assert dict(span.attributes)["deposit.outcome"] == "credited"
    assert dict(span.attributes)["our_ref"] == intent.our_ref


async def test_request_withdrawal_span_records_the_real_status(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    intent = await request_withdrawal(
        pool,
        redis,
        _FakeProvider(),
        user_id=user_id,
        amount=Decimal("50.00"),
        method_kind="telebirr",
        account_ref="0911000444",
        holder_name="Tracing Test Holder",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("2000.00"),
        kyc_threshold=Decimal("5000.00"),
        chargeback_window_minutes=30,
        min_account_age_hours=0.0,
    )

    span = next(s for s in _EXPORTER.get_finished_spans() if s.name == "withdrawal.request")
    assert dict(span.attributes)["withdrawal.status"] == intent.status == "approved"
    assert dict(span.attributes)["withdrawal.our_ref"] == intent.our_ref
    # A code-review pass caught review_reason (the "which rule actually
    # failed" field added to the payments row itself) never reaching the
    # trace -- an on-call engineer reading this span in Jaeger/Tempo had
    # no way to see why a review-routed request landed there without a
    # separate database lookup. Nothing to show on the *approved* path
    # (no rule failed), confirmed here as the negative case; the review
    # -path positive case is the next test.
    assert "withdrawal.review_reason" not in dict(span.attributes)


async def test_request_withdrawal_span_records_the_review_reason_when_routed_to_review(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("10000.00"))
    intent = await request_withdrawal(
        pool,
        redis,
        _FakeProvider(),
        user_id=user_id,
        amount=Decimal("3000.00"),
        method_kind="telebirr",
        account_ref="0911000666",
        holder_name="Tracing Test Holder Review",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("2000.00"),
        kyc_threshold=Decimal("5000.00"),
        chargeback_window_minutes=30,
        min_account_age_hours=0.0,
    )

    span = next(s for s in _EXPORTER.get_finished_spans() if s.name == "withdrawal.request")
    assert dict(span.attributes)["withdrawal.status"] == intent.status == "review"
    assert "auto-approve limit" in dict(span.attributes)["withdrawal.review_reason"]


async def test_payout_dispatch_produces_real_nested_spans(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    await request_withdrawal(
        pool,
        redis,
        _FakeProvider(),
        user_id=user_id,
        amount=Decimal("40.00"),
        method_kind="telebirr",
        account_ref="0911000555",
        holder_name="Tracing Test Holder 2",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("2000.00"),
        kyc_threshold=Decimal("5000.00"),
        chargeback_window_minutes=30,
        min_account_age_hours=0.0,
    )
    _EXPORTER.clear()

    outcome = await payout_worker.process_next(
        pool, redis, _FakeProvider(), consumer_name="tracing-test-worker"
    )
    assert outcome == "succeeded"

    spans = _EXPORTER.get_finished_spans()
    names = [s.name for s in spans]
    assert "payout.dispatch" in names
    assert "payout.provider_call" in names

    outer = next(s for s in spans if s.name == "payout.dispatch")
    inner = next(s for s in spans if s.name == "payout.provider_call")
    assert inner.parent is not None
    assert inner.parent.span_id == outer.context.span_id
    assert dict(outer.attributes)["payout.outcome"] == "succeeded"
