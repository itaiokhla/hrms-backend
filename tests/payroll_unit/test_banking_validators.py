"""Bank account format checks (3.7 §2.3), verification rules (3.7 §4) and anti-diversion controls (3.7 §3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.payroll.banking import controls
from app.payroll.banking.errors import BankValidationError
from app.payroll.banking.validators import (
    IBAN_LENGTHS,
    SEPA_COUNTRIES,
    BankAccountDetails,
    DirectoryEntry,
    aba_checksum_ok,
    account_identity,
    iban_checksum_ok,
    is_valid_aba,
    is_valid_bic,
    is_valid_iban,
    is_valid_ifsc,
    make_iban,
    rule_set,
    validate_account,
)

AE_IBAN = "AE070331234567890123456"
SA_IBAN = "SA0380000000608010167519"
GB_IBAN = "GB82WEST12345698765432"
DE_IBAN = "DE89370400440532013000"


def _in(**overrides: object) -> BankAccountDetails:
    data: dict[str, object] = {
        "country": "IN",
        "holder_name": "Asha Rao",
        "account_number": "50100123456789",
        "routing": {"ifsc": "HDFC0001234"},
        "account_type": "SAVINGS",
    }
    data.update(overrides)
    return BankAccountDetails(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- India


def test_india_valid_account_passes_format_only() -> None:
    result = validate_account(_in())
    assert result.valid
    assert result.rule_set == "IN"
    assert result.method == "FORMAT"
    assert {"HOLDER_NAME", "ACCOUNT_NUMBER_FORMAT", "IFSC_FORMAT"} <= set(result.checks)
    assert "DIRECTORY_NOT_CHECKED" in result.warning_codes()


def test_india_normalises_spaces_and_case() -> None:
    result = validate_account(_in(account_number=" 5010 0123 4567 89 ", routing={"IFSC": " hdfc0001234 "}, account_type="savings"))
    assert result.valid
    assert result.account.account_number == "50100123456789"
    assert result.account.routing["ifsc"] == "HDFC0001234"
    assert result.account.account_type == "SAVINGS"


@pytest.mark.parametrize("ifsc", ["HDFC1001234", "HDF00001234", "HDFC000123", "HDFC00012345", "HDFC0_01234"])
def test_india_bad_ifsc_rejected(ifsc: str) -> None:
    result = validate_account(_in(routing={"ifsc": ifsc}))
    assert "IFSC_FORMAT" in result.error_codes()


@pytest.mark.parametrize("number,ok", [("12345678", False), ("123456789", True), ("1" * 18, True), ("1" * 19, False), ("0000000000", False)])
def test_india_account_number_9_to_18_digits(number: str, ok: bool) -> None:
    assert validate_account(_in(account_number=number)).valid is ok


def test_india_requires_holder_ifsc_account_and_type() -> None:
    result = validate_account(BankAccountDetails(country="IN"))
    fields = {issue.field for issue in result.errors if issue.code == "REQUIRED"}
    assert fields == {"holder_name", "account_number", "routing.ifsc", "account_type"}


def test_india_account_type_must_be_savings_or_current() -> None:
    assert "ACCOUNT_TYPE_INVALID" in validate_account(_in(account_type="CHECKING")).error_codes()


def test_ifsc_helper() -> None:
    assert is_valid_ifsc("sbin0005943")
    assert not is_valid_ifsc("SBIN1005943")


# --------------------------------------------------------------------------- UAE, Saudi Arabia, SEPA


def test_uae_iban_valid() -> None:
    result = validate_account(BankAccountDetails(country="AE", holder_name="Omar Ali", iban="ae07 0331 2345 6789 0123 456"))
    assert result.valid
    assert result.account.iban == AE_IBAN
    assert {"IBAN_LENGTH", "IBAN_CHECKSUM"} <= set(result.checks)


def test_uae_wps_requires_agent_routing_code() -> None:
    details = BankAccountDetails(country="AE", holder_name="Omar Ali", iban=AE_IBAN)
    assert validate_account(details).valid
    missing = validate_account(details, wps=True)
    assert [(i.field, i.code) for i in missing.errors] == [("routing.wps_routing_code", "REQUIRED")]
    good = BankAccountDetails(country="AE", holder_name="Omar Ali", iban=AE_IBAN, routing={"wps_routing_code": "803320101"})
    assert validate_account(good, wps=True).valid
    bad = BankAccountDetails(country="AE", holder_name="Omar Ali", iban=AE_IBAN, routing={"wps_routing_code": "80332010"})
    assert "WPS_ROUTING_FORMAT" in validate_account(bad, wps=True).error_codes()


def test_uae_iban_wrong_length_and_prefix() -> None:
    assert "IBAN_LENGTH" in validate_account(BankAccountDetails(country="AE", holder_name="O A", iban=AE_IBAN + "1")).error_codes()
    result = validate_account(BankAccountDetails(country="AE", holder_name="O A", iban=SA_IBAN))
    assert "IBAN_COUNTRY_MISMATCH" in result.error_codes()


def test_saudi_iban_24() -> None:
    assert validate_account(BankAccountDetails(country="SA", holder_name="Fahad", iban=SA_IBAN)).valid
    assert IBAN_LENGTHS["SA"] == 24
    assert "REQUIRED" in validate_account(BankAccountDetails(country="SA", holder_name="Fahad")).error_codes()


def test_sepa_iban_with_optional_bic() -> None:
    assert rule_set("DE") == "SEPA"
    plain = validate_account(BankAccountDetails(country="DE", holder_name="Jan Weber", iban=DE_IBAN))
    assert plain.valid
    with_bic = validate_account(BankAccountDetails(country="DE", holder_name="Jan Weber", iban=DE_IBAN, routing={"bic": "COBADEFFXXX"}))
    assert with_bic.valid and "BIC_FORMAT" in with_bic.checks


def test_iban_registry_covers_all_sepa_prefixes() -> None:
    from app.payroll.banking.validators import iban_country

    for country in SEPA_COUNTRIES:
        assert iban_country(country) in IBAN_LENGTHS, country


@pytest.mark.parametrize(
    "iban",
    [
        "DE89370400440532013000",
        "FR1420041010050500013M02606",
        "NL91ABNA0417164300",
        "NO9386011117947",
        "BE68539007547034",
        "MT84MALT011000012345MTLCAST001S",
        "GB82WEST12345698765432",
        "SA0380000000608010167519",
        "AE070331234567890123456",
    ],
)
def test_published_example_ibans_validate(iban: str) -> None:
    assert is_valid_iban(iban)


@given(st.sampled_from(sorted(IBAN_LENGTHS)), st.data())
def test_make_iban_round_trips_for_every_registry_country(country: str, data: st.DataObject) -> None:
    length = IBAN_LENGTHS[country] - 4
    bban = data.draw(st.text(alphabet="0123456789", min_size=length, max_size=length))
    iban = make_iban(country, bban)
    assert len(iban) == IBAN_LENGTHS[country]
    assert iban_checksum_ok(iban)


# --------------------------------------------------------------------------- EC-BNK-04 / EC-BNK-05


def test_ec_bnk_04_ifsc_on_uae_account_rejected() -> None:
    details = BankAccountDetails(country="AE", holder_name="Omar Ali", iban=AE_IBAN, routing={"ifsc": "HDFC0001234"})
    result = validate_account(details)
    assert not result.valid
    assert [(i.field, i.code) for i in result.errors] == [("routing.ifsc", "FIELD_NOT_FOR_COUNTRY")]


def test_ec_bnk_04_sort_code_and_aba_rejected_outside_their_countries() -> None:
    result = validate_account(_in(routing={"ifsc": "HDFC0001234", "sort_code": "123456", "aba": "021000021"}))
    assert {i.field for i in result.errors if i.code == "FIELD_NOT_FOR_COUNTRY"} == {"routing.sort_code", "routing.aba"}


def test_ec_bnk_04_iban_in_country_without_iban_rejected() -> None:
    result = validate_account(_in(iban=DE_IBAN))
    assert "IBAN_NOT_USED_IN_COUNTRY" in result.error_codes()


def test_ec_bnk_04_native_fields_accepted() -> None:
    assert validate_account(_in()).valid
    assert validate_account(BankAccountDetails(country="AE", holder_name="O A", iban=AE_IBAN, routing={"wps_routing_code": "803320101"})).valid


def test_ec_bnk_04_unknown_routing_field_rejected() -> None:
    assert "ROUTING_FIELD_UNKNOWN" in validate_account(_in(routing={"ifsc": "HDFC0001234", "swift_code": "X"})).error_codes()


def test_ec_bnk_05_iban_checksum_failure_rejected() -> None:
    broken = AE_IBAN[:-1] + ("7" if AE_IBAN[-1] != "7" else "8")
    result = validate_account(BankAccountDetails(country="AE", holder_name="Omar Ali", iban=broken))
    assert result.error_codes() == ("IBAN_CHECKSUM",)
    assert not is_valid_iban(broken)


def test_ec_bnk_05_transposed_digits_detected_valid_iban_accepted() -> None:
    swapped = DE_IBAN[:12] + DE_IBAN[13] + DE_IBAN[12] + DE_IBAN[14:]
    assert swapped != DE_IBAN
    assert not is_valid_iban(swapped)
    assert is_valid_iban(DE_IBAN)


def test_ec_bnk_05_check_digits_out_of_range_rejected() -> None:
    assert not is_valid_iban("DE00370400440532013000")
    assert not is_valid_iban("DE99370400440532013000")


def test_messages_never_echo_the_submitted_number() -> None:
    broken = "AE070331234567890123457"
    result = validate_account(BankAccountDetails(country="AE", holder_name="Omar Ali", iban=broken, routing={"ifsc": "HDFC0001234"}))
    for issue in result.errors + result.warnings:
        assert "0331234567890123457" not in issue.message
        assert "HDFC0001234" not in issue.message
    assert "0331234567890123457" not in repr(result.account)


# --------------------------------------------------------------------------- UK and US


def test_gb_valid_with_modulus_warning_never_claimed() -> None:
    result = validate_account(BankAccountDetails(country="GB", holder_name="Ann Lee", account_number="98765432", routing={"sort_code": "12-34-56"}))
    assert result.valid
    assert "UK_MODULUS_NOT_CHECKED" in result.warning_codes()
    assert not any("MODULUS" in check for check in result.checks)


def test_gb_formats_and_iban_consistency() -> None:
    bad = validate_account(BankAccountDetails(country="GB", holder_name="Ann Lee", account_number="9876543", routing={"sort_code": "12345"}))
    assert {"SORT_CODE_FORMAT", "ACCOUNT_NUMBER_FORMAT"} <= set(bad.error_codes())
    matching = BankAccountDetails(country="GB", holder_name="Ann Lee", account_number="98765432", routing={"sort_code": "123456"}, iban=GB_IBAN)
    assert validate_account(matching).valid
    other = BankAccountDetails(country="GB", holder_name="Ann Lee", account_number="11111111", routing={"sort_code": "123456"}, iban=GB_IBAN)
    assert "IBAN_ACCOUNT_MISMATCH" in validate_account(other).error_codes()


@pytest.mark.parametrize("routing", ["011000015", "021000021", "026009593"])
def test_aba_checksum_known_numbers(routing: str) -> None:
    assert aba_checksum_ok(routing)
    assert is_valid_aba(routing)


def test_aba_checksum_formula() -> None:
    # 3(d1+d4+d7) + 7(d2+d5+d8) + (d3+d6+d9) for 021000021 = 0 + 28 + 2 = 30
    assert aba_checksum_ok("021000021")
    assert not aba_checksum_ok("021000022")
    assert not aba_checksum_ok("02100002")
    assert aba_checksum_ok("990000013") and not is_valid_aba("990000013")  # checksum fine, no Federal Reserve prefix 99


def test_us_account_rules() -> None:
    good = BankAccountDetails(country="US", holder_name="Sam Hill", account_number="000123456789", routing={"aba": "021000021"}, account_type="checking")
    assert validate_account(good).valid
    bad = BankAccountDetails(country="US", holder_name="Sam Hill", account_number="123456789", routing={"aba": "021000022"})
    codes = validate_account(bad).error_codes()
    assert "ABA_CHECKSUM" in codes and "REQUIRED" in codes


# --------------------------------------------------------------------------- international, BIC, directory


def test_international_requires_identifier_bic_and_address() -> None:
    assert rule_set("SG") == "INTERNATIONAL"
    empty = validate_account(BankAccountDetails(country="SG", holder_name="Tan Wei"))
    assert {"ACCOUNT_IDENTIFIER_REQUIRED", "REQUIRED"} <= set(empty.error_codes())
    good = BankAccountDetails(
        country="SG", holder_name="Tan Wei", account_number="1234567890", routing={"bic": "DBSSSGSG"}, bank_address="12 Marina Blvd"
    )
    assert validate_account(good).valid


@pytest.mark.parametrize("bic,ok", [("DEUTDEFF", True), ("DEUTDEFF500", True), ("DEUTDEF", False), ("DEUT1EFF", False), ("deutdeff", True)])
def test_bic_pattern(bic: str, ok: bool) -> None:
    assert is_valid_bic(bic) is ok


class _Directory:
    def __init__(self, entries: dict[tuple[str, str], DirectoryEntry]) -> None:
        self.entries = entries

    def lookup(self, scheme: str, code: str) -> DirectoryEntry | None:
        return self.entries.get((scheme, code))


def test_ec_bnk_16_directory_merged_or_invalid_is_a_warning() -> None:
    merged = _Directory({("IFSC", "HDFC0001234"): DirectoryEntry("IFSC", "HDFC0001234", "MERGED", successor="HDFC0009999")})
    result = validate_account(_in(), directory=merged)
    assert result.valid and "DIRECTORY_MERGED" in result.warning_codes()
    invalid = _Directory({("IFSC", "HDFC0001234"): DirectoryEntry("IFSC", "HDFC0001234", "INVALID")})
    assert "DIRECTORY_INVALID" in validate_account(_in(), directory=invalid).warning_codes()


def test_ec_bnk_16_directory_active_records_check() -> None:
    active = _Directory({("IFSC", "HDFC0001234"): DirectoryEntry("IFSC", "HDFC0001234", "ACTIVE", bank_name="HDFC Bank")})
    result = validate_account(_in(), directory=active)
    assert "IFSC_DIRECTORY" in result.checks and not result.warnings
    assert "DIRECTORY_NOT_FOUND" in validate_account(_in(), directory=_Directory({})).warning_codes()


def test_holder_name_rejects_bidi_controls_and_digits_only() -> None:
    assert "HOLDER_NAME_INVALID" in validate_account(_in(holder_name="Asha‮Rao")).error_codes()
    assert "HOLDER_NAME_INVALID" in validate_account(_in(holder_name="12345")).error_codes()


def test_bad_arguments_raise_typed_error() -> None:
    with pytest.raises(BankValidationError) as exc:
        validate_account("IN")  # type: ignore[arg-type]
    assert exc.value.code == "INVALID_ARGUMENT"
    assert "COUNTRY_INVALID" in validate_account(BankAccountDetails(country="India")).error_codes()


def test_account_identity_for_fingerprints() -> None:
    assert account_identity(validate_account(_in()).account) == ("IN", "HDFC", "50100123456789")
    ae = validate_account(BankAccountDetails(country="AE", holder_name="O A", iban=AE_IBAN)).account
    assert account_identity(ae) == ("AE", "", AE_IBAN)


# --------------------------------------------------------------------------- verification and controls (3.7 §3-§4)


def test_format_never_sufficient_for_verified() -> None:
    with pytest.raises(BankValidationError) as exc:
        controls.verification_status({"FORMAT"}, {"FORMAT"})
    assert exc.value.code == "FORMAT_NOT_SUFFICIENT"
    assert controls.verification_status({"FORMAT"}, {"FORMAT", "DOCUMENT_REVIEW"}) == "PENDING"
    assert controls.verification_status({"FORMAT", "DOCUMENT_REVIEW"}, {"FORMAT", "DOCUMENT_REVIEW"}) == "VERIFIED"


def test_verification_failed_review_and_expiry() -> None:
    required = {"PENNY_DROP"}
    assert controls.verification_status((), required, submitted=False) == "NOT_SUBMITTED"
    assert controls.verification_status({"PENNY_DROP"}, required, failed={"PENNY_DROP"}) == "FAILED"
    assert controls.verification_status({"PENNY_DROP"}, required, risk_flags={"NAME_MISMATCH"}) == "REQUIRES_REVIEW"
    kwargs = {"verified_on": date(2025, 1, 1), "max_age_days": 365}
    assert controls.verification_status({"PENNY_DROP"}, required, as_of=date(2026, 1, 2), **kwargs) == "EXPIRED"  # type: ignore[arg-type]
    assert controls.verification_status({"PENNY_DROP"}, required, as_of=date(2026, 1, 1), **kwargs) == "VERIFIED"  # type: ignore[arg-type]


def test_ec_bnk_07_same_account_on_two_payees_flags_both_holds_newer() -> None:
    accounts = [
        controls.AccountFingerprint(10, "EMPLOYEE:1", "fp-a", 10),
        controls.AccountFingerprint(25, "EMPLOYEE:2", "fp-a", 25),
        controls.AccountFingerprint(11, "EMPLOYEE:1", "fp-b", 11),
    ]
    (finding,) = controls.duplicate_fingerprints(accounts)
    assert finding.flagged_account_ids == (10, 25)
    assert finding.held_account_ids == (25,)


def test_ec_bnk_07_same_payee_history_is_not_a_duplicate() -> None:
    accounts = [controls.AccountFingerprint(1, "EMPLOYEE:1", "fp", 1), controls.AccountFingerprint(2, "EMPLOYEE:1", "fp", 2)]
    assert controls.duplicate_fingerprints(accounts) == ()


UTC = timezone.utc
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def test_ec_bnk_06_step_up_window_and_cooling_off() -> None:
    assert controls.step_up_is_fresh(T0, T0 + timedelta(minutes=5))
    assert not controls.step_up_is_fresh(T0, T0 + timedelta(minutes=5, seconds=1))
    assert not controls.step_up_is_fresh(None, T0)
    assert controls.usable_from(T0, 48) == T0 + timedelta(hours=48)
    assert controls.usable_from(T0, 48, waived_reason="Employee verified in person") == T0
    with pytest.raises(BankValidationError):
        controls.usable_from(T0, 48, waived_reason=" ")
    with pytest.raises(BankValidationError):
        controls.usable_from(datetime(2026, 10, 20), 48)


def test_ec_bnk_13_third_change_in_90_days_blocked() -> None:
    first, second = T0 - timedelta(days=80), T0 - timedelta(days=10)
    assert not controls.velocity_blocked([first], T0)
    assert controls.velocity_blocked([first, second], T0)
    assert not controls.velocity_blocked([T0 - timedelta(days=90), second], T0)
