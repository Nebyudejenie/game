"""Unit tests for packages/core/telegram_auth.py -- the entire security
boundary for Mini App authentication. Every rejection path the spec calls
out gets its own test.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from packages.core.telegram_auth import InvalidInitData, validate_init_data

BOT_TOKEN = "123456:AAFake-Bot-Token-For-Tests-Only"


def build_init_data(
    fields: dict[str, str], bot_token: str = BOT_TOKEN, *, corrupt_hash: bool = False
) -> str:
    """Builds a correctly-signed initData string the same way Telegram
    does, so tests exercise the real algorithm rather than a stand-in.
    """
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    all_fields = dict(fields)
    all_fields["hash"] = "0" * 64 if corrupt_hash else computed
    return urlencode(all_fields)


def default_fields(auth_date: int | None = None) -> dict[str, str]:
    return {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAFakeQueryId",
        "user": json.dumps(
            {"id": 123456789, "first_name": "Nebyu", "username": "nebyu", "language_code": "am"}
        ),
    }


def test_valid_init_data_is_accepted():
    raw = build_init_data(default_fields())
    result = validate_init_data(raw, BOT_TOKEN)
    assert result.user.id == 123456789
    assert result.user.username == "nebyu"
    assert result.user.language_code == "am"


def test_tampered_hash_is_rejected():
    raw = build_init_data(default_fields(), corrupt_hash=True)
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(raw, BOT_TOKEN)
    assert exc_info.value.reason == "bad_hash"


def test_tampered_field_after_signing_is_rejected():
    fields = default_fields()
    raw = build_init_data(fields)
    # Flip a signed field post-hoc, as if a MITM or a tampered client tried
    # to change their own display name after the fact.
    assert "Nebyu" in raw
    tampered = raw.replace("Nebyu", "Hacked")
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(tampered, BOT_TOKEN)
    assert exc_info.value.reason == "bad_hash"


def test_wrong_bot_token_is_rejected():
    raw = build_init_data(default_fields(), bot_token=BOT_TOKEN)
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(raw, "999999:SomeoneElsesToken")
    assert exc_info.value.reason == "bad_hash"


def test_stale_auth_date_over_24h_is_rejected():
    stale = int(time.time()) - (25 * 60 * 60)
    raw = build_init_data(default_fields(auth_date=stale))
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(raw, BOT_TOKEN)
    assert exc_info.value.reason == "stale_auth_date"


def test_auth_date_just_under_24h_is_accepted():
    almost_stale = int(time.time()) - (24 * 60 * 60 - 60)
    raw = build_init_data(default_fields(auth_date=almost_stale))
    result = validate_init_data(raw, BOT_TOKEN)
    assert result.auth_date == almost_stale


def test_auth_date_far_in_the_future_is_rejected():
    future = int(time.time()) + 3600
    raw = build_init_data(default_fields(auth_date=future))
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(raw, BOT_TOKEN)
    assert exc_info.value.reason == "auth_date_in_future"


def test_missing_hash_is_rejected():
    fields = default_fields()
    raw = urlencode(fields)  # no hash field at all
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(raw, BOT_TOKEN)
    assert exc_info.value.reason == "missing_hash"


def test_missing_user_is_rejected():
    fields = {"auth_date": str(int(time.time())), "query_id": "AAFakeQueryId"}
    raw = build_init_data(fields)
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(raw, BOT_TOKEN)
    assert exc_info.value.reason == "missing_user"


def test_malformed_user_json_is_rejected():
    fields = default_fields()
    fields["user"] = "{not valid json"
    raw = build_init_data(fields)
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data(raw, BOT_TOKEN)
    assert exc_info.value.reason == "malformed_user"


def test_empty_init_data_is_rejected():
    with pytest.raises(InvalidInitData) as exc_info:
        validate_init_data("", BOT_TOKEN)
    assert exc_info.value.reason == "empty"


def test_comparison_is_constant_time_not_shortcut_equality():
    # A regression guard, not a timing measurement: assert the
    # implementation actually calls hmac.compare_digest rather than `==`,
    # since `==` on strings short-circuits on the first differing byte and
    # is the textbook timing side-channel this function exists to avoid.
    import inspect

    import packages.core.telegram_auth as mod

    source = inspect.getsource(mod.validate_init_data)
    assert "compare_digest" in source
