"""Pure tests for the ISO 3166 catalogue in app.payroll.countries (3.2 §2.1, §5, §11)."""

from __future__ import annotations

import json

import pycountry
import pytest

from app.payroll import countries, currencies
from app.payroll.countries import CountryError

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def code_of(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    return getattr(excinfo.value, "code")


def test_active_countries_are_iso_3166_1() -> None:
    assert {info.alpha2 for info in countries.active()} == {entry.alpha_2 for entry in pycountry.countries}
    india = countries.get("IN")
    assert (india.alpha3, india.numeric, india.name, india.status) == ("IND", "356", "India", countries.ACTIVE)
    assert india.successor_alpha2 is None and india.withdrawn is None


@pytest.mark.parametrize("alpha2, successor", [("ZR", "CD"), ("BU", "MM"), ("TP", "TL"), ("DD", "DE"), ("YD", "YE"), ("AN", None), ("SU", None), ("CS", None)])
def test_withdrawn_codes_are_deprecated_with_successor(alpha2: str, successor: str | None) -> None:
    info = countries.get(alpha2)
    assert info.status == countries.DEPRECATED and info.successor_alpha2 == successor and info.withdrawn
    with pytest.raises(CountryError) as excinfo:
        countries.require_active_country(alpha2)
    assert code_of(excinfo) == "DEPRECATED_COUNTRY"
    if successor:
        assert successor in excinfo.value.message
        assert countries.get(successor).status == countries.ACTIVE


def test_reused_withdrawn_code_keeps_latest_meaning() -> None:
    assert countries.get("CS").name == "Serbia and Montenegro"


def test_reassigned_codes_stay_active() -> None:
    assert countries.get("BQ").status == countries.ACTIVE  # British Antarctic Territory code reused for Bonaire
    assert countries.get("BY").status == countries.ACTIVE


@pytest.mark.parametrize("value, error", [("in", "INVALID_COUNTRY_CODE"), ("IND", "INVALID_COUNTRY_CODE"), (356, "INVALID_COUNTRY_CODE"), (None, "INVALID_COUNTRY_CODE"), ("ZZ", "UNKNOWN_COUNTRY")])
def test_get_rejects_malformed_and_unknown(value: object, error: str) -> None:
    with pytest.raises(CountryError) as excinfo:
        countries.get(value)
    assert code_of(excinfo) == error
    assert countries.find(value) is None


def test_subdivisions_iso_3166_2() -> None:
    india = countries.subdivisions("IN")
    assert any(item.code == "IN-MH" and item.type == "State" for item in india)
    assert [item.code for item in india] == sorted(item.code for item in india)
    assert all(item.country == "IN" and item.status == countries.ACTIVE for item in india)
    assert countries.subdivision("AE-DU").type == "Emirate"
    assert countries.subdivision("US-CA").country == "US"
    assert countries.subdivision("GB-MRT").parent_code == "GB-ENG"
    assert countries.subdivisions("AQ") == ()


def test_subdivision_errors() -> None:
    for value, error in (("MH", "INVALID_SUBDIVISION_CODE"), ("in-mh", "INVALID_SUBDIVISION_CODE"), ("IN-ZZZ", "UNKNOWN_SUBDIVISION")):
        with pytest.raises(CountryError) as excinfo:
            countries.subdivision(value)
        assert code_of(excinfo) == error
    with pytest.raises(CountryError):
        countries.subdivisions("AN")


@pytest.mark.parametrize(
    "alpha2, currency",
    [("IN", "INR"), ("AE", "AED"), ("US", "USD"), ("GB", "GBP"), ("JP", "JPY"), ("KW", "KWD"), ("SA", "SAR"), ("CH", "CHF"), ("HR", "EUR"), ("SL", "SLE"), ("CW", "XCG")],
)
def test_default_currency_from_cldr(alpha2: str, currency: str) -> None:
    assert countries.default_currency(alpha2) == currency
    assert countries.get(alpha2).default_currency == currency


def test_default_currency_replaces_historic_code_with_successor() -> None:
    """Bulgaria adopted the euro on 1 Jan 2026; the pinned CLDR still lists BGN, which the catalogue marks historic."""
    assert countries.default_currency("BG") == "EUR"
    assert all(currencies.get(code).payroll_allowed for code in countries.territory_currencies("PA"))


def test_default_currency_none_without_tender_and_for_deprecated() -> None:
    assert countries.default_currency("AQ") is None
    assert countries.get("ZR").default_currency is None
    with pytest.raises(CountryError):
        countries.default_currency("ZR")


@pytest.mark.parametrize(
    "alpha2, weekend",
    [("US", {SAT, SUN}), ("GB", {SAT, SUN}), ("AE", {SAT, SUN}), ("SA", {FRI, SAT}), ("IN", {SUN}), ("IR", {FRI}), ("QA", {FRI, SAT})],
)
def test_default_weekend_from_cldr(alpha2: str, weekend: set[int]) -> None:
    assert countries.default_weekend(alpha2) == weekend


def test_default_weekend_falls_back_to_saturday_sunday() -> None:
    assert countries.default_weekend("AQ") == {SAT, SUN}  # no CLDR locale for the territory
    assert countries.default_weekend("HM") == {SAT, SUN}


def test_default_weekend_is_a_fresh_set() -> None:
    first = countries.default_weekend("US")
    first.add(MON)
    assert countries.default_weekend("US") == {SAT, SUN}


@pytest.mark.parametrize("alpha2, start", [("IN", "04-01"), ("GB", "04-06"), ("AU", "07-01"), ("NZ", "04-01"), ("ZA", "03-01"), ("US", "01-01"), ("AE", "01-01"), ("SG", "01-01")])
def test_tax_year_start(alpha2: str, start: str) -> None:
    assert countries.tax_year_start(alpha2) == start


def test_tax_year_start_rejects_deprecated_and_unknown() -> None:
    with pytest.raises(CountryError) as excinfo:
        countries.tax_year_start("SU")
    assert code_of(excinfo) == "DEPRECATED_COUNTRY"
    with pytest.raises(CountryError):
        countries.tax_year_start("ZZ")


def test_requires_specialist_review_for_more_than_one_jurisdiction() -> None:
    """3.2 §11: more than one tax jurisdiction in the profile -> "Requires specialist review"."""
    assert countries.requires_specialist_review(["IN", "AE"])
    assert countries.requires_specialist_review(["US-CA", "GB"])
    assert not countries.requires_specialist_review(["US-CA", "US-NY"])
    assert not countries.requires_specialist_review(["IN"])
    assert not countries.requires_specialist_review([])
    with pytest.raises(CountryError):
        countries.requires_specialist_review(["XX"])


def test_display_names_from_cldr() -> None:
    assert countries.display_name("AE", "en") == "United Arab Emirates"
    assert countries.display_name("DE", "de") == "Deutschland"
    assert countries.display_name("AN", "en")


def test_to_dict_is_json_safe() -> None:
    json.dumps([info.to_dict() for info in countries.all()])
    india = countries.get("IN").to_dict()
    assert india["default_currency"] == "INR" and india["weekend"] == [SUN] and india["tax_year_start"] == "04-01"
    zaire = countries.get("ZR").to_dict()
    assert zaire["status"] == "DEPRECATED" and zaire["weekend"] is None and zaire["successor_alpha2"] == "CD"
    json.dumps([item.to_dict() for item in countries.subdivisions("IN")])


def test_deprecated_country_properties_are_none_not_errors() -> None:
    """Old records referencing a withdrawn code stay readable: no default currency, weekend or tax year."""
    zaire = countries.get("ZR")
    assert (zaire.default_currency, zaire.weekend, zaire.tax_year_start) == (None, None, None)
    india = countries.get("IN")
    assert india.weekend == frozenset({6}) and india.tax_year_start == "04-01" and india.default_currency == "INR"
