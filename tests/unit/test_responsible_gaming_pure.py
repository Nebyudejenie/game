"""Pure, no-DB tests for packages/core/responsible_gaming.py's parsing and
effective-cap logic -- the instant-decrease/24h-delayed-increase rule and
the /limits command parser, both testable without touching Postgres.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.core.responsible_gaming import (
    Limits,
    LimitsAction,
    effective_deposit_cap,
    effective_loss_cap,
    parse_limits_command,
)


def _limits(**overrides: object) -> Limits:
    base = dict(
        user_id=1,
        daily_deposit_cap=None,
        pending_daily_deposit_cap=None,
        pending_daily_deposit_cap_effective_at=None,
        daily_loss_cap=None,
        pending_daily_loss_cap=None,
        pending_daily_loss_cap_effective_at=None,
        cooloff_until=None,
        self_excluded_until=None,
    )
    base.update(overrides)
    return Limits(**base)


def test_no_cap_set_returns_none():
    assert effective_deposit_cap(_limits()) is None
    assert effective_loss_cap(_limits()) is None


def test_current_cap_with_no_pending_change():
    limits = _limits(daily_deposit_cap=Decimal("1000.00"))
    assert effective_deposit_cap(limits) == Decimal("1000.00")


def test_pending_increase_not_yet_effective_still_shows_old_cap():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    limits = _limits(
        daily_deposit_cap=Decimal("1000.00"),
        pending_daily_deposit_cap=Decimal("5000.00"),
        pending_daily_deposit_cap_effective_at=now + timedelta(hours=24),
    )
    assert effective_deposit_cap(limits, now=now) == Decimal("1000.00")
    assert effective_deposit_cap(limits, now=now + timedelta(hours=23, minutes=59)) == Decimal("1000.00")


def test_pending_increase_effective_after_the_delay():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    limits = _limits(
        daily_deposit_cap=Decimal("1000.00"),
        pending_daily_deposit_cap=Decimal("5000.00"),
        pending_daily_deposit_cap_effective_at=now + timedelta(hours=24),
    )
    assert effective_deposit_cap(limits, now=now + timedelta(hours=24)) == Decimal("5000.00")
    assert effective_deposit_cap(limits, now=now + timedelta(days=5)) == Decimal("5000.00")


def test_loss_cap_same_delayed_increase_rule():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    limits = _limits(
        daily_loss_cap=Decimal("200.00"),
        pending_daily_loss_cap=Decimal("800.00"),
        pending_daily_loss_cap_effective_at=now + timedelta(hours=24),
    )
    assert effective_loss_cap(limits, now=now) == Decimal("200.00")
    assert effective_loss_cap(limits, now=now + timedelta(hours=25)) == Decimal("800.00")


def test_parse_set_deposit_limit():
    parsed = parse_limits_command("deposit 500")
    assert parsed.action is LimitsAction.SET_DEPOSIT
    assert parsed.value == "500"


def test_parse_set_loss_limit():
    parsed = parse_limits_command("loss 300")
    assert parsed.action is LimitsAction.SET_LOSS
    assert parsed.value == "300"


def test_parse_cooloff():
    parsed = parse_limits_command("cooloff 24h")
    assert parsed.action is LimitsAction.COOL_OFF
    assert parsed.value == "24h"


def test_parse_self_exclude():
    parsed = parse_limits_command("selfexclude confirm")
    assert parsed.action is LimitsAction.SELF_EXCLUDE
    assert parsed.value == "confirm"


def test_parse_is_case_insensitive_on_subcommand():
    parsed = parse_limits_command("DEPOSIT 500")
    assert parsed.action is LimitsAction.SET_DEPOSIT


def test_parse_unknown_subcommand_returns_none_action():
    parsed = parse_limits_command("banana 500")
    assert parsed.action is None
    assert parsed.value is None


def test_parse_empty_or_malformed_returns_none_action():
    assert parse_limits_command("").action is None
    assert parse_limits_command("deposit").action is None
    assert parse_limits_command("deposit 500 extra").action is None
