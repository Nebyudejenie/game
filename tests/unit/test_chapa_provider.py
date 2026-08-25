"""Pure, no-network tests for services/payments/chapa.py's webhook
signature verification -- the actual security boundary between "anyone on
the internet who knows our webhook URL" and "a trusted payment
confirmation". Chapa's own docs: two headers must both check out, or the
request is discarded.
"""

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from services.payments.chapa import ChapaProvider
from services.payments.provider import InvalidSignature

SECRET = "test-chapa-secret-key"


def _sign(secret: str, raw_body: bytes) -> dict[str, str]:
    payload_signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    key_signature = hmac.new(secret.encode(), secret.encode(), hashlib.sha256).hexdigest()
    return {"x-chapa-signature": payload_signature, "chapa-signature": key_signature}


def _body(**overrides: object) -> bytes:
    payload = {
        "event": "charge.success",
        "status": "success",
        "amount": "200.00",
        "currency": "ETB",
        "tx_ref": "DEP-2026-000001",
        "reference": "chapa-ref-abc123",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_valid_signature_is_accepted_and_parsed():
    provider = ChapaProvider(SECRET)
    raw_body = _body()
    event = provider.verify_webhook(_sign(SECRET, raw_body), raw_body)
    assert event.our_ref == "DEP-2026-000001"
    assert event.event_id == "chapa-ref-abc123"
    assert event.status == "succeeded"
    assert event.amount == Decimal("200.00")


def test_missing_signature_headers_rejected():
    provider = ChapaProvider(SECRET)
    with pytest.raises(InvalidSignature):
        provider.verify_webhook({}, _body())


def test_payload_signature_only_present_is_rejected():
    provider = ChapaProvider(SECRET)
    raw_body = _body()
    headers = _sign(SECRET, raw_body)
    del headers["chapa-signature"]
    with pytest.raises(InvalidSignature):
        provider.verify_webhook(headers, raw_body)


def test_tampered_body_after_signing_is_rejected():
    provider = ChapaProvider(SECRET)
    raw_body = _body()
    headers = _sign(SECRET, raw_body)
    tampered_body = _body(amount="999999.00")
    with pytest.raises(InvalidSignature):
        provider.verify_webhook(headers, tampered_body)


def test_wrong_secret_is_rejected():
    provider = ChapaProvider(SECRET)
    raw_body = _body()
    headers = _sign("some-other-secret", raw_body)
    with pytest.raises(InvalidSignature):
        provider.verify_webhook(headers, raw_body)


def test_header_lookup_is_case_insensitive():
    provider = ChapaProvider(SECRET)
    raw_body = _body()
    headers = {k.upper(): v for k, v in _sign(SECRET, raw_body).items()}
    event = provider.verify_webhook(headers, raw_body)
    assert event.our_ref == "DEP-2026-000001"


def test_unrecognized_status_is_rejected_not_silently_accepted():
    provider = ChapaProvider(SECRET)
    raw_body = _body(status="some_new_status_chapa_invented_later")
    headers = _sign(SECRET, raw_body)
    with capture_logs() as logs, pytest.raises(InvalidSignature):
        provider.verify_webhook(headers, raw_body)
    assert any(
        e.get("event") == "chapa_webhook_content_rejected" and e.get("reason") == "unrecognized status"
        for e in logs
    ), logs


def test_missing_required_field_is_rejected():
    provider = ChapaProvider(SECRET)
    raw_body = json.dumps({"status": "success", "amount": "1"}).encode()  # no tx_ref/reference
    headers = _sign(SECRET, raw_body)
    with capture_logs() as logs, pytest.raises(InvalidSignature):
        provider.verify_webhook(headers, raw_body)
    assert any(
        e.get("event") == "chapa_webhook_content_rejected"
        and e.get("reason") == "missing required webhook fields"
        for e in logs
    ), logs


def test_malformed_amount_is_rejected_not_an_unhandled_crash():
    # A code review pass caught that a present-but-garbage "amount" (unlike
    # a *missing* one, already covered above) made Decimal(str(...)) raise
    # decimal.InvalidOperation -- a class of exception the only caller of
    # verify_webhook() (services/payments/app.py's chapa_webhook() route)
    # doesn't catch, turning a signed-but-malformed payload into an
    # unhandled 500 instead of the same deliberate, discard-and-401
    # response every other malformed-webhook case here already gets.
    provider = ChapaProvider(SECRET)
    raw_body = _body(amount="not-a-number")
    headers = _sign(SECRET, raw_body)
    with capture_logs() as logs, pytest.raises(InvalidSignature):
        provider.verify_webhook(headers, raw_body)
    assert any(
        e.get("event") == "chapa_webhook_content_rejected" and e.get("reason") == "malformed amount"
        for e in logs
    ), logs


def test_a_forged_signature_is_not_logged_as_content_rejected():
    # A code review pass caught that a *correctly signed* request rejected
    # for bad content (the three tests above) was indistinguishable from
    # an outright forgery attempt in this service's own logs -- both just
    # vanished as a silent InvalidSignature. Fixed by logging the content
    # -rejection cases specifically; this confirms the fix didn't also
    # start logging genuine signature failures as though they were that,
    # which would be actively misleading (a real forgery attempt logged
    # as "huh, weird payload" rather than "someone without our secret key
    # tried this").
    provider = ChapaProvider(SECRET)
    raw_body = _body()
    with capture_logs() as logs, pytest.raises(InvalidSignature):
        provider.verify_webhook(_sign("wrong-secret", raw_body), raw_body)
    assert not any(e.get("event") == "chapa_webhook_content_rejected" for e in logs), logs
