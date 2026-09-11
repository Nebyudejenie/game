"""Ingestion-device credential helpers -- pure, no I/O, no DB access.

Shared by services/admin/queries.py (provisions a device, generates its
token, shows it to an admin exactly once) and services/payments/
device_registry.py (looks a presented bearer token up by its hash on
every ingestion request). Neither service imports the other; both import
this instead, the same reason services/payments/telebirr_parser.py's
normalize_reference() is a standalone function rather than living inside
whichever module happened to need it first.

A device token is 256 bits of secrets.token_urlsafe randomness -- unlike
a human-chosen admin password (services/admin/auth.py's bcrypt-hashed
password_hash), there is no low-entropy/dictionary-guessing risk to
defend against, so a slow, deliberately-expensive hash (bcrypt/argon2)
buys nothing here and would only add real per-request latency to every
single SMS ingestion call. A plain SHA-256 digest, looked up by exact
equality against a unique-indexed column, is the same trade-off this
codebase already makes for the existing shared MacroDroid token
(services/payments/app.py's hmac.compare_digest check) and for admin
session tokens (services/admin/auth.py's secrets.token_urlsafe(32)) --
high-entropy secrets get a fast hash, low-entropy ones get a slow one.
"""

from __future__ import annotations

import hashlib
import secrets

_TOKEN_BYTES = 32  # 256 bits, matching admin session tokens' own strength


def generate_device_token() -> str:
    """A fresh, unguessable credential for one ingestion device. Returned
    to the caller exactly once -- only its hash (see hash_device_token
    below) is ever persisted.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
