"""Pure payout math for round settlement. No I/O.

Kept separate from the ledger-writing side (round_engine.py) so the actual
money arithmetic -- the part where a mistake costs someone their derash --
is trivially unit-testable and independently auditable from this file alone.
"""

from decimal import ROUND_DOWN, Decimal

CENT = Decimal("0.01")


def compute_derash(pot: Decimal, house_cut_bps: int) -> tuple[Decimal, Decimal]:
    """(derash, house_cut) for a pot with no split needed yet.

    derash = pot * (10000 - house_cut_bps) / 10000, rounded DOWN to 2dp;
    the rounding remainder goes to the house so pot == derash + house_cut
    exactly, always.
    """
    if pot < 0:
        raise ValueError("pot must be non-negative")
    if not (0 <= house_cut_bps <= 10000):
        raise ValueError("house_cut_bps must be between 0 and 10000")

    derash = (pot * (10000 - house_cut_bps) / Decimal(10000)).quantize(
        CENT, rounding=ROUND_DOWN
    )
    house_cut = pot - derash
    return derash, house_cut


def split_derash(derash: Decimal, num_winners: int) -> tuple[list[Decimal], Decimal]:
    """Splits derash evenly among co-winners.

    Each share is rounded DOWN to 2dp; whatever fraction of a cent that
    rounding leaves on the table goes to the house so
    sum(shares) + leftover == derash exactly, always.
    Returns (shares, leftover_to_house).
    """
    if num_winners <= 0:
        raise ValueError("num_winners must be positive")

    share = (derash / Decimal(num_winners)).quantize(CENT, rounding=ROUND_DOWN)
    shares = [share] * num_winners
    leftover = derash - (share * num_winners)
    return shares, leftover
