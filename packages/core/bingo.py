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

# Fixed forever: the 100-card pool must never change between deployments, or
# every player's "lucky card" silently becomes a different grid. Do not
# change this constant.
_POOL_SEED_LABEL = b"jobingo-card-pool-v1"
_POOL_SIZE = 100


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
    """The deterministic 100-card pool, card_no 1..100 -> pool[card_no - 1].

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
    kind: str  # "row" | "col" | "diag" | "corners"
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
    patterns.append(
        Pattern("corners", "corners", ((0, 0), (0, 4), (4, 0), (4, 4)))
    )
    return patterns


_ALL_PATTERNS = _all_patterns()


def winning_patterns(
    grid: Grid, called: set[int], enabled: list[str]
) -> list[Pattern]:
    marks = mark_grid(grid, called)
    enabled_set = set(enabled)
    won: list[Pattern] = []
    for pattern in _ALL_PATTERNS:
        if pattern.kind not in enabled_set:
            continue
        if all(marks[r][c] for r, c in pattern.cells):
            won.append(pattern)
    return won


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
