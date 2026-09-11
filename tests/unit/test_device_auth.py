"""Pure unit tests for packages/core/device_auth.py -- no DB, no network."""

from packages.core.device_auth import generate_device_token, hash_device_token


def test_generated_tokens_are_unique_and_high_entropy():
    tokens = {generate_device_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) >= 32 for t in tokens)


def test_hash_is_deterministic_and_never_equals_the_token():
    token = generate_device_token()
    first = hash_device_token(token)
    second = hash_device_token(token)
    assert first == second
    assert first != token


def test_different_tokens_hash_differently():
    a = hash_device_token("token-a")
    b = hash_device_token("token-b")
    assert a != b
