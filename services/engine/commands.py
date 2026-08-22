"""Gateway -> engine command channel, over one Redis Stream per room.

Exactly one engine owns a room at a time (room_lock.py); its command-serving
loop (round_engine.py's `_serve_commands`) is the only consumer of that
room's stream, so there's no need for consumer groups the way the payout
queue needs them elsewhere -- there is never more than one reader racing
another for the same entry.

Request/response correlation: each request carries a unique request_id. The
engine publishes its result to a pubsub channel named after that id; the
caller subscribes to that channel before sending the request and always
unsubscribes afterward, whether it got a reply or timed out.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

COMMAND_TIMEOUT_SECONDS = 5.0
STREAM_MAXLEN = 1000


def stream_key(room_id: int) -> str:
    return f"room:{room_id}:cmds"


def reply_channel(request_id: str) -> str:
    return f"cmdreply:{request_id}"


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class CommandTimeout(Exception):
    """No engine answered in time -- the room may have no live owner right
    now (between engine restarts, or a crashed worker not yet recovered).
    Safe to retry; sending the same action again is not itself unsafe
    (join/claim/etc. are all idempotent or safely re-checkable), though the
    caller decides whether retrying makes sense for that action.
    """


async def send_command(
    redis: Redis,
    room_id: int,
    action: str,
    user_id: int,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> CommandResult:
    request_id = uuid.uuid4().hex
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_channel(request_id))
        await redis.xadd(
            stream_key(room_id),
            {
                "request_id": request_id,
                "action": action,
                "user_id": str(user_id),
                "payload": json.dumps(payload or {}),
            },
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        try:
            message = await asyncio.wait_for(_next_message(pubsub), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CommandTimeout(
                f"no reply for '{action}' on room {room_id} within {timeout}s"
            ) from exc
        data = json.loads(message["data"])
        return CommandResult(
            ok=data["ok"], reason=data.get("reason"), payload=data.get("payload", {})
        )
    finally:
        await pubsub.unsubscribe(reply_channel(request_id))
        await pubsub.aclose()  # type: ignore[no-untyped-call]  # untyped in redis-py itself


async def _next_message(pubsub: PubSub) -> dict[str, Any]:
    async for message in pubsub.listen():
        if isinstance(message, dict) and message.get("type") == "message":
            return message
    raise RuntimeError("pubsub closed before a reply arrived")
