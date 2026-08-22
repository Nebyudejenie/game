"""i18n loader. Every user-facing string in the bot goes through this --
there is no hardcoded string in any handler. Amharic is the default
language; `om` and `ti` are stubbed and fall back to English, then Amharic,
for any key they don't carry yet (spec section 7.5).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SUPPORTED_LANGUAGES = ("am", "en", "om", "ti")
DEFAULT_LANGUAGE = "am"
FALLBACK_LANGUAGE = "en"

_LOCALES_DIR = Path(__file__).parent / "locales"


@lru_cache
def _load(language: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{language}.json"
    if not path.exists():
        return {}
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return data


def resolve_language(language: str | None) -> str:
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def t(key: str, language: str | None = DEFAULT_LANGUAGE, **kwargs: object) -> str:
    lang = resolve_language(language)
    template = (
        _load(lang).get(key)
        or _load(FALLBACK_LANGUAGE).get(key)
        or _load(DEFAULT_LANGUAGE).get(key)
    )
    if template is None:
        raise KeyError(f"missing i18n key: {key!r}")
    return template.format(**kwargs) if kwargs else template


def all_keys() -> frozenset[str]:
    """The canonical key set, defined by Amharic (the ship-complete
    default). Used by tests to verify en.json has no gaps.
    """
    return frozenset(_load(DEFAULT_LANGUAGE).keys())
