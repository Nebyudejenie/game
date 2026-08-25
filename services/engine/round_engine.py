"""The room state machine. One RoundEngine instance owns exactly one room,
runs one round at a time inside it, and is the sole writer of that room's
state -- see room_lock.py for how that single-writer property is enforced
across worker processes.

State machine (spec section 3.3):

    IDLE --first join--> LOBBY --countdown hits 0, enough players--> RUNNING
                            |                                          |
                            +--not enough players (refund, void)       |
                                                                        |
    RUNNING --valid claim (+ 50ms tie window)--> settle --> IDLE       |
    RUNNING --75 calls, no winner--> refund, void --> IDLE  <----------+

Nothing here trusts the client for anything that matters: the marked grid is
always recomputed server-side from the round's own called-numbers set, never
from anything a caller supplies.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import bingo, ledger, metrics, responsible_gaming
from packages.core.bingo import Grid
from packages.core.ledger import Entry, InsufficientFunds
from services.engine import commands, refunds, settlement
from services.engine.room_lock import RoomLock

logger = structlog.get_logger()

WINNER_TIE_WINDOW_SECONDS = 0.05


@dataclass(frozen=True)
class RoomConfig:
    id: int
    code: str
    stake: Decimal
    house_cut_bps: int
    min_players: int
    max_players: int
    lobby_seconds: int
    call_interval_ms: int
    result_seconds: int
    win_patterns: list[str]


@dataclass(frozen=True)
class RoundEntryState:
    card_no: int
    auto_mark: bool


@dataclass(frozen=True)
class PendingWinner:
    user_id: int
    card_no: int
    pattern: str
    call_index: int


@dataclass(frozen=True)
class JoinResult:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    reason: str | None = None


async def load_room_config(pool: asyncpg.Pool, room_id: int) -> RoomConfig:
    row = await pool.fetchrow("SELECT * FROM rooms WHERE id = $1", room_id)
    if row is None:
        raise ValueError(f"no such room: {room_id}")
    win_patterns = json.loads(row["win_patterns"]) if isinstance(
        row["win_patterns"], str
    ) else row["win_patterns"]
    return RoomConfig(
        id=row["id"],
        code=row["code"],
        stake=row["stake"],
        house_cut_bps=row["house_cut_bps"],
        min_players=row["min_players"],
        max_players=row["max_players"],
        lobby_seconds=row["lobby_seconds"],
        call_interval_ms=row["call_interval_ms"],
        result_seconds=row["result_seconds"],
        win_patterns=list(win_patterns),
    )


async def load_card_pool(pool: asyncpg.Pool) -> dict[int, Grid]:
    rows = await pool.fetch("SELECT card_no, grid FROM cards")
    out: dict[int, Grid] = {}
    for row in rows:
        grid = row["grid"]
        out[row["card_no"]] = json.loads(grid) if isinstance(grid, str) else grid
    return out


class RoundEngine:
    def __init__(
        self,
        pool: asyncpg.Pool,
        redis: Redis,
        room: RoomConfig,
        card_pool: dict[int, Grid],
        *,
        worker_id: str | None = None,
    ) -> None:
        self._pool = pool
        self._redis = redis
        self._room = room
        self._card_pool = card_pool
        self._lock = RoomLock(redis, room.id, worker_id)

        self._stop_requested = False
        self._round_active_event = asyncio.Event()
        self._winner_lock = asyncio.Lock()
        self._round_start_lock = asyncio.Lock()
        # Serializes join()'s capacity check through its self._entries
        # update -- in real production this is already effectively
        # single-threaded (one engine's _serve_commands() consumes its
        # room's command stream one entry at a time), but a code review
        # pass correctly noted the check itself has no lock of its own,
        # so a caller invoking join() concurrently (this codebase's own
        # load/chaos tests do exactly that) could let a room's
        # max_players cap be raced past: two different users, two
        # different card numbers, both reading the same under-capacity
        # count before either updates self._entries.
        self._join_lock = asyncio.Lock()
        self._settlement_task: asyncio.Task[None] | None = None

        self._round_id: int | None = None
        self._seq: int = 0
        self._status: str = "idle"
        self._server_seed: bytes | None = None
        self._server_seed_hash: str | None = None
        self._client_seed: str | None = None
        self._pot = Decimal("0")
        self._entries: dict[int, RoundEntryState] = {}
        self._called: set[int] = set()
        self._draw_order: list[int] = []
        self._call_index: int = 0
        self._running_started_at: float = 0.0
        self._lobby_deadline_monotonic: float = 0.0
        self._locked_out: set[int] = set()
        self._auto_claimed: set[int] = set()
        self._pending_winners: list[PendingWinner] = []
        self._winner_window_deadline: float | None = None

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, value: str) -> None:
        # engine_rooms_active counts idle-vs-not, so it only moves on the
        # actual idle boundary crossing -- not on every one of the five
        # status assignments below (settling/running/done are all already
        # "active" and shouldn't double-increment).
        if value != "idle" and self._status == "idle":
            metrics.engine_rooms_active.inc()
        elif value == "idle" and self._status != "idle":
            metrics.engine_rooms_active.dec()
        self._status = value

    @property
    def round_id(self) -> int | None:
        return self._round_id

    @property
    def pot(self) -> Decimal:
        return self._pot

    def player_count(self) -> int:
        return len(self._entries)

    def is_lock_held(self) -> bool:
        return self._lock.is_held()

    # --- lifecycle -----------------------------------------------------

    async def run_forever(self) -> bool:
        if not await self._lock.acquire():
            return False
        commands_task = asyncio.create_task(self._serve_commands())
        try:
            while not self._stop_requested and self._lock.is_held():
                await self._round_active_event.wait()
                if self._stop_requested or not self._lock.is_held():
                    break
                await self._run_lobby()
        finally:
            commands_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await commands_task
            await self._lock.release()
        return True

    async def stop(self) -> None:
        self._stop_requested = True
        self._round_active_event.set()

    # --- player actions --------------------------------------------------

    async def join(self, user_id: int, card_no: int, *, auto_mark: bool = True) -> JoinResult:
        if self._status == "idle":
            # Many joins can arrive concurrently against a genuinely idle
            # room (a burst of players hitting an empty room at once) --
            # without this lock, every one of them would see "idle" before
            # the first had a chance to flip it to "lobby", and each would
            # try to INSERT the same next round seq number, raising a
            # UniqueViolationError on rounds_room_id_seq_key instead of
            # just joining the one round that actually got created. The
            # inner re-check is what makes only the first caller actually
            # start a round; everyone else finds it already started once
            # they get the lock.
            async with self._round_start_lock:
                if self._status == "idle":
                    await self._start_new_round()
                    self._round_active_event.set()

        if self._status != "lobby":
            return JoinResult(False, "not_joinable")
        if not (1 <= card_no <= 100):
            return JoinResult(False, "invalid_card")

        # Covers the capacity check through the self._entries update below
        # -- see this lock's own definition in __init__ for why a room's
        # max_players cap needs an explicit lock here, not just the
        # single-consumer command-stream loop production already
        # naturally serializes joins through.
        async with self._join_lock:
            if len(self._entries) >= self._room.max_players:
                return JoinResult(False, "room_full")

            round_id = self._round_id
            assert round_id is not None
            idem = f"stake-{round_id}-{user_id}"

            async with self._pool.acquire() as conn:
                try:
                    async with conn.transaction():
                        # A per-user advisory lock, held for this whole
                        # transaction: self._join_lock above only serializes
                        # joins within *this* room, so without this, the same
                        # user joining two different rooms at once could have
                        # both check_stake_allowed() calls read today's net
                        # loss before either stake commits, letting both pass
                        # a daily_loss_cap that either one alone would have
                        # blocked. A row lock on the user_cash account would
                        # only help once that account_balances row exists (it
                        # doesn't for a brand-new user with no ledger history
                        # yet), so this uses pg_advisory_xact_lock keyed on
                        # user_id instead -- unconditional, and released
                        # automatically on commit or rollback. No other code
                        # in this codebase takes an advisory lock, so there's
                        # no cross-purpose key collision to worry about.
                        await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)

                        block = await responsible_gaming.check_stake_allowed(
                            conn, user_id, self._room.stake
                        )
                        if block.blocked:
                            assert block.reason is not None
                            return JoinResult(False, block.reason)

                        await conn.execute(
                            "INSERT INTO round_entries (round_id, card_no, user_id, auto_mark) "
                            "VALUES ($1, $2, $3, $4)",
                            round_id,
                            card_no,
                            user_id,
                            auto_mark,
                        )
                        cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
                        pot_account = await ledger.get_or_create_account(conn, None, "pot_escrow")
                        txn = await ledger.post(
                            conn,
                            "stake",
                            [Entry(cash.id, -self._room.stake), Entry(pot_account.id, self._room.stake)],
                            idempotency_key=idem,
                            round_id=round_id,
                        )
                        await conn.execute(
                            "UPDATE round_entries SET stake_txn_id = $1 "
                            "WHERE round_id = $2 AND card_no = $3",
                            txn.id,
                            round_id,
                            card_no,
                        )
                        await conn.execute(
                            "UPDATE rounds SET pot = pot + $1, player_count = player_count + 1 "
                            "WHERE id = $2",
                            self._room.stake,
                            round_id,
                        )
                except asyncpg.exceptions.UniqueViolationError as exc:
                    reason = "already_joined" if "user_id" in (exc.constraint_name or "") else "card_taken"
                    return JoinResult(False, reason)
                except InsufficientFunds:
                    return JoinResult(False, "insufficient_funds")

            self._entries[user_id] = RoundEntryState(card_no=card_no, auto_mark=auto_mark)
            self._pot += self._room.stake

        await ledger.publish_balance_update(self._pool, self._redis, user_id)
        await self._publish_room({"t": "card_taken", "card_no": card_no, "taken": True})
        return JoinResult(True, None)

    async def drop_card(self, user_id: int) -> JoinResult:
        if self._status != "lobby":
            return JoinResult(False, "not_droppable")
        entry = self._entries.get(user_id)
        if entry is None:
            return JoinResult(False, "not_in_round")

        round_id = self._round_id
        assert round_id is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
                pot_account = await ledger.get_or_create_account(conn, None, "pot_escrow")
                await ledger.post(
                    conn,
                    "refund",
                    [Entry(pot_account.id, -self._room.stake), Entry(cash.id, self._room.stake)],
                    idempotency_key=f"drop-{round_id}-{user_id}",
                    round_id=round_id,
                )
                await conn.execute(
                    "DELETE FROM round_entries WHERE round_id = $1 AND user_id = $2",
                    round_id,
                    user_id,
                )
                await conn.execute(
                    "UPDATE rounds SET pot = pot - $1, player_count = player_count - 1 "
                    "WHERE id = $2",
                    self._room.stake,
                    round_id,
                )

        del self._entries[user_id]
        self._pot -= self._room.stake
        await ledger.publish_balance_update(self._pool, self._redis, user_id)
        await self._publish_room({"t": "card_taken", "card_no": entry.card_no, "taken": False})
        return JoinResult(True, None)

    async def set_auto(self, user_id: int, auto: bool) -> JoinResult:
        entry = self._entries.get(user_id)
        if entry is None:
            return JoinResult(False, "not_in_round")

        self._entries[user_id] = RoundEntryState(card_no=entry.card_no, auto_mark=auto)
        if self._round_id is not None:
            await self._pool.execute(
                "UPDATE round_entries SET auto_mark = $1 WHERE round_id = $2 AND user_id = $3",
                auto,
                self._round_id,
                user_id,
            )
        return JoinResult(True, None)

    async def claim(self, user_id: int, *, source: str = "manual") -> ClaimResult:
        # Every attempt is logged to claim_attempts, including ones from a
        # user who isn't even in the round -- that's still an event worth an
        # audit trail, not just the attempts that get as far as a pattern
        # check. valid stays False unless a real pattern check below passes.
        valid = False
        try:
            with metrics.engine_claim_validation_seconds.time():
                if user_id in self._locked_out:
                    return ClaimResult(False, "locked_out")
                entry = self._entries.get(user_id)
                if entry is None:
                    return ClaimResult(False, "not_in_round")
                if self._status not in ("running", "settling"):
                    return ClaimResult(False, "round_not_running")

                grid = self._card_pool[entry.card_no]
                won = bingo.winning_patterns(grid, self._called, self._room.win_patterns)
                valid = bool(won)
                if not valid:
                    if source == "manual" and self._status == "running":
                        self._locked_out.add(user_id)
                    return ClaimResult(False, "no_pattern")
        finally:
            if self._round_id is not None:
                await self._record_claim_attempt(user_id, valid)

        now = time.monotonic()
        async with self._winner_lock:
            # A user can reach a valid claim through two independent paths
            # in the same round -- the server's own AUTO-mode scan
            # (_call_next_number) and a client-sent `claim` message (a
            # player with AUTO on client-side races to send one too, or a
            # manual player double-taps) -- either of which could otherwise
            # add the same user_id to _pending_winners twice and crash the
            # round_winners (round_id, user_id) primary key at settlement.
            # One claim per user per round, full stop, regardless of source.
            already_pending = any(w.user_id == user_id for w in self._pending_winners)
            if already_pending:
                return ClaimResult(False, "already_claimed")

            if self._status == "running":
                self._set_status("settling")
                deadline = now + WINNER_TIE_WINDOW_SECONDS
                self._winner_window_deadline = deadline
                self._pending_winners.append(
                    PendingWinner(user_id, entry.card_no, won[0].name, self._call_index)
                )
                self._settlement_task = asyncio.create_task(self._finalize_after_window(deadline))
                return ClaimResult(True, None)
            if (
                self._status == "settling"
                and self._winner_window_deadline is not None
                and now <= self._winner_window_deadline
            ):
                self._pending_winners.append(
                    PendingWinner(user_id, entry.card_no, won[0].name, self._call_index)
                )
                return ClaimResult(True, None)
        return ClaimResult(False, "round_already_settled")

    # --- round creation and phases --------------------------------------

    async def _start_new_round(self) -> None:
        server_seed = secrets.token_bytes(32)
        server_seed_hash = hashlib.sha256(server_seed).hexdigest()

        async with self._pool.acquire() as conn:
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM rounds WHERE room_id = $1",
                self._room.id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO rounds
                    (room_id, seq, status, stake, house_cut_bps, server_seed_hash, lobby_deadline)
                VALUES ($1, $2, 'lobby', $3, $4, $5, now() + make_interval(secs => $6))
                RETURNING id
                """,
                self._room.id,
                seq,
                self._room.stake,
                self._room.house_cut_bps,
                server_seed_hash,
                self._room.lobby_seconds,
            )

        assert row is not None
        self._round_id = row["id"]
        self._seq = seq
        self._set_status("lobby")
        self._server_seed = server_seed
        self._server_seed_hash = server_seed_hash
        self._client_seed = None
        self._pot = Decimal("0")
        self._entries = {}
        self._called = set()
        self._draw_order = []
        self._call_index = 0
        self._locked_out = set()
        self._auto_claimed = set()
        self._pending_winners = []
        self._winner_window_deadline = None
        self._settlement_task = None
        self._lobby_deadline_monotonic = time.monotonic() + self._room.lobby_seconds

    async def _run_lobby(self) -> None:
        while True:
            remaining = self._lobby_deadline_monotonic - time.monotonic()
            if remaining <= 0:
                break
            if not self._lock.is_held():
                return
            await self._publish_room(
                {
                    "t": "lobby_tick",
                    "seconds_left": max(0, round(remaining)),
                    "players": len(self._entries),
                    "pot": str(self._pot),
                    "derash": str(settlement.compute_derash(self._pot, self._room.house_cut_bps)[0]),
                }
            )
            await asyncio.sleep(min(1.0, remaining))

        if not self._lock.is_held():
            return

        if len(self._entries) >= self._room.min_players:
            await self._transition_to_running()
        else:
            round_id = self._round_id
            assert round_id is not None
            refunded_user_ids = list(self._entries)
            await refunds.refund_round(self._pool, round_id, reason="lobby_underfilled")
            for refunded_user_id in refunded_user_ids:
                await ledger.publish_balance_update(self._pool, self._redis, refunded_user_id)
            self._reset_to_idle()

    async def _transition_to_running(self) -> None:
        round_id = self._round_id
        assert round_id is not None
        assert self._server_seed is not None

        card_numbers = sorted(e.card_no for e in self._entries.values())
        client_seed = f"{','.join(str(c) for c in card_numbers)}-{round_id}"
        draw_order = bingo.derive_draw(self._server_seed, client_seed)

        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE rounds SET status = 'running', client_seed = $2, "
                "draw_order = $3, started_at = now() WHERE id = $1",
                round_id,
                client_seed,
                draw_order,
            )

        self._set_status("running")
        self._client_seed = client_seed
        self._draw_order = draw_order
        self._running_started_at = time.monotonic()

        derash_preview, _ = settlement.compute_derash(self._pot, self._room.house_cut_bps)
        await self._publish_room(
            {
                "t": "round_start",
                "round_id": round_id,
                "seq": self._seq,
                "players": len(self._entries),
                "pot": str(self._pot),
                "derash": str(derash_preview),
                "seed_hash": self._server_seed_hash,
            }
        )

        await self._run_running()

    async def _run_running(self) -> None:
        exhausted = False
        for idx in range(75):
            if self._status != "running" or not self._lock.is_held():
                break
            target = self._running_started_at + (idx + 1) * (self._room.call_interval_ms / 1000)
            now = time.monotonic()
            if target > now:
                await asyncio.sleep(target - now)
            if self._status != "running" or not self._lock.is_held():
                break
            await self._call_next_number()
            if self._status != "running":
                break
        else:
            exhausted = True

        if self._settlement_task is not None:
            task, self._settlement_task = self._settlement_task, None
            await task
            return

        if exhausted and self._status == "running":
            round_id = self._round_id
            assert round_id is not None
            assert self._server_seed is not None
            refunded_user_ids = list(self._entries)
            await refunds.refund_round(self._pool, round_id, reason="exhausted_no_winner")
            for refunded_user_id in refunded_user_ids:
                await ledger.publish_balance_update(self._pool, self._redis, refunded_user_id)
            await self._pool.execute(
                "UPDATE rounds SET server_seed = $2 WHERE id = $1", round_id, self._server_seed
            )
            await self._publish_room(
                {
                    "t": "round_end",
                    "round_id": round_id,
                    "winners": [],
                    "derash": "0.00",
                    "server_seed": self._server_seed.hex(),
                }
            )
            self._reset_to_idle()
        # Otherwise we lost the room lock mid-round with no winner and no
        # exhaustion. Leave state as-is -- run_forever's own loop exits
        # because the lock is gone, and services/engine/recovery.py voids
        # and refunds this round the next time an engine worker starts.

    async def _call_next_number(self) -> None:
        round_id = self._round_id
        assert round_id is not None

        self._call_index += 1
        number = self._draw_order[self._call_index - 1]
        self._called.add(number)
        metrics.engine_calls_total.inc()

        await self._pool.execute(
            "UPDATE rounds SET call_index = $1 WHERE id = $2", self._call_index, round_id
        )
        await self._publish_room(
            {
                "t": "call",
                "round_id": round_id,
                "index": self._call_index,
                "number": number,
                "letter": bingo.letter_for(number),
            }
        )

        # Keep scanning every auto-mark-eligible entry for this call, even
        # after the first winner flips self._status to "settling" -- a
        # real bug a code review pass caught: returning early here meant
        # any *later* entry in this same iteration who also completes a
        # winning pattern on this exact same number (a genuine
        # simultaneous auto-mark tie) was never even offered to claim(),
        # silently losing that player's share of the derash to whichever
        # entry happened to come first in dict order. claim() itself
        # already handles this correctly on its own -- a call while
        # status is "settling" and still within WINNER_TIE_WINDOW_SECONDS
        # registers a genuine tie (the same path a second *manual* claim
        # already relies on, per test_two_simultaneous_claims_split_
        # derash_evenly); nothing here needs to short-circuit for that to
        # work, it only needs to not give up early.
        for user_id, entry in list(self._entries.items()):
            if not entry.auto_mark:
                continue
            if user_id in self._auto_claimed or user_id in self._locked_out:
                continue
            grid = self._card_pool[entry.card_no]
            if bingo.winning_patterns(grid, self._called, self._room.win_patterns):
                self._auto_claimed.add(user_id)
                await self.claim(user_id, source="auto")

    async def _finalize_after_window(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

        async with self._winner_lock:
            winners = list(self._pending_winners)
            self._pending_winners = []
            self._winner_window_deadline = None

        await self._settle_with_winners(winners)

    async def _settle_with_winners(self, winners: list[PendingWinner]) -> None:
        round_id = self._round_id
        assert round_id is not None
        assert self._server_seed is not None

        pot = self._pot
        derash, house_cut = settlement.compute_derash(pot, self._room.house_cut_bps)
        shares, leftover = settlement.split_derash(derash, len(winners))
        house_total = house_cut + leftover

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                pot_account = await ledger.get_or_create_account(conn, None, "pot_escrow")
                house_account = await ledger.get_or_create_account(conn, None, "house_revenue")

                winner_cash_accounts = [
                    await ledger.get_or_create_account(conn, w.user_id, "user_cash")
                    for w in winners
                ]

                entries = [Entry(pot_account.id, -pot)]
                entries.extend(
                    Entry(acct.id, share) for acct, share in zip(winner_cash_accounts, shares)
                )
                entries.append(Entry(house_account.id, house_total))

                txn = await ledger.post(
                    conn,
                    "payout",
                    entries,
                    idempotency_key=f"settle-{round_id}",
                    round_id=round_id,
                )

                for w, share in zip(winners, shares):
                    await conn.execute(
                        """
                        INSERT INTO round_winners
                            (round_id, user_id, card_no, pattern, won_on_call, amount, payout_txn_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        round_id,
                        w.user_id,
                        w.card_no,
                        w.pattern,
                        w.call_index,
                        share,
                        txn.id,
                    )

                await conn.execute(
                    "UPDATE rounds SET status = 'done', derash = $2, server_seed = $3, "
                    "ended_at = now() WHERE id = $1",
                    round_id,
                    derash,
                    self._server_seed,
                )

        for w in winners:
            await ledger.publish_balance_update(self._pool, self._redis, w.user_id)

        await self._publish_room(
            {
                "t": "round_end",
                "round_id": round_id,
                "winners": [
                    {
                        "user_id": w.user_id,
                        "card_no": w.card_no,
                        "pattern": w.pattern,
                        "amount": str(share),
                    }
                    for w, share in zip(winners, shares)
                ],
                "derash": str(derash),
                "server_seed": self._server_seed.hex(),
            }
        )

        self._set_status("done")
        await asyncio.sleep(self._room.result_seconds)
        self._reset_to_idle()

    def _reset_to_idle(self) -> None:
        self._round_id = None
        self._set_status("idle")
        self._server_seed = None
        self._server_seed_hash = None
        self._client_seed = None
        self._pot = Decimal("0")
        self._entries = {}
        self._called = set()
        self._draw_order = []
        self._call_index = 0
        self._locked_out = set()
        self._auto_claimed = set()
        self._pending_winners = []
        self._winner_window_deadline = None
        self._settlement_task = None
        self._round_active_event.clear()

    async def _record_claim_attempt(self, user_id: int, valid: bool) -> None:
        await self._pool.execute(
            "INSERT INTO claim_attempts (round_id, user_id, call_index, valid) "
            "VALUES ($1, $2, $3, $4)",
            self._round_id,
            user_id,
            self._call_index,
            valid,
        )

    async def _publish_room(self, message: dict[str, object]) -> None:
        await self._redis.publish(f"room:{self._room.id}", json.dumps(message))

    # --- gateway command channel -----------------------------------------
    #
    # A gateway process holds the player's WebSocket, not this engine. Every
    # action a connected player takes (join, drop, claim, toggle auto) comes
    # in over the room's Redis Stream (services/engine/commands.py) rather
    # than a direct method call, because the gateway and the engine that
    # owns this room are, in production, different processes. Exactly this
    # engine instance is the sole reader of its own room's stream, for as
    # long as it holds the room lock.

    async def _serve_commands(self) -> None:
        stream = commands.stream_key(self._room.id)
        last_id = "$"  # only entries added from this moment on
        while True:
            response = await self._redis.xread({stream: last_id}, block=1000, count=20)
            if not response:
                continue
            # Plain xread() (no consumer group) always returns this shape;
            # the client library's return type is a broad union covering
            # other call patterns too, so narrow it explicitly.
            assert isinstance(response, list)
            for _stream_name, entries in response:
                for entry_id, fields in entries:
                    last_id = entry_id
                    await self._handle_command(fields)

    async def _handle_command(self, fields: dict[str, str]) -> None:
        request_id = fields.get("request_id")
        action = fields.get("action", "")
        try:
            user_id = int(fields.get("user_id", "0"))
        except ValueError:
            user_id = 0
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except json.JSONDecodeError:
            payload = {}

        result: JoinResult | ClaimResult
        try:
            if action == "join":
                result = await self.join(
                    user_id, payload.get("card_no", 0), auto_mark=payload.get("auto_mark", True)
                )
            elif action == "drop_card":
                result = await self.drop_card(user_id)
            elif action == "claim":
                result = await self.claim(user_id, source="manual")
            elif action == "set_auto":
                result = await self.set_auto(user_id, bool(payload.get("auto", True)))
            else:
                result = JoinResult(False, "unknown_action")
        except Exception:
            # A code review pass caught that this had no isolation at
            # all: one malformed payload or edge-case bug anywhere in
            # join()/drop_card()/claim()/set_auto() would propagate
            # straight out of _serve_commands()'s loop and kill this
            # room's single long-lived command consumer permanently (no
            # restart) -- every subsequent join/drop/claim/set_auto for
            # this room would silently time out for players (5s
            # CommandTimeout) while the round itself kept running
            # unattended. One bad command must fail that command, not
            # the room.
            logger.exception(
                "engine_command_handler_raised", room_id=self._room.id, action=action, user_id=user_id
            )
            result = JoinResult(False, "internal_error")

        if request_id:
            await self._redis.publish(
                commands.reply_channel(request_id),
                json.dumps({"ok": result.ok, "reason": result.reason, "payload": {}}),
            )
