"""Pure tests for the ISO 4217 catalogue in app.payroll.currencies (3.2 §2.2, EC-MON-01/02/03/14)."""

from __future__ import annotations

import json
from decimal import Decimal

import pycountry
import pytest
from babel.core import get_global

from app.payroll import currencies
from app.payroll.currencies import CurrencyError, LocaleError
from app.payroll.money import Money

# 3.2 §2.2, ISO 4217 List One published 2026-09-17
MINOR_UNITS_FROM_SPEC = {
    3: ("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"),
    0: ("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"),
    4: ("CLF", "UYW"),
}
FUNDS = ("BOV", "CHE", "CHW", "CLF", "COU", "MXV", "USN", "UYI", "UYW", "XAD")
NON_MONETARY = ("XAU", "XAG", "XPD", "XPT", "XBA", "XBB", "XBC", "XBD", "XDR", "XSU", "XUA", "XTS", "XXX")
HISTORIC = ("HRK", "SLL", "MRO", "STD", "BYR", "VEF", "ZWL", "LTL", "LVL", "EEK", "BGN", "ANG")
CASH = {"CHF": "0.05", "CAD": "0.05", "AUD": "0.05", "NZD": "0.10", "SEK": "1", "NOK": "1", "DKK": "0.50"}


def code_of(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    return getattr(excinfo.value, "code")


def test_golden_3_2_s2_2_minor_units_that_differ_from_two() -> None:
    """3.2 §2.2 table, exactly."""
    differing = {info.code: info.minor_unit for info in currencies.active() if info.minor_unit not in (2, None)}
    expected = {code: digits for digits, codes in MINOR_UNITS_FROM_SPEC.items() for code in codes}
    assert differing == expected


@pytest.mark.parametrize("code", ["INR", "USD", "EUR", "AED", "GBP", "SAR", "SGD", "XCG", "ZWG", "VES"])
def test_default_minor_unit_is_two(code: str) -> None:
    assert currencies.get(code).minor_unit == 2
    assert currencies.get(code).quantum == Decimal("0.01")


def test_active_catalogue_is_iso_list_one_from_pycountry() -> None:
    active_codes = {info.code for info in currencies.active()}
    assert active_codes == {entry.alpha_3 for entry in pycountry.currencies} == currencies.LIST_ONE_CODES
    assert all(info.reviewed for info in currencies.all())
    assert currencies.get("INR").numeric == "356" and currencies.get("INR").name == "Indian Rupee"


def test_ec_mon_01_jpy_catalogue_zero_digits() -> None:
    info = currencies.get("JPY")
    assert info.minor_unit == 0 and info.quantum == Decimal("1") and info.payroll_allowed


def test_ec_mon_02_kwd_catalogue_three_digits() -> None:
    for code in ("KWD", "BHD", "OMR"):
        info = currencies.require_payroll_currency(code)
        assert info.minor_unit == 3 and info.quantum == Decimal("0.001")


@pytest.mark.parametrize("code", FUNDS)
def test_ec_mon_03_fund_code_rejected_as_pay_currency(code: str) -> None:
    info = currencies.get(code)
    assert info.is_fund and not info.payroll_allowed and info.minor_unit is not None
    with pytest.raises(CurrencyError) as excinfo:
        currencies.require_payroll_currency(code)
    assert code_of(excinfo) == "FUND_CURRENCY"
    assert currencies.require_money_currency(code) is info  # representable for conversions and history


@pytest.mark.parametrize("code", NON_MONETARY)
def test_ec_mon_03_metals_and_testing_codes_rejected(code: str) -> None:
    info = currencies.get(code)
    assert info.minor_unit is None and info.quantum is None and not info.payroll_allowed
    assert info.cash_rounding_increment is None
    with pytest.raises(CurrencyError) as excinfo:
        currencies.require_payroll_currency(code)
    assert code_of(excinfo) == "NON_MONETARY_CURRENCY"
    with pytest.raises(CurrencyError):
        Money.of("1", code)


@pytest.mark.parametrize("code", ["INR", "AED", "USD", "JPY", "KWD", "EUR"])
def test_ec_mon_03_circulating_currency_accepted(code: str) -> None:
    assert currencies.require_payroll_currency(code).payroll_allowed


def test_ec_mon_03_payroll_currency_list_excludes_funds_metals_historic() -> None:
    allowed = {info.code for info in currencies.payroll_currencies()}
    assert "INR" in allowed and "KWD" in allowed
    assert not allowed & set(FUNDS + NON_MONETARY + HISTORIC)
    assert len(allowed) == len(currencies.LIST_ONE_CODES) - len(set(FUNDS) | set(NON_MONETARY))


@pytest.mark.parametrize("code", HISTORIC)
def test_ec_mon_14_historic_code_readable_not_selectable(code: str) -> None:
    info = currencies.get(code)
    assert info.status == currencies.HISTORIC and not info.payroll_allowed
    with pytest.raises(CurrencyError) as excinfo:
        currencies.require_payroll_currency(code)
    assert code_of(excinfo) == "HISTORIC_CURRENCY"
    assert info not in currencies.active()


def test_ec_mon_14_history_in_historic_currency_still_serialises() -> None:
    assert Money.of("1250.50", "HRK").to_json() == {"amount": "1250.50", "currency": "HRK"}
    assert Money.of("15000", "BYR").to_json()["amount"] == "15000"
    with pytest.raises(CurrencyError) as excinfo:
        currencies.require_payroll_currency("HRK")
    assert "EUR" in excinfo.value.message


@pytest.mark.parametrize("code, successor", [("HRK", "EUR"), ("SLL", "SLE"), ("MRO", "MRU"), ("STD", "STN"), ("VEF", "VES"), ("ZWL", "ZWG"), ("BGN", "EUR"), ("ANG", "XCG")])
def test_historic_successors_are_active_payroll_currencies(code: str, successor: str) -> None:
    assert currencies.get(code).replaced_by == successor
    assert currencies.require_payroll_currency(successor).status == currencies.ACTIVE


@pytest.mark.parametrize("code, increment", sorted(CASH.items()))
def test_cash_rounding_increments(code: str, increment: str) -> None:
    assert currencies.get(code).cash_rounding_increment == Decimal(increment)


def test_cash_rounding_absent_for_currencies_without_cash_rules() -> None:
    for code in ("INR", "USD", "EUR", "JPY", "KWD"):
        assert currencies.get(code).cash_rounding_increment is None


def test_cash_rounding_matches_pinned_cldr_data() -> None:
    """Entries sourced from Babel's CLDR currencyData must equal it (cashDigits/cashRounding)."""
    fractions = get_global("currency_fractions")
    pinned = [code for code, (_, source) in currencies.CASH_ROUNDING_SOURCES.items() if source.endswith("(Babel 2.16)")]
    assert pinned
    for code in pinned:
        _, _, cash_digits, cash_rounding = fractions[code]
        expected = Decimal(cash_rounding or 1).scaleb(-cash_digits)
        assert currencies.get(code).cash_rounding_increment == expected, code


def test_cash_increment_is_a_multiple_of_minor_unit() -> None:
    for info in currencies.all():
        if info.cash_rounding_increment is not None:
            assert info.quantum is not None
            assert (info.cash_rounding_increment % info.quantum).is_zero(), info.code


@pytest.mark.parametrize("value, error", [("inr", "INVALID_CURRENCY_CODE"), ("IN", "INVALID_CURRENCY_CODE"), (356, "INVALID_CURRENCY_CODE"), (None, "INVALID_CURRENCY_CODE"), ("ABC", "UNKNOWN_CURRENCY")])
def test_get_rejects_malformed_and_unknown(value: object, error: str) -> None:
    with pytest.raises(CurrencyError) as excinfo:
        currencies.get(value)
    assert code_of(excinfo) == error
    assert currencies.find(value) is None


def test_find_returns_entry() -> None:
    assert currencies.find("INR") is currencies.get("INR")


def test_to_dict_is_json_safe() -> None:
    payload = [info.to_dict() for info in currencies.all()]
    json.dumps(payload)
    chf = currencies.get("CHF").to_dict()
    assert chf["cash_rounding_increment"] == "0.05" and chf["minor_unit"] == 2 and chf["status"] == "ACTIVE"
    assert currencies.get("HRK").to_dict()["replaced_by"] == "EUR"


def test_all_is_sorted_and_contains_historic() -> None:
    codes = [info.code for info in currencies.all()]
    assert codes == sorted(codes)
    assert set(HISTORIC) <= set(codes)


def test_locale_helpers() -> None:
    assert currencies.display_name("INR", "en") == "Indian Rupee"
    assert currencies.symbol("INR", "en-IN") == "₹"
    assert currencies.symbol("USD", "en") == "$"
    assert currencies.babel_locale("en_IN") is currencies.babel_locale("en_IN")
    for bad in ("", "x", "en IN", "zz-ZZ-unknown-!!"):
        with pytest.raises(LocaleError) as excinfo:
            currencies.babel_locale(bad)
        assert code_of(excinfo) == "UNKNOWN_LOCALE"


@pytest.mark.parametrize("bad", [["en"], {"en": 1}, 1, None])
def test_babel_locale_rejects_non_string_with_locale_error(bad: object) -> None:
    with pytest.raises(LocaleError) as excinfo:
        currencies.babel_locale(bad)
    assert code_of(excinfo) == "UNKNOWN_LOCALE"


@pytest.mark.parametrize("locale, direction", [("ar-AE", "rtl"), ("he", "rtl"), ("ur_PK", "rtl"), ("en-IN", "ltr"), ("hi-IN", "ltr")])
def test_text_direction_for_rtl_payslips(locale: str, direction: str) -> None:
    """3.2 §5: dir="rtl" for right-to-left locales."""
    assert currencies.text_direction(locale) == direction


def test_ec_mon_14_reviewed_historic_entry_wins_over_unreviewed_pycountry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a future pycountry lists a withdrawn code again, old records keep the reviewed minor unit and status."""
    entries = [*pycountry.currencies, type("Entry", (), {"alpha_3": "BYR", "numeric": "974", "name": "Belarusian Ruble"})()]
    monkeypatch.setattr(currencies.pycountry, "currencies", entries)
    rebuilt = currencies._build()
    assert rebuilt["BYR"].status == currencies.HISTORIC and rebuilt["BYR"].minor_unit == 0
    assert rebuilt["BYR"].replaced_by == "BYN" and not rebuilt["BYR"].payroll_allowed
    assert rebuilt["INR"] == currencies.get("INR")


def test_unreviewed_new_code_never_defaults_to_two_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [*pycountry.currencies, type("Entry", (), {"alpha_3": "XQQ", "numeric": "999", "name": "Future Unit"})()]
    monkeypatch.setattr(currencies.pycountry, "currencies", entries)
    info = currencies._build()["XQQ"]
    assert info.minor_unit is None and not info.reviewed and not info.payroll_allowed
