"""ISO 4217 currency catalogue: minor units, funds, historic codes and cash rounding.

The catalogue is code, not database rows (decision I-06), so every process and
test sees the same data. Sources, checked on 20 Oct 2026:

* Active codes, numeric codes and English names come from pycountry 26.2.16
  (Debian iso-codes), whose 178 codes are exactly ISO 4217 List One as published
  by SIX on 2026-09-17. That reviewed set is embedded as LIST_ONE_CODES: a code
  a later pycountry adds is catalogued as unreviewed (no minor unit, not usable
  as money or pay currency), so a new 0- or 3-decimal currency can never
  silently default to two decimals.
* Minor units follow List One (pycountry has none): 3 for BHD IQD JOD KWD LYD
  OMR TND, 0 for BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF
  XPF, 4 for CLF UYW (3.2 §2.2), "N.A." (None: not money) for precious metals,
  bond-market units, SDR, SUCRE, the ADB unit of account, XTS and XXX, and 2
  for every other code.
* Funds are the List One entries marked IsFund: BOV CHE CHW CLF COU MXV USN UYI
  UYW XAD. They are representable (an amount can be held and converted) but are
  never a pay currency (EC-MON-03).
* Historic codes are the payroll-relevant part of ISO 4217 List Three: codes
  withdrawn since 2008, including BGN (Bulgaria adopted the euro on 1 Jan 2026)
  and ANG (replaced by the Caribbean guilder XCG in 2025). Old records in them
  stay readable; they are never selectable (EC-MON-14).
* Cash rounding increments (cash payments only, 3.7 §7.3) come from CLDR
  supplemental currencyData cashDigits/cashRounding as shipped with the pinned
  Babel 2.16, HUF from CLDR main (retrieved 20 Oct 2026), and central-bank rules
  CLDR does not record: AUD 0.05 (Reserve Bank of Australia, 1992), NZD 0.10
  (Reserve Bank of New Zealand, 2006), ILS 0.10 (Bank of Israel, 2008).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Mapping

import pycountry
from babel import Locale, UnknownLocaleError
from babel.numbers import get_currency_name, get_currency_symbol

from app.payroll.money_errors import CurrencyError, LocaleError

__all__ = [
    "ACTIVE",
    "CASH_ROUNDING_SOURCES",
    "CurrencyError",
    "CurrencyInfo",
    "FUND_CODES",
    "HISTORIC",
    "LIST_ONE_CODES",
    "LIST_ONE_PUBLISHED",
    "LocaleError",
    "NON_MONETARY_CODES",
    "all",
    "active",
    "babel_locale",
    "display_name",
    "find",
    "get",
    "payroll_currencies",
    "require_money_currency",
    "require_payroll_currency",
    "symbol",
    "text_direction",
]

ACTIVE = "ACTIVE"
HISTORIC = "HISTORIC"

LIST_ONE_PUBLISHED = date(2026, 9, 17)

LIST_ONE_CODES: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF
    CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP
    GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT
    LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR
    NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN
    SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAD
    XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWG
    """.split()
)

_MINOR_UNITS: Mapping[str, int] = MappingProxyType(
    {
        **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
        **dict.fromkeys(
            (
                "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
                "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
            ),
            0,
        ),
        **dict.fromkeys(("CLF", "UYW"), 4),
    }
)
_DEFAULT_MINOR_UNIT = 2

NON_MONETARY_CODES: frozenset[str] = frozenset(
    {"XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XDR", "XPD", "XPT", "XSU", "XTS", "XUA", "XXX"}
)
FUND_CODES: frozenset[str] = frozenset({"BOV", "CHE", "CHW", "CLF", "COU", "MXV", "USN", "UYI", "UYW", "XAD"})

# code: (numeric, ISO name, minor unit while active, successor or None)
_HISTORIC: Mapping[str, tuple[str, str, int, str | None]] = MappingProxyType(
    {
        "ANG": ("532", "Netherlands Antillean Guilder", 2, "XCG"),
        "BGN": ("975", "Bulgarian Lev", 2, "EUR"),
        "BYR": ("974", "Belarusian Ruble", 0, "BYN"),
        "CUC": ("931", "Peso Convertible", 2, None),
        "CYP": ("196", "Cyprus Pound", 2, "EUR"),
        "EEK": ("233", "Kroon", 2, "EUR"),
        "HRK": ("191", "Kuna", 2, "EUR"),
        "LTL": ("440", "Lithuanian Litas", 2, "EUR"),
        "LVL": ("428", "Latvian Lats", 2, "EUR"),
        "MRO": ("478", "Ouguiya", 2, "MRU"),
        "MTL": ("470", "Maltese Lira", 2, "EUR"),
        "SKK": ("703", "Slovak Koruna", 2, "EUR"),
        "SLL": ("694", "Leone", 2, "SLE"),
        "STD": ("678", "Dobra", 2, "STN"),
        "VEF": ("937", "Bolívar", 2, "VES"),
        "ZMK": ("894", "Zambian Kwacha", 2, "ZMW"),
        "ZWL": ("932", "Zimbabwe Dollar", 2, "ZWG"),
    }
)

_CLDR_PINNED = "CLDR currencyData (Babel 2.16)"
_CLDR_MAIN = "CLDR currencyData (main, retrieved 2026-10-20)"

# code: (smallest cash increment, source)
CASH_ROUNDING_SOURCES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "AMD": ("1", _CLDR_PINNED),
        "AUD": ("0.05", "Reserve Bank of Australia (1c and 2c coins withdrawn 1992)"),
        "CAD": ("0.05", _CLDR_PINNED),
        "CHF": ("0.05", _CLDR_PINNED),
        "COP": ("1", _CLDR_PINNED),
        "CRC": ("1", _CLDR_PINNED),
        "CZK": ("1", _CLDR_PINNED),
        "DKK": ("0.50", _CLDR_PINNED),
        "GYD": ("1", _CLDR_PINNED),
        "HUF": ("5", _CLDR_MAIN),
        "IDR": ("1", _CLDR_PINNED),
        "ILS": ("0.10", "Bank of Israel (5 agorot coin withdrawn 2008)"),
        "MNT": ("1", _CLDR_PINNED),
        "MUR": ("1", _CLDR_PINNED),
        "NOK": ("1", _CLDR_PINNED),
        "NZD": ("0.10", "Reserve Bank of New Zealand (5c coin withdrawn 2006)"),
        "PKR": ("1", _CLDR_PINNED),
        "SEK": ("1", _CLDR_PINNED),
        "TWD": ("1", _CLDR_PINNED),
        "TZS": ("1", _CLDR_PINNED),
        "UZS": ("1", _CLDR_PINNED),
    }
)

_CODE = re.compile(r"[A-Z]{3}")
_LOCALE = re.compile(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8}){0,4}")


@dataclass(frozen=True)
class CurrencyInfo:
    code: str
    numeric: str | None
    name: str
    minor_unit: int | None  # None: no minor unit ("N.A." in ISO 4217, or not yet reviewed)
    status: str  # ACTIVE | HISTORIC
    is_fund: bool
    payroll_allowed: bool
    cash_rounding_increment: Decimal | None
    replaced_by: str | None = None
    reviewed: bool = True  # part of the reviewed ISO 4217 List One snapshot (or of the historic table)

    @property
    def quantum(self) -> Decimal | None:
        """The smallest amount in this currency (Decimal("0.01") for INR), or None if it has no minor unit."""
        if self.minor_unit is None:
            return None
        return Decimal((0, (1,), -self.minor_unit))

    def to_dict(self) -> dict[str, Any]:
        increment = self.cash_rounding_increment
        return {
            "code": self.code,
            "numeric": self.numeric,
            "name": self.name,
            "minor_unit": self.minor_unit,
            "status": self.status,
            "is_fund": self.is_fund,
            "payroll_allowed": self.payroll_allowed,
            "cash_rounding_increment": None if increment is None else format(increment, "f"),
            "replaced_by": self.replaced_by,
        }


def _cash_increment(code: str) -> Decimal | None:
    entry = CASH_ROUNDING_SOURCES.get(code)
    return None if entry is None else Decimal(entry[0])


def _active_info(code: str, numeric: str | None, name: str) -> CurrencyInfo:
    reviewed = code in LIST_ONE_CODES
    minor_unit = None if not reviewed or code in NON_MONETARY_CODES else _MINOR_UNITS.get(code, _DEFAULT_MINOR_UNIT)
    is_fund = code in FUND_CODES
    return CurrencyInfo(
        code=code,
        numeric=numeric,
        name=name,
        minor_unit=minor_unit,
        status=ACTIVE,
        is_fund=is_fund,
        payroll_allowed=reviewed and minor_unit is not None and not is_fund,
        cash_rounding_increment=_cash_increment(code) if minor_unit is not None else None,
        reviewed=reviewed,
    )


def _historic_info(code: str, numeric: str | None, name: str, minor_unit: int | None, successor: str | None) -> CurrencyInfo:
    return CurrencyInfo(
        code=code,
        numeric=numeric,
        name=name,
        minor_unit=minor_unit,
        status=HISTORIC,
        is_fund=code in FUND_CODES,
        payroll_allowed=False,
        cash_rounding_increment=None,
        replaced_by=successor,
    )


def _english_name(code: str) -> str:
    name = get_currency_name(code, locale="en")
    return name if isinstance(name, str) and name else code


def _build() -> Mapping[str, CurrencyInfo]:
    catalogue: dict[str, CurrencyInfo] = {}
    for entry in pycountry.currencies:
        code = entry.alpha_3
        catalogue[code] = _active_info(code, getattr(entry, "numeric", None), entry.name)
    # The reviewed historic table wins over an unreviewed pycountry entry, so old records keep their minor unit.
    for code, (numeric, name, minor_unit, successor) in _HISTORIC.items():
        if code not in LIST_ONE_CODES:
            catalogue[code] = _historic_info(code, numeric, name, minor_unit, successor)
    # A reviewed List One code that a later pycountry drops stays readable as historic data.
    for code in LIST_ONE_CODES - catalogue.keys():
        minor_unit = None if code in NON_MONETARY_CODES else _MINOR_UNITS.get(code, _DEFAULT_MINOR_UNIT)
        catalogue[code] = _historic_info(code, None, _english_name(code), minor_unit, None)
    return MappingProxyType(dict(sorted(catalogue.items())))


_CATALOGUE: Mapping[str, CurrencyInfo] = _build()


def find(code: object) -> CurrencyInfo | None:
    """The catalogue entry for ``code``, or None when it is malformed or unknown."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return None
    return _CATALOGUE.get(code)


def get(code: object) -> CurrencyInfo:
    """The catalogue entry for an ISO 4217 alphabetic code (upper case), active or historic."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise CurrencyError("INVALID_CURRENCY_CODE", f"{code!r} is not a three-letter upper-case ISO 4217 code.")
    info = _CATALOGUE.get(code)
    if info is None:
        raise CurrencyError("UNKNOWN_CURRENCY", f"{code} is not an ISO 4217 currency in the catalogue.")
    return info


def all() -> tuple[CurrencyInfo, ...]:  # noqa: A001 - public name fixed by ARCHITECTURE.md §4.2
    """Every catalogued currency, active and historic, ordered by code."""
    return tuple(_CATALOGUE.values())


def active() -> tuple[CurrencyInfo, ...]:
    return tuple(info for info in _CATALOGUE.values() if info.status == ACTIVE)


def payroll_currencies() -> tuple[CurrencyInfo, ...]:
    """The currencies a pay group, contract or payment may use."""
    return tuple(info for info in _CATALOGUE.values() if info.payroll_allowed)


def require_money_currency(code: object) -> CurrencyInfo:
    """A currency an amount can be held in: known, reviewed and with a minor unit (funds and historic codes included)."""
    info = get(code)
    if not info.reviewed:
        raise CurrencyError(
            "UNREVIEWED_CURRENCY",
            f"{info.code} is new in the ISO data and its minor unit has not been reviewed for payroll yet.",
        )
    if info.minor_unit is None:
        raise CurrencyError(
            "NON_MONETARY_CURRENCY",
            f"{info.code} ({info.name}) has no minor unit in ISO 4217 and cannot hold payroll amounts.",
        )
    return info


def require_payroll_currency(code: object) -> CurrencyInfo:
    """A currency payroll may calculate or pay in; raises for unknown, fund, non-monetary and historic codes (EC-MON-03)."""
    info = get(code)
    if info.status == HISTORIC:
        successor = f"; use {info.replaced_by}" if info.replaced_by else ""
        raise CurrencyError("HISTORIC_CURRENCY", f"{info.code} has been withdrawn from ISO 4217{successor}.")
    require_money_currency(code)
    if info.is_fund:
        raise CurrencyError(
            "FUND_CURRENCY",
            f"{info.code} ({info.name}) is an ISO 4217 fund code: representable, but not a currency wages are paid in.",
        )
    if not info.payroll_allowed:
        raise CurrencyError("CURRENCY_NOT_PAYROLL_ALLOWED", f"{info.code} cannot be used for payroll.")
    return info


def babel_locale(identifier: object) -> Locale:
    """A Babel locale for a BCP 47 or POSIX-style tag ("en", "en-IN", "en_IN", "sr-Latn-RS")."""
    if not isinstance(identifier, str) or not _LOCALE.fullmatch(identifier):
        raise LocaleError("UNKNOWN_LOCALE", f"{identifier!r} is not a locale identifier.")
    return _parse_locale(identifier)


@lru_cache(maxsize=128)
def _parse_locale(identifier: str) -> Locale:
    try:
        return Locale.parse(identifier, sep="-" if "-" in identifier else "_")
    except (UnknownLocaleError, ValueError, TypeError) as exc:
        raise LocaleError("UNKNOWN_LOCALE", f"{identifier!r} is not a locale known to CLDR.") from exc


def text_direction(locale: str) -> str:
    """"rtl" or "ltr" for a locale (CLDR character order), for dir= on pages and payslip PDFs (3.2 §5)."""
    return "rtl" if babel_locale(locale).character_order == "right-to-left" else "ltr"


def display_name(code: str, locale: str = "en") -> str:
    """The CLDR name of a currency in ``locale``; falls back to the ISO name."""
    info = get(code)
    name = get_currency_name(info.code, locale=babel_locale(locale))
    return name if isinstance(name, str) and name and name != info.code else info.name


def symbol(code: str, locale: str = "en") -> str:
    """The CLDR symbol of a currency in ``locale`` (the ISO code when the locale has none)."""
    info = get(code)
    return get_currency_symbol(info.code, locale=babel_locale(locale))
