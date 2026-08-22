import asyncio
import itertools
import json
import os
import random
import socket
import uuid
from decimal import Decimal

import asyncpg
import pytest_asyncio
import uvicorn

# Fixed before anything imports packages.core.config: get_settings() is
# lru_cache'd, so whatever TELEGRAM_BOT_TOKEN is set to the first time it's
# read is what the whole suite gets -- including the gateway app under test,
# which needs a token the test's own initData-building helper also knows.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token-for-suite")

from packages.core import ledger
from packages.core.config import get_settings
from packages.core.redis_conn import get_redis
from services.engine.round_engine import load_card_pool

# Every test that needs a fresh user picks the next id off this counter,
# seeded randomly so re-runs of the suite never collide with leftover rows
# from a previous run against the same database.
_telegram_id_counter = itertools.count(random.randint(10**9, 2 * 10**9))


def next_telegram_id() -> int:
    return next(_telegram_id_counter)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pool():
    settings = get_settings()
    p = await asyncpg.create_pool(dsn=settings.database_url, min_size=5, max_size=50)
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="session")
async def conn(pool):
    async with pool.acquire() as connection:
        yield connection


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def redis():
    r = get_redis()
    yield r
    await r.aclose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def card_pool(pool):
    return await load_card_pool(pool)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def gateway_server():
    """Runs the real gateway app (services/gateway/app.py) via uvicorn, on
    this same event loop -- not a subprocess, not a separate thread, so
    there's no cross-event-loop asyncpg/redis connection hazard. Tests
    connect to it as a genuine WebSocket client would.
    """
    from services.gateway.app import app as gateway_app

    port = _free_port()
    config = uvicorn.Config(gateway_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("gateway server did not start in time")

    yield f"ws://127.0.0.1:{port}/ws"

    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


def build_init_data(telegram_id: int, *, first_name: str = "Test", auth_date: int | None = None) -> str:
    """Builds a correctly HMAC-signed initData string against this test
    session's TELEGRAM_BOT_TOKEN, the same way a real Telegram client would.
    """
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    bot_token = get_settings().telegram_bot_token
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": f"AAQ{uuid.uuid4().hex[:16]}",
        "user": json.dumps({"id": telegram_id, "first_name": first_name}),
    }
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def create_user(conn: asyncpg.Connection) -> int:
    telegram_id = next_telegram_id()
    row = await conn.fetchrow(
        """
        INSERT INTO users (telegram_id, display_name)
        VALUES ($1, $2)
        RETURNING id
        """,
        telegram_id,
        f"test-user-{telegram_id}",
    )
    return row["id"]


async def fund_user(conn: asyncpg.Connection, user_id: int, amount: Decimal) -> None:
    """Deposits amount into user_id's cash balance via a real ledger
    transaction -- joining a round debits user_cash for real, so a fresh
    test user needs real money in it first, the same as production.
    """
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    await ledger.post(
        conn,
        "deposit",
        [ledger.Entry(provider.id, -amount), ledger.Entry(cash.id, amount)],
        idempotency_key=f"test-fund-{user_id}-{uuid.uuid4()}",
    )


async def create_funded_user(conn: asyncpg.Connection, amount: Decimal = Decimal("1000.00")) -> int:
    user_id = await create_user(conn)
    await fund_user(conn, user_id, amount)
    return user_id


async def create_room(
    conn: asyncpg.Connection,
    *,
    stake: Decimal = Decimal("20.00"),
    house_cut_bps: int = 2000,
    min_players: int = 2,
    max_players: int = 100,
    lobby_seconds: int = 1,
    call_interval_ms: int = 20,
    result_seconds: int = 0,
    win_patterns: list[str] | None = None,
) -> int:
    """Fast-timing test room by default -- lobby closes in 1s, a number is
    called every 20ms (so all 75 calls take ~1.5s worst case), and there's
    no lingering result display. Override per test where the timing itself
    is what's under test.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO rooms
            (code, stake, house_cut_bps, min_players, max_players,
             lobby_seconds, call_interval_ms, result_seconds, win_patterns)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        f"test-room-{uuid.uuid4()}",
        stake,
        house_cut_bps,
        min_players,
        max_players,
        lobby_seconds,
        call_interval_ms,
        result_seconds,
        json.dumps(win_patterns if win_patterns is not None else ["row", "col", "diag", "corners"]),
    )
    return row["id"]
