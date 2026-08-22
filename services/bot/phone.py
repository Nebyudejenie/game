"""Ethiopian phone number normalization to E.164.

Only ever called on a number that arrived via Telegram's own contact-share
mechanism (a `Contact` object, verified to belong to the sender) -- never on
free-typed text. See registration.py for why that distinction matters.
"""

from __future__ import annotations

import re

_DIGITS_ONLY = re.compile(r"\D+")


def normalize_ethiopian_phone(raw: str) -> str | None:
    """Returns a `+2519XXXXXXXX` / `+2517XXXXXXXX` E.164 string, or None if
    `raw` isn't a recognizable Ethiopian mobile number. Accepts the forms
    Telegram contacts and manual entry commonly produce: `+251912345678`,
    `251912345678`, `0912345678`, with arbitrary spaces/dashes.
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
