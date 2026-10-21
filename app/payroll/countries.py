"""ISO 3166 country and subdivision catalogue with payroll defaults (3.2 §2.1, §5).

Like the currency catalogue this is code, not database rows (decision I-06).
Sources, checked on 20 Oct 2026:

* Countries (ISO 3166-1, status ACTIVE) and subdivisions (ISO 3166-2) come from
  pycountry 26.2.16. Withdrawn codes (ISO 3166-3) whose two letters are not
  reassigned are catalogued as DEPRECATED so old records stay readable; the
  successor is the active country named by the last two letters of the
  ISO 3166-3 four-letter code (ZRCD -> CD), or None when the country split
  ("HH", as for AN, CS and SU).
* The default currency is the first current tender currency CLDR records for
  the territory (babel.numbers.get_territory_currencies on CATALOGUE_DATE, so
  no clock is read). A currency the currency catalogue lists as historic is
  replaced by its successor (BGN -> EUR from 1 Jan 2026, which the pinned CLDR
  predates); a territory without one (AQ) has no default.
* The default weekend is CLDR territory week data (weekendStart..weekendEnd,
  through Babel's likely-subtags locale for the territory), Saturday-Sunday
  when CLDR has none. Mon = 0. Branches always override it (3.4 §3).
* tax_year_start is a starter value only: India 04-01 (Income-tax Act, 2025),
  United Kingdom 04-06, Australia 07-01, New Zealand 04-01, South Africa 03-01,
  Hong Kong 04-01 (year of assessment), Pakistan and Bangladesh 07-01, else the
  calendar year. The verified country pack for the period overrides it.

Names are for display only; codes are the identifiers (contract §3.1). The
capability level of a country is mutable platform state and lives in the
database, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import pycountry
from babel import Locale, UnknownLocaleError
from babel.numbers import get_territory_currencies

from app.payroll import currencies

__all__ = [
    "ACTIVE",
    "CATALOGUE_DATE",
    "CountryError",
    "CountryInfo",
    "DEFAULT_TAX_YEAR_START",
    "DEFAULT_WEEKEND",
    "DEPRECATED",
    "SubdivisionInfo",
    "TAX_YEAR_STARTS",
    "active",
    "all",
    "default_currency",
    "default_weekend",
    "display_name",
    "find",
    "get",
    "require_active_country",
    "requires_specialist_review",
    "subdivision",
    "subdivisions",
    "tax_year_start",
    "territory_currencies",
]

ACTIVE = "ACTIVE"
DEPRECATED = "DEPRECATED"
CATALOGUE_DATE = date(2026, 10, 20)

DEFAULT_WEEKEND: frozenset[int] = frozenset({5, 6})
DEFAULT_TAX_YEAR_START = "01-01"
TAX_YEAR_STARTS: Mapping[str, str] = MappingProxyType(
    {
        "AU": "07-01",
        "BD": "07-01",
        "GB": "04-06",
        "HK": "04-01",
        "IN": "04-01",
        "NZ": "04-01",
        "PK": "07-01",
        "ZA": "03-01",
    }
)

_ALPHA2 = re.compile(r"[A-Z]{2}")
_SUBDIVISION = re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}")
_NO_SUCCESSOR = frozenset({"HH", "XX", "AA"})


class CountryError(ValueError):
    """A country or subdivision code is malformed, unknown or not usable; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"


@dataclass(frozen=True)
class CountryInfo:
    alpha2: str
    alpha3: str
    numeric: str | None
    name: str
    official_name: str | None
    status: str  # ACTIVE | DEPRECATED
    successor_alpha2: str | None
    withdrawn: str | None  # ISO 3166-3 withdrawal date ("2010-12-15" or a bare year) for DEPRECATED codes

    @property
    def default_currency(self) -> str | None:
        return default_currency(self.alpha2) if self.status == ACTIVE else None

    @property
    def weekend(self) -> frozenset[int] | None:
        """CLDR weekend days (Mon = 0); None for a withdrawn code, like default_currency."""
        return frozenset(default_weekend(self.alpha2)) if self.status == ACTIVE else None

    @property
    def tax_year_start(self) -> str | None:
        return tax_year_start(self.alpha2) if self.status == ACTIVE else None

    def to_dict(self) -> dict[str, Any]:
        weekend = self.weekend
        return {
            "alpha2": self.alpha2,
            "alpha3": self.alpha3,
            "numeric": self.numeric,
            "name": self.name,
            "official_name": self.official_name,
            "status": self.status,
            "successor_alpha2": self.successor_alpha2,
            "default_currency": self.default_currency,
            "weekend": None if weekend is None else sorted(weekend),
            "tax_year_start": self.tax_year_start,
        }


@dataclass(frozen=True)
class SubdivisionInfo:
    code: str  # "IN-MH", "US-CA", "AE-DU"
    country: str
    name: str
    type: str  # "State", "Province", "Emirate"...
    parent_code: str | None
    status: str = ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "country": self.country,
            "name": self.name,
            "type": self.type,
            "parent_code": self.parent_code,
            "status": self.status,
        }


def _build_countries() -> Mapping[str, CountryInfo]:
    catalogue: dict[str, CountryInfo] = {}
    for entry in pycountry.countries:
        catalogue[entry.alpha_2] = CountryInfo(
            alpha2=entry.alpha_2,
            alpha3=entry.alpha_3,
            numeric=getattr(entry, "numeric", None),
            name=entry.name,
            official_name=getattr(entry, "official_name", None),
            status=ACTIVE,
            successor_alpha2=None,
            withdrawn=None,
        )
    active_codes = frozenset(catalogue)
    withdrawn = sorted(
        (entry for entry in pycountry.historic_countries if entry.alpha_2 not in active_codes),
        key=lambda entry: str(getattr(entry, "withdrawal_date", "")),
    )
    for entry in withdrawn:  # a reused two-letter code (CS) keeps its latest meaning
        suffix = str(getattr(entry, "alpha_4", ""))[2:]
        successor = suffix if suffix in active_codes and suffix not in _NO_SUCCESSOR else None
        catalogue[entry.alpha_2] = CountryInfo(
            alpha2=entry.alpha_2,
            alpha3=entry.alpha_3,
            numeric=getattr(entry, "numeric", None),
            name=entry.name,
            official_name=None,
            status=DEPRECATED,
            successor_alpha2=successor,
            withdrawn=str(getattr(entry, "withdrawal_date", "")) or None,
        )
    return MappingProxyType(dict(sorted(catalogue.items())))


def _build_subdivisions() -> Mapping[str, tuple[SubdivisionInfo, ...]]:
    grouped: dict[str, list[SubdivisionInfo]] = {}
    for entry in pycountry.subdivisions:
        info = SubdivisionInfo(
            code=entry.code,
            country=entry.country_code,
            name=entry.name,
            type=entry.type,
            parent_code=getattr(entry, "parent_code", None),
        )
        grouped.setdefault(info.country, []).append(info)
    return MappingProxyType({code: tuple(sorted(items, key=lambda s: s.code)) for code, items in sorted(grouped.items())})


_COUNTRIES: Mapping[str, CountryInfo] = _build_countries()
_SUBDIVISIONS: Mapping[str, tuple[SubdivisionInfo, ...]] = _build_subdivisions()
_SUBDIVISION_INDEX: Mapping[str, SubdivisionInfo] = MappingProxyType(
    {info.code: info for items in _SUBDIVISIONS.values() for info in items}
)


def find(alpha2: object) -> CountryInfo | None:
    """The catalogue entry for an ISO 3166-1 alpha-2 code (active or deprecated), or None."""
    if not isinstance(alpha2, str) or not _ALPHA2.fullmatch(alpha2):
        return None
    return _COUNTRIES.get(alpha2)


def get(alpha2: object) -> CountryInfo:
    """The catalogue entry for an upper-case ISO 3166-1 alpha-2 code, active or deprecated."""
    if not isinstance(alpha2, str) or not _ALPHA2.fullmatch(alpha2):
        raise CountryError("INVALID_COUNTRY_CODE", f"{alpha2!r} is not a two-letter upper-case ISO 3166-1 code.")
    info = _COUNTRIES.get(alpha2)
    if info is None:
        raise CountryError("UNKNOWN_COUNTRY", f"{alpha2} is not an ISO 3166-1 country in the catalogue.")
    return info


def require_active_country(alpha2: object) -> CountryInfo:
    """A country new records may use; raises DEPRECATED_COUNTRY for withdrawn codes (naming the successor)."""
    info = get(alpha2)
    if info.status != ACTIVE:
        successor = f"; use {info.successor_alpha2}" if info.successor_alpha2 else ""
        raise CountryError("DEPRECATED_COUNTRY", f"{info.alpha2} ({info.name}) was withdrawn from ISO 3166-1{successor}.")
    return info


def all() -> tuple[CountryInfo, ...]:  # noqa: A001 - mirrors currencies.all()
    """Every catalogued country, active and deprecated, ordered by code."""
    return tuple(_COUNTRIES.values())


def active() -> tuple[CountryInfo, ...]:
    return tuple(info for info in _COUNTRIES.values() if info.status == ACTIVE)


def subdivisions(alpha2: str) -> tuple[SubdivisionInfo, ...]:
    """The ISO 3166-2 subdivisions of an active country, ordered by code (empty when ISO defines none)."""
    return _SUBDIVISIONS.get(require_active_country(alpha2).alpha2, ())


def subdivision(code: object) -> SubdivisionInfo:
    """One ISO 3166-2 subdivision ("IN-MH")."""
    if not isinstance(code, str) or not _SUBDIVISION.fullmatch(code):
        raise CountryError("INVALID_SUBDIVISION_CODE", f"{code!r} is not an ISO 3166-2 code such as 'IN-MH'.")
    info = _SUBDIVISION_INDEX.get(code)
    if info is None:
        raise CountryError("UNKNOWN_SUBDIVISION", f"{code} is not an ISO 3166-2 subdivision in the catalogue.")
    return info


@lru_cache(maxsize=None)
def territory_currencies(alpha2: str) -> tuple[str, ...]:
    """The current tender currencies of an active country, CLDR order, historic codes replaced by their successor."""
    info = require_active_country(alpha2)
    result: list[str] = []
    for code in get_territory_currencies(info.alpha2, start_date=CATALOGUE_DATE, tender=True):
        currency = currencies.find(code)
        seen: set[str] = set()
        while currency is not None and currency.status == currencies.HISTORIC and currency.replaced_by and currency.code not in seen:
            seen.add(currency.code)
            currency = currencies.find(currency.replaced_by)
        if currency is not None and currency.payroll_allowed and currency.code not in result:
            result.append(currency.code)
    return tuple(result)


def default_currency(alpha2: str) -> str | None:
    """The default pay currency of a country ("INR" for IN), or None when it has no payroll currency (AQ)."""
    options = territory_currencies(alpha2)
    return options[0] if options else None


@lru_cache(maxsize=None)
def _weekend(alpha2: str) -> frozenset[int]:
    try:
        locale = Locale.parse(f"und_{alpha2}")
    except (UnknownLocaleError, ValueError):
        return DEFAULT_WEEKEND
    if locale.territory != alpha2:
        return DEFAULT_WEEKEND
    start, end = locale.weekend_start, locale.weekend_end
    return frozenset((start + offset) % 7 for offset in range((end - start) % 7 + 1))


def default_weekend(alpha2: str) -> set[int]:
    """The CLDR weekend days of a country (Mon = 0): {5, 6} for US and AE, {6} for IN, {4, 5} for SA; Sat-Sun without data."""
    return set(_weekend(require_active_country(alpha2).alpha2))


def tax_year_start(alpha2: str) -> str:
    """The usual first day ("MM-DD") of the country's tax year; a starter value the country pack overrides."""
    return TAX_YEAR_STARTS.get(require_active_country(alpha2).alpha2, DEFAULT_TAX_YEAR_START)


def display_name(alpha2: str, locale: str = "en") -> str:
    """The CLDR name of a country in ``locale``; falls back to the ISO name."""
    info = get(alpha2)
    name = currencies.babel_locale(locale).territories.get(info.alpha2)
    return name if isinstance(name, str) and name else info.name


def requires_specialist_review(tax_jurisdictions: Iterable[str]) -> bool:
    """Whether a tax profile lists more than one tax jurisdiction ("Requires specialist review", 3.2 §11).

    Jurisdictions are ISO 3166-1 alpha-2 codes or ISO 3166-2 codes; only the country part counts, so
    two US states are one jurisdiction for this check. Citizenship is never an input (contract §13 #63).
    """
    countries: set[str] = set()
    for code in tax_jurisdictions:
        if isinstance(code, str) and _SUBDIVISION.fullmatch(code):
            countries.add(subdivision(code).country)
        else:
            countries.add(get(code).alpha2)
    return len(countries) > 1
