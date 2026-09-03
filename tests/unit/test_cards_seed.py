from packages.core import bingo
from packages.core.cards_seed import seed_rows


def test_seed_rows_matches_card_pool_1_to_432():
    pool = bingo.generate_card_pool()
    rows = seed_rows()

    assert len(rows) == 432
    assert [card_no for card_no, _ in rows] == list(range(1, 433))
    assert [grid for _, grid in rows] == pool
