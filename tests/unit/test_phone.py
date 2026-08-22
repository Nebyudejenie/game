from services.bot.phone import normalize_ethiopian_phone


def test_plus_251_format_accepted():
    assert normalize_ethiopian_phone("+251912345678") == "+251912345678"


def test_bare_251_format_accepted():
    assert normalize_ethiopian_phone("251912345678") == "+251912345678"


def test_local_zero_prefixed_format_accepted():
    assert normalize_ethiopian_phone("0912345678") == "+251912345678"


def test_seven_prefix_mobile_accepted():
    assert normalize_ethiopian_phone("0712345678") == "+251712345678"


def test_spaces_and_dashes_are_stripped():
    assert normalize_ethiopian_phone("+251 91-234-5678") == "+251912345678"


def test_wrong_length_rejected():
    assert normalize_ethiopian_phone("09123456") is None
    assert normalize_ethiopian_phone("091234567890") is None


def test_non_mobile_prefix_rejected():
    assert normalize_ethiopian_phone("0512345678") is None


def test_garbage_input_rejected():
    assert normalize_ethiopian_phone("not a phone number") is None
    assert normalize_ethiopian_phone("") is None
