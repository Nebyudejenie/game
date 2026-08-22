import json
from pathlib import Path

import pytest

from services.bot import i18n

LOCALES_DIR = Path(__file__).parent.parent.parent / "services" / "bot" / "locales"


def test_am_and_en_have_matching_key_sets():
    am_keys = set(json.loads((LOCALES_DIR / "am.json").read_text(encoding="utf-8")))
    en_keys = set(json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8")))
    assert am_keys == en_keys, (
        f"am/en key sets differ: only in am={am_keys - en_keys}, only in en={en_keys - am_keys}"
    )


def test_default_language_is_amharic():
    assert i18n.DEFAULT_LANGUAGE == "am"
    assert i18n.resolve_language(None) == "am"


def test_unsupported_language_falls_back_to_default():
    assert i18n.resolve_language("fr") == "am"


def test_lookup_in_amharic():
    assert i18n.t("register.prompt", "am") == "ለመመዝገብ ስልክ ቁጥርዎን ያጋሩ።"


def test_lookup_in_english():
    assert i18n.t("register.use_button", "en") == "Please use the 'Share Phone Number' button."


def test_formatting_placeholders():
    result = i18n.t("register.success", "en", name="Nebyu")
    assert "Nebyu" in result


def test_om_falls_back_to_english_for_missing_keys():
    # om.json is a deliberate stub -- most keys aren't there yet.
    assert i18n.t("register.prompt", "om") == i18n.t("register.prompt", "en")


def test_om_has_its_own_value_for_keys_it_does_define():
    value = i18n.t("language.set", "om")
    assert value != i18n.t("language.set", "en")


def test_missing_key_raises():
    with pytest.raises(KeyError):
        i18n.t("this.key.does.not.exist", "en")


def test_all_keys_returns_amharic_key_set():
    keys = i18n.all_keys()
    assert "register.prompt" in keys
    assert "wallet.insufficient" in keys
