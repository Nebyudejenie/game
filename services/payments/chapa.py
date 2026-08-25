"""Chapa adapter (spec section 8.1) -- Ethiopia's primary payment rail:
Telebirr, CBE Birr, M-Pesa, banks, and cards for both deposits and payouts.

Endpoint contract confirmed against developer.chapa.co (2026-08-22):
  - POST /v1/transaction/initialize  -- create a checkout
  - GET  /v1/transaction/verify/{tx_ref}  -- poll a transaction's status
  - POST /v1/transfers  -- send a payout
  - Webhooks carry two headers, `chapa-signature` (HMAC-SHA256 of the
    secret key, signed with the secret key -- a static, per-account check)
    and `x-chapa-signature` (HMAC-SHA256 of the raw JSON body, signed with
    the secret key -- the actual payload-integrity check). Chapa's own
    docs: "If either header is missing or the value does not match,
    discard the request." Both are checked here.

Chapa's API wraps every response in {"status": "success"|"failed",
"message": ..., "data": {...}} -- this adapter always reads through that
envelope rather than trusting top-level fields, since Chapa returns HTTP
200 for some failure responses too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

import httpx

from services.payments.provider import (
    CheckoutResult,
    InvalidSignature,
    PayoutResult,
    StatusResult,
    VerifiedEvent,
)

BASE_URL = "https://api.chapa.co/v1"

# Chapa's own status vocabulary -> ours. Deliberately explicit and closed:
# an unrecognized status must never be silently treated as "succeeded".
_STATUS_MAP = {
    "success": "succeeded",
    "successful": "succeeded",
    "failed": "failed",
    "pending": "pending",
    "cancelled": "cancelled",
}


def _map_status(raw_status: str) -> str:
    mapped = _STATUS_MAP.get(raw_status.lower())
    if mapped is None:
        raise ValueError(f"unrecognized chapa status: {raw_status!r}")
    return mapped


class ChapaProvider:
    name = "chapa"

    def __init__(self, secret_key: str, *, base_url: str = BASE_URL) -> None:
        self._secret_key = secret_key
        self._base_url = base_url

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._secret_key}",
            "Content-Type": "application/json",
        }

    async def create_checkout(
        self, *, amount: Decimal, user_ref: str, our_ref: str, return_url: str
    ) -> CheckoutResult:
        # Chapa requires the caller to hand it a tx_ref up front, rather
        # than allocating one at checkout time -- so for this adapter,
        # our_ref *is* the provider-facing reference from the start. The
        # separate id Chapa allocates only shows up later, in the webhook's
        # "reference" field, and gets recorded onto payments.provider_ref
        # once that arrives.
        body = {
            "amount": str(amount),
            "currency": "ETB",
            "tx_ref": our_ref,
            "phone_number": user_ref,
            "return_url": return_url,
            "callback_url": return_url,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self._base_url}/transaction/initialize", headers=self._headers(), json=body
            )
        payload = response.json()
        if response.status_code != 200 or payload.get("status") != "success":
            raise RuntimeError(f"chapa checkout creation failed: {payload}")
        data = payload["data"]
        return CheckoutResult(
            checkout_url=data["checkout_url"], provider_ref=our_ref, raw_response=payload
        )

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> VerifiedEvent:
        lower_headers = {k.lower(): v for k, v in headers.items()}
        payload_signature = lower_headers.get("x-chapa-signature")
        key_signature = lower_headers.get("chapa-signature")
        if not payload_signature or not key_signature:
            raise InvalidSignature("missing signature header")

        expected_payload_signature = hmac.new(
            self._secret_key.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        expected_key_signature = hmac.new(
            self._secret_key.encode("utf-8"), self._secret_key.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_payload_signature, payload_signature):
            raise InvalidSignature("payload signature mismatch")
        if not hmac.compare_digest(expected_key_signature, key_signature):
            raise InvalidSignature("key signature mismatch")

        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise InvalidSignature("body is not valid json") from exc

        our_ref = data.get("tx_ref")
        reference = data.get("reference")
        raw_status = data.get("status")
        raw_amount = data.get("amount")
        if not our_ref or not reference or raw_status is None or raw_amount is None:
            raise InvalidSignature("missing required webhook fields")

        try:
            status = _map_status(str(raw_status))
        except ValueError as exc:
            raise InvalidSignature(str(exc)) from exc

        # A code review pass caught that this webhook's field-presence
        # checks above (not missing/None) didn't also cover well-formed
        # -ness: a signed but malformed "amount" (garbage text, a JSON
        # object where a number was expected) made Decimal(str(...)) raise
        # decimal.InvalidOperation, which handle_webhook()'s only caller
        # (services/payments/app.py's chapa_webhook() route) doesn't
        # catch -- propagating out as an unhandled 500 instead of the
        # same clean, deliberate "untrustworthy payload, discard it"
        # 401 response every other malformed-webhook case here already
        # gets. Matches the exact same try/except-and-reraise pattern
        # _map_status()'s own ValueError -> InvalidSignature conversion
        # just above already uses for the same class of problem.
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation as exc:
            raise InvalidSignature(f"malformed amount: {raw_amount!r}") from exc

        return VerifiedEvent(
            event_id=str(reference),
            our_ref=str(our_ref),
            status=status,  # type: ignore[arg-type]
            amount=amount,
            provider_ref=str(reference),
            raw=data,
        )

    async def fetch_status(self, our_ref: str) -> StatusResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{self._base_url}/transaction/verify/{our_ref}", headers=self._headers()
            )
        payload = response.json()
        if response.status_code == 404:
            return StatusResult(status="pending", amount=None, provider_ref=None, raw=payload)
        if payload.get("status") != "success":
            raise RuntimeError(f"chapa status check failed: {payload}")
        data = payload["data"]
        status = _map_status(str(data["status"]))
        return StatusResult(
            status=status,  # type: ignore[arg-type]
            amount=Decimal(str(data["amount"])),
            provider_ref=str(data.get("reference", our_ref)),
            raw=payload,
        )

    async def create_payout(
        self, *, method: dict[str, str], amount: Decimal, our_ref: str
    ) -> PayoutResult:
        body = {
            "account_name": method["holder_name"],
            "account_number": method["account_ref"],
            "bank_code": method["bank_code"],
            "amount": str(amount),
            "reference": our_ref,
            "currency": "ETB",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self._base_url}/transfers", headers=self._headers(), json=body
            )
        payload = response.json()
        if response.status_code not in (200, 201) or payload.get("status") != "success":
            raise RuntimeError(f"chapa payout creation failed: {payload}")
        data = payload.get("data") or {}
        return PayoutResult(
            provider_ref=str(data.get("reference", our_ref)), status="processing", raw_response=payload
        )
