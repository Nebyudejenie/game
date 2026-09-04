"""Unit tests for services/payments/telebirr_parser.py -- pure, no I/O.

The two REAL_SAMPLE_* constants below are verbatim real Telebirr "money
received" SMS text the user provided 2026-09-04 (CTO directive sections
94/95). Every other test derives a variation from these two real anchors
(whitespace, line breaks, Unicode whitespace, extra trailing text) rather
than inventing a new template from scratch -- per section 94/95, a parser
must be proven against real evidence, and a fabricated template proves
nothing about the real one.
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from services.payments.telebirr_parser import (
    ParseFailure,
    ParsedEvidence,
    parse_telebirr_sms,
)

ADDIS = ZoneInfo("Africa/Addis_Ababa")

REAL_SAMPLE_1 = (
    "Dear Nebyu \n"
    "You have received ETB 10.00 from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. "
    "Your transaction number is DI41FHSD4J. Your current E-Money Account balance is ETB 252.12.\n"
    "Thank you for using telebirr\n"
    "Ethio telecom"
)

REAL_SAMPLE_2 = (
    "Dear Nebyu \n"
    "You have received ETB 100.00 from Kertina Gizachew(2519****4271)  on 04/09/2026 10:51:16. "
    "Your transaction number is DI40FIN2FW. Your current E-Money Account balance is ETB 353.12.\n"
    "Thank you for using telebirr\n"
    "Ethio telecom"
)


def test_real_sample_1_parses_every_field_correctly():
    result = parse_telebirr_sms(REAL_SAMPLE_1)
    assert result == ParsedEvidence(
        external_reference="DI41FHSD4J",
        raw_reference="DI41FHSD4J",
        amount=Decimal("10.00"),
        fee=None,
        vat=None,
        payer_name="DAWIT WERKALEMAHU",
        payer_phone="2519****6294",
        recipient_name="Nebyu",
        recipient_phone=None,
        receipt_url=None,
        transaction_at=datetime(2026, 9, 4, 10, 27, 23, tzinfo=ADDIS),
        direction="received",
    )


def test_real_sample_2_parses_a_differently_cased_payer_name():
    # Sample 1's payer name is ALL CAPS, sample 2's is Title Case -- proves
    # casing is never assumed, only whitespace/position.
    result = parse_telebirr_sms(REAL_SAMPLE_2)
    assert result == ParsedEvidence(
        external_reference="DI40FIN2FW",
        raw_reference="DI40FIN2FW",
        amount=Decimal("100.00"),
        fee=None,
        vat=None,
        payer_name="Kertina Gizachew",
        payer_phone="2519****4271",
        recipient_name="Nebyu",
        recipient_phone=None,
        receipt_url=None,
        transaction_at=datetime(2026, 9, 4, 10, 51, 16, tzinfo=ADDIS),
        direction="received",
    )


def test_amount_is_the_received_amount_not_the_trailing_balance():
    # Both real samples contain a SECOND "ETB <number>" for the post-
    # transaction account balance (252.12 / 353.12) -- a naive first-match
    # search would still happen to get this right by position, but the
    # real extraction is anchored on "received" specifically so a future
    # template reordering these two mentions can't silently misfire.
    result = parse_telebirr_sms(REAL_SAMPLE_1)
    assert isinstance(result, ParsedEvidence)
    assert result.amount == Decimal("10.00")


def test_reference_is_normalized_uppercase_and_raw_is_preserved():
    lowercase_ref = REAL_SAMPLE_1.replace("DI41FHSD4J", "di41fhsd4j")
    result = parse_telebirr_sms(lowercase_ref)
    assert isinstance(result, ParsedEvidence)
    assert result.external_reference == "DI41FHSD4J"
    assert result.raw_reference == "di41fhsd4j"


def test_tolerates_extra_internal_whitespace():
    # The real samples already have a double space before "on" -- push it
    # further (extra spaces, a tab) to prove this isn't accidental.
    noisy = REAL_SAMPLE_1.replace("  on", "   \t  on").replace("ETB 10.00", "ETB   10.00")
    result = parse_telebirr_sms(noisy)
    assert isinstance(result, ParsedEvidence)
    assert result.amount == Decimal("10.00")


def test_tolerates_unicode_non_breaking_space():
    noisy = REAL_SAMPLE_1.replace(" from", " from")
    result = parse_telebirr_sms(noisy)
    assert isinstance(result, ParsedEvidence)
    assert result.payer_name == "DAWIT WERKALEMAHU"


def test_tolerates_crlf_line_endings():
    crlf = REAL_SAMPLE_1.replace("\n", "\r\n")
    result = parse_telebirr_sms(crlf)
    assert isinstance(result, ParsedEvidence)
    assert result.recipient_name == "Nebyu"


def test_tolerates_extra_leading_and_trailing_whitespace_on_whole_message():
    padded = "   \n\n" + REAL_SAMPLE_1 + "\n\n   "
    result = parse_telebirr_sms(padded)
    assert isinstance(result, ParsedEvidence)


def test_tolerates_additional_trailing_sms_text():
    # A real forwarding app (or a player's own phone) could append extra
    # text (a footer, a signature, an unrelated second line) -- the parser
    # must not choke on text after the fields it needs.
    extended = REAL_SAMPLE_1 + "\nReply STOP to unsubscribe from promotions."
    result = parse_telebirr_sms(extended)
    assert isinstance(result, ParsedEvidence)


def test_multi_word_ethiopian_name_with_three_parts():
    three_part = REAL_SAMPLE_1.replace(
        "DAWIT WERKALEMAHU(2519****6294)", "ABEBE KEBEDE TESFAYE(2519****1234)"
    )
    result = parse_telebirr_sms(three_part)
    assert isinstance(result, ParsedEvidence)
    assert result.payer_name == "ABEBE KEBEDE TESFAYE"
    assert result.payer_phone == "2519****1234"


def test_different_recipient_greeting_name():
    other_recipient = REAL_SAMPLE_1.replace("Dear Nebyu", "Dear Almaz")
    result = parse_telebirr_sms(other_recipient)
    assert isinstance(result, ParsedEvidence)
    assert result.recipient_name == "Almaz"


# --- the "transferred" template: real sample from the CTO directive -------
# (2026-09-04) -- lands on the PAYER's own phone, not the recipient's, so
# the greeting names the payer and the "to X (phone)" clause names the
# recipient. This is the reverse of the "received" template above, and
# getting the mapping right is safety-critical (section 13's "SMS
# direction check"): if the greeting were ever mistakenly read as the
# recipient here, a player forwarding their own outgoing payment to some
# unrelated third party could slip past recipient validation.

REAL_SAMPLE_TRANSFERRED = (
    "Dear Nebyu You have transferred ETB 20.00 to SURAFEL DESALEGNE (2519****0917) "
    "on 02/09/2026 07:32:00. Your transaction number is DI26D9N4AW. "
    "The service fee is ETB 0.87 and 15% VAT on the service fee is ETB 0.13. "
    "Your current E-Money Account balance is ETB 385.12. "
    "To download your payment information please click this link: "
    "https://transactioninfo.ethiotelecom.et/receipt/DI26D9N4AW. "
    "Thank you for using telebirr Ethio telecom"
)


def test_real_transferred_sample_parses_every_field_correctly():
    result = parse_telebirr_sms(REAL_SAMPLE_TRANSFERRED)
    assert result == ParsedEvidence(
        external_reference="DI26D9N4AW",
        raw_reference="DI26D9N4AW",
        amount=Decimal("20.00"),
        fee=Decimal("0.87"),
        vat=Decimal("0.13"),
        payer_name="Nebyu",
        payer_phone=None,
        recipient_name="SURAFEL DESALEGNE",
        recipient_phone="2519****0917",
        receipt_url="https://transactioninfo.ethiotelecom.et/receipt/DI26D9N4AW",
        transaction_at=datetime(2026, 9, 2, 7, 32, 0, tzinfo=ADDIS),
        direction="transferred",
    )


def test_transferred_greeting_is_the_payer_never_the_recipient():
    # The exact safety property section 13 asks for: the greeting names
    # whoever's phone the SMS landed on (the payer, for this template),
    # never the recipient -- recipient identity always comes from the
    # "to X (phone)" clause instead.
    result = parse_telebirr_sms(REAL_SAMPLE_TRANSFERRED)
    assert isinstance(result, ParsedEvidence)
    assert result.payer_name == "Nebyu"
    assert result.recipient_name == "SURAFEL DESALEGNE"
    assert result.recipient_name != result.payer_name


def test_transferred_to_an_unrelated_third_party_still_parses_but_names_them():
    # A player forwarding their own "transferred to someone else entirely"
    # SMS must still parse cleanly (the parser's job is only extraction),
    # but the extracted recipient is that unrelated person -- rejection is
    # telebirr_ingest.py's _find_matching_recipient()'s job, proven in
    # tests/integration/test_telebirr_ingest.py, not this module's.
    unrelated = REAL_SAMPLE_TRANSFERRED.replace(
        "SURAFEL DESALEGNE (2519****0917)", "RANDOM UNRELATED PERSON (2519****9999)"
    )
    result = parse_telebirr_sms(unrelated)
    assert isinstance(result, ParsedEvidence)
    assert result.recipient_name == "RANDOM UNRELATED PERSON"
    assert result.recipient_phone == "2519****9999"


def test_transferred_amount_is_not_confused_with_fee_vat_or_balance():
    # Four separate ETB amounts appear in this one real message (20.00
    # transferred, 0.87 fee, 0.13 VAT, 385.12 balance) -- each anchored
    # regex must find its own, never accidentally cross-match another.
    result = parse_telebirr_sms(REAL_SAMPLE_TRANSFERRED)
    assert isinstance(result, ParsedEvidence)
    assert result.amount == Decimal("20.00")
    assert result.fee == Decimal("0.87")
    assert result.vat == Decimal("0.13")


def test_transferred_without_fee_or_vat_mentioned_leaves_them_none():
    no_fee = REAL_SAMPLE_TRANSFERRED.replace(
        "The service fee is ETB 0.87 and 15% VAT on the service fee is ETB 0.13. ", ""
    )
    result = parse_telebirr_sms(no_fee)
    assert isinstance(result, ParsedEvidence)
    assert result.fee is None
    assert result.vat is None


def test_transferred_without_a_receipt_url_leaves_it_none_not_a_failure():
    no_url = REAL_SAMPLE_TRANSFERRED.replace(
        "To download your payment information please click this link: "
        "https://transactioninfo.ethiotelecom.et/receipt/DI26D9N4AW. ",
        "",
    )
    result = parse_telebirr_sms(no_url)
    assert isinstance(result, ParsedEvidence)
    assert result.receipt_url is None


def test_transferred_receipt_url_with_untrusted_domain_is_rejected():
    # A receipt URL that IS present but points somewhere other than Ethio
    # Telecom's own domain is a tamper signal, not something to silently
    # accept or silently drop -- the whole message fails closed.
    spoofed = REAL_SAMPLE_TRANSFERRED.replace("transactioninfo.ethiotelecom.et", "evil-phish.example")
    result = parse_telebirr_sms(spoofed)
    assert isinstance(result, ParseFailure)
    assert result.reason == "receipt_url_untrusted_domain"


def test_transferred_rejects_missing_recipient():
    broken = REAL_SAMPLE_TRANSFERRED.replace("to SURAFEL DESALEGNE (2519****0917) ", "")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "recipient_not_found"


def test_transferred_rejects_missing_reference():
    broken = REAL_SAMPLE_TRANSFERRED.replace(
        "Your transaction number is DI26D9N4AW.", "Your transaction is complete."
    )
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "reference_not_found"


def test_a_message_with_neither_received_nor_transferred_is_rejected():
    result = parse_telebirr_sms(
        "Dear Nebyu your balance is ETB 100.00. Your transaction number is DI26D9N4AW."
    )
    assert isinstance(result, ParseFailure)
    assert result.reason == "unrecognized_template"


def test_a_message_claiming_both_received_and_transferred_is_rejected():
    # Deliberately ambiguous/malformed input -- never guess which
    # template applies when the anchor phrases for both are present.
    both = REAL_SAMPLE_TRANSFERRED + " You have received ETB 5.00 from Someone(2519****0000) on 01/01/2026 00:00:00."
    result = parse_telebirr_sms(both)
    assert isinstance(result, ParseFailure)
    assert result.reason == "unrecognized_template"


# --- fail-closed: every required field, independently ----------------------


def test_rejects_missing_amount():
    broken = REAL_SAMPLE_1.replace("You have received ETB 10.00 from", "You have received from")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "amount_not_found"


def test_rejects_zero_amount():
    broken = REAL_SAMPLE_1.replace("ETB 10.00 from", "ETB 0.00 from")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "amount_not_positive"


def test_rejects_missing_sender():
    broken = REAL_SAMPLE_1.replace("from DAWIT WERKALEMAHU(2519****6294)", "")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "sender_not_found"


def test_rejects_malformed_masked_phone():
    # Missing one digit from the masked phone shape -- must not loosely
    # accept a near-miss.
    broken = REAL_SAMPLE_1.replace("(2519****6294)", "(2519****629)")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "sender_not_found"


def test_rejects_missing_transaction_reference():
    broken = REAL_SAMPLE_1.replace(
        "Your transaction number is DI41FHSD4J.", "Your transaction is being processed."
    )
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "reference_not_found"


def test_rejects_missing_recipient_greeting():
    broken = REAL_SAMPLE_1.replace("Dear Nebyu \n", "")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "recipient_greeting_not_found"


def test_rejects_missing_datetime():
    broken = REAL_SAMPLE_1.replace("on 04/09/2026 10:27:23.", ".")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "transaction_datetime_not_found"


def test_rejects_impossible_date():
    broken = REAL_SAMPLE_1.replace("04/09/2026", "32/13/2026")
    result = parse_telebirr_sms(broken)
    assert isinstance(result, ParseFailure)
    assert result.reason == "transaction_datetime_invalid"


def test_rejects_completely_unrelated_text():
    result = parse_telebirr_sms("Your OTP code is 483920. Do not share it with anyone.")
    assert isinstance(result, ParseFailure)


def test_rejects_empty_string():
    result = parse_telebirr_sms("")
    assert isinstance(result, ParseFailure)


def test_never_guesses_a_reference_from_an_ambiguous_message():
    # A message that mentions a plausible-looking alphanumeric code but
    # not through the real "Your transaction number is" anchor must never
    # be treated as if it were the reference -- this is the concrete
    # "do not infer from previous transactions / do not guess" case.
    result = parse_telebirr_sms(
        "Dear Nebyu\nYour account DI41FHSD4J was accessed from a new device."
    )
    assert isinstance(result, ParseFailure)
