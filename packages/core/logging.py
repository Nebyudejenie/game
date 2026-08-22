"""Structured JSON logging shared by every service.

Every event goes through a redaction processor first: phone numbers, tokens,
and Telegram initData strings must never reach a log line in clear text, in
any service, no matter who adds a new log call later.
"""

import logging
import sys
from typing import cast

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

_REDACTED_KEYS = {
    "phone",
    "phone_e164",
    "token",
    "bot_token",
    "telegram_bot_token",
    "init_data",
    "initdata",
    "webhook_secret",
    "api_key",
    "password",
}


def _redact(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> FilteringBoundLogger:
    return cast(FilteringBoundLogger, structlog.get_logger(name))
