"""Simulated-players bot-runner process.

A separate process, deliberately never imported into gateway or
engine-worker: if a bug here crashes this process, every real room's own
engine (a completely separate OS process) keeps running untouched -- the
same isolation services/payments/payout_worker.py already gets from
engine-worker.

Every bot action goes through services.engine.commands.send_command(), the
exact same Redis Stream a real player's WebSocket join/claim travels
through (services/gateway/connection.py::ConnectionHandler._run_action()).
There is no bot-only branch anywhere in RoundEngine -- join()'s real
capacity/stake/idempotency checks and claim()'s real pattern validation
apply identically. In particular, every join here is sent with
auto_mark=True, so a bot never calls claim() itself: the engine's own
auto-claim (round_engine.py's self._auto_claimed handling) already wins
for whichever card -- bot or real player's -- completes a pattern first.
That single fact is what keeps this worker from needing any pattern-
matching or reaction-timing logic of its own: winning is 100%
server-authoritative, exactly like a real player who has auto-mark on.

This process's only real job is deciding *when* a bot joins a room and
*which* room -- a pacing/scheduling decision, not a gameplay one. It is
DB-driven and poll-based (no persistent per-bot subscription): every tick,
it re-reads simulated_players_settings.enabled (the global kill switch)
and each active bot's row, so an admin's Pause/Stop/Stop-All action is
picked up within one poll interval, the same latency EngineWorker's own
run_active_rooms() already tolerates for a newly-activated room.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import metrics
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.logging import configure_logging
from packages.core.redis_conn import get_redis
from packages.core.tracing import configure_tracing
from services.engine import commands
from services.gateway.queries import list_rooms

logger = structlog.get_logger()

METRICS_PORT = 8006

TICK_INTERVAL_SECONDS = 5.0
ETHIOPIA_TZ = ZoneInfo("Africa/Addis_Ababa")

_ACTIVE_STATUSES = ("idle", "joining", "playing")


@dataclass(frozen=True)
class StrategyProfile:
    # Randomized within this range so ten bots on the same strategy don't
    # all tick in lockstep -- a real production incident class this
    # deliberately avoids (see engine worker's own CLAIM_POLL_INTERVAL_SECONDS
    # for the analogous "don't let independent pollers synchronize" reasoning).
    check_interval_range: tuple[float, float]
    room_preference: str  # "quiet" | "any" | "full" | "random"


STRATEGY_PROFILES: dict[str, StrategyProfile] = {
    "conservative": StrategyProfile((45.0, 75.0), "quiet"),
    "normal": StrategyProfile((15.0, 25.0), "any"),
    "active": StrategyProfile((3.0, 7.0), "full"),
    "randomized": StrategyProfile((5.0, 45.0), "random"),
}


def _pick_room(joinable: list[dict[str, Any]], preference: str) -> dict[str, Any] | None:
    """Pure selection given an already-filtered candidate list -- kept
    separate from _choose_room's own DB scan so this can be tested
    against a small, controlled list instead of the real rooms table,
    which (like every other table in this suite's shared dev database)
    accumulates real rows across runs that a naive DB-integration test
    would have no reliable way to exclude.
    """
    if not joinable:
        return None
    if preference == "quiet":
        return min(joinable, key=lambda r: r["players"])
    if preference == "full":
        return max(joinable, key=lambda r: r["players"])
    return random.choice(joinable)


class SimulatedPlayersWorker:
    def __init__(self, pool: asyncpg.Pool, redis: Redis) -> None:
        self._pool = pool
        self._redis = redis
        # In-memory pacing only -- losing this on a process restart just
        # means every bot is immediately eligible to be re-checked, never
        # a correctness issue (no money moves from a "check", only from a
        # real send_command() join the check may or may not decide to make).
        self._next_check_at: dict[int, float] = {}

    async def tick(self) -> None:
        settings = await self._pool.fetchrow(
            "SELECT enabled, max_concurrent_bots FROM simulated_players_settings WHERE id = 1"
        )
        if settings is None or not settings["enabled"]:
            logger.debug("simulated_players_disabled_skipping")
            return

        bots = await self._pool.fetch(
            """
            SELECT user_id, status, strategy, join_probability_pct, max_cards_per_join,
                   schedule_mode, schedule_window_start_minute, schedule_window_end_minute,
                   pinned_room_id, current_room_id
            FROM simulated_players
            WHERE status = ANY($1::text[])
            """,
            list(_ACTIVE_STATUSES),
        )
        active_count = sum(1 for b in bots if b["current_room_id"] is not None)
        now = time.monotonic()
        for bot in bots:
            if now < self._next_check_at.get(bot["user_id"], 0.0):
                continue
            profile = STRATEGY_PROFILES.get(bot["strategy"], STRATEGY_PROFILES["normal"])
            try:
                joined = await self._process_bot(bot, profile, active_count)
                if joined:
                    active_count += 1
            except Exception:
                # One bot's bad tick (a stale room row, a transient Redis
                # hiccup) must not take down every other bot's own
                # scheduling for the rest of this process's life -- same
                # "isolate per-item failure" principle as
                # EngineWorker.run_active_rooms()'s try/except.
                logger.exception("simulated_player_tick_failed", user_id=bot["user_id"])
            self._next_check_at[bot["user_id"]] = now + random.uniform(*profile.check_interval_range)

    async def _process_bot(self, bot: asyncpg.Record, profile: StrategyProfile, active_count: int) -> bool:
        user_id = bot["user_id"]
        if bot["current_room_id"] is not None:
            await self._continue_or_leave_room(user_id, bot["current_room_id"], bot["max_cards_per_join"])
            return False

        if not self._within_schedule(bot):
            return False
        if active_count >= await self._max_concurrent_bots():
            return False
        if random.randint(1, 100) > bot["join_probability_pct"]:
            return False

        room = await self._choose_room(profile, bot["schedule_mode"], bot["pinned_room_id"])
        if room is None:
            return False
        return await self._join_room(user_id, room["room_id"], bot["max_cards_per_join"])

    async def _max_concurrent_bots(self) -> int:
        value = await self._pool.fetchval("SELECT max_concurrent_bots FROM simulated_players_settings WHERE id = 1")
        return int(value)

    def _within_schedule(self, bot: asyncpg.Record) -> bool:
        if bot["schedule_mode"] != "scheduled_window":
            return True
        raw_start = bot["schedule_window_start_minute"]
        raw_end = bot["schedule_window_end_minute"]
        if raw_start is None or raw_end is None:
            return True
        start, end = int(raw_start), int(raw_end)
        now_local = datetime.now(ETHIOPIA_TZ)
        minute_of_day = now_local.hour * 60 + now_local.minute
        if start <= end:
            return start <= minute_of_day <= end
        return minute_of_day >= start or minute_of_day <= end  # wraps past midnight

    async def _choose_room(
        self, profile: StrategyProfile, schedule_mode: str, pinned_room_id: int | None
    ) -> dict[str, Any] | None:
        rooms = await list_rooms(self._pool)
        joinable = [
            r for r in rooms
            if r["status"] == "lobby" and r["players"] < r["max_players"]
        ]
        if schedule_mode == "room_specific" and pinned_room_id is not None:
            joinable = [r for r in joinable if r["room_id"] == pinned_room_id]
        return _pick_room(joinable, profile.room_preference)

    async def _join_room(self, user_id: int, room_id: int, max_cards_per_join: int) -> bool:
        round_row = await self._pool.fetchrow(
            "SELECT r.id AS id, ro.max_cards_per_player FROM rounds r "
            "JOIN rooms ro ON ro.id = r.room_id "
            "WHERE r.room_id = $1 AND r.status = 'lobby' ORDER BY r.seq DESC LIMIT 1",
            room_id,
        )
        if round_row is None:
            return False  # lobby closed between the scan above and now -- try again next tick
        card_count = max(1, min(max_cards_per_join, round_row["max_cards_per_player"]))
        taken = await self._pool.fetchval(
            "SELECT coalesce(array_agg(card_no), '{}') FROM round_entries WHERE round_id = $1",
            round_row["id"],
        )
        cards = await self._pool.fetch(
            "SELECT card_no FROM cards WHERE card_no != ALL($1::int[]) ORDER BY random() LIMIT $2",
            taken, card_count,
        )
        if not cards:
            return False

        await self._pool.execute(
            "UPDATE simulated_players SET status = 'joining', last_activity_at = now() WHERE user_id = $1",
            user_id,
        )
        joined_any = False
        for card_row in cards:
            result = await commands.send_command(
                self._redis, room_id, "join", user_id, {"card_no": card_row["card_no"], "auto_mark": True}
            )
            if result.ok:
                joined_any = True
            else:
                logger.info(
                    "simulated_player_join_rejected",
                    user_id=user_id, room_id=room_id, card_no=card_row["card_no"], reason=result.reason,
                )
                break  # room likely filled up mid-attempt; don't keep pushing more cards

        await self._pool.execute(
            "UPDATE simulated_players SET status = $2, current_room_id = $3, last_activity_at = now() "
            "WHERE user_id = $1",
            user_id, "playing" if joined_any else "idle", room_id if joined_any else None,
        )
        return joined_any

    async def _continue_or_leave_room(self, user_id: int, room_id: int, max_cards_per_join: int) -> None:
        room_active = await self._pool.fetchval("SELECT is_active FROM rooms WHERE id = $1", room_id)
        if not room_active:
            await self._pool.execute(
                "UPDATE simulated_players SET status = 'idle', current_room_id = NULL WHERE user_id = $1",
                user_id,
            )
            return

        round_row = await self._pool.fetchrow(
            "SELECT id, status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        if round_row is None or round_row["status"] != "lobby":
            return  # round still running/settling, or between rounds -- nothing to do this tick

        already_in = await self._pool.fetchval(
            "SELECT count(*) FROM round_entries WHERE round_id = $1 AND user_id = $2", round_row["id"], user_id
        )
        if already_in:
            return  # already holds a card in the current lobby

        # A new round's lobby has opened at this bot's own table -- rejoin
        # it, exactly like a real player parked at the same room across
        # rounds re-picking a card each time. Bots never wander to a
        # different room on their own; only an admin action (Pause/Stop/
        # Reset, or a fresh room pick after Start) moves one elsewhere.
        await self._join_room(user_id, room_id, max_cards_per_join)


def main() -> None:
    """Real production entrypoint. Every runtime toggle (global enabled
    switch, per-bot status/strategy/schedule) is a polled DB column, not a
    process signal -- an admin action takes effect within one
    TICK_INTERVAL_SECONDS, the same latency this codebase already accepts
    for engine-worker noticing a newly-activated room.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing("simulated-players-worker", settings.otel_exporter_endpoint)

    async def _run() -> None:
        pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=10)
        redis = get_redis()
        worker = SimulatedPlayersWorker(pool, redis)
        metrics_runner = await metrics.start_metrics_server(METRICS_PORT)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        try:
            while not stop_event.is_set():
                try:
                    await worker.tick()
                except Exception:
                    logger.exception("simulated_players_worker_tick_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=TICK_INTERVAL_SECONDS)
        finally:
            await metrics_runner.cleanup()
            await redis.aclose()
            await pool.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
