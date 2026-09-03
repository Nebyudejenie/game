"""Tests for packages/core/logging.py's redaction processor -- spec
section 9.2: "logs must never contain full [phone] numbers or `initData`
strings." Had zero test coverage anywhere in the codebase before this
file (confirmed by grep, not assumed) -- reconcile_job.py is the only
existing caller of configure_logging(), and it's only ever exercised as
a real subprocess (tests/integration/test_reconcile_job.py), so nothing
ever captured and inspected the actual JSON a log call produces.

Every test here runs configure_logging() in a real, throwaway subprocess
rather than the shared pytest process, for a confirmed reason, not a
theoretical one: structlog's cache_logger_on_first_use=True (part of
configure_logging()'s own config) permanently freezes a logger's level
filter the first time that specific logger instance is actually used --
confirmed directly by calling configure_logging("INFO") then
configure_logging("DEBUG") against the same already-used logger and
finding its DEBUG-level output still filtered, and confirming
structlog.reset_defaults() does not undo that freeze either. Every other
module in this codebase creates its own module-level logger at import
time (`logger = structlog.get_logger()` in services/payments/deposits.py
and elsewhere) -- calling configure_logging() for the first time inside
the single shared pytest process risks permanently freezing whichever of
those loggers happens to be used next, for the rest of the whole
`pytest tests/` run, depending on unrelated test execution order. A
subprocess makes that risk structurally impossible instead of merely
unlikely.
"""

import json
import subprocess
import sys


def _log_one_line(**fields: object) -> dict[str, object]:
    fields_json = json.dumps(fields)
    script = (
        "import json\n"
        "from packages.core.logging import configure_logging, get_logger\n"
        "configure_logging('INFO')\n"
        "logger = get_logger('test_logging')\n"
        f"logger.info('test_event', **json.loads({fields_json!r}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip()
    result: dict[str, object] = json.loads(line)
    return result


def test_phone_field_is_redacted() -> None:
    parsed = _log_one_line(phone="+251911000000")
    assert parsed["phone"] == "***REDACTED***"


def test_phone_e164_field_is_redacted() -> None:
    parsed = _log_one_line(phone_e164="+251911000000")
    assert parsed["phone_e164"] == "***REDACTED***"


def test_init_data_field_is_redacted() -> None:
    parsed = _log_one_line(init_data="user=%7B%22id%22...&hash=abc123")
    assert parsed["init_data"] == "***REDACTED***"


def test_bot_token_and_webhook_secret_are_redacted() -> None:
    parsed = _log_one_line(bot_token="123456:FAKE-TOKEN", webhook_secret="s3cr3t")
    assert parsed["bot_token"] == "***REDACTED***"
    assert parsed["webhook_secret"] == "***REDACTED***"


def test_admin_credentials_are_redacted() -> None:
    # A code review pass caught these missing -- nothing currently logs
    # any of them, but that's exactly the future-mistake this allowlist
    # exists to guard against (see this file's own docstring), the same
    # way password/token already were covered.
    parsed = _log_one_line(totp_code="123456", totp_secret="JBSWY3DPEHPK3PXP", session_token="abc.def")
    assert parsed["totp_code"] == "***REDACTED***"
    assert parsed["totp_secret"] == "***REDACTED***"
    assert parsed["session_token"] == "***REDACTED***"


def test_redaction_is_case_insensitive() -> None:
    # structlog callers are free to spell a key however they like --
    # the redaction check itself must not silently rely on exact casing.
    parsed = _log_one_line(Phone="+251911000000", TOKEN="secret123")
    assert parsed["Phone"] == "***REDACTED***"
    assert parsed["TOKEN"] == "***REDACTED***"


def test_unrelated_fields_are_left_alone() -> None:
    # The redaction list is a fixed set of known-sensitive keys, not a
    # blanket "hide everything" -- a real log line still needs to be
    # useful for debugging.
    parsed = _log_one_line(user_id=42, amount="100.00", room_id=7)
    assert parsed["user_id"] == 42
    assert parsed["amount"] == "100.00"
    assert parsed["room_id"] == 7


def test_output_is_real_json_with_level_and_event() -> None:
    parsed = _log_one_line(amount="50.00")
    assert parsed["event"] == "test_event"
    assert parsed["level"] == "info"
    assert "timestamp" in parsed


def _log_one_exception(local_secret: str | None = None) -> dict[str, object]:
    # A real function call, not logger.exception() at module scope --
    # module-level "locals" are just globals, which wouldn't exercise the
    # actual bug (a real function's own local variables never reaching
    # the log) at all.
    secret_line = f"    bot_token = {local_secret!r}\n" if local_secret is not None else ""
    script = (
        "from packages.core.logging import configure_logging, get_logger\n"
        "configure_logging('INFO')\n"
        "logger = get_logger('test_logging')\n"
        "def do_the_thing():\n"
        f"{secret_line}"
        "    1 / 0\n"
        "try:\n"
        "    do_the_thing()\n"
        "except ZeroDivisionError:\n"
        "    logger.exception('engine_worker_run_active_rooms_failed')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip()
    result: dict[str, object] = json.loads(line)
    return result


def test_logger_exception_renders_a_real_traceback_not_a_bare_bool() -> None:
    """A real production gap: exc_info=True (structlog's own default for
    logger.exception()) was never consumed by anything in the processor
    chain, so JSONRenderer just serialized the raw bool -- every
    logger.exception() call in the codebase produced `"exc_info": true`
    with the actual exception and traceback lost. This is the fix.
    """
    parsed = _log_one_exception()
    assert parsed.get("exc_info") is not True, "the old bug: a bare bool, no real traceback"
    exception = parsed["exception"]
    assert isinstance(exception, list) and exception
    assert exception[0]["exc_type"] == "ZeroDivisionError"
    assert exception[0]["exc_value"] == "division by zero"
    assert exception[0]["frames"], "must include real frame/line info, not just the exception name"


def test_logger_exception_never_captures_local_variables() -> None:
    """The security-critical half of the fix above: structlog's
    dict_tracebacks default (show_locals=True) would otherwise dump every
    frame's local variables into the log verbatim -- completely bypassing
    _redact, which only ever scans the top-level event dict, never
    traceback frame contents. A bot token, a phone number, or a raw
    initData string sitting in some function's own local scope at the
    moment it raised would leak in clear text through exactly this path
    if this regressed.
    """
    parsed = _log_one_exception(local_secret="super-secret-token-value-not-to-leak")
    raw = json.dumps(parsed)
    assert "super-secret-token-value-not-to-leak" not in raw
    exception = parsed["exception"]
    assert isinstance(exception, list) and exception
    frame = exception[0]["frames"][0]
    assert "locals" not in frame
