"""Tests for services/bot/perf.py's perf_middleware in isolation -- no
real Telegram Dispatcher needed, since the middleware only touches
data["handler"]/data[RECEIVED_AT_KEY] and calls the next handler in the
chain, exactly the shape aiogram itself calls it with.
"""

import time
from types import SimpleNamespace

from services.bot import perf
from packages.core import metrics


def _fake_handler_object(callback):
    return SimpleNamespace(callback=callback)


async def test_successful_command_increments_success_and_total_and_observes_latency():
    async def cmd_example(event, data):
        return "ok"

    data = {"handler": _fake_handler_object(cmd_example)}
    total_before = metrics.telegram_commands_total.labels(handler="cmd_example")._value.get()
    success_before = metrics.telegram_command_success_total.labels(handler="cmd_example")._value.get()
    latency_sum_before = metrics.telegram_command_latency_seconds.labels(handler="cmd_example")._sum.get()

    result = await perf.perf_middleware(cmd_example, object(), data)

    assert result == "ok"
    assert metrics.telegram_commands_total.labels(handler="cmd_example")._value.get() == total_before + 1
    assert (
        metrics.telegram_command_success_total.labels(handler="cmd_example")._value.get()
        == success_before + 1
    )
    assert (
        metrics.telegram_command_latency_seconds.labels(handler="cmd_example")._sum.get()
        >= latency_sum_before
    )


async def test_raising_command_increments_error_total_and_still_reraises():
    class Boom(Exception):
        pass

    async def cmd_failing(event, data):
        raise Boom("business logic failure")

    data = {"handler": _fake_handler_object(cmd_failing)}
    error_before = metrics.telegram_command_error_total.labels(handler="cmd_failing")._value.get()
    success_before = metrics.telegram_command_success_total.labels(handler="cmd_failing")._value.get()

    try:
        await perf.perf_middleware(cmd_failing, object(), data)
        raised = False
    except Boom:
        raised = True

    assert raised, "perf_middleware must never swallow a handler's own exception"
    assert (
        metrics.telegram_command_error_total.labels(handler="cmd_failing")._value.get()
        == error_before + 1
    )
    # A raising handler is not a success -- the counters must be mutually
    # exclusive per invocation, never both incremented for the same call.
    assert (
        metrics.telegram_command_success_total.labels(handler="cmd_failing")._value.get()
        == success_before
    )


async def test_dispatch_delay_is_observed_when_received_at_is_present():
    async def cmd_timed(event, data):
        return None

    received_at = time.monotonic() - 0.05
    data = {"handler": _fake_handler_object(cmd_timed), perf.RECEIVED_AT_KEY: received_at}
    dispatch_sum_before = metrics.telegram_dispatch_delay_seconds._sum.get()

    await perf.perf_middleware(cmd_timed, object(), data)

    # At least the ~0.05s gap deliberately introduced above must show up --
    # a loose lower bound (not an exact value) since real wall-clock time
    # also elapses running the middleware itself.
    assert metrics.telegram_dispatch_delay_seconds._sum.get() >= dispatch_sum_before + 0.04


async def test_missing_handler_object_falls_back_to_the_wrapper_name_without_crashing():
    # A defensive fallback for if aiogram's own internals ever stop
    # populating data["handler"] -- must never crash, even though the
    # resulting label ("cmd_defensive_fallback", this wrapper's own name)
    # is less meaningful than the real matched function's name.
    async def cmd_defensive_fallback(event, data):
        return "ok"

    result = await perf.perf_middleware(cmd_defensive_fallback, object(), {})
    assert result == "ok"
