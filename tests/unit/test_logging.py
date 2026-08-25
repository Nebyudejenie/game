"""Tests for packages/core/logging.py's redaction processor -- spec
section 9.2: "logs must never contain full [phone] numbers or `initData`
strings." Had zero test coverage anywhere in the codebase before this
file (confirmed by grep, not assumed) -- reconcile_job.py is the only
existing caller of configure_logging(), and it's only ever exercised as
a real subprocess (tests/integration/test_reconcile_job.py), so nothing
ever captured and inspected the actual JSON a log call produces.

Pure and synchronous -- structlog's configure() reconfigures global state
freely (unlike OpenTelemetry's TracerProvider, it has no "first call
wins" guard), and nothing else in this pytest process calls
configure_logging() itself (only ever exercised via a real subprocess
elsewhere), so this is safe to run without cross-test interference.
"""

import io
import json
from contextlib import redirect_stdout

from packages.core.logging import configure_logging, get_logger


def _capture_one_log_line(**fields: object) -> dict[str, object]:
    configure_logging("INFO")
    logger = get_logger("test_logging")
    buf = io.StringIO()
    with redirect_stdout(buf):
        logger.info("test_event", **fields)
    line = buf.getvalue().strip()
    result: dict[str, object] = json.loads(line)
    return result


def test_phone_field_is_redacted() -> None:
    parsed = _capture_one_log_line(phone="+251911000000")
    assert parsed["phone"] == "***REDACTED***"


def test_phone_e164_field_is_redacted() -> None:
    parsed = _capture_one_log_line(phone_e164="+251911000000")
    assert parsed["phone_e164"] == "***REDACTED***"


def test_init_data_field_is_redacted() -> None:
    parsed = _capture_one_log_line(init_data="user=%7B%22id%22...&hash=abc123")
    assert parsed["init_data"] == "***REDACTED***"


def test_bot_token_and_webhook_secret_are_redacted() -> None:
    parsed = _capture_one_log_line(bot_token="123456:FAKE-TOKEN", webhook_secret="s3cr3t")
    assert parsed["bot_token"] == "***REDACTED***"
    assert parsed["webhook_secret"] == "***REDACTED***"


def test_redaction_is_case_insensitive() -> None:
    # structlog callers are free to spell a key however they like --
    # the redaction check itself must not silently rely on exact casing.
    parsed = _capture_one_log_line(Phone="+251911000000", TOKEN="secret123")
    assert parsed["Phone"] == "***REDACTED***"
    assert parsed["TOKEN"] == "***REDACTED***"


def test_unrelated_fields_are_left_alone() -> None:
    # The redaction list is a fixed set of known-sensitive keys, not a
    # blanket "hide everything" -- a real log line still needs to be
    # useful for debugging.
    parsed = _capture_one_log_line(user_id=42, amount="100.00", room_id=7)
    assert parsed["user_id"] == 42
    assert parsed["amount"] == "100.00"
    assert parsed["room_id"] == 7


def test_output_is_real_json_with_level_and_event() -> None:
    parsed = _capture_one_log_line(amount="50.00")
    assert parsed["event"] == "test_event"
    assert parsed["level"] == "info"
    assert "timestamp" in parsed
