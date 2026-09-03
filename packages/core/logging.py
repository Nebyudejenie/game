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
    # A code-review pass caught these missing: nothing currently logs a
    # TOTP code/secret or an admin session token as a structured field,
    # confirmed by grep -- but that's exactly the class of mistake this
    # allowlist exists to catch for a *future* log call this file's own
    # docstring already promises to cover ("no matter who adds a new log
    # call later"), the same way password/token already are.
    "totp_code",
    "totp_secret",
    "session_token",
    "authorization",
}


def _redact(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


# A real production gap: every logger.exception(...) call in this codebase
# (services/engine/worker.py's own polling-loop guard among them) relied
# on structlog's default exc_info=True actually being rendered into the
# log line -- without a processor that consumes it, JSONRenderer() below
# just tried to JSON-serialize the raw value, producing a bare
# `"exc_info": true` with the real exception and traceback never written
# anywhere. dict_tracebacks (not the older format_exc_info, which renders
# a single preformatted string meant for plain-text output) renders the
# traceback as a JSON-safe nested structure, matching this pipeline's own
# JSONRenderer downstream -- but its own default (show_locals=True) would
# dump every frame's local variables into the log verbatim, completely
# bypassing _redact above (which only ever scans the top-level event
# dict, never traceback frame contents): a bot token, a phone number, a
# raw initData string sitting in some function's own local scope at the
# moment it raised would leak in clear text through a path this file's
# own docstring already promises closed. show_locals=False is not the
# default -- it has to be requested explicitly, which is what building a
# real ExceptionRenderer here (instead of using the pre-built
# dict_tracebacks convenience object) is for.
_render_exceptions = structlog.processors.ExceptionRenderer(
    structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
)


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
            _render_exceptions,
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
