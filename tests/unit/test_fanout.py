"""Unit tests for services/gateway/fanout.py's ConnectionQueue -- pure
asyncio state, no Redis or Postgres needed.
"""

import asyncio
import json

from services.gateway.fanout import MAX_QUEUE_SIZE, ConnectionQueue


async def test_offer_delivers_messages_in_order():
    cq = ConnectionQueue()
    cq.offer("a")
    cq.offer("b")
    assert await cq.get_or_wake() == "a"
    assert await cq.get_or_wake() == "b"


async def test_droppable_overflow_sets_needs_state_sync_without_enqueueing():
    cq = ConnectionQueue()
    for i in range(MAX_QUEUE_SIZE):
        cq.offer(json.dumps({"t": "call", "index": i}))
    assert cq.needs_state_sync is False

    cq.offer(json.dumps({"t": "call", "index": MAX_QUEUE_SIZE}))  # overflow, droppable type
    assert cq.needs_state_sync is True
    assert cq.queue.qsize() == MAX_QUEUE_SIZE  # nothing new was actually enqueued


async def test_non_droppable_overflow_clears_the_backlog_and_keeps_it():
    cq = ConnectionQueue()
    for i in range(MAX_QUEUE_SIZE):
        cq.offer(json.dumps({"t": "call", "index": i}))

    cq.offer(json.dumps({"t": "round_end", "round_id": 1}))  # overflow, non-droppable
    assert cq.needs_state_sync is False
    assert cq.queue.qsize() == 1
    remaining = await cq.get_or_wake()
    assert json.loads(remaining)["t"] == "round_end"


async def test_get_or_wake_wakes_immediately_when_the_flag_flips_while_blocked():
    # A code review pass caught that the writer loop's own needs_state_
    # sync check only ever runs once per iteration, immediately before
    # blocking on queue.get() -- if the flag flips while that call is
    # already parked waiting on an empty queue, nothing woke it up until
    # some unrelated message happened to arrive later, which near a quiet
    # round boundary (calls pausing before settlement) could leave a
    # recovering client's board stale indefinitely. get_or_wake() exists
    # to close that: it must return promptly once the flag flips, not
    # wait for a real message to eventually show up.
    cq = ConnectionQueue()
    task = asyncio.ensure_future(cq.get_or_wake())
    await asyncio.sleep(0)  # let the task actually start and block on queue.get()
    assert not task.done()

    cq._handle_full(json.dumps({"t": "call", "index": 0}))  # noqa: SLF001
    assert cq.needs_state_sync is True

    result = await asyncio.wait_for(task, timeout=1.0)
    assert result is None  # woken by the flag, not a real message -- nothing to send


async def test_get_or_wake_returns_a_real_message_normally():
    cq = ConnectionQueue()
    task = asyncio.ensure_future(cq.get_or_wake())
    await asyncio.sleep(0)
    cq.offer("hello")
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == "hello"
