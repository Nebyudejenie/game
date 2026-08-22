"""Telegram Mini App `initData` validation (spec section 9.1).

This is the entire security boundary between "someone with a browser" and
"an authenticated player" -- there is no session cookie, no separate login
step. Every WebSocket handshake and every REST call that claims to be a
particular player must go through this, and get it exactly right:

    1. Parse initData as a query string.
    2. Pull out and remove the 'hash' field.
    3. data_check_string = remaining keys, sorted, joined "k=v" with \\n.
    4. secret_key = HMAC-SHA256(key="WebAppData", message=BOT_TOKEN).
    5. computed = HMAC-SHA256(key=secret_key, message=data_check_string).
    6. Compare computed to hash -- constant-time, or a timing side-channel
       leaks the valid hash one byte at a time.
    7. Reject if auth_date is older than 24 hours -- otherwise a captured
       initData string (logged by a proxy, stuck in browser history, shared
       by an over-eager screenshot) becomes a permanent session token.

Never log the raw initData string or the bot token -- see
packages/core/logging.py's redaction list.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


class InvalidInitData(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    last_name: str | None
    username: str | None
    language_code: str | None


@dataclass(frozen=True)
class TelegramInitData:
    user: TelegramUser
    auth_date: int


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: int | None = None,
) -> TelegramInitData:
    """Validates a raw initData string. Returns the parsed, authenticated
    data on success. Raises InvalidInitData with a specific reason on any
    failure -- caller should treat every reason identically (reject the
    connection); the distinct reasons exist for logging/testing, not for
    different client-facing behavior.
    """
    if not init_data:
        raise InvalidInitData("empty")

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    fields: dict[str, str] = {}
    for key, value in pairs:
        fields[key] = value  # last one wins, matches typical query semantics

    provided_hash = fields.pop("hash", None)
    if not provided_hash:
        raise InvalidInitData("missing_hash")

    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, provided_hash):
        raise InvalidInitData("bad_hash")

    auth_date_raw = fields.get("auth_date")
    if auth_date_raw is None or not auth_date_raw.isdigit():
        raise InvalidInitData("missing_auth_date")
    auth_date = int(auth_date_raw)

    current_time = now if now is not None else int(time.time())
    age = current_time - auth_date
    if age > max_age_seconds:
        raise InvalidInitData("stale_auth_date")
    if age < -60:  # allow a minute of clock skew, but not a future-dated token
        raise InvalidInitData("auth_date_in_future")

    user_raw = fields.get("user")
    if not user_raw:
        raise InvalidInitData("missing_user")
    try:
        user_obj = json.loads(user_raw)
        user = TelegramUser(
            id=int(user_obj["id"]),
            first_name=str(user_obj.get("first_name", "")),
            last_name=user_obj.get("last_name"),
            username=user_obj.get("username"),
            language_code=user_obj.get("language_code"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InvalidInitData("malformed_user") from exc

    return TelegramInitData(user=user, auth_date=auth_date)
