"""Pure tests for services/payments/deposits.reconcile() -- the hourly job's
actual comparison logic (spec: "pull the provider's settlement report,
match on our_ref, and flag any payment where provider and ledger
disagree"), kept fetch-free so it's testable without a live provider, the
same reasoning as packages.core.ledger.reconcile().
"""

from decimal import Decimal

from services.payments.deposits import SettlementRecord, reconcile


def _payment(our_ref: str, amount: str, status: str) -> dict[str, object]:
    return {"our_ref": our_ref, "amount": Decimal(amount), "status": status}


def test_matching_succeeded_payments_produce_no_mismatch():
    ours = [_payment("DEP-1", "100.00", "succeeded")]
    theirs = [SettlementRecord("DEP-1", Decimal("100.00"), "succeeded")]
    assert reconcile(ours, theirs) == []


def test_pending_payment_absent_from_provider_report_is_not_a_mismatch():
    # Nothing to reconcile yet -- the provider hasn't settled it, and we
    # haven't credited it. Not an anomaly, just "still pending".
    ours = [_payment("DEP-1", "100.00", "pending")]
    assert reconcile(ours, []) == []


def test_we_credited_but_provider_report_never_mentions_it():
    ours = [_payment("DEP-1", "100.00", "succeeded")]
    mismatches = reconcile(ours, [])
    assert len(mismatches) == 1
    assert mismatches[0].reason == "missing_from_provider_report"
    assert mismatches[0].our_ref == "DEP-1"


def test_provider_succeeded_but_we_never_credited_it():
    ours = [_payment("DEP-1", "100.00", "pending")]
    theirs = [SettlementRecord("DEP-1", Decimal("100.00"), "succeeded")]
    mismatches = reconcile(ours, theirs)
    assert len(mismatches) == 1
    assert mismatches[0].reason == "status_disagreement"


def test_provider_has_a_settlement_we_have_no_record_of_at_all():
    mismatches = reconcile([], [SettlementRecord("DEP-999", Decimal("50.00"), "succeeded")])
    assert len(mismatches) == 1
    assert mismatches[0].reason == "missing_from_our_records"
    assert mismatches[0].our_ref == "DEP-999"


def test_amount_disagreement_on_an_otherwise_matching_succeeded_payment():
    ours = [_payment("DEP-1", "100.00", "succeeded")]
    theirs = [SettlementRecord("DEP-1", Decimal("120.00"), "succeeded")]
    mismatches = reconcile(ours, theirs)
    assert len(mismatches) == 1
    assert mismatches[0].reason == "amount_disagreement"


def test_provider_reports_failed_and_we_agree_no_mismatch():
    ours = [_payment("DEP-1", "100.00", "failed")]
    theirs = [SettlementRecord("DEP-1", Decimal("100.00"), "failed")]
    assert reconcile(ours, theirs) == []


def test_multiple_payments_only_the_disagreeing_one_is_flagged():
    ours = [
        _payment("DEP-1", "100.00", "succeeded"),
        _payment("DEP-2", "50.00", "succeeded"),
    ]
    theirs = [
        SettlementRecord("DEP-1", Decimal("100.00"), "succeeded"),
        SettlementRecord("DEP-2", Decimal("999.00"), "succeeded"),
    ]
    mismatches = reconcile(ours, theirs)
    assert len(mismatches) == 1
    assert mismatches[0].our_ref == "DEP-2"
