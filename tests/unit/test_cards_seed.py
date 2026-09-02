from packages.core import bingo
from packages.core.cards_seed import seed_rows


def test_seed_rows_matches_card_pool_1_to_150():
    pool = bingo.generate_card_pool()
    rows = seed_rows()

    assert len(rows) == 150
    assert [card_no for card_no, _ in rows] == list(range(1, 151))
    assert [grid for _, grid in rows] == pool
