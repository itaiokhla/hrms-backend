"""Bank account validation: per-country required fields and format checks (3.7 §2.3).

``validate_account`` normalises what a person typed (whitespace removed, letters upper-cased,
hyphens dropped from numeric codes) and returns a ``ValidationResult`` with field errors and
warnings instead of raising, because invalid bank details are user data, not a programming error.
Only the fields of the account's country are required (contract §22.2):

    IN    holder, account number (9-18 digits), IFSC ^[A-Z]{4}0[A-Z0-9]{6}$, account type
    AE    holder, IBAN (23 characters, "AE", mod-97); WPS agent routing code (9 digits) for WPS
    SA    holder, IBAN (24 characters, "SA", mod-97)
    GB    holder, sort code (6 digits), account number (8 digits); an IBAN, if given, must agree
    US    holder, ABA routing number (9 digits, checksum), account number, account type
    SEPA  holder, IBAN (registry length for its country, mod-97); BIC optional
    other holder, IBAN or account number, BIC, bank address

Crown dependencies follow GB, US territories follow US, and territories that bank under another
country's IBAN prefix (French overseas departments, Åland) expect that prefix. A national routing
identifier on an account of another country (an IFSC on a UAE account), an IBAN for a country that
does not use IBANs or with another country's prefix, and a failed IBAN checksum are rejected on
entry (EC-BNK-04, EC-BNK-05).

Passing every check proves the format only (method FORMAT): it never makes an account VERIFIED
(3.7 §4; see ``controls.verification_status``). UK bank modulus checking needs the Vocalink tables,
which are not bundled, so every GB account carries a warning that it was not performed. Directory
lookups (IFSC, BIC) are optional; codes the directory marks invalid, closed or merged are warnings
(EC-BNK-16). Messages never contain the submitted numbers.

The IBAN registry follows the SWIFT IBAN Registry (all countries registered by 2025) and the SEPA
list the EPC scheme register as of 2024; both are data to refresh when those registers change.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from app.payroll.banking.errors import BankValidationError
from app.payroll.banking.masking import mask_account, mask_iban, mask_name

IBAN_LENGTHS: Mapping[str, int] = MappingProxyType(
    {
        "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22,
        "BI": 27, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27,
        "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27,
        "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HN": 28, "HR": 21, "HU": 28,
        "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28,
        "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22,
        "MK": 19, "MN": 20, "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23,
        "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24,
        "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SO": 23, "ST": 25, "SV": 28,
        "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "YE": 30,
    }
)

IBAN_TERRITORIES: Mapping[str, str] = MappingProxyType(
    {
        "AX": "FI", "BL": "FR", "GF": "FR", "GP": "FR", "MF": "FR", "MQ": "FR", "NC": "FR", "PF": "FR",
        "PM": "FR", "RE": "FR", "TF": "FR", "WF": "FR", "YT": "FR", "GG": "GB", "IM": "GB", "JE": "GB",
    }
)

SEPA_COUNTRIES: frozenset[str] = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT",
        "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
        "IS", "LI", "NO", "CH", "GB", "MC", "SM", "VA", "AD", "GI",
        "AX", "BL", "GF", "GP", "MF", "MQ", "PM", "RE", "YT", "GG", "IM", "JE",
    }
)

NATIONAL_ROUTING: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "ifsc": frozenset({"IN"}),
        "sort_code": frozenset({"GB", "GG", "IM", "JE"}),
        "aba": frozenset({"US", "AS", "GU", "MP", "PR", "UM", "VI"}),
        "bsb": frozenset({"AU"}),
        "wps_routing_code": frozenset({"AE"}),
    }
)
GENERIC_ROUTING: tuple[str, ...] = ("bic", "bank_code", "branch_code")
ROUTING_FIELDS: tuple[str, ...] = (*NATIONAL_ROUTING, *GENERIC_ROUTING)

RULE_SETS: tuple[str, ...] = ("IN", "AE", "SA", "GB", "US", "SEPA", "INTERNATIONAL")
_RULE_SET_BY_COUNTRY: Mapping[str, str] = MappingProxyType(
    {
        "IN": "IN", "AE": "AE", "SA": "SA",
        "GB": "GB", "GG": "GB", "IM": "GB", "JE": "GB",
        "US": "US", "AS": "US", "GU": "US", "MP": "US", "PR": "US", "UM": "US", "VI": "US",
    }
)

ACCOUNT_TYPES: frozenset[str] = frozenset({"CHECKING", "CURRENT", "SAVINGS"})
_REQUIRED_ACCOUNT_TYPES: Mapping[str, frozenset[str]] = MappingProxyType(
    {"IN": frozenset({"CURRENT", "SAVINGS"}), "US": frozenset({"CHECKING", "SAVINGS"})}
)
DIRECTORY_STATUSES: tuple[str, ...] = ("ACTIVE", "INVALID", "CLOSED", "MERGED")

FORMAT_METHOD = "FORMAT"
MAX_FIELD_LENGTH = 256
MAX_HOLDER_NAME = 140
MAX_BANK_ADDRESS = 300

_COUNTRY = re.compile(r"[A-Z]{2}")
_IFSC = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")
_BIC = re.compile(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?")
_IBAN_SHAPE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}")
_SIX_DIGITS = re.compile(r"[0-9]{6}")
_NINE_DIGITS = re.compile(r"[0-9]{9}")
_IN_ACCOUNT = re.compile(r"[0-9]{9,18}")
_GB_ACCOUNT = re.compile(r"[0-9]{8}")
_US_ACCOUNT = re.compile(r"[0-9]{4,17}")
_GENERIC_ACCOUNT = re.compile(r"[A-Z0-9]{1,34}")
_BANK_CODE = re.compile(r"[A-Z0-9]{1,15}")
_BBAN: Mapping[str, re.Pattern[str]] = MappingProxyType(
    {
        "AE": re.compile(r"[0-9]{3}[0-9]{16}"),
        "SA": re.compile(r"[0-9]{2}[A-Z0-9]{18}"),
        "GB": re.compile(r"[A-Z]{4}[0-9]{6}[0-9]{8}"),
    }
)
_ABA_PREFIXES = frozenset(range(1, 13)) | frozenset(range(21, 33)) | frozenset(range(61, 73))
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


# --------------------------------------------------------------------------- identifier checks


def normalize_code(value: str) -> str:
    """Upper-case and drop whitespace and hyphens: ``"12-34-56"`` -> ``"123456"``."""
    return "".join(value.split()).replace("-", "").upper()


def normalize_iban(value: str) -> str:
    """Electronic IBAN format: no whitespace, upper case, an ``IBAN`` print prefix removed."""
    compact = "".join(value.split()).upper()
    if compact.startswith("IBAN"):
        compact = compact[4:].lstrip(":")
    return compact


def iban_country(country: str) -> str | None:
    """The IBAN prefix used by accounts held in ``country``, or None when it does not use IBANs."""
    prefix = IBAN_TERRITORIES.get(country, country)
    return prefix if prefix in IBAN_LENGTHS else None


def iban_checksum_ok(iban: str) -> bool:
    """ISO 13616 mod-97: the rearranged IBAN read as a number leaves remainder 1."""
    remainder = 0
    for char in iban[4:] + iban[:4]:
        digit = int(char, 36)
        remainder = (remainder * (100 if digit > 9 else 10) + digit) % 97
    return remainder == 1


def make_iban(country: str, bban: str) -> str:
    """An IBAN with correct check digits for a country code and BBAN (for tests and tools)."""
    country, bban = country.upper(), normalize_code(bban)
    if not _COUNTRY.fullmatch(country) or not re.fullmatch(r"[A-Z0-9]{1,30}", bban):
        raise BankValidationError("INVALID_ARGUMENT", "country must be 2 letters and the BBAN 1-30 letters or digits")
    remainder = 0
    for char in bban + country + "00":
        digit = int(char, 36)
        remainder = (remainder * (100 if digit > 9 else 10) + digit) % 97
    return f"{country}{98 - remainder:02d}{bban}"


def iban_problems(iban: str, expected_country: str | None = None) -> tuple[tuple[str, str], ...]:
    """``(code, message)`` for the first structural problem of a normalised IBAN; empty when valid."""
    if not _IBAN_SHAPE.fullmatch(iban):
        return (("IBAN_FORMAT", "IBAN must be a country code, two check digits and up to 30 letters or digits"),)
    prefix = iban[:2]
    if prefix not in IBAN_LENGTHS:
        return (("IBAN_COUNTRY_UNKNOWN", f"{prefix} is not a country in the IBAN registry"),)
    if expected_country is not None and prefix != expected_country:
        return (("IBAN_COUNTRY_MISMATCH", f"IBAN must start with {expected_country} for this account"),)
    if len(iban) != IBAN_LENGTHS[prefix]:
        return (("IBAN_LENGTH", f"{prefix} IBANs have {IBAN_LENGTHS[prefix]} characters"),)
    if not 2 <= int(iban[2:4]) <= 98:
        return (("IBAN_CHECK_DIGITS", "IBAN check digits must be between 02 and 98"),)
    if not iban_checksum_ok(iban):
        return (("IBAN_CHECKSUM", "IBAN check digits do not match (mod-97); check for a typing error"),)
    bban = _BBAN.get(prefix)
    if bban is not None and not bban.fullmatch(iban[4:]):
        return (("IBAN_BBAN_FORMAT", f"the account part does not have the {prefix} structure"),)
    return ()


def is_valid_iban(value: str, country: str | None = None) -> bool:
    """True for a structurally valid IBAN (optionally for an account held in ``country``)."""
    expected = iban_country(country) if country else None
    if country and expected is None:
        return False
    return not iban_problems(normalize_iban(value), expected)


def is_valid_ifsc(value: str) -> bool:
    return _IFSC.fullmatch(normalize_code(value)) is not None


def is_valid_bic(value: str) -> bool:
    return _BIC.fullmatch(normalize_code(value)) is not None


def aba_checksum_ok(routing: str) -> bool:
    """3(d1+d4+d7) + 7(d2+d5+d8) + (d3+d6+d9) ≡ 0 (mod 10) for a 9-digit routing number."""
    if not _NINE_DIGITS.fullmatch(routing):
        return False
    d = [int(char) for char in routing]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def is_valid_aba(value: str) -> bool:
    """9 digits, a Federal Reserve prefix valid for ACH (01-12, 21-32, 61-72) and the ABA checksum."""
    routing = normalize_code(value)
    return aba_checksum_ok(routing) and int(routing[:2]) in _ABA_PREFIXES


def rule_set(country: str) -> str:
    """Which row of the 3.7 §2.3 table applies to an account held in ``country``."""
    if country in _RULE_SET_BY_COUNTRY:
        return _RULE_SET_BY_COUNTRY[country]
    return "SEPA" if country in SEPA_COUNTRIES else "INTERNATIONAL"


# --------------------------------------------------------------------------- input and result


@dataclass(frozen=True, repr=False)
class BankAccountDetails:
    """Bank details as entered; ``routing`` keys are ROUTING_FIELDS (ifsc, sort_code, aba, bic, ...)."""

    country: str
    holder_name: str | None = None
    account_number: str | None = None
    iban: str | None = None
    routing: Mapping[str, str] = field(default_factory=dict)
    account_type: str | None = None
    bank_address: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.country, str):
            raise BankValidationError("INVALID_ARGUMENT", "country must be a string")
        for name in ("holder_name", "account_number", "iban", "account_type", "bank_address"):
            if not isinstance(getattr(self, name), (str, type(None))):
                raise BankValidationError("INVALID_ARGUMENT", f"{name} must be a string or None")
        if not isinstance(self.routing, Mapping):
            raise BankValidationError("INVALID_ARGUMENT", "routing must be a mapping")
        object.__setattr__(self, "routing", MappingProxyType(dict(self.routing)))

    def __repr__(self) -> str:
        return (
            f"BankAccountDetails(country={self.country!r}, holder_name={mask_name(self.holder_name)!r}, "
            f"account_number={mask_account(self.account_number)!r}, iban={mask_iban(self.iban)!r}, "
            f"routing={dict(self.routing)!r}, account_type={self.account_type!r})"
        )


@dataclass(frozen=True)
class FieldIssue:
    field: str  # "iban", "routing.ifsc", "holder_name", ...
    code: str
    message: str


@dataclass(frozen=True)
class DirectoryEntry:
    """A bank directory record (IFSC, BIC): status ACTIVE, INVALID, CLOSED or MERGED."""

    scheme: str
    code: str
    status: str
    bank_name: str | None = None
    branch_name: str | None = None
    successor: str | None = None


class BankDirectory(Protocol):
    def lookup(self, scheme: str, code: str) -> DirectoryEntry | None: ...


@dataclass(frozen=True)
class ValidationResult:
    """Format check of one account. ``valid`` proves the format only, never ownership (3.7 §4)."""

    rule_set: str
    account: BankAccountDetails  # normalised
    errors: tuple[FieldIssue, ...]
    warnings: tuple[FieldIssue, ...]
    checks: tuple[str, ...]  # format checks that passed
    directory: tuple[DirectoryEntry, ...] = ()

    method = FORMAT_METHOD

    @property
    def valid(self) -> bool:
        return not self.errors

    def error_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.errors)

    def warning_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.warnings)


class _Collector:
    def __init__(self) -> None:
        self.errors: list[FieldIssue] = []
        self.warnings: list[FieldIssue] = []
        self.checks: list[str] = []
        self.entries: list[DirectoryEntry] = []

    def error(self, name: str, code: str, message: str) -> None:
        self.errors.append(FieldIssue(name, code, message))

    def warn(self, name: str, code: str, message: str) -> None:
        self.warnings.append(FieldIssue(name, code, message))

    def has_error(self, name: str) -> bool:
        return any(issue.field == name for issue in self.errors)


# --------------------------------------------------------------------------- validation


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def has_unsafe_characters(value: str) -> bool:
    """True when text contains control characters or bidirectional overrides (display spoofing)."""
    return any(unicodedata.category(char) == "Cc" or char in _BIDI_CONTROLS for char in value)


def _normalize(details: BankAccountDetails, out: _Collector) -> BankAccountDetails:
    routing: dict[str, str] = {}
    for key, raw in details.routing.items():
        name = key.strip().lower() if isinstance(key, str) else ""
        if name not in ROUTING_FIELDS:
            out.error("routing", "ROUTING_FIELD_UNKNOWN", f"unknown routing field; allowed: {', '.join(ROUTING_FIELDS)}")
        elif not isinstance(raw, str):
            out.error(f"routing.{name}", "ROUTING_VALUE_INVALID", "routing values must be text")
        elif normalize_code(raw):
            routing[name] = normalize_code(raw)
    account_type = details.account_type.strip().upper() if details.account_type else None
    return BankAccountDetails(
        country=details.country.strip().upper(),
        holder_name=_clean_text(details.holder_name),
        account_number=normalize_code(details.account_number) or None if details.account_number else None,
        iban=normalize_iban(details.iban) or None if details.iban else None,
        routing=routing,
        account_type=account_type or None,
        bank_address=_clean_text(details.bank_address),
    )


def _too_long(account: BankAccountDetails, out: _Collector) -> set[str]:
    skip: set[str] = set()
    values = {
        "holder_name": account.holder_name,
        "account_number": account.account_number,
        "iban": account.iban,
        "account_type": account.account_type,
        "bank_address": account.bank_address,
        **{f"routing.{k}": v for k, v in account.routing.items()},
    }
    for name, value in values.items():
        if value is not None and len(value) > MAX_FIELD_LENGTH:
            out.error(name, "FIELD_TOO_LONG", f"must be at most {MAX_FIELD_LENGTH} characters")
            skip.add(name)
    return skip


def _check_holder(account: BankAccountDetails, out: _Collector) -> None:
    name = account.holder_name
    if name is None:
        out.error("holder_name", "REQUIRED", "the account holder's name is required")
    elif len(name) > MAX_HOLDER_NAME or has_unsafe_characters(name):
        out.error("holder_name", "HOLDER_NAME_INVALID", f"use at most {MAX_HOLDER_NAME} printable characters")
    elif not any(unicodedata.category(char).startswith("L") for char in name):
        out.error("holder_name", "HOLDER_NAME_INVALID", "the holder name must contain letters")
    else:
        out.checks.append("HOLDER_NAME")


def _check_foreign_routing(account: BankAccountDetails, out: _Collector) -> None:
    for key, countries in NATIONAL_ROUTING.items():
        if key in account.routing and account.country not in countries:
            out.error(
                f"routing.{key}",
                "FIELD_NOT_FOR_COUNTRY",
                f"{key} is used only for accounts in {', '.join(sorted(countries))}, not {account.country}",
            )


def _check_pattern(
    out: _Collector, name: str, value: str | None, pattern: re.Pattern[str], code: str, message: str, check: str
) -> bool:
    if value is None:
        return False
    if pattern.fullmatch(value) is None:
        out.error(name, code, message)
        return False
    out.checks.append(check)
    return True


def _require(out: _Collector, name: str, value: str | None, label: str) -> bool:
    if value is None:
        out.error(name, "REQUIRED", f"{label} is required")
        return False
    return True


def _check_account_digits(out: _Collector, value: str, pattern: re.Pattern[str], message: str) -> None:
    if pattern.fullmatch(value) is None:
        out.error("account_number", "ACCOUNT_NUMBER_FORMAT", message)
    elif set(value) == {"0"}:
        out.error("account_number", "ACCOUNT_NUMBER_ALL_ZERO", "the account number cannot be all zeros")
    else:
        out.checks.append("ACCOUNT_NUMBER_FORMAT")


def _check_iban(account: BankAccountDetails, out: _Collector, *, required: bool) -> None:
    if account.iban is None:
        if required:
            out.error("iban", "REQUIRED", "the IBAN is required")
        return
    expected = iban_country(account.country)
    if expected is None:
        out.error("iban", "IBAN_NOT_USED_IN_COUNTRY", f"{account.country} does not use IBANs; enter the local account details")
        return
    problems = iban_problems(account.iban, expected)
    for code, message in problems:
        out.error("iban", code, message)
    if not problems:
        out.checks.extend(("IBAN_LENGTH", "IBAN_CHECKSUM"))


def _account_number_unused(account: BankAccountDetails, out: _Collector) -> None:
    if account.account_number is not None and account.iban is not None:
        out.warn("account_number", "ACCOUNT_NUMBER_NOT_USED", "the IBAN identifies this account; the account number is not used")


def _rules_in(account: BankAccountDetails, out: _Collector, wps: bool) -> None:
    _check_iban(account, out, required=False)
    if _require(out, "account_number", account.account_number, "the account number"):
        _check_account_digits(out, account.account_number or "", _IN_ACCOUNT, "Indian account numbers have 9 to 18 digits")
    ifsc = account.routing.get("ifsc")
    if _require(out, "routing.ifsc", ifsc, "the IFSC"):
        _check_pattern(out, "routing.ifsc", ifsc, _IFSC, "IFSC_FORMAT", "IFSC is 4 letters, 0, then 6 letters or digits", "IFSC_FORMAT")


def _rules_ae(account: BankAccountDetails, out: _Collector, wps: bool) -> None:
    _check_iban(account, out, required=True)
    _account_number_unused(account, out)
    code = account.routing.get("wps_routing_code")
    if wps:
        _require(out, "routing.wps_routing_code", code, "the WPS agent routing code")
    _check_pattern(
        out, "routing.wps_routing_code", code, _NINE_DIGITS, "WPS_ROUTING_FORMAT",
        "the WPS agent routing code has 9 digits", "WPS_ROUTING_FORMAT",
    )


def _rules_iban_only(account: BankAccountDetails, out: _Collector, wps: bool) -> None:
    _check_iban(account, out, required=True)
    _account_number_unused(account, out)


def _rules_gb(account: BankAccountDetails, out: _Collector, wps: bool) -> None:
    sort_code = account.routing.get("sort_code")
    if _require(out, "routing.sort_code", sort_code, "the sort code"):
        _check_pattern(out, "routing.sort_code", sort_code, _SIX_DIGITS, "SORT_CODE_FORMAT", "the sort code has 6 digits", "SORT_CODE_FORMAT")
    if _require(out, "account_number", account.account_number, "the account number"):
        _check_account_digits(out, account.account_number or "", _GB_ACCOUNT, "UK account numbers have 8 digits")
    _check_iban(account, out, required=False)
    if (
        account.iban is not None
        and not out.has_error("iban")
        and not out.has_error("routing.sort_code")
        and not out.has_error("account_number")
        and sort_code is not None
        and account.account_number is not None
        and (account.iban[8:14], account.iban[14:22]) != (sort_code, account.account_number)
    ):
        out.error("iban", "IBAN_ACCOUNT_MISMATCH", "the IBAN does not contain this sort code and account number")
    out.warn(
        "account_number", "UK_MODULUS_NOT_CHECKED",
        "UK bank modulus checking was not performed (needs the Vocalink tables); the format is valid only",
    )


def _rules_us(account: BankAccountDetails, out: _Collector, wps: bool) -> None:
    _check_iban(account, out, required=False)
    aba = account.routing.get("aba")
    if _require(out, "routing.aba", aba, "the ABA routing number") and aba is not None:
        if not _NINE_DIGITS.fullmatch(aba):
            out.error("routing.aba", "ABA_FORMAT", "the ABA routing number has 9 digits")
        elif int(aba[:2]) not in _ABA_PREFIXES:
            out.error("routing.aba", "ABA_PREFIX", "the routing number does not start with a valid Federal Reserve prefix")
        elif not aba_checksum_ok(aba):
            out.error("routing.aba", "ABA_CHECKSUM", "the routing number check digit does not match")
        else:
            out.checks.append("ABA_CHECKSUM")
    if _require(out, "account_number", account.account_number, "the account number"):
        _check_account_digits(out, account.account_number or "", _US_ACCOUNT, "US account numbers have 4 to 17 digits")


def _rules_international(account: BankAccountDetails, out: _Collector, wps: bool) -> None:
    if account.iban is None and account.account_number is None:
        out.error("account_number", "ACCOUNT_IDENTIFIER_REQUIRED", "enter the IBAN or the account number")
    _check_iban(account, out, required=False)
    _account_number_unused(account, out)
    _check_pattern(
        out, "account_number", account.account_number, _GENERIC_ACCOUNT, "ACCOUNT_NUMBER_FORMAT",
        "the account number has 1 to 34 letters or digits", "ACCOUNT_NUMBER_FORMAT",
    )
    if account.country == "AU":
        bsb = account.routing.get("bsb")
        _check_pattern(out, "routing.bsb", bsb, _SIX_DIGITS, "BSB_FORMAT", "the BSB has 6 digits", "BSB_FORMAT")
    _require(out, "routing.bic", account.routing.get("bic"), "the BIC")
    if account.bank_address is None:
        out.error("bank_address", "REQUIRED", "the bank address is required for international accounts")
    elif len(account.bank_address) > MAX_BANK_ADDRESS or has_unsafe_characters(account.bank_address):
        out.error("bank_address", "BANK_ADDRESS_INVALID", f"use at most {MAX_BANK_ADDRESS} printable characters")


_RULES: Mapping[str, Callable[[BankAccountDetails, _Collector, bool], None]] = MappingProxyType(
    {
        "IN": _rules_in,
        "AE": _rules_ae,
        "SA": _rules_iban_only,
        "GB": _rules_gb,
        "US": _rules_us,
        "SEPA": _rules_iban_only,
        "INTERNATIONAL": _rules_international,
    }
)


def _check_generic_routing(account: BankAccountDetails, out: _Collector) -> None:
    bic = account.routing.get("bic")
    if _check_pattern(out, "routing.bic", bic, _BIC, "BIC_FORMAT", "BIC is 8 or 11 characters: bank, country, location, branch", "BIC_FORMAT"):
        expected = {account.country, IBAN_TERRITORIES.get(account.country, account.country)}
        if account.iban is not None:
            expected.add(account.iban[:2])
        if bic is not None and bic[4:6] not in expected:
            out.warn("routing.bic", "BIC_COUNTRY_MISMATCH", f"the BIC belongs to a bank in {bic[4:6]}, not {account.country}")
    _check_pattern(out, "routing.bank_code", account.routing.get("bank_code"), _BANK_CODE, "BANK_CODE_FORMAT", "use 1 to 15 letters or digits", "BANK_CODE_FORMAT")
    _check_pattern(out, "routing.branch_code", account.routing.get("branch_code"), _BANK_CODE, "BRANCH_CODE_FORMAT", "use 1 to 15 letters or digits", "BRANCH_CODE_FORMAT")


def _check_account_type(account: BankAccountDetails, rules: str, out: _Collector) -> None:
    allowed = _REQUIRED_ACCOUNT_TYPES.get(rules)
    value = account.account_type
    if value is None:
        if allowed is not None:
            out.error("account_type", "REQUIRED", f"the account type is required ({', '.join(sorted(allowed))})")
        return
    options = allowed or ACCOUNT_TYPES
    if value not in options:
        out.error("account_type", "ACCOUNT_TYPE_INVALID", f"the account type must be one of {', '.join(sorted(options))}")


def _lookup(directory: BankDirectory, scheme: str, code: str, name: str, out: _Collector) -> None:
    entry = directory.lookup(scheme, code)
    if entry is None:
        out.warn(name, "DIRECTORY_NOT_FOUND", f"this {scheme} was not found in the bank directory")
    elif entry.status == "ACTIVE":
        out.checks.append(f"{scheme}_DIRECTORY")
        out.entries.append(entry)
    elif entry.status == "MERGED":
        successor = f" into {entry.successor}" if entry.successor else ""
        out.warn(name, "DIRECTORY_MERGED", f"the bank directory says this {scheme} was merged{successor}")
        out.entries.append(entry)
    else:
        out.warn(name, "DIRECTORY_INVALID", f"the bank directory marks this {scheme} as {entry.status.lower()}")
        out.entries.append(entry)


def _check_directory(account: BankAccountDetails, rules: str, directory: BankDirectory | None, out: _Collector) -> None:
    ifsc = account.routing.get("ifsc") if rules == "IN" and "IFSC_FORMAT" in out.checks else None
    bic = account.routing.get("bic") if "BIC_FORMAT" in out.checks else None
    if directory is None:
        if ifsc is not None:
            out.warn("routing.ifsc", "DIRECTORY_NOT_CHECKED", "the IFSC directory was not consulted; the format is valid only")
        return
    if ifsc is not None:
        _lookup(directory, "IFSC", ifsc, "routing.ifsc", out)
    if bic is not None:
        _lookup(directory, "BIC", bic, "routing.bic", out)


def validate_account(
    details: BankAccountDetails, *, wps: bool = False, directory: BankDirectory | None = None
) -> ValidationResult:
    """Normalise and check one bank account against the rules of its country (3.7 §2.3).

    ``wps`` makes the WPS agent routing code mandatory for UAE accounts; ``directory`` (IFSC/BIC)
    is consulted when given. Invalid details are reported in the result, never raised.
    """
    if not isinstance(details, BankAccountDetails):
        raise BankValidationError("INVALID_ARGUMENT", "details must be BankAccountDetails")
    out = _Collector()
    account = _normalize(details, out)
    if not _COUNTRY.fullmatch(account.country):
        out.error("country", "COUNTRY_INVALID", "the country must be an ISO 3166 two-letter code")
        return ValidationResult("UNKNOWN", account, tuple(out.errors), (), ())
    if _too_long(account, out):
        return ValidationResult(rule_set(account.country), account, tuple(out.errors), (), ())
    rules = rule_set(account.country)
    _check_holder(account, out)
    _check_foreign_routing(account, out)
    _RULES[rules](account, out, wps)
    _check_generic_routing(account, out)
    _check_account_type(account, rules, out)
    _check_directory(account, rules, directory, out)
    return ValidationResult(rules, account, tuple(out.errors), tuple(out.warnings), tuple(out.checks), tuple(out.entries))


def account_identity(account: BankAccountDetails) -> tuple[str, str, str]:
    """``(country, routing, account)`` naming the real account, for duplicate fingerprints (3.7 §3.5).

    Expects the normalised account of a valid result. IBAN accounts are identified by the IBAN
    alone (whatever country was selected), UK-style accounts by sort code and account number,
    Indian accounts by the bank part of the IFSC (account numbers are unique within a bank), and
    other accounts by their national routing code or BIC8.
    """
    rules = rule_set(account.country)
    number = account.account_number
    if rules == "GB" and number and account.routing.get("sort_code"):
        return ("GB", account.routing["sort_code"], number)
    if account.iban:
        return (account.iban[:2], "", account.iban)
    if not number:
        raise BankValidationError("NOT_IDENTIFIABLE", "the account has neither an IBAN nor an account number")
    if rules == "IN" and account.routing.get("ifsc"):
        return ("IN", account.routing["ifsc"][:4], number)
    if rules == "US" and account.routing.get("aba"):
        return ("US", account.routing["aba"], number)
    routing = (
        account.routing.get("bsb")
        or (account.routing["bic"][:8] if account.routing.get("bic") else None)
        or account.routing.get("bank_code")
        or ""
    )
    return (account.country, routing, number)
