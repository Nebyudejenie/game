"""Phone number encryption at rest (spec section 9.2: "PII: phone numbers
encrypted at rest; logs must never contain full numbers or `initData`
strings" -- the logging half is packages/core/logging.py's `_redact`).

Two things a phone number needs to do in this schema that a single opaque
ciphertext can't do on its own:

- **Uniqueness** (one phone, one account -- a real anti-multi-accounting
  control, not a formality) and **exact-match lookup** (registration's
  "does this phone already have an account" check, admin's "search by
  phone"). AES-GCM's random nonce means the same phone number encrypts to
  different ciphertext every time, so equality can't be checked in SQL
  against the ciphertext column directly.
- **Confidentiality at rest** -- a stolen database dump must not read
  phone numbers in clear.

So every phone number is stored as two derived values instead of one
plaintext column: `encrypt_phone()` (AES-256-GCM, random nonce, decryptable
only with the key -- confidentiality) and `phone_lookup_hash()` (a
deterministic HMAC-SHA256 -- the same phone always hashes the same way, so
it can carry the UNIQUE constraint and power exact-match lookups, without
which the ciphertext leaks nothing about equality either). This is the
standard "blind index" pattern for encrypted-but-searchable columns.

Both derived from one `PHONE_ENCRYPTION_KEY` root secret via HKDF, with
distinct `info` labels for real domain separation -- a key good enough to
decrypt phone numbers must never also be reusable to compute the lookup
hash (or vice versa) by accident.

**Product decision, not an engineering default:** admin's phone search
went from substring (`ILIKE '%...%'`) to exact-match only, since a blind
index fundamentally cannot support partial-match search against
ciphertext -- confirmed with the user rather than picked unilaterally
(see DECISIONS.md).
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from packages.core.config import get_settings

_NONCE_LENGTH = 12
_ENCRYPTION_INFO = b"jobingo-phone-encryption-v1"
_LOOKUP_INFO = b"jobingo-phone-lookup-v1"


class PhoneEncryptionNotConfigured(RuntimeError):
    pass


def _root_key() -> bytes:
    hex_key = get_settings().phone_encryption_key
    if not hex_key:
        raise PhoneEncryptionNotConfigured(
            "PHONE_ENCRYPTION_KEY is not set -- phone numbers cannot be "
            "stored or read without it. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    try:
        key = bytes.fromhex(hex_key)
    except ValueError as exc:
        raise PhoneEncryptionNotConfigured("PHONE_ENCRYPTION_KEY must be 64 hex characters") from exc
    if len(key) != 32:
        raise PhoneEncryptionNotConfigured("PHONE_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def _derive_key(info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(_root_key())


def encrypt_phone(plaintext_e164: str) -> bytes:
    """nonce || ciphertext+tag. A fresh random nonce every call, so this is
    never comparable for equality -- see phone_lookup_hash() for that.
    """
    nonce = os.urandom(_NONCE_LENGTH)
    ciphertext = AESGCM(_derive_key(_ENCRYPTION_INFO)).encrypt(nonce, plaintext_e164.encode(), None)
    return nonce + ciphertext


def decrypt_phone(blob: bytes) -> str:
    nonce, ciphertext = blob[:_NONCE_LENGTH], blob[_NONCE_LENGTH:]
    return AESGCM(_derive_key(_ENCRYPTION_INFO)).decrypt(nonce, ciphertext, None).decode()


def phone_lookup_hash(plaintext_e164: str) -> str:
    """Deterministic -- the same E.164 string always produces the same
    hash, which is exactly what makes this safe to put a UNIQUE index on
    and query with `WHERE phone_lookup_hash = $1` without ever comparing
    ciphertext. Callers must pass an already-normalized E.164 string (see
    services/bot/phone.py) -- this function does no normalization of its
    own, so "+251912345678" and "0912345678" hash differently even though
    they're the same number.
    """
    return hmac.new(_derive_key(_LOOKUP_INFO), plaintext_e164.encode(), hashlib.sha256).hexdigest()
