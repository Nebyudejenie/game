"""Unit tests for services/engine/settlement.py -- pure payout math."""

from decimal import Decimal

import pytest

from services.engine import settlement


def test_compute_derash_matches_reference_economics():
    # 35 players x 20 ETB = 700 pot, 20% house cut -> 560 derash, 140 house.
    derash, house = settlement.compute_derash(Decimal("700.00"), 2000)
    assert derash == Decimal("560.00")
    assert house == Decimal("140.00")
    assert derash + house == Decimal("700.00")


def test_compute_derash_rounds_down_remainder_to_house():
    derash, house = settlement.compute_derash(Decimal("100.01"), 2000)
    assert derash + house == Decimal("100.01")
    assert derash == derash.quantize(Decimal("0.01"))


@pytest.mark.parametrize("pot_cents", list(range(1, 300)) + [70000, 123456])
def test_compute_derash_always_sums_to_pot(pot_cents):
    pot = Decimal(pot_cents) / 100
    derash, house = settlement.compute_derash(pot, 2000)
    assert derash + house == pot
    assert derash >= 0
    assert house >= 0


def test_compute_derash_rejects_invalid_input():
    with pytest.raises(ValueError):
        settlement.compute_derash(Decimal("-1"), 2000)
    with pytest.raises(ValueError):
        settlement.compute_derash(Decimal("10"), 10001)
    with pytest.raises(ValueError):
        settlement.compute_derash(Decimal("10"), -1)


def test_compute_derash_zero_cut_gives_everything_to_derash():
    derash, house = settlement.compute_derash(Decimal("50.00"), 0)
    assert derash == Decimal("50.00")
    assert house == Decimal("0.00")


def test_compute_derash_full_cut_gives_everything_to_house():
    derash, house = settlement.compute_derash(Decimal("50.00"), 10000)
    assert derash == Decimal("0.00")
    assert house == Decimal("50.00")


def test_split_derash_even_split_no_leftover():
    shares, leftover = settlement.split_derash(Decimal("32.00"), 2)
    assert shares == [Decimal("16.00"), Decimal("16.00")]
    assert leftover == Decimal("0.00")


def test_split_derash_uneven_leftover_goes_to_house():
    shares, leftover = settlement.split_derash(Decimal("10.01"), 3)
    assert shares == [Decimal("3.33")] * 3
    assert sum(shares, Decimal("0")) + leftover == Decimal("10.01")


@pytest.mark.parametrize("num_winners", range(1, 25))
def test_split_derash_always_sums_to_derash(num_winners):
    derash = Decimal("560.37")
    shares, leftover = settlement.split_derash(derash, num_winners)
    assert len(shares) == num_winners
    assert sum(shares, Decimal("0")) + leftover == derash
    assert leftover >= 0


def test_split_derash_single_winner_gets_it_all():
    shares, leftover = settlement.split_derash(Decimal("560.00"), 1)
    assert shares == [Decimal("560.00")]
    assert leftover == Decimal("0.00")


def test_split_derash_rejects_zero_or_negative_winners():
    with pytest.raises(ValueError):
        settlement.split_derash(Decimal("10.00"), 0)
    with pytest.raises(ValueError):
        settlement.split_derash(Decimal("10.00"), -1)
