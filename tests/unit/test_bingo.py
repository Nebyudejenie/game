"""Unit tests for packages/core/bingo.py -- pure functions, no I/O, no DB.
"""

import hashlib
import json

import pytest

from packages.core import bingo

# Independent oracle for pattern cells, defined here rather than imported
# from bingo.py, so a bug that corrupts bingo.py's internal pattern table
# can't also corrupt the thing checking it.
FREE = (2, 2)
ALL_PATTERNS = [
    ("row_0", "row", [(0, c) for c in range(5)]),
    ("row_1", "row", [(1, c) for c in range(5)]),
    ("row_2", "row", [(2, c) for c in range(5)]),
    ("row_3", "row", [(3, c) for c in range(5)]),
    ("row_4", "row", [(4, c) for c in range(5)]),
    ("col_0", "col", [(r, 0) for r in range(5)]),
    ("col_1", "col", [(r, 1) for r in range(5)]),
    ("col_2", "col", [(r, 2) for r in range(5)]),
    ("col_3", "col", [(r, 3) for r in range(5)]),
    ("col_4", "col", [(r, 4) for r in range(5)]),
    ("diag_main", "diag", [(i, i) for i in range(5)]),
    ("diag_anti", "diag", [(i, 4 - i) for i in range(5)]),
]
ALL_KINDS = ["row", "col", "diag"]

COLUMN_RANGES = [
    ("B", range(1, 16)),
    ("I", range(16, 31)),
    ("N", range(31, 46)),
    ("G", range(46, 61)),
    ("O", range(61, 76)),
]


def make_grid() -> bingo.Grid:
    grid = [
        [1, 16, 31, 46, 61],
        [2, 17, 32, 47, 62],
        [3, 18, 0, 48, 63],
        [4, 19, 34, 49, 64],
        [5, 20, 35, 50, 65],
    ]
    assert grid[2][2] == 0
    return grid


@pytest.mark.parametrize("name,kind,cells", ALL_PATTERNS)
def test_pattern_detected_in_isolation(name, kind, cells):
    grid = make_grid()
    called = {grid[r][c] for (r, c) in cells if (r, c) != FREE}

    won = bingo.winning_patterns(grid, called, ALL_KINDS)
    won_names = {p.name for p in won}

    assert name in won_names
    # Marking exactly this pattern's cells must not spuriously complete any
    # other pattern.
    assert won_names == {name}


@pytest.mark.parametrize("name,kind,cells", ALL_PATTERNS)
def test_one_number_short_reports_no_win(name, kind, cells):
    grid = make_grid()
    non_free_cells = [c for c in cells if c != FREE]
    called = {grid[r][c] for (r, c) in non_free_cells[:-1]}  # withhold one

    won = bingo.winning_patterns(grid, called, ALL_KINDS)
    assert name not in {p.name for p in won}


def test_free_space_completes_middle_row_column_and_both_diagonals():
    grid = make_grid()
    crossing = ["row_2", "col_2", "diag_main", "diag_anti"]
    for name, kind, cells in ALL_PATTERNS:
        if name not in crossing:
            continue
        called = {grid[r][c] for (r, c) in cells if (r, c) != FREE}
        won = bingo.winning_patterns(grid, called, ALL_KINDS)
        assert name in {p.name for p in won}, f"{name} should win via free space"


def test_corners_is_not_a_recognized_pattern():
    """"corners" was removed as a win-pattern concept entirely (not folded
    into the two-line count -- a room with only corners enabled would
    otherwise be mathematically unwinnable, since there is exactly one
    corners pattern). Even with all four corners called and "corners"
    explicitly passed as an enabled kind, nothing should be reported won
    under that name -- it simply no longer exists in _ALL_PATTERNS.
    """
    grid = make_grid()
    corners_cells = [(0, 0), (0, 4), (4, 0), (4, 4)]
    called = {grid[r][c] for r, c in corners_cells}
    won = bingo.winning_patterns(grid, called, ALL_KINDS + ["corners"])
    assert "corners" not in {p.name for p in won}


def test_winning_patterns_respects_enabled_filter():
    grid = make_grid()
    called = {grid[r][c] for r, c in [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]}
    assert bingo.winning_patterns(grid, called, ["row"])
    assert bingo.winning_patterns(grid, called, ["col", "diag"]) == []


# --- has_won(): the real win verdict (>= MIN_WINNING_LINES lines) ---


def test_has_won_false_with_only_one_complete_line():
    grid = make_grid()
    called = {grid[r][c] for r, c in [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]}
    assert bingo.winning_patterns(grid, called, ALL_KINDS)  # sanity: row_0 is complete
    assert bingo.has_won(grid, called, ALL_KINDS) is False


def test_has_won_false_when_second_line_is_one_number_short():
    grid = make_grid()
    row_0 = {grid[0][c] for c in range(5)}
    col_0_incomplete = {grid[r][0] for r in range(4)}  # withhold row 4's own number
    called = row_0 | col_0_incomplete
    assert len(bingo.winning_patterns(grid, called, ALL_KINDS)) == 1
    assert bingo.has_won(grid, called, ALL_KINDS) is False


def test_has_won_true_for_two_rows():
    grid = make_grid()
    called = {grid[r][c] for r in (0, 1) for c in range(5)}
    assert bingo.has_won(grid, called, ALL_KINDS) is True


def test_has_won_true_for_row_plus_column():
    grid = make_grid()
    called = {grid[0][c] for c in range(5)} | {grid[r][1] for r in range(5)}
    assert bingo.has_won(grid, called, ALL_KINDS) is True


def test_has_won_true_for_row_plus_diagonal():
    grid = make_grid()
    called = {grid[0][c] for c in range(5)} | {grid[i][i] for i in range(5)}
    assert bingo.has_won(grid, called, ALL_KINDS) is True


def test_has_won_true_for_two_diagonals():
    grid = make_grid()
    called = {grid[i][i] for i in range(5)} | {grid[i][4 - i] for i in range(5)}
    assert bingo.has_won(grid, called, ALL_KINDS) is True


def test_has_won_respects_enabled_filter_even_with_two_lines_physically_marked():
    # Two rows are physically complete on the card, but the room only
    # allows "col" -- has_won() must follow the same enabled-kind
    # filtering winning_patterns() already does, not just count blindly.
    grid = make_grid()
    called = {grid[r][c] for r in (0, 1) for c in range(5)}
    assert bingo.has_won(grid, called, ["col"]) is False


def test_letter_for_and_label():
    assert bingo.letter_for(1) == "B"
    assert bingo.letter_for(15) == "B"
    assert bingo.letter_for(16) == "I"
    assert bingo.letter_for(30) == "I"
    assert bingo.letter_for(31) == "N"
    assert bingo.letter_for(45) == "N"
    assert bingo.letter_for(46) == "G"
    assert bingo.letter_for(60) == "G"
    assert bingo.letter_for(61) == "O"
    assert bingo.letter_for(75) == "O"
    assert bingo.label(40) == "N40"
    with pytest.raises(ValueError):
        bingo.letter_for(0)
    with pytest.raises(ValueError):
        bingo.letter_for(76)


# --- card pool ---


def test_card_pool_has_432_distinct_cards_with_correct_column_ranges():
    pool = bingo.generate_card_pool()
    assert len(pool) == 432

    seen = set()
    for grid in pool:
        assert grid[2][2] == 0
        for col_index, (letter, rng) in enumerate(COLUMN_RANGES):
            values = [grid[r][col_index] for r in range(5)]
            non_free = [v for v in values if v != 0]
            assert all(v in rng for v in non_free)
            assert non_free == sorted(non_free)
            assert len(set(values)) == len(values)  # no repeats within column
        seen.add(tuple(v for row in grid for v in row))
    assert len(seen) == 432  # all cards distinct


def test_card_pool_is_deterministic_across_calls():
    assert bingo.generate_card_pool() == bingo.generate_card_pool()


def test_card_pool_golden_hash_is_pinned():
    """If this ever fails after a refactor, someone's lucky card silently
    changed. That is not allowed -- update this only with a deliberate,
    reviewed decision to reset the pool, never as a side effect of cleanup.
    """
    pool = bingo.generate_card_pool()
    digest = hashlib.sha256(json.dumps(pool).encode("utf-8")).hexdigest()
    assert digest == "790561fb265cca080e371f38c8433e004c100b44ad7982f84cf653162a251d12"


def test_card_pool_first_100_cards_are_byte_identical_to_the_original_pool():
    """The actual machine-verified proof that raising _POOL_SIZE from 100 to
    150 (2026-09-02, spec: a 150-card grid, confirmed in the reference video)
    was a pure append -- generate_card_pool() draws sequentially from one
    seeded stream, so every card_no that was ever dealt before this change
    must still map to the exact same grid. This is the original 100-card
    pool's own golden hash (pinned before the pool size changed), asserted
    here against just the first 100 entries of the now-432 pool.
    """
    pool = bingo.generate_card_pool()
    digest = hashlib.sha256(json.dumps(pool[:100]).encode("utf-8")).hexdigest()
    assert digest == "ba46c87210f7c3fd7d21748293aa6a26953bc7689586d75ee00e9d866da0bcca"


def test_card_pool_first_150_cards_are_byte_identical_to_the_previous_pool():
    """The actual machine-verified proof that raising _POOL_SIZE from 150 to
    432 (2026-09-03) was a pure append. The first 150 was itself shipped and
    briefly live in production on the strength of an unscrolled video frame
    that happened to end exactly at row 150 and looked complete -- a second,
    longer recording of the same reference app proved the real grid keeps
    going, scrolled to a confirmed, clean end at card 432 across four
    independent frames. This is the 150-card pool's own golden hash (the one
    pinned above before this change), asserted here against just the first
    150 entries of the now-432 pool.
    """
    pool = bingo.generate_card_pool()
    digest = hashlib.sha256(json.dumps(pool[:150]).encode("utf-8")).hexdigest()
    assert digest == "d82ac03fd31694170a303d5f3926e258ddc21775bd3e256547b18df2e13dad03"


# --- provably fair draw ---


def test_derive_draw_is_deterministic():
    a = bingo.derive_draw(b"seed-bytes-1234567890123456789012", "round-88")
    b = bingo.derive_draw(b"seed-bytes-1234567890123456789012", "round-88")
    assert a == b


def test_derive_draw_is_a_permutation_of_1_to_75():
    draw = bingo.derive_draw(b"another-seed-value-0123456789012", "round-1")
    assert len(draw) == 75
    assert set(draw) == set(range(1, 76))


def test_derive_draw_changes_completely_on_one_bit_seed_flip():
    seed = bytearray(b"flip-bit-seed-value-01234567890123")
    base = bingo.derive_draw(bytes(seed), "round-42")

    flipped = bytearray(seed)
    flipped[0] ^= 0b00000001
    changed = bingo.derive_draw(bytes(flipped), "round-42")

    assert base != changed
    differing_positions = sum(1 for x, y in zip(base, changed) if x != y)
    assert differing_positions > len(base) * 0.5


def test_derive_draw_changes_completely_on_client_seed_change():
    server_seed = b"stable-server-seed-value-0123456789"
    a = bingo.derive_draw(server_seed, "round-1")
    b = bingo.derive_draw(server_seed, "round-2")
    assert a != b
    differing_positions = sum(1 for x, y in zip(a, b) if x != y)
    assert differing_positions > len(a) * 0.5


def test_verify_draw_accepts_matching_and_rejects_tampered():
    server_seed = b"verify-me-seed-value-01234567890123"
    client_seed = "round-99"
    draw = bingo.derive_draw(server_seed, client_seed)

    assert bingo.verify_draw(server_seed, client_seed, draw) is True

    tampered = list(draw)
    tampered[0], tampered[1] = tampered[1], tampered[0]
    assert bingo.verify_draw(server_seed, client_seed, tampered) is False
