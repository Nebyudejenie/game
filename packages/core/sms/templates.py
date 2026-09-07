"""Template rendering and real (not template-only) per-recipient message
intelligence: variable extraction, `{{var}}` rendering, and a real GSM-7 /
UCS-2 encoding + segment-count calculation -- the directive is explicit
that segment counts must reflect the actual rendered text, not a rough
guess against the template body alone (a template with a variable
substituted for a longer real value can cross a segment boundary the
template's own raw length never would).
"""

from __future__ import annotations

import re

VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# GSM 03.38 default alphabet (the basic set; the handful of extension
# characters that cost 2 septets each are deliberately not modelled here --
# an incorrect-but-safe overcount for those rare characters is the correct
# direction to be wrong in for a segment *count*, never an undercount).
_GSM7_BASIC_SET = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

SEGMENT_LIMIT_GSM7_SINGLE = 160
SEGMENT_LIMIT_GSM7_MULTI = 153
SEGMENT_LIMIT_UCS2_SINGLE = 70
SEGMENT_LIMIT_UCS2_MULTI = 67


class MissingVariables(Exception):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"missing template variables: {missing}")


def extract_variables(body: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in VARIABLE_PATTERN.finditer(body):
        seen.setdefault(match.group(1), None)
    return list(seen)


def render_template(body: str, variables: dict[str, str]) -> str:
    """Renders `{{var}}` placeholders. Raises MissingVariables (never
    silently leaves a raw placeholder or an empty string in production
    message text) when the template references a variable this call
    wasn't given a value for.
    """
    required = extract_variables(body)
    missing = [name for name in required if name not in variables]
    if missing:
        raise MissingVariables(missing)

    def _sub(match: re.Match[str]) -> str:
        return variables[match.group(1)]

    return VARIABLE_PATTERN.sub(_sub, body)


def is_gsm7(text: str) -> bool:
    return all(ch in _GSM7_BASIC_SET for ch in text)


def count_segments(text: str) -> int:
    """Real per-message segment count, matching carrier billing math: a
    message fitting in one segment uses that alphabet's single-segment
    limit; anything longer is split at the (smaller) multi-segment-per-part
    limit, per SMS's own concatenation overhead.
    """
    if not text:
        return 1
    length = len(text)
    if is_gsm7(text):
        if length <= SEGMENT_LIMIT_GSM7_SINGLE:
            return 1
        return -(-length // SEGMENT_LIMIT_GSM7_MULTI)  # ceil div
    if length <= SEGMENT_LIMIT_UCS2_SINGLE:
        return 1
    return -(-length // SEGMENT_LIMIT_UCS2_MULTI)
