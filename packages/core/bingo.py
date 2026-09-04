"""Pure bingo game logic. No I/O, no database, no unseeded randomness.

Everything here is a pure function of its inputs so it can be tested without
any infrastructure and reasoned about by an auditor from this file alone.
"""

from __future__ import annotations

import hashlib
import hmac
import random
from dataclasses import dataclass

Grid = list[list[int]]

FREE_ROW = 2
FREE_COL = 2

_COLUMN_RANGES: dict[str, range] = {
    "B": range(1, 16),
    "I": range(16, 31),
    "N": range(31, 46),
    "G": range(46, 61),
    "O": range(61, 76),
}
_COLUMNS = ("B", "I", "N", "G", "O")

# Fixed forever: every card_no 1.._POOL_SIZE that has ever been dealt must
# keep mapping to the exact same grid, or a player's "lucky card" silently
# changes. generate_card_pool() below draws sequentially from one seeded
# stream, so RAISING this constant is the one safe change -- every existing
# card_no's grid is unaffected, only new ones get appended. Never lower it,
# never change _POOL_SEED_LABEL, never change the generation algorithm.
#
# 432, not the 150 this project shipped a few hours earlier in the same
# session: that number came from a video frame showing the card-selection
# grid's *unscrolled* first screen, which happens to end at exactly row
# 150 and looks complete (empty space below, no visible scrollbar) --
# genuinely indistinguishable from the real end without scrolling further.
# A second, longer recording of the same reference app scrolled the
# identical-looking grid well past that point; 432 is the confirmed true
# end, verified directly (not by inference) across four independent
# frames all showing the same final "...431, 432" row followed by clean
# empty space, and cross-checked against three real winning "Card #"
# numbers from the same video (194, 403, 126) that only make sense
# against a pool this large.
_POOL_SEED_LABEL = b"jobingo-card-pool-v1"
_POOL_SIZE = 432


def letter_for(n: int) -> str:
    for letter, rng in _COLUMN_RANGES.items():
        if n in rng:
            return letter
    raise ValueError(f"{n} is not a valid bingo number (must be 1-75)")


def label(n: int) -> str:
    return f"{letter_for(n)}{n}"


def _generate_one_card(rng: random.Random) -> Grid:
    columns: list[list[int]] = []
    for letter in _COLUMNS:
        pool = list(_COLUMN_RANGES[letter])
        chosen = rng.sample(pool, 5)
        chosen.sort()
        columns.append(chosen)

    grid = [[columns[c][r] for c in range(5)] for r in range(5)]
    grid[FREE_ROW][FREE_COL] = 0
    return grid


def generate_card_pool() -> list[Grid]:
    """The deterministic card pool, card_no 1.._POOL_SIZE -> pool[card_no - 1].

    Same output on every machine, forever, because the seed is a fixed
    constant. Cards are regenerated (not rejected-and-kept) on a rare hash
    collision so the whole pool stays a function of one seeded stream.
    """
    seed = int.from_bytes(hashlib.sha256(_POOL_SEED_LABEL).digest()[:8], "big")
    rng = random.Random(seed)

    pool: list[Grid] = []
    seen: set[tuple[int, ...]] = set()
    while len(pool) < _POOL_SIZE:
        grid = _generate_one_card(rng)
        key = tuple(v for row in grid for v in row)
        if key in seen:
            continue
        seen.add(key)
        pool.append(grid)
    return pool


def mark_grid(grid: Grid, called: set[int]) -> list[list[bool]]:
    marks: list[list[bool]] = []
    for r in range(5):
        row: list[bool] = []
        for c in range(5):
            if r == FREE_ROW and c == FREE_COL:
                row.append(True)
            else:
                row.append(grid[r][c] in called)
        marks.append(row)
    return marks


@dataclass(frozen=True)
class Pattern:
    name: str
    kind: str  # "row" | "col" | "diag"
    cells: tuple[tuple[int, int], ...]


def _all_patterns() -> list[Pattern]:
    patterns: list[Pattern] = []
    for r in range(5):
        patterns.append(Pattern(f"row_{r}", "row", tuple((r, c) for c in range(5))))
    for c in range(5):
        patterns.append(Pattern(f"col_{c}", "col", tuple((r, c) for r in range(5))))
    patterns.append(Pattern("diag_main", "diag", tuple((i, i) for i in range(5))))
    patterns.append(
        Pattern("diag_anti", "diag", tuple((i, 4 - i) for i in range(5)))
    )
    return patterns


_ALL_PATTERNS = _all_patterns()

# Product rule: a single completed line is never enough -- a card only wins
# once it holds this many simultaneously-complete lines (any mix of rows,
# columns, and diagonals). The one and only threshold check lives in
# has_won() below; every caller (round_engine.py's manual claim() and its
# auto-mark scan) must go through it rather than re-testing len(...) itself,
# so the real-money win rule can never drift out of sync between the two.
MIN_WINNING_LINES = 2


def winning_patterns(
    grid: Grid, called: set[int], enabled: list[str]
) -> list[Pattern]:
    """Every enabled pattern this grid has fully completed -- not itself a
    win/no-win verdict (see has_won()), just the raw set of complete lines,
    which callers also need in full to record/display exactly which lines
    a winning claim completed."""
    marks = mark_grid(grid, called)
    enabled_set = set(enabled)
    won: list[Pattern] = []
    for pattern in _ALL_PATTERNS:
        if pattern.kind not in enabled_set:
            continue
        if all(marks[r][c] for r, c in pattern.cells):
            won.append(pattern)
    return won


def has_won(grid: Grid, called: set[int], enabled: list[str]) -> bool:
    """The real win verdict: at least MIN_WINNING_LINES complete lines, in
    any combination (e.g. two rows, or one row and one diagonal)."""
    return len(winning_patterns(grid, called, enabled)) >= MIN_WINNING_LINES


def _hmac_stream(server_seed: bytes, client_seed: str) -> "_ByteStream":
    return _ByteStream(server_seed, client_seed.encode("utf-8"))


class _ByteStream:
    """Deterministic infinite byte stream: HMAC-SHA256(server_seed, client_seed || counter),
    counter-mode expansion so Fisher-Yates always has enough entropy for
    rejection sampling without bias, however many numbers remain to shuffle.
    """

    def __init__(self, server_seed: bytes, client_seed: bytes) -> None:
        self._server_seed = server_seed
        self._client_seed = client_seed
        self._counter = 0
        self._buffer = b""

    def take(self, n: int) -> bytes:
        while len(self._buffer) < n:
            block = hmac.new(
                self._server_seed,
                self._client_seed + self._counter.to_bytes(4, "big"),
                hashlib.sha256,
            ).digest()
            self._buffer += block
            self._counter += 1
        chunk, self._buffer = self._buffer[:n], self._buffer[n:]
        return chunk

    def below(self, upper_exclusive: int) -> int:
        """Unbiased random int in [0, upper_exclusive) via rejection sampling."""
        if upper_exclusive <= 0:
            raise ValueError("upper_exclusive must be positive")
        num_bytes = max(1, (upper_exclusive - 1).bit_length() // 8 + 1)
        limit = 256**num_bytes
        threshold = limit - (limit % upper_exclusive)
        while True:
            value = int.from_bytes(self.take(num_bytes), "big")
            if value < threshold:
                return value % upper_exclusive


def derive_draw(server_seed: bytes, client_seed: str) -> list[int]:
    """The fixed 75-number draw order for a round, provably fair.

    Fisher-Yates shuffle of [1..75] driven entirely by a deterministic byte
    stream seeded from (server_seed, client_seed). Same inputs -> same
    output, forever; flipping a single bit in either seed changes the whole
    sequence.
    """
    numbers = list(range(1, 76))
    stream = _hmac_stream(server_seed, client_seed)
    for i in range(len(numbers) - 1, 0, -1):
        j = stream.below(i + 1)
        numbers[i], numbers[j] = numbers[j], numbers[i]
    return numbers


def verify_draw(server_seed: bytes, client_seed: str, claimed_draw: list[int]) -> bool:
    return derive_draw(server_seed, client_seed) == claimed_draw
