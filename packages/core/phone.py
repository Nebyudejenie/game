"""Ethiopian phone number normalization to E.164.

The one canonical normalization path for the whole codebase -- originally
lived in services/bot/phone.py (only ever called there, on a number that
arrived via Telegram's own verified contact-share mechanism), then grew
two more callers in services/admin (phone search) that already imported
it cross-service. Promoted here, to packages/core, when the SMS Control
Plane's CSV importer needed the identical normalization for free-typed
phone numbers -- a second real implementation was never on the table
(the enterprise SMS directive is explicit: "do not duplicate
phone-normalization logic across multiple services").
"""

from __future__ import annotations

import re

_DIGITS_ONLY = re.compile(r"\D+")


def normalize_ethiopian_phone(raw: str) -> str | None:
    """Returns a `+2519XXXXXXXX` / `+2517XXXXXXXX` E.164 string, or None if
    `raw` isn't a recognizable Ethiopian mobile number. Accepts the forms
    Telegram contacts, manual admin entry, and free-typed CSV cells
    commonly produce: `+251912345678`, `251912345678`, `0912345678`, with
    arbitrary spaces/dashes.
    """
    digits = _DIGITS_ONLY.sub("", raw)

    if digits.startswith("251"):
        national = digits[3:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    if len(national) != 9 or national[0] not in ("7", "9"):
        return None

    return f"+251{national}"
