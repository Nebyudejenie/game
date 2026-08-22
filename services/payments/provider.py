"""Provider-agnostic payment interface (spec section 8.1). Every rail
(Chapa, SantimPay, ArifPay, manual) implements this Protocol; nothing in
services/payments/deposits.py ever imports a specific provider module by
name, so adding a rail is a new adapter file, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

PaymentStatus = Literal["pending", "processing", "succeeded", "failed", "cancelled"]


class InvalidSignature(Exception):
    """A webhook's signature header was missing or did not match."""


@dataclass(frozen=True)
class CheckoutResult:
    checkout_url: str
    provider_ref: str
    raw_response: dict[str, object]


@dataclass(frozen=True)
class VerifiedEvent:
    """A webhook payload whose signature has already been checked."""

    event_id: str
    our_ref: str
    status: PaymentStatus
    amount: Decimal
    provider_ref: str
    raw: dict[str, object]


@dataclass(frozen=True)
class StatusResult:
    status: PaymentStatus
    amount: Decimal | None
    provider_ref: str | None
    raw: dict[str, object]


@dataclass(frozen=True)
class PayoutResult:
    provider_ref: str
    status: PaymentStatus
    raw_response: dict[str, object]


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(
        self, *, amount: Decimal, user_ref: str, our_ref: str, return_url: str
    ) -> CheckoutResult: ...

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> VerifiedEvent:
        """Raises InvalidSignature if the webhook cannot be trusted."""
        ...

    async def fetch_status(self, our_ref: str) -> StatusResult: ...

    async def create_payout(
        self, *, method: dict[str, str], amount: Decimal, our_ref: str
    ) -> PayoutResult: ...
