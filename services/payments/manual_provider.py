"""The 'manual' rail's PaymentProvider adapter (spec-adjacent P1 directive:
Jo Bingo must keep taking deposits and paying out withdrawals even when no
automatic provider is available). See services/payments/manual.py for
manual deposit creation, and services/admin/queries.py's manual_* functions
for the human-driven approve/settle/reject flow.

Only request_withdrawal() actually needs this class -- its signature
requires a real PaymentProvider so it can store provider.name into
payments.provider without a special case. Manual deposit creation
(manual.py) never touches this Protocol at all: there's no checkout step,
so it just writes the literal string "manual" directly.

Every method here is genuinely unreachable for the manual rail: a manual
withdrawal never auto-approves (see request_withdrawal's force_review
parameter), so payout_worker.py's automatic dispatch -- the only caller of
create_payout() -- never sees one. There is no checkout, no webhook, and
nothing to poll. Structurally identical to tests/integration/
test_admin_withdrawals.py's own _NullProvider test stub, promoted to
production code because "manual" is now a real, live rail rather than
just something a test needs to satisfy a type signature for.
"""

from __future__ import annotations

from decimal import Decimal

from services.payments.provider import CheckoutResult, PayoutResult, StatusResult, VerifiedEvent


class ManualProvider:
    name = "manual"

    async def create_checkout(
        self, *, amount: Decimal, user_ref: str, our_ref: str, return_url: str, callback_url: str
    ) -> CheckoutResult:
        raise NotImplementedError("manual deposits have no checkout step")

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> VerifiedEvent:
        raise NotImplementedError("manual payments have no webhook")

    async def fetch_status(self, our_ref: str) -> StatusResult:
        raise NotImplementedError("manual payments are never polled for status")

    async def create_payout(
        self, *, method: dict[str, str], amount: Decimal, our_ref: str
    ) -> PayoutResult:
        raise NotImplementedError("manual withdrawals never auto-dispatch to a provider")
