"""i18n loader. Every user-facing string in the bot goes through this --
there is no hardcoded string in any handler. Amharic is the default
language; `om` and `ti` are stubbed and fall back to English, then Amharic,
for any key they don't carry yet (spec section 7.5).
"""

from __future__ import annotations

import json
import string
from functools import lru_cache
from pathlib import Path

SUPPORTED_LANGUAGES = ("am", "en", "om", "ti")
DEFAULT_LANGUAGE = "am"
FALLBACK_LANGUAGE = "en"

_LOCALES_DIR = Path(__file__).parent / "locales"

# Admin-editable overrides on top of the file-based defaults below (the
# Bot Content admin screen, services/admin/queries.py's bot_content_*
# functions + services/bot/bot_content_sync.py's periodic refresh loop).
# A plain module-level dict, not a DB call from inside t() itself: t() is
# called synchronously from dozens of existing call sites across the bot
# codebase (handlers, keyboards, the notification relay), and none of
# them are set up to await anything -- making t() async would mean
# rewriting every one of those call sites for a feature that only
# changes rarely. The bot process refreshes this cache from Postgres on
# a short interval instead (see bot_content_sync.py); tests that exercise
# an override call set_overrides() directly and must reset it afterward
# (this dict is shared, mutable, per-process state -- see this module's
# own test file for the reset fixture that keeps that from leaking
# between tests).
_overrides: dict[tuple[str, str], str] = {}


def set_overrides(overrides: dict[tuple[str, str], str]) -> None:
    global _overrides
    _overrides = overrides


@lru_cache
def _load(language: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{language}.json"
    if not path.exists():
        return {}
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return data


def resolve_language(language: str | None) -> str:
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def default_template(key: str, language: str | None = DEFAULT_LANGUAGE) -> str | None:
    """The file-based value t() would use if no admin override existed --
    same fallback chain (own language -> English -> Amharic) t() itself
    follows. Bypasses _overrides on purpose: this is "what ships in the
    repo," the baseline the Bot Content admin screen shows an admin
    editing away from, and what a "reset to default" click restores.
    """
    lang = resolve_language(language)
    return (
        _load(lang).get(key)
        or _load(FALLBACK_LANGUAGE).get(key)
        or _load(DEFAULT_LANGUAGE).get(key)
    )


def required_placeholders(template: str) -> frozenset[str]:
    """The {name} fields a template's own .format(**kwargs) call actually
    needs -- used by the Bot Content admin screen to reject an edited
    value that drops or adds a placeholder, which would otherwise only
    surface as a live KeyError/format crash the next time a real player
    hits that code path.
    """
    return frozenset(
        name for _, name, _, _ in string.Formatter().parse(template) if name
    )


def t(key: str, language: str | None = DEFAULT_LANGUAGE, **kwargs: object) -> str:
    lang = resolve_language(language)
    template = _overrides.get((key, lang)) or default_template(key, lang)
    if template is None:
        raise KeyError(f"missing i18n key: {key!r}")
    return template.format(**kwargs) if kwargs else template


def all_keys() -> frozenset[str]:
    """The canonical key set, defined by Amharic (the ship-complete
    default). Used by tests to verify en.json has no gaps.
    """
    return frozenset(_load(DEFAULT_LANGUAGE).keys())
