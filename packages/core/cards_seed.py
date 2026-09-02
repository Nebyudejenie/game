"""Bridges the pure card pool in bingo.py to the `cards` table (spec §4.3).

Kept separate from bingo.py so that module stays free of I/O concerns, and
separate from the migration file so the seed-row logic is independently
unit-testable.
"""

from packages.core.bingo import Grid, generate_card_pool


def seed_rows() -> list[tuple[int, Grid]]:
    """Returns (card_no, grid) pairs for card_no 1..150, card_no = index + 1."""
    return [(i + 1, grid) for i, grid in enumerate(generate_card_pool())]
