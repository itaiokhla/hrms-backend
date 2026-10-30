"""Payroll settings registry: every payroll setting, declared in code (3.1 §2.1).

A setting that is not declared here cannot be stored. Each ``SettingDef`` carries its type, the
PLATFORM default, the levels where it may be set (``allowed_levels``), its Settings › Payroll
page (``section``), help text that explains both ON and OFF, constraints, dependencies, the
resolution grain and the change-control flags. Country packs add their employer choices in the
``payroll.statutory.<CC>.*`` namespace with ``register_pack_settings``.

Resolution follows 02 §14: the first value found from the most specific level to the most
general one wins (EMPLOYEE, PAY_GROUP, BRANCH, LEGAL_ENTITY, TENANT, PARTNER, COUNTRY_PACK,
PLATFORM), except that a locked value beats every narrower override and a value the law requires
beats everything. Values are validated strictly and fail closed: a stored payroll value that no
longer validates is a configuration exception, never a silently substituted default (EC-CFG-09).
``cross_validate`` implements the cross-setting rules V-01..V-16 of 3.1 §7. Problems found in
stored data are returned as ``Issue`` records; only programming errors and rejected writes raise
``SettingValidationError``.

Some PLATFORM defaults depend on context (the currency's minor unit, the entity's base currency,
the branch's holiday calendar…). They are ``Derived`` markers that ``derive_default`` turns into
a value, failing closed when the context lacks the fact. A zero money amount may omit its
currency, because zero is the same in every currency ("approval above 0" means always).

Configuration profiles (decisions I-13, I-14, I-16). Tenants never inherit live PLATFORM
defaults: a profile is materialized as explicit TENANT rows. ``STARTER_V1`` (new tenants) turns
ON only features that change pay through records a person approved, or that fail closed into a
review exception; features that need a legal constant or a company decision stay OFF with a
DRAFT policy and ``decision_required``; statutory schemes are never switched ON. ``LEGACY_V1``
(existing tenants) reproduces the legacy payroll. ``new_tenant_default`` is read from
``STARTER_V1``, the single source of truth. The design's single holiday switch is split into
holiday pay for non-salaried staff and the holiday-work premium; the old key is an alias.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, time, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from app.payroll.policy_types import AssignmentRule, EmployeeAttributes

REGISTRY_VERSION: Final = "2026.10.17"
STARTER_REASON: Final = "Starter configuration"
LEGACY_REASON: Final = "Legacy payroll migration"


class _Unset:
    """Marker for "no value" where ``None`` is a legitimate value."""

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _Unset:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Unset:
        return self


UNSET: Final = _Unset()


class Level(str, Enum):
    PLATFORM = "PLATFORM"
    COUNTRY_PACK = "COUNTRY_PACK"
    PARTNER = "PARTNER"
    TENANT = "TENANT"
    LEGAL_ENTITY = "LEGAL_ENTITY"
    BRANCH = "BRANCH"
    PAY_GROUP = "PAY_GROUP"
    EMPLOYEE = "EMPLOYEE"


# First match wins (02 §14.1).
PRECEDENCE: Final[tuple[Level, ...]] = (
    Level.EMPLOYEE,
    Level.PAY_GROUP,
    Level.BRANCH,
    Level.LEGAL_ENTITY,
    Level.TENANT,
    Level.PARTNER,
    Level.COUNTRY_PACK,
    Level.PLATFORM,
)
# Levels where tenants store values (setting_values.scope_type).
SCOPE_LEVELS: Final[tuple[Level, ...]] = (
    Level.TENANT,
    Level.LEGAL_ENTITY,
    Level.BRANCH,
    Level.PAY_GROUP,
    Level.EMPLOYEE,
)

SETTING_TYPES: Final = frozenset(
    {"bool", "int", "decimal", "money", "enum", "ref", "date", "time", "string", "list", "set", "json"}
)
GRAINS: Final = ("PERIOD", "DAY")
SENSITIVITIES: Final = ("standard", "confidential", "restricted")
SEVERITIES: Final = ("ERROR", "WARNING")
SUPPORT_STATUSES: Final = ("NOT_IMPLEMENTED", "BETA", "SUPPORTED")
LEGAL_SENSITIVITIES: Final = ("NONE", "LEGAL_FLOOR", "STATUTORY")
PAYROLL_MODULE: Final = "Payroll"


class SettingValidationError(ValueError):
    """A value, definition or write that the registry rejects. ``code`` is stable for clients."""

    def __init__(
        self, code: str, message: str, *, key: str | None = None, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.key = key
        self.details: dict[str, Any] = dict(details or {})

    def __repr__(self) -> str:
        return f"SettingValidationError({self.code!r}, {self.message!r}, key={self.key!r})"


@dataclass(frozen=True)
class Issue:
    """A cross-setting, legal or stored-data problem, returned as data (3.1 §7)."""

    rule: str
    severity: str
    keys: tuple[str, ...]
    message: str
    details: Mapping[str, Any] = field(default_factory=dict, hash=False)
    # Run pre-check exception only: reported, but never blocks saving a setting (V-11).
    precheck_only: bool = False

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        object.__setattr__(self, "keys", tuple(self.keys))

    @property
    def blocks_save(self) -> bool:
        return self.severity == "ERROR" and not self.precheck_only


@dataclass(frozen=True)
class Derived:
    """A PLATFORM default that comes from context, for example the currency's minor unit."""

    source: str
    description: str


@dataclass(frozen=True)
class MoneyValue:
    """A validated money setting. ``currency`` may be None only for a zero amount."""

    amount: Decimal
    currency: str | None

    def to_json(self) -> dict[str, str | None]:
        return {"amount": decimal_str(self.amount), "currency": self.currency}


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: str
    default: Any = field(hash=False)
    allowed_levels: tuple[Level, ...]
    module: str
    section: str
    label: str
    help: str
    constraints: Mapping[str, Any] = field(default_factory=dict, hash=False)
    depends_on: tuple[str, ...] = ()
    resolution_grain: str = "PERIOD"
    effective_dated: bool = True
    sensitivity: str = "standard"
    requires_approval: bool = False
    admin_only: bool = False
    lockable: bool = True
    statutory_scheme: str | None = None
    aliases: tuple[str, ...] = ()
    ui: Mapping[str, Any] = field(default_factory=dict, hash=False)
    new_tenant_default: Any = field(default=UNSET, hash=False)
    # Configuration profiles (I-13): NOT_IMPLEMENTED features cannot be switched ON; LEGAL_FLOOR
    # values may be constrained by a country pack; decision_required = asked before the first run.
    support_status: str = "SUPPORTED"
    legal_sensitivity: str = "NONE"
    decision_required: bool = False
    # False for a key added after migration 0011 froze the LEGACY_V1 literals (3.1 §9, ARCHITECTURE §9): existing
    # tenants get no row for it and it resolves to its registry default (which must be the safe/OFF choice) until
    # an admin writes it. STARTER_V1 still materializes every tenant key (EC-CFG-23).
    legacy_v1: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_levels", tuple(Level(lv) for lv in self.allowed_levels))
        object.__setattr__(self, "constraints", MappingProxyType(dict(self.constraints)))
        object.__setattr__(self, "ui", MappingProxyType(dict(self.ui)))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "aliases", tuple(self.aliases))

    @property
    def fails_closed(self) -> bool:
        """Payroll, leave and holiday keys never fall back to a default when a stored value is invalid (3.1 §2.2;
        LH V3-085, 02 §10.1 rule 7, LH-WL-02). Leave / holiday keys whose default is ``Derived`` from context (the
        branch's ``holiday.calendar_id`` mirror) keep their documented derivation fallback."""
        if self.module == PAYROLL_MODULE:
            return True
        return self.module in ("Leave", "Holidays") and not isinstance(self.default, Derived)

    @property
    def nullable(self) -> bool:
        return bool(self.constraints.get("nullable"))


@dataclass(frozen=True)
class Section:
    """One page under Settings › Payroll (3.8 §3.1)."""

    slug: str
    title: str
    spec: str
    policy_types: tuple[str, ...] = ()


SECTIONS: Final[tuple[Section, ...]] = (
    Section("general", "General & calendars", "3.1 §5.1"),
    Section("attendance", "Attendance & LOP", "3.1 §5.2", ("ATTENDANCE_PAY_POLICY",)),
    Section("leave", "Leave pay", "3.1 §5.3", ("LEAVE_PAY_POLICY",)),
    Section("holidays", "Holidays & weekly offs", "3.1 §5.4", ("HOLIDAY_PAY_POLICY",)),
    Section("holiday-policies", "Holidays", "LH 14 WI-1.10"),
    Section("leave-policies", "Leave", "LH SPINE §10", ("LEAVE_PLAN",)),
    Section("overtime", "Overtime", "3.1 §5.5", ("OVERTIME_POLICY",)),
    Section("tasks", "Task pay", "3.1 §5.6", ("TASK_PAY_POLICY",)),
    Section("late-penalties", "Late & penalties", "3.1 §5.7", ("LATE_PENALTY_POLICY",)),
    Section("shifts", "Shifts & allowances", "3.1 §5.8", ("SHIFT_ALLOWANCE_POLICY",)),
    Section("bonus-incentives", "Bonus, incentives & arrears", "3.1 §5.9", ("INCENTIVE_PLAN",)),
    Section("reimbursements", "Reimbursements", "3.1 §5.10", ("REIMBURSEMENT_POLICY",)),
    Section("loans", "Loans & advances", "3.1 §5.11", ("LOAN_POLICY", "ADVANCE_POLICY")),
    Section("statutory", "Statutory & tax", "3.1 §5.12"),
    Section("currency", "Currency, FX & rounding", "3.1 §5.13"),
    Section("approvals", "Approvals & controls", "3.1 §5.14", ("APPROVAL_CHAIN",)),
    Section("payslips", "Payslips & documents", "3.1 §5.15", ("PAYSLIP_TEMPLATE",)),
    Section("payments", "Payments & banking", "3.1 §5.16"),
    Section("notifications", "Notifications", "3.1 §5.17"),
    Section("accounting", "Accounting", "3.1 §5.18"),
    Section("privacy", "Retention & privacy", "3.1 §5.19"),
    Section("tables", "Tables", "3.3 §4.2"),
)
SECTION_BY_TITLE: Final[Mapping[str, Section]] = MappingProxyType({s.title: s for s in SECTIONS})
SECTION_BY_SLUG: Final[Mapping[str, Section]] = MappingProxyType({s.slug: s for s in SECTIONS})


# --------------------------------------------------------------------------- value formats

_DECIMAL_RE = re.compile(r"-?\d{1,15}(\.\d{1,12})?")
_INT_DIGITS = 15
_MAX_PLACES = 12
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
LOCALE_RE: Final = re.compile(r"[a-z]{2,3}(-[A-Z][a-z]{3})?(-(?:[A-Z]{2}|\d{3}))?")
CODE_RE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,49}")
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+)+")
_STATUTORY_KEY_RE = re.compile(r"payroll\.statutory\.([A-Z]{2})\.[a-z0-9_]+(\.[a-z0-9_]+)*")
_NUMBERING_TOKEN_RE = re.compile(r"\{(LE|FY|YYYY|MM|SEQ:(\d{1,2}))\}")
_NUMBERING_LITERAL_RE = re.compile(r"[A-Za-z0-9/_.\-]*")
_DEFAULT_MAX_LENGTH = 500


def decimal_str(value: Decimal) -> str:
    """Canonical decimal text: plain notation, no trailing zeros, never "-0"."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "", "-") else text


def _places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def parse_decimal(value: object, *, what: str = "value", key: str | None = None) -> Decimal:
    """Strict decimal input: a decimal string or a Decimal, never a float, int or bool."""
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise SettingValidationError(
            "INVALID_TYPE", f"{what} must be a decimal string such as \"1.5\"", key=key
        )
    if isinstance(value, str):
        if not _DECIMAL_RE.fullmatch(value):
            raise SettingValidationError("INVALID_DECIMAL", f"{what} is not a plain decimal number", key=key)
        return Decimal(value)
    if not value.is_finite():
        raise SettingValidationError("INVALID_DECIMAL", f"{what} must be a finite number", key=key)
    if value != 0 and (value.adjusted() >= _INT_DIGITS or _places(value) > _MAX_PLACES):
        raise SettingValidationError("INVALID_DECIMAL", f"{what} is out of range", key=key)
    return value


def _check_numbering_pattern(text: str) -> str | None:
    seq = [m for m in _NUMBERING_TOKEN_RE.finditer(text) if m.group(1).startswith("SEQ")]
    if len(seq) != 1:
        return "must contain exactly one {SEQ:n} token"
    width = int(seq[0].group(2))
    if not 1 <= width <= 12:
        return "{SEQ:n} width must be between 1 and 12"
    literal = _NUMBERING_TOKEN_RE.sub("", text)
    if "{" in literal or "}" in literal:
        return "unknown token; use {LE}, {FY}, {YYYY}, {MM} and {SEQ:n}"
    if not _NUMBERING_LITERAL_RE.fullmatch(literal):
        return "may contain only letters, digits, / _ . - and tokens"
    return None


_FORMATS: Final[Mapping[str, Callable[[str], str | None]]] = MappingProxyType(
    {
        "iso4217": lambda s: None if _CURRENCY_RE.fullmatch(s) else "must be an ISO 4217 code such as INR",
        "code": lambda s: None if CODE_RE.fullmatch(s) else "must be an upper-case code such as US_NACHA",
        "locale": lambda s: None if LOCALE_RE.fullmatch(s) else "must be a locale such as en-IN",
        "numbering_pattern": _check_numbering_pattern,
    }
)


# --------------------------------------------------------------------------- the catalogue

T, LE, B, PG, E = Level.TENANT, Level.LEGAL_ENTITY, Level.BRANCH, Level.PAY_GROUP, Level.EMPLOYEE
_T: Final = (T,)
_T_LE: Final = (T, LE)
_T_LE_PG: Final = (T, LE, PG)
_PG: Final = (PG,)

S_GENERAL = "General & calendars"
S_ATTENDANCE = "Attendance & LOP"
S_LEAVE = "Leave pay"
S_HOLIDAYS = "Holidays & weekly offs"
S_HOLIDAY_POLICIES = "Holidays"
S_LEAVE_POLICIES = "Leave"
S_OVERTIME = "Overtime"
S_TASKS = "Task pay"
S_LATE = "Late & penalties"
S_SHIFTS = "Shifts & allowances"
S_BONUS = "Bonus, incentives & arrears"
S_REIMBURSEMENTS = "Reimbursements"
S_LOANS = "Loans & advances"
S_STATUTORY = "Statutory & tax"
S_CURRENCY = "Currency, FX & rounding"
S_APPROVALS = "Approvals & controls"
S_PAYSLIPS = "Payslips & documents"
S_PAYMENTS = "Payments & banking"
S_NOTIFICATIONS = "Notifications"
S_ACCOUNTING = "Accounting"
S_PRIVACY = "Retention & privacy"

_MODULE_BY_PREFIX: Final = (
    ("tasks.", "Tasks"),
    ("leave.", "Leave"),
    ("holiday.", "Holidays"),
    ("attendance.", "Attendance"),
    ("org.", "Organization"),
    ("settings.", "Settings"),
)
_CONTROL_BY_TYPE: Final = {
    "bool": "switch",
    "int": "number",
    "decimal": "number",
    "money": "money",
    "enum": "select",
    "ref": "reference",
    "date": "date",
    "time": "time",
    "string": "text",
    "list": "tags",
    "set": "checkboxes",
    "json": "json",
}
_WEEKDAYS: Final = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

# Documents numbered from a tenant pattern (3.6 §2, §9); pack-numbered documents are not listed.
NUMBERED_DOCUMENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "PAYSLIP": "PS",
        "PAYMENT_ADVICE": "PA",
        "INVOICE": "INV",
        "CREDIT_NOTE": "CN",
        "PAYROLL_REGISTER": "REG",
        "BANK_TRANSFER_REPORT": "BTR",
        "FUNDING_REQUEST": "FR",
        "FINAL_SETTLEMENT_STATEMENT": "FS",
        "SALARY_CERTIFICATE": "SC",
        "EMPLOYMENT_LETTER": "EL",
        "INCREMENT_LETTER": "IL",
    }
)

# Context facts that Derived defaults read (derive_default).
DERIVED_SOURCES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ENTITY_BASE_CURRENCY": "entity_base_currency",
        "CURRENCY_MINOR_UNIT": "currency_minor_unit",
        "COUNTRY_BANKING_CALENDAR": "country_banking_calendar_id",
        "BRANCH_HOLIDAY_CALENDAR": "branch_holiday_calendar_id",
        "DEFAULT_PAYROLL_APPROVAL_CHAIN": "default_payroll_approval_chain_id",
        "ENTITY_PRIMARY_FUNDING_ACCOUNT": "entity_primary_funding_account_id",
        "TENANT_LOCALE": "tenant_locale",
        "TENANT_CURRENCY_NOTATION": "tenant_currency",
    }
)


# Derived defaults that name an optional record. When none exists the setting is unset rather than
# invalid: the banking calendar falls back to the country's national holiday calendar (else weekends
# only), approvals use the engine's default chain (holders of the module's approve permission), and the
# funding account is chosen when a payment batch is created.
OPTIONAL_REFERENCE_SOURCES: Final = frozenset(
    {"COUNTRY_BANKING_CALENDAR", "DEFAULT_PAYROLL_APPROVAL_CHAIN", "ENTITY_PRIMARY_FUNDING_ACCOUNT"}
)


def _module_of(key: str) -> str:
    for prefix, module in _MODULE_BY_PREFIX:
        if key.startswith(prefix):
            return module
    return PAYROLL_MODULE


def _setting(
    key: str,
    type_: str,
    default: Any,
    levels: Sequence[Level],
    section: str,
    label: str,
    help_: str,
    *,
    constraints: Mapping[str, Any] | None = None,
    ui: Mapping[str, Any] | None = None,
    module: str | None = None,
    **options: Any,
) -> SettingDef:
    return SettingDef(
        key=key,
        type=type_,
        default=default,
        allowed_levels=tuple(levels),
        module=module or _module_of(key),
        section=section,
        label=label,
        help=help_,
        constraints=dict(constraints or {}),
        ui={"control": _CONTROL_BY_TYPE[type_], **(ui or {})},
        **options,
    )


def _flag(
    key: str,
    label: str,
    *,
    on: str,
    off: str,
    default: bool,
    levels: Sequence[Level],
    section: str,
    ui: Mapping[str, Any] | None = None,
    **options: Any,
) -> SettingDef:
    return _setting(key, "bool", default, levels, section, label, f"ON: {on} OFF: {off}", ui=ui, **options)


def _switch(
    key: str,
    label: str,
    *,
    on: str,
    off: str,
    levels: Sequence[Level],
    section: str,
    default: bool = False,
    policy: str | None = None,
    **options: Any,
) -> SettingDef:
    ui: dict[str, Any] = {"switch_card": True}
    if policy:
        ui["policy_type"] = policy
    return _flag(key, label, on=on, off=off, default=default, levels=levels, section=section, ui=ui, **options)


def _enum(
    key: str,
    choices: Sequence[str],
    default: str | Derived,
    levels: Sequence[Level],
    section: str,
    label: str,
    help_: str,
    *,
    constraints: Mapping[str, Any] | None = None,
    **options: Any,
) -> SettingDef:
    return _setting(
        key, "enum", default, levels, section, label, help_,
        constraints={"choices": tuple(choices), **(constraints or {})}, **options,
    )


def _exemption_codes() -> tuple[str, ...]:
    """Choices of ``shift.working_time.held_exemptions``: every exemption code of the WORKING_TIME packs."""
    from app.services.working_time_packs import EXEMPTION_CODES

    return EXEMPTION_CODES


def _numbering(doc_type: str, prefix: str) -> SettingDef:
    label = doc_type.replace("_", " ").capitalize()
    return _setting(
        f"documents.numbering.{doc_type.lower()}.pattern",
        "string",
        f"{prefix}/{{LE}}/{{FY}}/{{SEQ:6}}",
        (LE,),
        S_PAYSLIPS,
        f"{label} numbering",
        f"Number pattern for {label.lower()} documents, per legal entity. Tokens: {{LE}} entity code, "
        "{FY} fiscal year (for example 2026-27), {YYYY}, {MM} and {SEQ:n}, a zero-padded sequence of n "
        "digits. Numbers are unique per legal entity and never reused.",
        constraints={"format": "numbering_pattern", "max_length": 64},
        ui={"group": "Numbering", "control": "pattern"},
    )


def _catalogue() -> list[SettingDef]:
    on_attendance = {"key": "payroll.attendance.enabled", "equals": True}
    return [
        # 3.1 §4 and §5.1 — General and pay calendar
        _switch(
            "payroll.enabled", "Payroll",
            on="The payroll workspace is available.",
            off="Payroll menus are hidden and no runs can be created.",
            levels=_T, section=S_GENERAL,
        ),
        _flag(
            "org.allow_concurrent_assignments", "Allow concurrent assignments",
            on="An employee may hold several assignments at once, each paid in its own pay group.",
            off="One assignment at a time.",
            default=False, levels=_T, section=S_GENERAL,
        ),
        _enum(
            "payroll.pay_frequency", ("MONTHLY", "SEMI_MONTHLY", "BI_WEEKLY", "WEEKLY", "DAILY"), "MONTHLY",
            _PG, S_GENERAL, "Pay frequency",
            "How often the pay group is paid: monthly, semi-monthly, bi-weekly, weekly or daily.",
            ui={"group": "Pay calendar"},
        ),
        _setting(
            "payroll.calendar.monthly_start_day", "int", 1, _PG, S_GENERAL, "Monthly period start day",
            "First day of a monthly period. Values beyond a month's length clamp to its last day "
            "(31 becomes 28 or 29 February, 30 April).",
            constraints={"min": 1, "max": 31},
            ui={"group": "Pay calendar", "unit": "day of month",
                "visible_when": {"key": "payroll.pay_frequency", "equals": "MONTHLY"}},
        ),
        _setting(
            "payroll.calendar.semi_monthly_split_day", "int", 15, _PG, S_GENERAL, "Semi-monthly split day",
            "Last day of the first half; the second half runs from the next day to the month end.",
            constraints={"min": 1, "max": 27},
            ui={"group": "Pay calendar", "unit": "day of month",
                "visible_when": {"key": "payroll.pay_frequency", "equals": "SEMI_MONTHLY"}},
        ),
        _enum(
            "payroll.calendar.week_start_day", _WEEKDAYS, "MON", _PG, S_GENERAL, "Week start day",
            "First day of weekly and bi-weekly periods.",
            ui={"group": "Pay calendar",
                "visible_when": {"key": "payroll.pay_frequency", "in": ["WEEKLY", "BI_WEEKLY"]}},
        ),
        _setting(
            "payroll.calendar.biweekly_anchor_date", "date", None, _PG, S_GENERAL, "Bi-weekly anchor date",
            "Start date of the first bi-weekly period; later periods follow every 14 days. "
            "Required for bi-weekly pay groups.",
            constraints={"nullable": True},
            ui={"group": "Pay calendar",
                "visible_when": {"key": "payroll.pay_frequency", "equals": "BI_WEEKLY"}},
        ),
        _enum(
            "payroll.calendar.pay_day_rule", ("LAST_DAY", "LAST_WORKING_DAY", "DAY_OF_MONTH", "DAYS_AFTER_PERIOD_END"),
            "LAST_WORKING_DAY", _PG, S_GENERAL, "Pay day rule",
            "How the nominal pay date is chosen: the last day of the period, its last working day, "
            "a day of the month, or a number of days after the period end.",
            ui={"group": "Pay date"},
        ),
        _setting(
            "payroll.calendar.pay_day_value", "int", None, _PG, S_GENERAL, "Pay day number",
            "N for DAY_OF_MONTH (day 1-31 of the month in which the period ends) or for "
            "DAYS_AFTER_PERIOD_END (0-31 days after the period end).",
            constraints={"min": 0, "max": 31, "nullable": True},
            ui={"group": "Pay date", "visible_when": {
                "key": "payroll.calendar.pay_day_rule", "in": ["DAY_OF_MONTH", "DAYS_AFTER_PERIOD_END"]}},
        ),
        _enum(
            "payroll.calendar.pay_day_shift",
            ("PREVIOUS_BUSINESS_DAY", "NEXT_BUSINESS_DAY", "NEAREST_BUSINESS_DAY", "NO_SHIFT"),
            "PREVIOUS_BUSINESS_DAY", _PG, S_GENERAL, "When the pay date is not a business day",
            "What happens when the pay date falls on a weekend or a bank holiday of the banking calendar: "
            "move it to the previous, next or nearest business day, or keep it.",
            ui={"group": "Pay date"},
        ),
        _setting(
            "payroll.calendar.banking_calendar_id", "ref",
            Derived("COUNTRY_BANKING_CALENDAR", "Country default"),
            (LE, PG), S_GENERAL, "Banking calendar",
            "Bank holidays used to shift pay dates and value dates. May differ from employee holidays. "
            "Defaults to the country's banking calendar.",
            ui={"group": "Pay date", "reference": "banking_calendar"},
        ),
        _enum(
            "payroll.attendance_cycle.mode", ("SAME_AS_PERIOD", "PREVIOUS_PERIOD", "CUSTOM_START_DAY"),
            "SAME_AS_PERIOD", _PG, S_GENERAL, "Attendance cycle",
            "Which dates' attendance and leave feed the period: the same dates, the previous period, or a "
            "custom start day (for example 26th-25th). Fixed pay is always for the pay period itself.",
            ui={"group": "Attendance cycle"},
        ),
        _setting(
            "payroll.attendance_cycle.start_day", "int", 26, _PG, S_GENERAL, "Attendance cycle start day",
            "Start day for CUSTOM_START_DAY, for example 26 for a 26th-25th cycle. Required for that mode.",
            constraints={"min": 1, "max": 28, "nullable": True},
            ui={"group": "Attendance cycle", "unit": "day of month",
                "visible_when": {"key": "payroll.attendance_cycle.mode", "equals": "CUSTOM_START_DAY"}},
        ),
        _setting(
            "payroll.cutoff.input_days_before_pay_day", "int", 5, _PG, S_GENERAL, "Input cutoff",
            "Inputs received later than this many days before the pay date go to the next period or "
            "become exceptions.",
            constraints={"min": 0, "max": 31},
            ui={"group": "Cutoffs", "unit": "days before pay day"},
        ),
        _setting(
            "payroll.cutoff.approval_days_before_pay_day", "int", 2, _PG, S_GENERAL, "Approval deadline",
            "Approval deadline shown on the run, in days before the pay date.",
            constraints={"min": 0, "max": 31},
            ui={"group": "Cutoffs", "unit": "days before pay day"},
        ),
        _setting(
            "payroll.cutoff.payment_lead_business_days", "int", 1, (LE, PG), S_GENERAL, "Payment lead time",
            "Business days the bank needs before the value date.",
            constraints={"min": 0, "max": 10},
            ui={"group": "Cutoffs", "unit": "business days"},
        ),
        _setting(
            "payroll.standard_hours_per_day", "decimal", Decimal("8.0"), (PG, E), S_GENERAL,
            "Standard hours per day",
            "Hours in a standard working day, used for hourly rates. Replaces the environment value "
            "DEFAULT_WORK_HOURS_PER_DAY.",
            constraints={"min": Decimal("1"), "max": Decimal("24"), "max_places": 4},
            ui={"group": "Working hours", "unit": "hours"},
        ),
        _flag(
            "payroll.standard_hours.use_shift_duration", "Use shift duration",
            on="Hourly rates use the duration of the employee's shift.",
            off="Hourly rates use the standard hours per day.",
            default=True, levels=_PG, section=S_GENERAL, ui={"group": "Working hours"},
        ),
        _setting(
            "payroll.currency", "string", Derived("ENTITY_BASE_CURRENCY", "Entity base currency"),
            _PG, S_GENERAL, "Pay group currency",
            "Calculation currency of the pay group (ISO 4217). Defaults to the legal entity's base currency.",
            constraints={"format": "iso4217", "max_length": 3},
            ui={"group": "Currency", "control": "currency"},
        ),
        _flag(
            "payroll.run.allow_multiple_regular_runs", "Allow several regular runs per period",
            on="More than one regular run may be created for a period.",
            off="Only one regular run per period.",
            default=False, levels=_PG, section=S_GENERAL,
        ),
        _flag(
            "payroll.structures.net_based_enabled", "Net-based salary structures",
            on="Net-based salary structures and the gross-up solver are available.",
            off="Salary structures are gross- or CTC-based only.",
            default=False, levels=_T, section=S_GENERAL,
        ),
        _flag(
            "payroll.compensation.enforce_grade_range", "Enforce grade salary ranges",
            on="A salary outside the grade's range is blocked.",
            off="A salary outside the grade's range is saved with a warning.",
            default=False, levels=_T_LE, section=S_GENERAL,
        ),
        # 3.1 §5.2 — Attendance and LOP
        _switch(
            "payroll.attendance.enabled", "Use attendance for payroll",
            on="Approved attendance feeds the day ledger; absences become LOP.",
            off="Punches never change pay. Pay is prorated only by employment dates and manual LOP inputs.",
            levels=_T_LE_PG, section=S_ATTENDANCE, policy="ATTENDANCE_PAY_POLICY",
            requires_approval=True,
        ),
        _setting(
            "payroll.attendance.half_day_value", "decimal", Decimal("0.5"), _T_LE_PG, S_ATTENDANCE,
            "Paid fraction of a half day",
            "Paid fraction of a half-day attendance: 0, 0.5 or 1. Only these values keep the half-day slot "
            "model exact.",
            constraints={"choices": (Decimal("0"), Decimal("0.5"), Decimal("1"))},
            depends_on=("payroll.attendance.enabled",), requires_approval=True,
            ui={"control": "select", "visible_when": on_attendance},
        ),
        # JC-AS-12 / JC-AS-23 (AS 05 §6): severity of the attendance pending items at the cutoff (consumed from AS-M4).
        _enum(
            "payroll.attendance.pending_at_cutoff", ("EXCEPTION", "WARNING"), "EXCEPTION", (T, LE), S_ATTENDANCE,
            "Attendance still pending at cutoff",
            "Attendance requests and undecided days still pending at the input cutoff: raise a blocking exception "
            "(waivable), or only warn.",
            depends_on=("payroll.attendance.enabled",), legacy_v1=False, ui={"visible_when": on_attendance},
        ),
        _enum(
            "payroll.lop.proration_basis",
            ("CALENDAR_DAYS", "WORKING_DAYS", "FIXED_30", "FIXED_26", "CUSTOM_DAYS", "HOURS"),
            "CALENDAR_DAYS", _T_LE_PG, S_ATTENDANCE, "Proration basis",
            "Denominator for prorating pay: calendar days, scheduled working days, a fixed 30 or 26 days, a "
            "custom number of days, or hours (needs attendance). Fixed bases treat every month as N days, so "
            "joining on 2 February pays 27/30 under FIXED_30. Use CALENDAR_DAYS unless a contract, collective "
            "agreement or law requires another basis.",
            requires_approval=True, legal_sensitivity="LEGAL_FLOOR", ui={"group": "LOP and proration"},
        ),
        _setting(
            "payroll.lop.custom_days", "int", None, _T_LE_PG, S_ATTENDANCE, "Custom basis days",
            "Basis days N when the proration basis is CUSTOM_DAYS; required for that basis.",
            constraints={"min": 1, "max": 31, "nullable": True}, requires_approval=True,
            ui={"group": "LOP and proration", "unit": "days",
                "visible_when": {"key": "payroll.lop.proration_basis", "equals": "CUSTOM_DAYS"}},
        ),
        _enum(
            "payroll.lop.sandwich_rule", ("OFF", "WEEKLY_OFFS", "HOLIDAYS", "BOTH"), "OFF", _T_LE_PG,
            S_ATTENDANCE, "Sandwich rule",
            "Count weekly offs, holidays or both as LOP when the scheduled working days immediately before "
            "and after them are both full LOP days. OFF never reclassifies them.",
            requires_approval=True, ui={"group": "LOP and proration"},
        ),
        _enum(
            "payroll.lop.zero_paid_days", ("EXCEPTION", "PAY_ZERO"), "EXCEPTION", _T_LE_PG, S_ATTENDANCE,
            "Employees with no paid working day",
            "EXCEPTION sends an employee with no paid working day in the period to review (\"No paid working "
            "days - confirm before paying\"); PAY_ZERO pays zero for LOP-applicable components. Nobody is "
            "silently paid zero by default.",
            requires_approval=True, ui={"group": "LOP and proration"},
        ),
        # 3.1 §5.3 — Leave
        _switch(
            "payroll.leave.enabled", "Use leave for payroll",
            on="Each leave type is mapped to full, unpaid, partial, tiered or statutory pay.",
            off="Leave never changes pay (approved leave days follow the setting below when attendance is on).",
            levels=_T_LE_PG, section=S_LEAVE, policy="LEAVE_PAY_POLICY",
            requires_approval=True,
        ),
        _enum(
            "payroll.leave.when_disabled_treat_approved_leave_as", ("PAID_EXCUSED", "ABSENT"), "PAID_EXCUSED",
            _T_LE_PG, S_LEAVE, "Approved leave when leave pay is off",
            "Only relevant when attendance is ON and leave is OFF: PAID_EXCUSED keeps employees on approved "
            "leave from being marked absent; ABSENT treats those days as absences.",
            depends_on=("payroll.attendance.enabled",),
            ui={"visible_when": {"all": [on_attendance, {"key": "payroll.leave.enabled", "equals": False}]}},
        ),
        _enum(
            "payroll.leave.unapproved_at_cutoff", ("EXCEPTION", "ABSENT", "WAIT_AND_RETRO"), "EXCEPTION",
            _T_LE_PG, S_LEAVE, "Leave still pending at cutoff",
            "Leave still pending at the input cutoff: raise an exception, treat the days as absent, or wait "
            "and settle it as a retro adjustment once decided.",
            depends_on=("payroll.leave.enabled",),
        ),
        _flag(
            "leave.override.requires_step_up", "Step-up for leave overrides",
            on="A company-wide approver who decides a request that is not waiting for them (an override) confirms "
               "it's them first (LH-SEC-02).",
            off="Overrides need no extra confirmation (the original behaviour).",
            default=False, levels=_T, section=S_LEAVE, legacy_v1=False,
        ),
        _flag(
            "leave.plan_activation.requires_step_up", "Step-up for leave and holiday plan activation",
            on="Activating a version of a leave or holiday plan (the leave pay and holiday pay policies) needs a fresh "
               "step-up first (LH-SEC-02).",
            off="Activation needs no extra confirmation (the original behaviour).",
            default=False, levels=_T, section=S_LEAVE, legacy_v1=False,
        ),
        _flag(
            "leave.half_day.enabled", "Half-day leave",
            on="Employees may apply for half-day leave.",
            off="Leave is taken in whole days only.",
            default=True, levels=_T, section=S_LEAVE,
        ),
        # LH SPINE §10 — the Leave section (leave v2). ``leave.v2.enabled`` is written only by the leave cut-over.
        _flag(
            "leave.v2.enabled", "Leave v2",
            on="Balances come from the leave ledger and leave plans decide entitlement, accrual and year end (set by "
               "the leave cut-over).",
            off="Legacy leave: each year starts again from the leave type's yearly quota.",
            default=False, levels=_T, section=S_LEAVE_POLICIES, legacy_v1=False, lockable=False,
            effective_dated=False, ui={"system_written": True, "policy_type": "LEAVE_PLAN"},
        ),
        _setting(
            "leave.go_live_date", "date", None, _T_LE, S_LEAVE_POLICIES, "Leave go-live date",
            "The first day the leave plans and the leave ledger apply. Empty: the cut-over date.",
            constraints={"nullable": True}, legacy_v1=False,
        ),
        _setting(
            "leave.unpaid_type_code", "string", "unpaid", _T_LE, S_LEAVE_POLICIES, "Unpaid leave type",
            "The leave type used when days above a balance become unpaid leave, and for conversions to unpaid.",
            constraints={"regex": r"[a-z][a-z0-9_]{1,29}", "max_length": 30}, legacy_v1=False,
        ),
        _flag(
            "leave.hourly.enabled", "Hourly leave",
            on="Leave types counted in hours can be created and booked by the hour.",
            off="Leave is counted in days and half days.",
            default=False, levels=_T_LE, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _flag(
            "leave.apply_on_behalf.enabled", "Apply leave for others",
            on="Managers and HR with the permission may apply for leave on someone's behalf.",
            off="Only the employee applies; HR may still record approved leave.",
            default=True, levels=_T_LE, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _flag(
            "leave.plan.requires_second_person", "Second person activates leave plans",
            on="Activating a leave plan version needs a second leave administrator; the first only requests it.",
            off="One leave administrator activates plan versions.",
            default=False, levels=_T_LE, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _flag(
            "leave.approval.reject_reason_required", "Reason required to reject",
            on="Rejecting or returning a request needs a reason the employee can read.",
            off="A reason is optional when rejecting.",
            default=True, levels=_T, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _flag(
            "leave.approval.return_enabled", "Send back for changes",
            on="Approvers may return a request to the employee for changes instead of rejecting it.",
            off="Approvers only approve or reject.",
            default=True, levels=_T, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _setting(
            "leave.approval.returned_expiry_days", "int", 14, _T, S_LEAVE_POLICIES, "Returned requests expire after",
            "A request sent back for changes and not resubmitted is withdrawn after this many days.",
            constraints={"min": 1, "max": 60}, ui={"unit": "days"}, legacy_v1=False,
        ),
        _setting(
            "leave.approval.sla_hours", "int", 48, _T_LE, S_LEAVE_POLICIES, "Approval reminder after",
            "Hours a request may wait at one approval step before the approver is reminded (or the request "
            "escalated).",
            constraints={"min": 1, "max": 720}, ui={"unit": "hours"}, legacy_v1=False,
        ),
        _enum(
            "leave.approval.sla_action", ("REMIND", "ESCALATE"), "REMIND", _T_LE, S_LEAVE_POLICIES,
            "When an approval waits too long",
            "REMIND notifies the approver again; ESCALATE also routes the step to the approver's manager.",
            legacy_v1=False,
        ),
        _enum(
            "leave.cancellation.requires_approval", ("ALWAYS", "IF_STARTED_OR_WITHIN_DAYS", "NEVER"), "ALWAYS",
            _T_LE, S_LEAVE_POLICIES, "Cancelling approved leave",
            "ALWAYS: an approver confirms every cancellation. IF_STARTED_OR_WITHIN_DAYS: only once the leave has "
            "started or starts within the free window. NEVER: cancellations apply at once.",
            legacy_v1=False,
        ),
        _setting(
            "leave.cancellation.free_window_days", "int", 0, _T_LE, S_LEAVE_POLICIES, "Free cancellation window",
            "With IF_STARTED_OR_WITHIN_DAYS: leave starting more than this many days ahead is cancelled at once.",
            constraints={"min": 0, "max": 90}, ui={"unit": "days"}, legacy_v1=False,
        ),
        _enum(
            "leave.team_calendar.visibility", ("OFF", "TEAM", "DEPARTMENT", "BRANCH", "COMPANY"), "TEAM",
            (T, LE, B), S_LEAVE_POLICIES, "Who sees colleagues' leave",
            "Colleagues see approved leave as 'Away' with dates only, never the reason or balance. TEAM = people "
            "with the same manager and that manager.",
            legacy_v1=False,
        ),
        _flag(
            "leave.team_calendar.show_type", "Show the leave type to colleagues",
            on="Colleagues see the leave type of standard (not sensitive) leave.",
            off="Colleagues only see 'Away'.",
            default=False, levels=_T, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _flag(
            "leave.discussion.enabled", "Leave discussions",
            on="Employees and approvers can comment on a request.",
            off="No comments on leave requests.",
            default=True, levels=_T, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _setting(
            "leave.documents.max_file_mb", "int", 10, _T, S_LEAVE_POLICIES, "Largest leave document",
            "The largest file that can be attached to a leave request.",
            constraints={"min": 1, "max": 25}, ui={"unit": "MB"}, legacy_v1=False,
        ),
        _setting(
            "leave.documents.max_files", "int", 5, _T, S_LEAVE_POLICIES, "Documents per request",
            "How many files can be attached to one leave request.",
            constraints={"min": 1, "max": 10}, legacy_v1=False,
        ),
        _enum(
            "leave.year_end.mode", ("MANUAL_REVIEW", "AUTO_POST"), "MANUAL_REVIEW", _T_LE, S_LEAVE_POLICIES,
            "Year-end processing",
            "MANUAL_REVIEW: HR previews carry-forward, lapse and encashment and a second person posts them. AUTO_POST: "
            "the year end posts itself after the cycle ends.",
            legacy_v1=False,
        ),
        _flag(
            "leave.comp_off.claims.enabled", "Comp-off claims",
            on="Employees who work on a holiday or weekly off can claim compensatory leave.",
            off="Comp-off is credited only by payroll from overtime.",
            default=True, levels=(T, LE, B), section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _setting(
            "leave.expiry_reminder.days_before", "int", 30, _T_LE, S_LEAVE_POLICIES, "Expiry reminder",
            "Days before carried-forward or comp-off credit expires that the employee is reminded.",
            constraints={"min": 1, "max": 120}, ui={"unit": "days"}, legacy_v1=False,
        ),
        _setting(
            "leave.adjustments.step_up_units", "decimal", Decimal("10"), _T, S_LEAVE_POLICIES,
            "Large balance adjustment",
            "A manual balance adjustment of more than this many days or hours needs a fresh identity check.",
            constraints={"min": Decimal("0.5"), "max": Decimal("366")}, legacy_v1=False,
        ),
        _flag(
            "leave.notifications.email.enabled", "Leave emails",
            on="Leave decisions and approval requests are also emailed.",
            off="Leave notifications stay in the app.",
            default=True, levels=_T, section=S_LEAVE_POLICIES, legacy_v1=False,
        ),
        _switch(
            "payroll.leave.encashment.enabled", "Leave encashment",
            on="Balances are paid out on exit and/or at year end, per the leave pay policy's encashment fields.",
            off="Leave balances are never paid out (final settlement still raises an encashment decision).",
            levels=_T_LE_PG, section=S_LEAVE, policy="LEAVE_PAY_POLICY", legal_sensitivity="LEGAL_FLOOR",
        ),
        _enum(
            "payroll.leave.negative_balance_on_exit", ("RECOVER", "WRITE_OFF", "EXCEPTION"), "EXCEPTION",
            _T_LE, S_LEAVE, "Negative leave balance on exit",
            "Leaving with a negative leave balance: recover it (days x rate, within the protected net pay), "
            "write it off, or raise an exception for review.",
        ),
        # 3.1 §5.4 — Holidays and weekly offs
        _switch(
            "payroll.holiday_pay.non_salaried.enabled", "Holiday pay for hourly and daily workers",
            on="Hourly and daily workers are paid for holidays as the holiday pay policy says (standard hours or "
               "average daily earnings, with optional adjacent-day eligibility).",
            off="No separate holiday pay for hourly or daily workers unless the law requires it. Monthly salaried "
                "employees are always paid for holidays as part of salary; that is not a switch.",
            levels=_T_LE_PG, section=S_HOLIDAYS, policy="HOLIDAY_PAY_POLICY",
            resolution_grain="DAY", aliases=("payroll.holiday_pay.enabled",), legal_sensitivity="LEGAL_FLOOR",
        ),
        _switch(
            "payroll.holiday_work_premium.enabled", "Holiday-work premium",
            on="Work on a holiday earns the policy's premium or comp-off, up to standard hours; hours beyond go "
               "to the overtime HOLIDAY bucket when overtime is on.",
            off="Working on a holiday earns no premium (the overtime HOLIDAY bucket may still apply).",
            levels=_T_LE_PG, section=S_HOLIDAYS, policy="HOLIDAY_PAY_POLICY",
            resolution_grain="DAY", legal_sensitivity="LEGAL_FLOOR",
        ),
        _switch(
            "payroll.weekly_off_pay.enabled", "Weekly-off work pay",
            on="Work on a weekly off earns the policy's premium or comp-off, up to standard hours; hours beyond go "
               "to the overtime WEEKLY_OFF bucket when overtime is on.",
            off="Working on a weekly off earns nothing extra (overtime may still apply).",
            levels=_T_LE_PG, section=S_HOLIDAYS, policy="HOLIDAY_PAY_POLICY",
            resolution_grain="DAY", legal_sensitivity="LEGAL_FLOOR",
        ),
        _setting(
            "holiday.calendar_id", "ref", Derived("BRANCH_HOLIDAY_CALENDAR", "Created by the backfill"),
            (B,), S_HOLIDAYS, "Holiday calendar",
            "The branch's holiday calendar. A transferred employee follows the calendar of the branch on each "
            "date. Created for every branch by the organisation backfill.",
            resolution_grain="DAY", ui={"reference": "holiday_calendar"},
        ),
        _setting(
            "holiday.optional_quota_per_year", "int", 2, (T, LE, B), S_HOLIDAYS, "Optional holidays per year",
            "Optional holidays an employee may choose each year.",
            constraints={"min": 0, "max": 10}, ui={"unit": "days"},
        ),
        # LH 14 WI-1.10 — holiday calendar publication (04-holiday/01 §6, 02 §6); not pay settings.
        _flag(
            "holiday.provisional_counts", "Expected holidays count",
            on="A holiday whose date is still expected (moon sighting, government notice) counts on its expected "
               "date once its year is published.",
            off="An expected holiday counts only after HR confirms its date.",
            default=True, levels=_T_LE, section=S_HOLIDAY_POLICIES, legacy_v1=False,
        ),
        _setting(
            "holiday.backdate_days", "int", 7, _T_LE, S_HOLIDAY_POLICIES, "Backdated holiday changes (days)",
            "How many days back a published calendar may still be changed: closures recorded, expected dates "
            "confirmed, moved or cancelled. Older dates are kept for payroll history.",
            constraints={"min": 0, "max": 31}, ui={"unit": "days"}, legacy_v1=False,
        ),
        _flag(
            "holiday.publish.requires_second_person", "Second person publishes holidays",
            on="Publishing a holiday year, every amendment and every backdated closure needs a second holiday "
               "publisher; the first one only requests it.",
            off="One holiday publisher publishes and amends on their own.",
            default=False, levels=_T_LE, section=S_HOLIDAY_POLICIES, legacy_v1=False,
        ),
        _flag(
            "holiday.publish.notify_employees", "Tell employees about published holidays",
            on="Employees get a notification when their holiday year is published or a date changes.",
            off="Publication and amendments are not announced to employees.",
            default=True, levels=_T, section=S_HOLIDAY_POLICIES, legacy_v1=False,
        ),
        _setting(
            "holiday.rollover.generate_on", "string", "10-01", _T, S_HOLIDAY_POLICIES, "Draft next year on",
            "The day (MM-DD) on which next year's holidays are drafted from the repeating rules for review.",
            constraints={"regex": r"(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])", "max_length": 5}, legacy_v1=False,
        ),
        _enum(
            "holiday.legacy.unknown_location", ("UNMATCHED", "REFUSE"), "UNMATCHED", _T, S_HOLIDAY_POLICIES,
            "Holiday location that matches no branch",
            "What the Holidays page does with a location that names no branch: keep it on the review calendar "
            "UNMATCHED (the original behaviour), or refuse it so the person picks a branch (LH-HOL-27).",
            legacy_v1=False,
        ),
        _setting(
            "holiday.provisional.confirm_reminder_days", "json", [14, 2], _T_LE, S_HOLIDAY_POLICIES,
            "Remind publishers before expected holidays",
            "Days before an expected holiday on which holiday publishers are reminded to confirm its date "
            "(whole numbers 0-60).",
            legacy_v1=False,
        ),
        # 3.1 §5.5 — Overtime
        _switch(
            "payroll.overtime.enabled", "Overtime pay",
            on="Approved overtime is paid by multiplier buckets.",
            off="No overtime earnings (hours are still recorded and reported). Locked ON for workers the law covers.",
            levels=_T_LE_PG, section=S_OVERTIME, policy="OVERTIME_POLICY",
            resolution_grain="DAY", requires_approval=True, legal_sensitivity="LEGAL_FLOOR",
        ),
        # 3.1 §5.6 — Task-based pay
        _switch(
            "tasks.enabled", "Tasks module",
            on="Tasks can be created, submitted and approved.",
            off="No tasks menu.",
            levels=_T, section=S_TASKS,
        ),
        _switch(
            "payroll.task_pay.enabled", "Task-based pay",
            on="Approved tasks generate TASK_EARNING lines. Requires the tasks module.",
            off="Approved tasks generate no earnings.",
            levels=_T_LE_PG, section=S_TASKS, policy="TASK_PAY_POLICY",
            depends_on=("tasks.enabled",), resolution_grain="DAY",
        ),
        # 3.1 §5.7 — Late, early exit and penalties
        _switch(
            "payroll.late_penalty.enabled", "Apply late / early-exit penalties",
            on="Late marks become LOP fractions or amounts, after grace and caps, where lawful. Requires attendance.",
            off="Late marks never change pay.",
            levels=_T_LE_PG, section=S_LATE, policy="LATE_PENALTY_POLICY",
            depends_on=("payroll.attendance.enabled",), resolution_grain="DAY", requires_approval=True,
            legal_sensitivity="LEGAL_FLOOR",
        ),
        _setting(
            "attendance.grace_minutes", "int", 15, (T, LE, B), S_LATE, "Grace time",
            "Minutes after the shift start before a check-in counts as late. Flexible shifts are never late.",
            constraints={"min": 0, "max": 240}, aliases=("graceTime",), resolution_grain="DAY",
            ui={"unit": "minutes"},
        ),
        # AS-M3 evaluation settings (AS 04-attendance/02 §3.3; 04-attendance/03 §2.2). ``attendance.v2.enabled`` is
        # written only by the cut-over (spine §15.1): the settings API refuses it (``ui.system_written``).
        _flag(
            "attendance.v2.enabled", "Attendance v2",
            on="Days are evaluated and payroll reads the evaluations (set by the attendance cut-over).",
            off="Legacy attendance: payroll reads the sessions as before.",
            default=False, levels=_T, section=S_ATTENDANCE, legacy_v1=False, lockable=False,
            effective_dated=False, ui={"system_written": True},
        ),
        _flag(
            "attendance.capture.enabled", "Attendance capture",
            on="People punch in and out.",
            off="Nobody punches: expected days are presumed present (keep payroll attendance off in the same scope).",
            default=True, levels=(T, LE, B), section=S_ATTENDANCE, legacy_v1=False, resolution_grain="DAY",
        ),
        _setting(
            "attendance.day_close.buffer_minutes", "int", 240, (T, LE, B), S_ATTENDANCE, "Day close buffer",
            "Minutes after the last shift end before the day is decided (absent, missing punch).",
            constraints={"min": 0, "max": 720}, legacy_v1=False, resolution_grain="DAY", ui={"unit": "minutes"},
        ),
        _setting(
            "attendance.night_window", "json", {"start": "22:00", "end": "06:00"}, (T, LE, B), S_SHIFTS,
            "Night window",
            "Local start and end (HH:MM) of the night: worked time inside it is the day's night minutes.",
            legacy_v1=False, resolution_grain="DAY",
        ),
        # AS-M4 approvals of attendance requests (04-attendance/04 §9.5; SP-AS-027, SP-AS-227). No AUTO_APPROVE.
        _setting(
            "attendance.approval.sla_hours", "int", 48, (T, LE), S_ATTENDANCE, "Approval due within",
            "Hours an approver has to decide an attendance request before reminders start.",
            constraints={"min": 1, "max": 336}, legacy_v1=False, ui={"unit": "hours"},
        ),
        _enum(
            "attendance.approval.sla_action", ("NONE", "REMIND", "ESCALATE"), "REMIND", (T, LE), S_ATTENDANCE,
            "When an approval is overdue",
            "Do nothing, remind the approvers, or remind them and escalate to the next manager. Requests are "
            "never approved automatically.", legacy_v1=False,
        ),
        _setting(
            "attendance.approval.max_reminders", "int", 3, (T, LE), S_ATTENDANCE, "Reminders per step",
            "How many reminders an overdue approval step sends at most.",
            constraints={"min": 0, "max": 5}, legacy_v1=False,
        ),
        _setting(
            "attendance.approval.max_escalations", "int", 1, (T, LE), S_ATTENDANCE, "Escalation levels",
            "How many levels an overdue approval step escalates at most.",
            constraints={"min": 1, "max": 3}, legacy_v1=False,
        ),
        # Team attendance (managers acting for their team; IG-AS-98). Written on Settings > Attendance > Team.
        _flag(
            "attendance.team.proxy_punch.enabled", "Managers punch for their team",
            on="Managers and supervisors can punch team members in and out (recorded as their action, with location).",
            off="Only employees punch for themselves; managers correct attendance afterwards.",
            default=False, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        _enum(
            "attendance.team.proxy_punch.location", ("REQUIRED", "OPTIONAL", "OFF"), "REQUIRED", (T, LE),
            S_ATTENDANCE, "Location for team punches",
            "Whether a team punch needs a location: the manager's device position or a location entered by hand.",
            legacy_v1=False,
        ),
        _flag(
            "attendance.team.proxy_punch.allow_manual_location", "Location entered by hand",
            on="When the device cannot give a position, the manager can enter coordinates or an address.",
            off="Only the device position is accepted.",
            default=True, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        _flag(
            "attendance.team.proxy_punch.reason_required", "Reason for team punches",
            on="Every team punch needs a reason (kept in the audit trail).",
            off="A reason is optional.",
            default=True, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        _flag(
            "attendance.team.edit_enabled", "Managers correct and delete team attendance",
            on="Managers with the correct-attendance permission can edit and delete their team's sessions.",
            off="Only HR corrects or deletes attendance.",
            default=True, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        # AS-M6 (G-15; 03-shifts/03 §3.1): employee shift requests and open shifts. Which kinds are allowed, cutoffs and
        # approval come from SCHEDULING_POLICY.requests; these switches turn the features on per tenant / entity.
        _flag(
            "shift.requests.enabled", "Shift swaps and changes",
            on="Employees can swap, give away, release or change their published shifts (as the scheduling policy "
               "allows).",
            off="Only planners change rosters.",
            default=False, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        _flag(
            "shift.open_shifts.enabled", "Open shifts",
            on="Planners post open shifts and eligible employees claim them; released shifts go to the open pool.",
            off="No open shifts.",
            default=False, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        # AS-M5 §5.2 (owner decision IG-AS-150): a roster publication goes to a covering roster:publish approver.
        _flag(
            "shift.roster.publish_requires_approval", "Roster publication needs approval",
            on="A planner sends the roster for approval; a second person with Publish schedules approves and it is "
               "published as previewed.",
            off="People with Publish schedules publish rosters directly.",
            default=False, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        # AS-M5 SoD (03-shifts/02 §2.4, §5.2; EC AS-ROS-11): the last saver of roster drafts cannot publish them.
        _flag(
            "shift.roster.planner_cannot_publish", "Planners cannot publish their own drafts",
            on="The person who last saved a roster's drafts cannot publish them; a second person with Publish "
               "schedules does.",
            off="Anyone with Publish schedules can publish, including the planner who drafted the roster.",
            default=False, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        # appendix-a §5.10 / §8.4 (EC AS-CTY-17): the working-time pack exemptions an establishment holds. A held code
        # waives its pack fields only where its subdivision condition holds and only for dates inside its validity.
        _setting(
            "shift.working_time.held_exemptions", "set", (), (T, LE, B), S_ATTENDANCE,
            "Working-time exemptions held",
            "Government exemptions this establishment holds (for example Telangana IT/ITES). Each one waives "
            "the country rules it names, only in its state and only until it expires; the overview warns 90 days "
            "before the end date.",
            constraints={"choices": _exemption_codes()}, requires_approval=True, legacy_v1=False,
        ),
        # 04-attendance/02 (roster settings), 03-shifts/03 §2, 06 roster:read SELF: co-workers' shift codes in the swap
        # picker and the period team only when TEAM (name and code, nothing else).
        _enum(
            "shift.roster.team_visibility", ("OFF", "TEAM"), "OFF", (T, LE, B), S_ATTENDANCE,
            "Co-workers' shifts visible",
            "TEAM: employees see the published shift codes of co-workers in their roster team (name and code only), "
            "which makes the swap picker useful. OFF: they see only names and whether someone can swap.",
            legacy_v1=False,
        ),
        # G-22 (owner request 2026-10-29): adopt the legal entity's country WORKING_TIME pack (IN / AE / US):
        # statutory breaks, rest, overtime consent and caps, privacy caps and scheduling notice start to apply.
        _flag(
            "attendance.packs.enabled", "Apply the country working-time pack",
            on="The legal entity's country rules (breaks, rest, overtime consent and caps, privacy limits, schedule "
               "notice) apply to attendance and schedules.",
            off="Only your own policies apply; country rules are shown as guidance.",
            default=False, levels=(T, LE), section=S_ATTENDANCE, legacy_v1=False,
        ),
        # 3.1 §5.8 — Shifts and allowances
        _switch(
            "payroll.shift_allowance.enabled", "Shift / night allowance",
            on="Night and weekend shift allowances are paid.",
            off="No shift premium (statutory night premiums come only from a verified country pack).",
            levels=_T_LE_PG, section=S_SHIFTS, policy="SHIFT_ALLOWANCE_POLICY", resolution_grain="DAY",
            legal_sensitivity="LEGAL_FLOOR",
        ),
        _flag(
            "attendance.lunch_is_paid", "Paid lunch",
            on="Lunch counts as worked and payable time.",
            off="Lunch is unpaid and deducted from worked hours. The choice is recorded on each break when it starts.",
            default=False, levels=_T, section=S_SHIFTS, aliases=("lunchIsPaid",), resolution_grain="DAY",
        ),
        # 3.1 §5.9 — Bonus, incentives and arrears
        _switch(
            "payroll.wage_inputs.enabled", "Manual wage inputs",
            on="One-time and period inputs (bonus, commission, adjustments) can be entered.",
            off="Bonus, commission and adjustment inputs cannot be entered.",
            levels=_T_LE_PG, section=S_BONUS, default=True,
        ),
        _setting(
            "payroll.wage_inputs.require_approval_above", "money", MoneyValue(Decimal("0"), None),
            _T_LE_PG, S_BONUS, "Approval needed above",
            "Wage inputs above this amount need approval. 0 means every input needs approval.",
            constraints={"min": Decimal("0")}, depends_on=("payroll.wage_inputs.enabled",),
            requires_approval=True,
        ),
        _switch(
            "payroll.incentives.enabled", "Incentive plans (automatic)",
            on="Incentives are calculated from imported measures by incentive plans.",
            off="No formula-based incentives.",
            levels=_T_LE_PG, section=S_BONUS, policy="INCENTIVE_PLAN",
        ),
        _switch(
            "payroll.arrears.auto_compute", "Automatic arrears",
            on="Backdated changes produce arrears proposals for approval.",
            off="Retro differences must be entered manually as wage inputs.",
            levels=_T_LE, section=S_BONUS, default=True,
        ),
        _enum(
            "payroll.arrears.payment_mode", ("NEXT_REGULAR_RUN", "OFF_CYCLE"), "NEXT_REGULAR_RUN", _T_LE,
            S_BONUS, "Where arrears are paid",
            "Where approved arrears are paid: in the next regular run or in an off-cycle run.",
            depends_on=("payroll.arrears.auto_compute",),
        ),
        _enum(
            "payroll.arrears.recovery_mode", ("NEXT_RUN", "INSTALMENTS", "MANUAL"), "NEXT_RUN", _T_LE,
            S_BONUS, "Recovery of negative arrears",
            "Negative arrears are recovered in the next run, in instalments, or manually. They are split into "
            "instalments automatically when one run cannot absorb them within the protected net pay.",
            depends_on=("payroll.arrears.auto_compute",),
        ),
        # 3.1 §5.10 — Reimbursements
        _switch(
            "payroll.reimbursements.enabled", "Reimbursements",
            on="Approved claims are paid with payroll or separately.",
            off="No claims in payroll.",
            levels=_T_LE_PG, section=S_REIMBURSEMENTS, policy="REIMBURSEMENT_POLICY",
        ),
        _enum(
            "payroll.reimbursements.payout_route", ("WITH_PAYROLL", "SEPARATE_PAYMENT"), "WITH_PAYROLL", _T_LE,
            S_REIMBURSEMENTS, "Claim payout route",
            "WITH_PAYROLL adds approved claims to net pay as a reimbursement line (not wages); "
            "SEPARATE_PAYMENT pays them in their own payment batch with a payment advice.",
            depends_on=("payroll.reimbursements.enabled",),
        ),
        # 3.1 §5.11 — Loans, advances and deduction limits
        _switch(
            "payroll.loans.enabled", "Loans",
            on="Loan schedules are deducted in each run.",
            off="No loan recoveries.",
            levels=_T_LE, section=S_LOANS, policy="LOAN_POLICY",
        ),
        _switch(
            "payroll.advances.enabled", "Salary advances",
            on="Advances are paid and recovered.",
            off="No advances.",
            levels=_T_LE, section=S_LOANS, policy="ADVANCE_POLICY",
        ),
        _setting(
            "payroll.deductions.min_net_pay_percent", "decimal", None, _T_LE, S_LOANS,
            "Protected net pay",
            "Net pay may not fall below this share of gross because of non-statutory deductions; the country "
            "pack's minimum is a floor. While it is not set, automated recoveries (loans, advances, penalty "
            "amounts, negative arrears) are blocked and become exceptions, so set it before turning loans or "
            "advances on.",
            constraints={"min": Decimal("0"), "max": Decimal("100"), "max_places": 6, "nullable": True},
            requires_approval=True, legal_sensitivity="LEGAL_FLOOR", ui={"unit": "%"},
        ),
        # 3.1 §5.13 — Currency, FX and rounding
        _enum(
            "payroll.fx.provider", ("MANUAL",), "MANUAL", _T, S_CURRENCY, "FX rate source",
            "Where FX rates come from. Only MANUAL is available until a rate provider is integrated.",
            ui={"group": "FX"},
        ),
        _enum(
            "payroll.fx.rate_type", ("MID", "BUY", "SELL"), "MID", _T_LE, S_CURRENCY, "FX rate type",
            "Which rate is used for conversions: mid, buy or sell.", ui={"group": "FX"},
        ),
        _setting(
            "payroll.fx.max_rate_age_hours", "int", 24, _T, S_CURRENCY, "Maximum FX rate age",
            "Rates older than this block conversion.",
            constraints={"min": 1}, ui={"group": "FX", "unit": "hours"},
        ),
        _setting(
            "payroll.fx.anomaly_threshold_percent", "decimal", Decimal("5.0"), _T, S_CURRENCY,
            "FX anomaly threshold",
            "Day-to-day rate moves bigger than this percentage need approval.",
            constraints={"min": Decimal("0"), "max_places": 6}, ui={"group": "FX", "unit": "%"},
        ),
        _flag(
            "payroll.fx.manual_rate_requires_approval", "Approve manual FX rates",
            on="Manually entered FX rates need approval before they are used.",
            off="Manual rates are used as soon as they are saved.",
            default=True, levels=_T, section=S_CURRENCY, requires_approval=True, ui={"group": "FX"},
        ),
        _enum(
            "payroll.fx.conversion_date_rule", ("PERIOD_END", "PAY_DATE", "CALCULATION_DATE"), "PAY_DATE",
            _T_LE_PG, S_CURRENCY, "FX conversion date",
            "Which day's rate converts amounts: the period end, the pay date or the calculation date.",
            ui={"group": "FX"},
        ),
        _enum(
            "payroll.rounding.line_mode", ("HALF_UP", "HALF_EVEN", "UP", "DOWN"), "HALF_UP", _T_LE_PG,
            S_CURRENCY, "Line rounding",
            "Rounding of each payroll line to the currency's minor unit. Statutory lines follow the country "
            "pack's rounding instead.",
            requires_approval=True, ui={"group": "Rounding"},
        ),
        _setting(
            "payroll.rounding.net_pay_increment", "decimal",
            Derived("CURRENCY_MINOR_UNIT", "Currency minor unit"), _T_LE_PG, S_CURRENCY,
            "Net pay rounding increment",
            "Rounds net pay to a multiple of this amount, for example 1 for whole rupees. It must be a multiple "
            "of the currency's minor unit and at most 100 units. The default, the minor unit, adds no rounding.",
            constraints={"gt": Decimal("0"), "max": Decimal("100"), "max_places": 4},
            requires_approval=True, ui={"group": "Rounding"},
        ),
        _enum(
            "payroll.rounding.net_pay_mode", ("HALF_UP", "UP", "DOWN"), "HALF_UP", _T_LE_PG, S_CURRENCY,
            "Net pay rounding direction", "Direction used when net pay is rounded to the increment.",
            requires_approval=True, ui={"group": "Rounding"},
        ),
        _enum(
            "payroll.rounding.difference_handling", ("ROUNDING_LINE", "CARRY_FORWARD"), "ROUNDING_LINE",
            _T_LE_PG, S_CURRENCY, "Rounding difference",
            "Show the net-pay rounding difference as a NET_ROUNDING line, or carry it to the next period.",
            requires_approval=True, ui={"group": "Rounding"},
        ),
        _enum(
            "ui.money.compact_notation", ("NONE", "INDIAN", "SHORT"),
            Derived("TENANT_CURRENCY_NOTATION", "INDIAN for INR tenants, else SHORT"),
            _T, S_CURRENCY, "Compact amounts in KPI cards",
            "Compact amounts in KPI cards only (4.2L, 1.3M); tables, payslips and exports always show full "
            "amounts. Defaults to INDIAN for INR tenants and SHORT otherwise.",
            effective_dated=False, ui={"group": "Display"},
        ),
        # 3.1 §5.14 — Approvals and controls
        _flag(
            "payroll.approval.separation_of_duties", "Separation of duties",
            on="Preparer, approver and payment releaser must be different people.",
            off="The preparer may approve their own run (never their own pay).",
            default=True, levels=_T, section=S_APPROVALS, admin_only=True, requires_approval=True,
            aliases=("payrollSeparationOfDuties",),
        ),
        _setting(
            "payroll.approval.run_chain_id", "ref",
            Derived("DEFAULT_PAYROLL_APPROVAL_CHAIN", "One step: \"Payroll approver\" role"),
            _T_LE_PG, S_APPROVALS, "Pay run approval chain",
            "Approval chain for pay runs. Defaults to one step: the Payroll approver role.",
            requires_approval=True, ui={"reference": "approval_chain", "module": "payroll_run"},
        ),
        _setting(
            "payroll.approval.compensation_chain_id", "ref",
            Derived("DEFAULT_PAYROLL_APPROVAL_CHAIN", "One step: \"Payroll approver\" role"),
            _T_LE, S_APPROVALS, "Compensation approval chain",
            "Approval chain for salary assignments and revisions. Defaults to one step: the Payroll approver role.",
            requires_approval=True, ui={"reference": "approval_chain", "module": "compensation"},
        ),
        _flag(
            "payroll.approval.step_up_for_finalize", "Re-authenticate to finalize",
            on="Finalizing a run or releasing payments requires MFA re-authentication.",
            off="No re-authentication is requested.",
            default=True, levels=_T, section=S_APPROVALS, requires_approval=True,
        ),
        _setting(
            "payroll.approval.variance_review_threshold_percent", "decimal", Decimal("20"), _T_LE_PG,
            S_APPROVALS, "Variance review threshold",
            "Employees whose net pay changed by more than this percentage versus the last period must be "
            "reviewed before approval.",
            constraints={"min": Decimal("0"), "max_places": 6}, requires_approval=True, ui={"unit": "%"},
        ),
        _flag(
            "payroll.approval.payment_dual_control", "Payment dual control",
            on="The person who prepares a payment batch cannot release it.",
            off="The preparer of a payment batch may also release it.",
            default=True, levels=_T_LE, section=S_APPROVALS, requires_approval=True,
        ),
        _flag(
            "settings.change_approval.enabled", "Approval for sensitive settings",
            on="Sensitive settings are saved as pending until a second administrator approves them; the old "
               "value stays in force until then.",
            off="Sensitive settings apply as soon as they are saved.",
            default=False, levels=_T, section=S_APPROVALS, requires_approval=True,
        ),
        _setting(
            "payroll.migration.legacy_review_acknowledged", "date", None, _T, S_APPROVALS,
            "Legacy payroll changes acknowledged",
            "Date an administrator acknowledged the differences between legacy payroll and payroll v2 (no "
            "automatic penalty per late mark, net pay no longer clipped at zero, no unverified statutory items). "
            "Until then the first v2 run of a migrated company stops at the LEGACY_REVIEW_PENDING pre-check.",
            constraints={"nullable": True}, lockable=False,
            ui={"group": "Migration", "visible_when": {"profile": "LEGACY_V1"}},
        ),
        # 3.1 §5.15 — Payslips and documents
        _enum(
            "payslip.publish_trigger", ("ON_FINALIZE", "ON_PAYMENT_SUBMITTED", "ON_SETTLED", "SCHEDULED"),
            "ON_FINALIZE", _T_LE_PG, S_PAYSLIPS, "Publish payslips",
            "When employees can see the payslip: when the run is finalized, when their payment instruction is "
            "submitted, when their payment is settled, or at a scheduled time on the pay date.",
            legal_sensitivity="LEGAL_FLOOR", ui={"group": "Publication"},
        ),
        _setting(
            "payslip.scheduled_publish_time", "time", time(9, 0), _T_LE_PG, S_PAYSLIPS, "Scheduled publish time",
            "With SCHEDULED: the local time on the pay date when payslips are published.",
            ui={"group": "Publication",
                "visible_when": {"key": "payslip.publish_trigger", "equals": "SCHEDULED"}},
        ),
        _flag(
            "payslip.show_ytd", "Year-to-date column",
            on="The payslip shows a year-to-date column.", off="No year-to-date column.",
            default=True, levels=_T_LE_PG, section=S_PAYSLIPS, ui={"group": "Payslip content"},
        ),
        _flag(
            "payslip.show_employer_contributions", "Employer contributions",
            on="Employer contributions appear in their own section, never added to net pay.",
            off="Employer contributions are not shown.",
            default=False, levels=_T_LE_PG, section=S_PAYSLIPS, ui={"group": "Payslip content"},
        ),
        _flag(
            "payslip.show_leave_balances", "Leave balances",
            on="Leave balances are shown per leave type.", off="Leave balances are not shown.",
            default=True, levels=_T_LE_PG, section=S_PAYSLIPS, ui={"group": "Payslip content"},
        ),
        _flag(
            "payslip.show_attendance_summary", "Attendance summary",
            on="Paid days, LOP days and overtime hours are shown.", off="No attendance summary.",
            default=True, levels=_T_LE_PG, section=S_PAYSLIPS, ui={"group": "Payslip content"},
        ),
        _flag(
            "payslip.show_bank_masked", "Masked bank account",
            on="The bank account is shown masked, for example HDFC ****4321.",
            off="The bank account is not shown.",
            default=True, levels=_T_LE_PG, section=S_PAYSLIPS, ui={"group": "Payslip content"},
        ),
        _flag(
            "payslip.show_tax_projection", "Tax projection",
            on="Projected annual tax is shown where the country pack supports it.",
            off="No tax projection.",
            default=False, levels=_T_LE, section=S_PAYSLIPS, ui={"group": "Payslip content"},
        ),
        _setting(
            "payslip.languages", "list", Derived("TENANT_LOCALE", "[tenant default]"), _T_LE, S_PAYSLIPS,
            "Payslip languages",
            "One language, or two side by side for bilingual payslips (for example English and Arabic). "
            "Defaults to the tenant's language.",
            constraints={"item_format": "locale", "min_items": 1, "max_items": 2, "unique": True},
            ui={"group": "Payslip content", "control": "locales"},
        ),
        _enum(
            "payslip.pdf_protection", ("NONE", "EMPLOYEE_SECRET"), "NONE", _T_LE, S_PAYSLIPS,
            "PDF protection",
            "EMPLOYEE_SECRET encrypts payslip PDFs with a password the employee sets in self-service (never a "
            "guessable value; HR never sees it); NONE leaves PDFs unprotected.",
            ui={"group": "Delivery"},
        ),
        _switch(
            "payslip.email_notify", "Email when a payslip is published",
            on="An email with a link is sent (no amounts by default).",
            off="In-app notification only.",
            levels=_T_LE_PG, section=S_PAYSLIPS, default=True,
        ),
        _flag(
            "payslip.email_include_net_pay", "Show net pay in the email",
            on="The net amount appears in the email body.", off="No amounts in the email.",
            default=False, levels=_T, section=S_PAYSLIPS, depends_on=("payslip.email_notify",),
            ui={"group": "Delivery"},
        ),
        _flag(
            "payslip.email_attach_pdf", "Attach the PDF to the email",
            on="The payslip PDF is attached to the email; this requires PDF protection.",
            off="The email carries a link only.",
            default=False, levels=_T, section=S_PAYSLIPS, depends_on=("payslip.email_notify",),
            ui={"group": "Delivery"},
        ),
        _flag(
            "payslip.queries_enabled", "Payslip queries",
            on="Employees see a \"Raise a query\" button on payslips.", off="No query button on payslips.",
            default=True, levels=_T, section=S_PAYSLIPS, ui={"group": "Delivery"},
        ),
        *(_numbering(doc_type, prefix) for doc_type, prefix in NUMBERED_DOCUMENT_TYPES.items()),
        # 3.1 §5.16 — Payments and banking
        _enum(
            "payroll.payments.default_method", ("BANK_FILE", "API", "MANUAL"), "BANK_FILE", (LE, PG),
            S_PAYMENTS, "Payment method",
            "How salaries are paid: a bank file, an API where a provider is integrated, or manually.",
            ui={"group": "Payments"},
        ),
        _setting(
            "payroll.payments.bank_file_format", "string", "GENERIC_CSV", (LE,), S_PAYMENTS, "Bank file format",
            "Bank file format from the catalogue, for example AE_WPS_SIF, UK_BACS_STD18, US_NACHA or "
            "SEPA_PAIN_001. Defaults to GENERIC_CSV until a verified adapter exists for the bank; a file is "
            "never claimed to be bank-compatible without one.",
            constraints={"format": "code", "max_length": 50},
            ui={"group": "Payments", "control": "catalogue", "catalogue": "bank_file_formats"},
        ),
        _setting(
            "payroll.payments.funding_account_id", "ref",
            Derived("ENTITY_PRIMARY_FUNDING_ACCOUNT", "Entity primary account"), (LE, PG), S_PAYMENTS,
            "Funding account",
            "The company account salaries are paid from. Defaults to the legal entity's primary account.",
            sensitivity="confidential", requires_approval=True,
            ui={"group": "Payments", "reference": "funding_account"},
        ),
        _setting(
            "payroll.payments.batch_max_items", "int", 1000, (LE,), S_PAYMENTS, "Maximum payments per batch",
            "Larger payment batches are split.",
            constraints={"min": 1}, ui={"group": "Payments"},
        ),
        _setting(
            "payroll.payments.unclaimed_after_days", "int", None, _T_LE, S_PAYMENTS, "Mark unpaid pay as unclaimed after",
            "Company policy, off when empty: net pay that is still unpaid this many days after its pay date, with "
            "nothing in flight (never sent, failed, returned or rejected), is moved to UNCLAIMED by a daily job. "
            "Unsent payments are held, finance and the employee are notified, and a person releases it again with a "
            "reason. Nothing is paid or deleted. Escheatment (unclaimed property) rules are country-specific and are "
            "not implemented: this is a company policy value, not a legal limit.",
            constraints={"min": 1, "max": 3650, "nullable": True}, requires_approval=True, legacy_v1=False,
            ui={"group": "Payments", "unit": "days"},
        ),
        _flag(
            "payroll.payments.reissue_requires_approval", "Approve payment reissues",
            on="Reissuing a failed or returned payment needs approval.",
            off="Reissues need no approval.",
            default=True, levels=_T_LE, section=S_PAYMENTS, requires_approval=True, ui={"group": "Payments"},
        ),
        _switch(
            "payroll.bank.self_service_enabled", "Employees manage own bank accounts",
            on="Employees request bank account changes themselves (step-up, approval).",
            off="Only HR adds bank accounts.",
            levels=_T_LE, section=S_PAYMENTS, default=True,
        ),
        _flag(
            "payroll.bank.change_requires_approval", "Approve bank account changes",
            on="HR or finance approves employee bank account changes.",
            off="Changes take effect after the cooling-off period without approval.",
            default=True, levels=_T_LE, section=S_PAYMENTS, requires_approval=True,
            ui={"group": "Bank accounts"},
        ),
        _setting(
            "payroll.bank.cooling_off_hours", "int", 48, _T_LE, S_PAYMENTS, "Cooling-off period",
            "A new account receives no payments until this many hours after approval, unless an approver "
            "waives it with a reason.",
            constraints={"min": 0, "max": 168}, requires_approval=True,
            ui={"group": "Bank accounts", "unit": "hours"},
        ),
        _switch(
            "payroll.bank.require_verification", "Flag unverified bank accounts before approval",
            on="Unverified or missing accounts appear as run exceptions.",
            off="No pre-check exception for unverified accounts.",
            levels=_T_LE, section=S_PAYMENTS, default=True, requires_approval=True,
        ),
        _switch(
            "payroll.bank.exclude_unverified_from_payout", "Hold payments to unverified accounts",
            on="Their instructions go ON_HOLD and are left out of the bank file; calculation is unaffected.",
            off="Unverified accounts are included in bank files (with a warning).",
            levels=_T_LE, section=S_PAYMENTS, default=True, requires_approval=True,
        ),
        _setting(
            "payroll.bank.verification_methods", "set", ("FORMAT", "DOCUMENT_REVIEW"), _T_LE, S_PAYMENTS,
            "What counts as verified",
            "Checks an account must pass to count as verified: format validation, document review, a penny "
            "drop, or a provider name match.",
            constraints={"choices": ("FORMAT", "DOCUMENT_REVIEW", "PENNY_DROP", "PROVIDER_NAME_MATCH"),
                         "min_items": 1},
            requires_approval=True, ui={"group": "Bank accounts"},
        ),
        _switch(
            "payroll.bank.split_payments_enabled", "Split salary across accounts",
            on="Several accounts per payee, by percentage or amount.",
            off="One account per payee.",
            levels=_T_LE, section=S_PAYMENTS,
        ),
        _setting(
            "payroll.bank.max_accounts_per_payee", "int", 2, _T, S_PAYMENTS, "Accounts per payee",
            "Maximum bank accounts per payee when split payments are on.",
            constraints={"min": 1, "max": 5}, ui={"group": "Bank accounts"},
        ),
        # 3.1 §5.17 — Notifications (no Levels column in the doc: tenant-wide)
        _setting(
            "notify.payroll.approval_requested", "set", ("IN_APP", "EMAIL"), _T, S_NOTIFICATIONS,
            "Approval requests",
            "Channels used to tell approvers that runs, revisions, inputs or payments await them.",
            constraints={"choices": ("IN_APP", "EMAIL")}, effective_dated=False,
        ),
        _setting(
            "notify.payroll.input_cutoff_reminder_days", "int", 2, _T, S_NOTIFICATIONS, "Cutoff reminder",
            "Days before the input cutoff when managers and HR are reminded.",
            constraints={"min": 0, "max": 31}, effective_dated=False, ui={"unit": "days"},
        ),
        _flag(
            "notify.payroll.payment_status_to_employee", "Payment status to employees",
            on="Employees are told when salary is credited or a payment is returned (\"update your bank details\").",
            off="Employees get no payment status messages.",
            default=True, levels=_T, section=S_NOTIFICATIONS, effective_dated=False,
        ),
        _flag(
            "notify.payroll.bank_change_alert", "Bank change alerts",
            on="Alerts are always sent to the old and new contact details when bank details change.",
            off="Not available: this control is locked ON.",
            default=True, levels=(), section=S_NOTIFICATIONS, effective_dated=False, lockable=False,
            constraints={"const": True}, ui={"locked": True},
        ),
        _enum(
            "notify.payroll.exceptions_digest", ("DAILY", "OFF"), "DAILY", _T, S_NOTIFICATIONS,
            "Exceptions digest",
            "DAILY sends payroll staff a digest of open exceptions; OFF sends none.",
            effective_dated=False,
        ),
        # 3.1 §5.18 — Accounting (levels follow payroll.accounting.enabled)
        _switch(
            "payroll.accounting.enabled", "Accounting journals",
            on="Journals are generated on finalize or settlement.",
            off="No journals.",
            levels=_T_LE, section=S_ACCOUNTING, support_status="NOT_IMPLEMENTED",
        ),
        _enum(
            "payroll.accounting.journal_format", ("GENERIC_CSV",), "GENERIC_CSV", _T_LE, S_ACCOUNTING,
            "Journal format", "Journal export format: GENERIC_CSV (more adapters later).",
            depends_on=("payroll.accounting.enabled",), support_status="NOT_IMPLEMENTED",
        ),
        _enum(
            "payroll.accounting.post_on", ("FINALIZE", "PAYMENT_SETTLED"), "FINALIZE", _T_LE, S_ACCOUNTING,
            "Post journals on", "When journals are produced: when the run is finalized or when payments settle.",
            depends_on=("payroll.accounting.enabled",), support_status="NOT_IMPLEMENTED",
        ),
        # 3.1 §5.19 — Retention and privacy (no Levels column in the doc: tenant-wide)
        _setting(
            "privacy.payroll_retention_years", "int", 8, _T, S_PRIVACY, "Payroll record retention",
            "Years payroll records are kept. Cannot be set below the country pack's minimum (8 if unknown).",
            constraints={"min": 1}, requires_approval=True, legal_sensitivity="LEGAL_FLOOR", ui={"unit": "years"},
        ),
        _setting(
            "privacy.small_group_suppression_size", "int", 5, _T, S_PRIVACY, "Small-group suppression",
            "Salary analytics hide groups smaller than this.",
            constraints={"min": 1}, ui={"unit": "people"},
        ),
        _flag(
            "privacy.bank_details_view_logging", "Log unmasked bank detail views",
            on="Every unmasked view of bank details is audited.",
            off="Not available: this control is locked ON.",
            default=True, levels=(), section=S_PRIVACY, lockable=False,
            constraints={"const": True}, ui={"locked": True},
        ),
    ]


# Keys whose change in an open period requires "Preview impact" first (3.1 §2.5).
IMPACT_PREVIEW_KEYS: Final = frozenset(
    {
        "payroll.attendance.enabled",
        "payroll.attendance.half_day_value",
        "payroll.lop.proration_basis",
        "payroll.lop.custom_days",
        "payroll.lop.sandwich_rule",
        "payroll.lop.zero_paid_days",
        "payroll.overtime.enabled",
        "payroll.late_penalty.enabled",
        "payroll.rounding.line_mode",
        "payroll.rounding.net_pay_increment",
        "payroll.rounding.net_pay_mode",
        "payroll.rounding.difference_handling",
    }
)

REGISTRY: dict[str, SettingDef] = {}
ALIASES: dict[str, str] = {}
_PACK_KEYS: set[str] = set()


# --------------------------------------------------------------------------- lookup


def canonical_key(key: str) -> str:
    """The registry key for ``key`` or one of its legacy aliases (3.1 §2.4)."""
    if key in REGISTRY:
        return key
    if key in ALIASES:
        return ALIASES[key]
    raise SettingValidationError("UNKNOWN_SETTING", f"{key} is not a registered setting", key=key)


def definition(key: str) -> SettingDef:
    return REGISTRY[canonical_key(key)]


def definitions(*, module: str | None = None, section: str | None = None) -> list[SettingDef]:
    """Definitions in section order, then catalogue order; filters match case-insensitively."""
    order = {s.title: i for i, s in enumerate(SECTIONS)}
    found = [
        d
        for d in REGISTRY.values()
        if (module is None or d.module.lower() == module.lower())
        and (section is None or section in (d.section, SECTION_BY_TITLE[d.section].slug))
    ]
    return sorted(found, key=lambda d: (order[d.section], d.ui.get("order", 0)))


def fails_closed(key: str) -> bool:
    return definition(key).fails_closed


def new_tenant_settings() -> dict[str, Any]:
    """TENANT-level values written for a newly provisioned tenant (``STARTER_V1``, decision I-13)."""
    return {d.key: d.new_tenant_default for d in definitions() if d.new_tenant_default is not UNSET}


# --------------------------------------------------------------------------- validation


def _fail(code: str, key: str, message: str, **details: Any) -> SettingValidationError:
    return SettingValidationError(code, message, key=key, details=details)


def _check_bounds(defn: SettingDef, number: Decimal | int, *, what: str) -> None:
    c = defn.constraints
    low, high, above = c.get("min"), c.get("max"), c.get("gt")
    if low is not None and number < low:
        raise _fail("BELOW_MINIMUM", defn.key, f"{what} must be at least {low}", minimum=str(low))
    if above is not None and number <= above:
        raise _fail("BELOW_MINIMUM", defn.key, f"{what} must be greater than {above}", minimum=str(above))
    if high is not None and number > high:
        raise _fail("ABOVE_MAXIMUM", defn.key, f"{what} must be at most {high}", maximum=str(high))


def _check_string(defn: SettingDef, value: object, *, fmt: str | None, what: str) -> str:
    if not isinstance(value, str):
        raise _fail("INVALID_TYPE", defn.key, f"{what} must be text")
    max_length = defn.constraints.get("max_length", _DEFAULT_MAX_LENGTH)
    if not value or len(value) > max_length:
        raise _fail("INVALID_LENGTH", defn.key, f"{what} must be 1-{max_length} characters")
    if fmt is not None:
        problem = _FORMATS[fmt](value)
        if problem:
            raise _fail("INVALID_FORMAT", defn.key, f"{what} {problem}")
    regex = defn.constraints.get("regex")
    if regex is not None and not re.fullmatch(regex, value):
        raise _fail("INVALID_FORMAT", defn.key, f"{what} has an invalid format")
    return value


def _validate_items(defn: SettingDef, value: object, *, ordered: bool) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise _fail("INVALID_TYPE", defn.key, f"{defn.label} must be a list")
    items = list(value) if ordered else sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
    c = defn.constraints
    choices = c.get("choices")
    checked: list[Any] = []
    for item in items:
        if choices is not None:
            if not isinstance(item, str) or item not in choices:
                raise _fail("INVALID_CHOICE", defn.key, f"{item!r} is not one of {', '.join(choices)}")
        else:
            _check_string(defn, item, fmt=c.get("item_format"), what=f"Each {defn.label.lower()} item")
        checked.append(item)
    if (not ordered or c.get("unique")) and len(set(checked)) != len(checked):
        raise _fail("DUPLICATE_ITEM", defn.key, f"{defn.label} lists the same value twice")
    if len(checked) < c.get("min_items", 0):
        raise _fail("TOO_FEW_ITEMS", defn.key, f"{defn.label} needs at least {c['min_items']} value(s)")
    if "max_items" in c and len(checked) > c["max_items"]:
        raise _fail("TOO_MANY_ITEMS", defn.key, f"{defn.label} allows at most {c['max_items']} value(s)")
    if not ordered and choices is not None:
        checked.sort(key=choices.index)
    return tuple(checked)


def _validate_json(defn: SettingDef, value: object, depth: int = 0) -> Any:
    if depth > 10:
        raise _fail("INVALID_JSON", defn.key, f"{defn.label} is nested too deeply")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise _fail("INVALID_TYPE", defn.key, f"{defn.label} must not contain floating-point numbers")
    if isinstance(value, (list, tuple)):
        return [_validate_json(defn, item, depth + 1) for item in value]
    if isinstance(value, Mapping) and all(isinstance(k, str) for k in value):
        return {k: _validate_json(defn, v, depth + 1) for k, v in value.items()}
    raise _fail("INVALID_TYPE", defn.key, f"{defn.label} must be JSON")


def _validate_money(defn: SettingDef, value: object) -> MoneyValue:
    if isinstance(value, MoneyValue):
        amount_in: object = value.amount
        currency: object = value.currency
    elif isinstance(value, Mapping) and set(value) == {"amount", "currency"}:
        amount_in, currency = value["amount"], value["currency"]
    else:
        raise _fail("INVALID_MONEY", defn.key, f"{defn.label} must be {{\"amount\": \"...\", \"currency\": \"XXX\"}}")
    amount = parse_decimal(amount_in, what=f"{defn.label} amount", key=defn.key)
    if _places(amount) > 4:
        raise _fail("TOO_MANY_PLACES", defn.key, f"{defn.label} has more than 4 decimal places")
    if currency is None:
        if amount != 0:
            raise _fail("INVALID_CURRENCY", defn.key, f"{defn.label} needs a currency unless it is zero")
    elif not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
        raise _fail("INVALID_CURRENCY", defn.key, f"{defn.label} currency must be an ISO 4217 code")
    _check_bounds(defn, amount, what=f"{defn.label} amount")
    return MoneyValue(amount, currency)


def validate_value(key: str, value: Any) -> Any:
    """The normalized value for ``key``, or ``SettingValidationError`` (strict, fails closed).

    Booleans must be real booleans, decimals are strings or ``Decimal`` (never floats or ints),
    money is ``{"amount": "...", "currency": "XXX"}``, enums come from the declared choices and
    sets are returned in choice order. Returns domain values: ``Decimal``, ``MoneyValue``,
    ``date``, ``time`` and tuples for lists and sets.
    """
    defn = definition(key)
    c = defn.constraints
    if value is None:
        if defn.nullable:
            return None
        raise _fail("VALUE_REQUIRED", defn.key, f"{defn.label} needs a value")
    if isinstance(value, (Derived, _Unset)):
        raise _fail("INVALID_TYPE", defn.key, f"{defn.label} needs a concrete value")
    kind = defn.type
    result: Any
    if kind == "bool":
        if not isinstance(value, bool):
            raise _fail("INVALID_TYPE", defn.key, f"{defn.label} must be true or false")
        result = value
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail("INVALID_TYPE", defn.key, f"{defn.label} must be a whole number")
        _check_bounds(defn, value, what=defn.label)
        result = value
    elif kind == "decimal":
        result = parse_decimal(value, what=defn.label, key=defn.key)
        if "choices" in c and result not in c["choices"]:
            allowed = ", ".join(decimal_str(x) for x in c["choices"])
            raise _fail("INVALID_CHOICE", defn.key, f"{defn.label} must be one of {allowed}")
        _check_bounds(defn, result, what=defn.label)
        if "max_places" in c and _places(result) > c["max_places"]:
            raise _fail("TOO_MANY_PLACES", defn.key, f"{defn.label} allows at most {c['max_places']} decimal places")
        if "multiple_of" in c and result % c["multiple_of"] != 0:
            raise _fail("NOT_A_MULTIPLE", defn.key, f"{defn.label} must be a multiple of {c['multiple_of']}")
    elif kind == "money":
        result = _validate_money(defn, value)
    elif kind == "enum":
        if not isinstance(value, str) or value not in c["choices"]:
            raise _fail("INVALID_CHOICE", defn.key, f"{defn.label} must be one of {', '.join(c['choices'])}")
        result = value
    elif kind == "ref":
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _fail("INVALID_TYPE", defn.key, f"{defn.label} must be a record id")
        result = value
    elif kind == "date":
        if isinstance(value, date) and not hasattr(value, "hour"):
            result = value
        elif isinstance(value, str) and _DATE_RE.fullmatch(value):
            try:
                result = date.fromisoformat(value)
            except ValueError:
                raise _fail("INVALID_FORMAT", defn.key, f"{defn.label} is not a valid date") from None
        else:
            raise _fail("INVALID_FORMAT", defn.key, f"{defn.label} must be a date (YYYY-MM-DD)")
    elif kind == "time":
        if isinstance(value, time):
            if value.tzinfo is not None or value.second or value.microsecond:
                raise _fail("INVALID_FORMAT", defn.key, f"{defn.label} must be a local time in hours and minutes")
            result = value
        elif isinstance(value, str) and (match := _TIME_RE.fullmatch(value)):
            result = time(int(match.group(1)), int(match.group(2)))
        else:
            raise _fail("INVALID_FORMAT", defn.key, f"{defn.label} must be a time (HH:MM)")
    elif kind == "string":
        result = _check_string(defn, value, fmt=c.get("format"), what=defn.label)
    elif kind == "list":
        result = _validate_items(defn, value, ordered=True)
    elif kind == "set":
        result = _validate_items(defn, value, ordered=False)
    elif kind == "json":
        result = _validate_json(defn, value)
    else:  # pragma: no cover - definitions are checked at import
        raise _fail("INVALID_DEFINITION", defn.key, f"unknown type {kind}")
    if "const" in c and result != c["const"]:
        raise _fail("FIXED_SETTING", defn.key, f"{defn.label} cannot be changed")
    return result


def to_json(key: str, value: Any) -> Any:
    """The JSON form of a normalized value, as stored in ``setting_values.value``."""
    defn = definition(key)
    if value is None:
        return None
    if isinstance(value, Derived):
        raise _fail("INVALID_TYPE", defn.key, f"{defn.label}: a derived default is not a stored value")
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, MoneyValue):
        return value.to_json()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return list(value)
    return value


def constraint_json(constraints: Mapping[str, Any]) -> dict[str, Any]:
    def convert(item: Any) -> Any:
        if isinstance(item, Decimal):
            return decimal_str(item)
        if isinstance(item, (tuple, list)):
            return [convert(x) for x in item]
        return item

    return {name: convert(item) for name, item in constraints.items()}


def default_json(defn: SettingDef, value: Any) -> Any:
    if value is UNSET:
        return None
    if isinstance(value, Derived):
        return None
    return to_json(defn.key, value)


def definition_json(defn: SettingDef) -> dict[str, Any]:
    """A JSON-ready description of one definition (GET /settings/definitions, generated TypeScript)."""
    derived = defn.default if isinstance(defn.default, Derived) else None
    return {
        "key": defn.key,
        "type": defn.type,
        "default": default_json(defn, defn.default),
        "derivedDefault": {"source": derived.source, "description": derived.description} if derived else None,
        "allowedLevels": [lv.value for lv in defn.allowed_levels],
        "module": defn.module,
        "section": defn.section,
        "sectionSlug": SECTION_BY_TITLE[defn.section].slug,
        "label": defn.label,
        "help": defn.help,
        "constraints": constraint_json(defn.constraints),
        "dependsOn": list(defn.depends_on),
        "resolutionGrain": defn.resolution_grain,
        "effectiveDated": defn.effective_dated,
        "sensitivity": defn.sensitivity,
        "requiresApproval": defn.requires_approval,
        "adminOnly": defn.admin_only,
        "lockable": defn.lockable,
        "statutoryScheme": defn.statutory_scheme,
        "aliases": list(defn.aliases),
        "ui": dict(defn.ui),
        "newTenantDefault": default_json(defn, defn.new_tenant_default),
        "supportStatus": defn.support_status,
        "legalSensitivity": defn.legal_sensitivity,
        "decisionRequired": defn.decision_required,
        "failsClosed": defn.fails_closed,
    }


def registry_fingerprint() -> str:
    """SHA-256 of every definition, used as the ETag of the definitions endpoint."""
    payload = [definition_json(d) for d in sorted(REGISTRY.values(), key=lambda d: d.key)]
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{REGISTRY_VERSION}:{text}".encode("utf-8")).hexdigest()


def derive_default(key: str, context: Mapping[str, Any]) -> Any:
    """Turn a ``Derived`` PLATFORM default into a value from context, or fail closed.

    Optional references (OPTIONAL_REFERENCE_SOURCES) whose record the context cannot name resolve to
    None: the setting is unset and its consumer applies the documented fallback.
    """
    defn = definition(key)
    if not isinstance(defn.default, Derived):
        return defn.default
    source = defn.default.source
    fact = DERIVED_SOURCES[source]
    if context.get(fact) is None:
        if source in OPTIONAL_REFERENCE_SOURCES:
            return None
        raise _fail(
            "DEFAULT_UNRESOLVED", defn.key,
            f"{defn.label}: the default ({defn.default.description}) needs {fact}", fact=fact,
        )
    raw = context[fact]
    if source == "CURRENCY_MINOR_UNIT":
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 4:
            raise _fail("DEFAULT_UNRESOLVED", defn.key, f"{defn.label}: invalid currency minor unit {raw!r}")
        value: Any = decimal_str(Decimal(1).scaleb(-raw))
    elif source == "TENANT_LOCALE":
        value = [raw]
    elif source == "TENANT_CURRENCY_NOTATION":
        value = "INDIAN" if raw == "INR" else "SHORT"
    else:
        value = raw
    return validate_value(defn.key, value)


@dataclass(frozen=True)
class StoredRead:
    """A stored value re-validated on read (EC-CFG-09)."""

    key: str
    value: Any
    error: SettingValidationError | None = None
    fell_back: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def issue(self) -> Issue | None:
        if self.error is None:
            return None
        defn = REGISTRY[self.key]
        if self.fell_back:
            return Issue(
                "CONFIG_INVALID", "WARNING", (self.key,),
                f"Stored value of {defn.label} is invalid ({self.error.message}); the default is used.",
                {"code": self.error.code},
            )
        return Issue(
            "CONFIG_INVALID", "ERROR", (self.key,),
            f"Stored value of {defn.label} is invalid ({self.error.message}). Fix it before calculating payroll.",
            {"code": self.error.code},
        )


def read_stored_value(key: str, raw: Any) -> StoredRead:
    """Validate a stored JSON value. Payroll keys fail closed; other keys keep today's fallback."""
    defn = definition(key)
    try:
        return StoredRead(defn.key, validate_value(defn.key, raw))
    except SettingValidationError as error:
        if defn.fails_closed:
            return StoredRead(defn.key, UNSET, error)
        return StoredRead(defn.key, defn.default, error, fell_back=True)


# --------------------------------------------------------------------------- legal limits


@dataclass(frozen=True)
class LegalConstraint:
    """A country pack's limit on a setting key or a policy field (02 §14.3, 3.2 §6.1).

    ``target`` is a setting key or ``"<POLICY_TYPE>.<field>[.<field>…]"``; list fields apply the
    limit to every item, optionally filtered by ``match`` (for example ``{"day_type": "HOLIDAY"}``).
    ``required_value`` locks the target (True = locked ON, False = locked OFF). Policy limits use
    the field's own meaning: holiday multipliers are the extra pay on top of the normal day.
    """

    target: str
    source: str
    minimum: Decimal | int | None = None
    maximum: Decimal | int | None = None
    allowed: tuple[Any, ...] | None = None
    required_value: Any = field(default=UNSET, hash=False)
    match: Mapping[str, Any] | None = field(default=None, hash=False)
    applies_to: str | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("a legal constraint needs its legal source")
        if self.allowed is not None:
            object.__setattr__(self, "allowed", tuple(self.allowed))

    @property
    def is_policy_target(self) -> bool:
        return self.target.split(".", 1)[0].isupper()


Constraint = LegalConstraint  # the name used by the country-pack protocol (3.2 §6.5)


def _as_number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, str) and _DECIMAL_RE.fullmatch(value):
        return Decimal(value)
    return None


def _same(left: Any, right: Any) -> bool:
    a, b = _as_number(left), _as_number(right)
    if a is not None and b is not None:
        return a == b
    return type(left) is type(right) and left == right


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, Decimal):
        return decimal_str(value)
    return str(value)


def check_legal_value(constraint: LegalConstraint, value: Any, *, label: str | None = None) -> Issue | None:
    """The violation of ``constraint`` by ``value``, if any (V-10, V-16, LEGAL_LIMIT)."""
    if value is None or value is UNSET or isinstance(value, Derived):
        return None
    name = label or constraint.target
    who = f" for {constraint.applies_to}" if constraint.applies_to else ""
    base = {"source": constraint.source, "target": constraint.target}
    if constraint.required_value is not UNSET and not _same(value, constraint.required_value):
        required = constraint.required_value
        rule = "V-10" if required is True else "LEGAL_LIMIT"
        return Issue(
            rule, "ERROR", (constraint.target,),
            f"{name} must be {_show(required)}{who}: required by law ({constraint.source}).",
            {**base, "required_value": _show(required), "locked": True},
        )
    if constraint.allowed is not None and not any(_same(value, a) for a in constraint.allowed):
        return Issue(
            "LEGAL_LIMIT", "ERROR", (constraint.target,),
            f"{name} must be one of {', '.join(_show(a) for a in constraint.allowed)}{who} ({constraint.source}).",
            {**base, "allowed": [_show(a) for a in constraint.allowed]},
        )
    number = _as_number(value)
    if number is None:
        return None
    if constraint.minimum is not None and number < Decimal(constraint.minimum):
        minimum = _show(Decimal(constraint.minimum))
        return Issue(
            "V-16", "ERROR", (constraint.target,),
            f"{name} {_show(number)} is below the legal minimum {minimum}{who} ({constraint.source}).",
            {**base, "minimum": minimum, "apply_legal_minimum": minimum},
        )
    if constraint.maximum is not None and number > Decimal(constraint.maximum):
        maximum = _show(Decimal(constraint.maximum))
        return Issue(
            "LEGAL_LIMIT", "ERROR", (constraint.target,),
            f"{name} {_show(number)} is above the legal maximum {maximum}{who} ({constraint.source}).",
            {**base, "maximum": maximum},
        )
    return None


def policy_field_values(fields: Mapping[str, Any], path: str, match: Mapping[str, Any] | None = None) -> list[Any]:
    """Values at a dotted ``path`` inside policy fields; list items may be filtered by ``match``."""
    current: list[Any] = [fields]
    parts = path.split(".")
    for index, part in enumerate(parts):
        found: list[Any] = []
        for node in current:
            if not isinstance(node, Mapping) or part not in node:
                continue
            item = node[part]
            if isinstance(item, list):
                rows = [r for r in item if isinstance(r, Mapping)] if index < len(parts) - 1 else item
                if match:
                    rows = [r for r in rows if isinstance(r, Mapping) and all(r.get(k) == v for k, v in match.items())]
                found.extend(rows)
            else:
                found.append(item)
        current = found
    return current


def policy_legal_issues(
    policy_type: str, fields: Mapping[str, Any], constraints: Iterable[LegalConstraint], *, name: str | None = None
) -> list[Issue]:
    """Legal-limit violations of one policy's (normalized) fields."""
    issues: list[Issue] = []
    for constraint in constraints:
        head, _, path = constraint.target.partition(".")
        if head != policy_type or not path:
            continue
        label = f"{name or policy_type}: {path}"
        for value in policy_field_values(fields, path, constraint.match):
            issue = check_legal_value(constraint, value, label=label)
            if issue is not None:
                issues.append(issue)
                break
    return issues


# --------------------------------------------------------------------------- writes


def validate_write(
    key: str,
    value: Any,
    *,
    level: Level | str,
    locked: bool = False,
    actor_is_admin: bool = False,
    current: Any = UNSET,
    current_locked: bool | None = None,
    legal_limits: Iterable[LegalConstraint] = (),
) -> Any:
    """Check one value an administrator wants to store at ``level``; returns the normalized value.

    Covers fixed and pack-owned settings (EC-CFG-20), allowed levels, strict typing, locks,
    admin-only keys (EC-CFG-11: saving an unchanged value is not an attempt) and statutory locks
    and legal limits (EC-CFG-19, EC-CFG-20). Scope and permission checks belong to the service.
    """
    defn = definition(key)
    scope = Level(level)
    if not defn.allowed_levels:
        code = "PACK_OWNED" if defn.statutory_scheme else "FIXED_SETTING"
        reason = "is set by the country pack" if defn.statutory_scheme else "cannot be changed"
        raise _fail(code, defn.key, f"{defn.label} {reason}")
    if scope not in defn.allowed_levels:
        allowed = ", ".join(lv.value for lv in defn.allowed_levels)
        raise _fail("LEVEL_NOT_ALLOWED", defn.key, f"{defn.label} can only be set at: {allowed}", level=scope.value)
    normalized = validate_value(defn.key, value)
    if defn.support_status == "NOT_IMPLEMENTED" and normalized is True:
        raise _fail("FEATURE_NOT_AVAILABLE", defn.key, f"{defn.label} is not available in this release")
    if locked and not defn.lockable:
        raise _fail("NOT_LOCKABLE", defn.key, f"{defn.label} cannot be locked")
    if defn.admin_only and not actor_is_admin:
        unchanged = current is not UNSET and _normalized_or_unset(defn.key, current) == normalized
        if not unchanged or (current_locked is not None and current_locked != locked):
            raise _fail("ADMIN_ONLY", defn.key, f"Only company administrators can change {defn.label}")
    for constraint in legal_limits:
        if constraint.target != defn.key:
            continue
        issue = check_legal_value(constraint, normalized, label=defn.label)
        if issue is None:
            continue
        if issue.rule == "V-16":
            code = "LEGAL_MINIMUM"
        elif issue.details.get("locked"):
            code = "STATUTORY_LOCK"
        else:
            code = "LEGAL_LIMIT"
        raise _fail(code, defn.key, issue.message, **issue.details)
    return normalized


def _normalized_or_unset(key: str, value: Any) -> Any:
    try:
        return validate_value(key, value)
    except SettingValidationError:
        return UNSET


@dataclass(frozen=True)
class ChangeRequirements:
    """What saving a value with an effective date requires (3.1 §2.2, §2.3, §2.5)."""

    status: str
    approval_required: bool
    reason_required: bool
    retro_preview_required: bool
    impact_preview_required: bool
    notice: str | None


def _day_text(day: date) -> str:
    return f"{day.day} {day:%B %Y}"


def effective_date_notice(
    grain: str, effective_from: date, period_start: date, period_end: date, *, period_label: str | None = None
) -> str | None:
    """What an effective date means for an open period: whole period (PERIOD) or from a day (DAY)."""
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {GRAINS}")
    if effective_from > period_end:
        return None
    label = period_label or f"{_day_text(period_start)} - {_day_text(period_end)}"
    if grain == "PERIOD":
        return f"Applies to the whole {label} period"
    return f"Applies from {_day_text(max(effective_from, period_start))}"


def change_requirements(
    key: str,
    effective_from: date,
    *,
    change_approval_enabled: bool,
    finalized_through: date | None = None,
    open_period: tuple[date, date] | None = None,
    period_label: str | None = None,
) -> ChangeRequirements:
    """Reason, previews, approval and the save-dialog notice for one change (EC-CFG-02, -03, -12)."""
    defn = definition(key)
    approval = change_approval_enabled and defn.requires_approval
    in_finalized = finalized_through is not None and effective_from <= finalized_through
    notice = None
    affects_open = False
    if open_period is not None:
        start, end = open_period
        notice = effective_date_notice(defn.resolution_grain, effective_from, start, end, period_label=period_label)
        affects_open = effective_from <= end
    return ChangeRequirements(
        status="PENDING_APPROVAL" if approval else "ACTIVE",
        approval_required=approval,
        reason_required=in_finalized,
        retro_preview_required=in_finalized,
        impact_preview_required=affects_open and defn.key in IMPACT_PREVIEW_KEYS,
        notice=notice,
    )


# --------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class Candidate:
    """A value found at one level for one key (one row of the explain view, 02 §14.5)."""

    level: Level
    value: Any = field(hash=False)
    locked: bool = False
    scope_id: int | str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", Level(self.level))


@dataclass(frozen=True)
class Shadowed:
    candidate: Candidate
    reason: str  # OVERRIDDEN | LOCKED_ABOVE | REQUIRED_BY_LAW | LEVEL_NOT_ALLOWED
    by: Level | None


@dataclass(frozen=True)
class Resolution:
    key: str | None
    value: Any
    level: Level | None
    locked: bool
    legal_source: str | None
    shadowed: tuple[Shadowed, ...]
    issues: tuple[Issue, ...]
    chain: tuple[Candidate, ...]

    @property
    def ok(self) -> bool:
        return self.value is not UNSET and not any(i.severity == "ERROR" for i in self.issues)


def resolve_chain(
    candidates: Iterable[Candidate],
    *,
    key: str | None = None,
    legal_limits: Iterable[LegalConstraint] = (),
    derive_context: Mapping[str, Any] | None = None,
) -> Resolution:
    """The effective value among per-level candidates (02 §14.1-§14.3).

    Precedence decides (EC-CFG-01) unless a candidate is locked: then the most general locked
    value wins and narrower ones are shadowed (EC-CFG-04). A value the law requires wins over
    everything (EC-CFG-19); a stored value that contradicts it is reported (V-10). Other legal
    limits never change the value silently: violations come back as issues with the legal
    source (EC-CFG-06). With ``key``, candidates at levels the key does not allow are ignored,
    the registry default is the PLATFORM candidate, and the winning value is re-validated: an
    invalid payroll value resolves to UNSET with a CONFIG_INVALID error, never to a default.
    """
    defn = definition(key) if key is not None else None
    name = defn.key if defn else None
    ignored: list[Shadowed] = []
    usable: list[Candidate] = []
    seen: set[Level] = set()
    for candidate in candidates:
        if not isinstance(candidate, Candidate):
            raise TypeError("candidates must be Candidate instances")
        if candidate.level in seen:
            raise SettingValidationError(
                "DUPLICATE_LEVEL", f"two candidates at {candidate.level.value}", key=name
            )
        seen.add(candidate.level)
        if defn and candidate.level in SCOPE_LEVELS and candidate.level not in defn.allowed_levels:
            ignored.append(Shadowed(candidate, "LEVEL_NOT_ALLOWED", None))
        else:
            usable.append(candidate)
    if defn and Level.PLATFORM not in seen:
        usable.append(Candidate(Level.PLATFORM, defn.default))
    ordered = sorted(usable, key=lambda c: PRECEDENCE.index(c.level))
    limits = [lim for lim in legal_limits if name is None or lim.target == name]
    required = [lim for lim in limits if lim.required_value is not UNSET]
    if len({repr(lim.required_value) for lim in required}) > 1:
        raise SettingValidationError("CONFLICTING_LEGAL_REQUIREMENTS", "legal constraints disagree", key=name)

    issues: list[Issue] = []
    shadowed: list[Shadowed] = list(ignored)
    if required:
        law = required[0]
        level, locked, legal_source = Level.COUNTRY_PACK, True, law.source
        raw: Any = law.required_value
        for candidate in ordered:
            if candidate.level is Level.PLATFORM:
                continue
            shadowed.append(Shadowed(candidate, "REQUIRED_BY_LAW", Level.COUNTRY_PACK))
            if candidate.level is not Level.COUNTRY_PACK and not _same(candidate.value, raw):
                issues.append(
                    Issue(
                        "V-10", "ERROR", (name or law.target,),
                        f"The value saved at {candidate.level.value} ({_show(candidate.value)}) contradicts the law "
                        f"({law.source}); {_show(raw)} applies. Update the saved value.",
                        {"source": law.source, "level": candidate.level.value, "required_value": _show(raw)},
                    )
                )
    elif ordered:
        locked_candidates = [c for c in ordered if c.locked]
        winner = locked_candidates[-1] if locked_candidates else ordered[0]
        index = ordered.index(winner)
        for candidate in ordered[:index]:
            shadowed.append(Shadowed(candidate, "LOCKED_ABOVE", winner.level))
        for candidate in ordered[index + 1:]:
            if candidate.level is not Level.PLATFORM:
                shadowed.append(Shadowed(candidate, "OVERRIDDEN", winner.level))
        level, locked, legal_source, raw = winner.level, winner.locked, None, winner.value
    else:
        return Resolution(name, UNSET, None, False, None, tuple(shadowed), (), ())

    value = raw
    if defn is not None:
        value, problem = _resolve_value(defn, raw, level, derive_context)
        if problem is not None:
            issues.append(problem)
    for limit in limits:
        if limit.required_value is UNSET:
            issue = check_legal_value(limit, value, label=defn.label if defn else None)
            if issue is not None:
                issues.append(issue)
    return Resolution(name, value, level, locked, legal_source, tuple(shadowed), tuple(issues), tuple(ordered))


def _resolve_value(
    defn: SettingDef, raw: Any, level: Level, context: Mapping[str, Any] | None
) -> tuple[Any, Issue | None]:
    if isinstance(raw, Derived):
        if context is None:
            return raw, None
        try:
            return derive_default(defn.key, context), None
        except SettingValidationError as error:
            return UNSET, Issue("CONFIG_INVALID", "ERROR", (defn.key,), error.message, {"code": error.code})
    try:
        return validate_value(defn.key, raw), None
    except SettingValidationError as error:
        details = {"code": error.code, "level": level.value}
        if defn.fails_closed:
            return UNSET, Issue(
                "CONFIG_INVALID", "ERROR", (defn.key,),
                f"The value of {defn.label} saved at {level.value} is invalid ({error.message}).", details,
            )
        return defn.default, Issue(
            "CONFIG_INVALID", "WARNING", (defn.key,),
            f"The value of {defn.label} saved at {level.value} is invalid ({error.message}); the default is used.",
            details,
        )


@dataclass(frozen=True)
class EffectiveValue:
    """One effective-dated value of a key at one scope (setting_values row, policy version)."""

    effective_from: date
    effective_to: date | None
    value: Any = field(hash=False)

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)


def value_on(rows: Iterable[EffectiveValue], day: date) -> Any:
    """The value whose window contains ``day``; overlapping windows are a data error."""
    hits = [row for row in rows if row.covers(day)]
    if len(hits) > 1:
        raise SettingValidationError("OVERLAPPING_ROWS", f"several values are effective on {day.isoformat()}")
    return hits[0].value if hits else UNSET


def daily_values(
    rows: Sequence[EffectiveValue], period_start: date, period_end: date, *, grain: str
) -> dict[date, Any]:
    """The value that applies to each day of a period (3.1 §2.2, EC-CFG-02, EC-CFG-08, EC-CFG-18).

    PERIOD grain: the value effective on the period end applies to every day. DAY grain: each
    day takes the value effective on that day.
    """
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {GRAINS}")
    if period_end < period_start:
        raise ValueError("period_end is before period_start")
    days = [period_start + timedelta(days=n) for n in range((period_end - period_start).days + 1)]
    if grain == "PERIOD":
        closing = value_on(rows, period_end)
        return {day: closing for day in days}
    return {day: value_on(rows, day) for day in days}


# --------------------------------------------------------------------------- cross-setting rules

_ATTENDANCE_SOURCES: Final = ("ATTENDANCE_WITH_APPROVAL", "ATTENDANCE_AUTO")
_RULE_ORDER: Final = (
    "UNKNOWN_SETTING", "CONFIG_INVALID", "POLICY_INVALID", "FEATURE_NOT_AVAILABLE",
    *(f"V-{n:02d}" for n in range(1, 17)),
    "LEGAL_LIMIT", "PROTECTED_NET_REQUIRED", "REQUIRED_PAY_DAY_VALUE", "REQUIRED_BIWEEKLY_ANCHOR",
)


def _normalize_values(values: Mapping[str, Any], issues: list[Issue]) -> tuple[dict[str, Any], set[str]]:
    normalized: dict[str, Any] = {}
    invalid: set[str] = set()
    origin: dict[str, str] = {}
    for given, value in values.items():
        try:
            key = canonical_key(given)
        except SettingValidationError as error:
            issues.append(Issue("UNKNOWN_SETTING", "ERROR", (given,), error.message))
            continue
        if key in origin:
            issues.append(
                Issue("CONFIG_INVALID", "ERROR", (key,), f"{key} is given twice (as {origin[key]} and {given})")
            )
            invalid.add(key)
            continue
        origin[key] = given
        if isinstance(value, Derived):
            normalized[key] = value
            continue
        try:
            normalized[key] = validate_value(key, value)
        except SettingValidationError as error:
            invalid.add(key)
            issues.append(
                Issue("CONFIG_INVALID", "ERROR", (key,), f"{REGISTRY[key].label}: {error.message}", {"code": error.code})
            )
    return normalized, invalid


def _minor_unit(code: str, minor_units: Mapping[str, int] | None) -> int | None:
    if minor_units is not None and code in minor_units:
        return minor_units[code]
    try:
        from app.payroll import currencies
    except ModuleNotFoundError as error:
        if error.name not in ("app.payroll.currencies",):
            raise
        return None
    # find() returns None for unknown or malformed codes; the caller reports them as invalid values.
    info = currencies.find(code)
    return getattr(info, "minor_unit", None)


def _v14_static(eff: Callable[[str], Any]) -> bool:
    """Monthly DAY_OF_MONTH pay dates (day N of the month the period ends in) before the period end."""
    if eff("payroll.pay_frequency") != "MONTHLY" or eff("payroll.calendar.pay_day_rule") != "DAY_OF_MONTH":
        return False
    day = eff("payroll.calendar.pay_day_value")
    start = eff("payroll.calendar.monthly_start_day")
    if not isinstance(day, int) or not isinstance(start, int):
        return False
    latest_period_end_day = 31 if start == 1 else start - 1
    return day < latest_period_end_day


def cross_validate(
    values: Mapping[str, Any],
    pack_constraints: Iterable[LegalConstraint] | None = None,
    *,
    policies: Iterable[tuple[str, str, Mapping[str, Any]]] = (),
    rules: Mapping[str, Sequence[AssignmentRule]] | None = None,
    employees: Sequence[EmployeeAttributes] = (),
    as_of: date | None = None,
    currencies: Iterable[str] = (),
    minor_units: Mapping[str, int] | None = None,
    periods: Iterable[tuple[date, date]] = (),
) -> list[Issue]:
    """Cross-setting rules V-01..V-16 (3.1 §7) for the effective values at one scope.

    ``values`` are the effective values at the scope (keys or legacy aliases); missing keys take
    the PLATFORM default. ``pack_constraints`` are the country pack's legal limits (V-10, V-16).
    ``policies`` are the ``(type, name, fields)`` in force at the scope (V-02, V-03, V-16). V-11
    and V-12 run when ``rules`` (assignment rules per policy type) and ``employees`` are given.
    V-13 checks the net-pay increment against ``payroll.currency`` and every code in
    ``currencies``; minor units come from ``minor_units`` or the currency catalogue. V-14 also
    checks concrete ``(period_end, pay_date)`` pairs. Invalid values are reported as
    CONFIG_INVALID and the rules that need them are skipped, never evaluated on a default.
    """
    issues: list[Issue] = []
    eff_values, invalid = _normalize_values(values, issues)

    def eff(key: str) -> Any:
        return eff_values[key] if key in eff_values else REGISTRY[key].default

    def usable(*keys: str) -> bool:
        return not invalid.intersection(keys)

    def add(rule: str, severity: str, keys: Sequence[str], message: str, **details: Any) -> None:
        issues.append(Issue(rule, severity, tuple(keys), message, details))

    constraints = list(pack_constraints or ())
    switched_on = {key for key, defn in REGISTRY.items() if defn.type == "bool" and usable(key) and eff(key) is True}
    checked_policies = _checked_policies(policies, issues, switched_on)
    for key in sorted(switched_on):
        if REGISTRY[key].support_status == "NOT_IMPLEMENTED":
            add("FEATURE_NOT_AVAILABLE", "ERROR", (key,),
                f"{REGISTRY[key].label} is not available in this release; turn it off.")
    attendance_on = eff("payroll.attendance.enabled") is True
    attendance = "payroll.attendance.enabled"

    if usable("payroll.late_penalty.enabled", attendance) and eff("payroll.late_penalty.enabled") is True \
            and not attendance_on:
        add("V-01", "ERROR", ("payroll.late_penalty.enabled", attendance),
            "Late penalties need attendance in the same scope: there are no late marks without it. "
            "Turn on \"Use attendance for payroll\" or turn penalties off.")
    if usable("payroll.overtime.enabled", attendance) and eff("payroll.overtime.enabled") is True and not attendance_on:
        for ptype, pname, fields in checked_policies:
            if ptype == "OVERTIME_POLICY" and fields.get("source") in _ATTENDANCE_SOURCES:
                add("V-02", "ERROR", (attendance, "payroll.overtime.enabled", "OVERTIME_POLICY.source"),
                    f"Overtime policy \"{pname}\" takes overtime from attendance ({fields['source']}), which is off. "
                    "Turn on attendance or use APPROVED_REQUESTS.", policy=pname)
    holiday_pay = "payroll.holiday_pay.non_salaried.enabled"
    if usable(holiday_pay, attendance) and eff(holiday_pay) is True and not attendance_on:
        for ptype, pname, fields in checked_policies:
            if ptype == "HOLIDAY_PAY_POLICY" and fields.get("eligibility_adjacent_days", "NONE") != "NONE":
                add("V-03", "ERROR",
                    (attendance, holiday_pay, "HOLIDAY_PAY_POLICY.eligibility_adjacent_days"),
                    f"Holiday pay policy \"{pname}\" pays hourly and daily workers only when they are present on "
                    "adjacent days, which needs attendance. Turn on attendance or set adjacent-day eligibility to "
                    "NONE.", policy=pname)
    if usable("payroll.lop.proration_basis", attendance) and eff("payroll.lop.proration_basis") == "HOURS" \
            and not attendance_on:
        add("V-04", "ERROR", ("payroll.lop.proration_basis", attendance),
            "The HOURS proration basis needs attendance hours. Turn on attendance or choose another basis.")
    if usable("payroll.lop.proration_basis", "payroll.lop.custom_days") \
            and eff("payroll.lop.proration_basis") == "CUSTOM_DAYS" and eff("payroll.lop.custom_days") is None:
        add("V-05", "ERROR", ("payroll.lop.custom_days", "payroll.lop.proration_basis"),
            "Enter the custom number of basis days for the CUSTOM_DAYS proration basis.")
    if usable("payroll.attendance_cycle.mode", "payroll.attendance_cycle.start_day") \
            and eff("payroll.attendance_cycle.mode") == "CUSTOM_START_DAY" \
            and eff("payroll.attendance_cycle.start_day") is None:
        add("V-06", "ERROR", ("payroll.attendance_cycle.start_day", "payroll.attendance_cycle.mode"),
            "Enter the start day of the custom attendance cycle.")
    cutoffs = ("payroll.cutoff.input_days_before_pay_day", "payroll.cutoff.approval_days_before_pay_day",
               "payroll.cutoff.payment_lead_business_days")
    if usable(*cutoffs):
        input_days, approval_days, lead = (eff(k) for k in cutoffs)
        if not input_days > approval_days > lead:
            add("V-07", "ERROR", cutoffs,
                f"Deadlines must come in order before the pay date: input cutoff ({input_days}) > approval "
                f"deadline ({approval_days}) > payment lead time ({lead}).")
    if usable("payroll.task_pay.enabled", "tasks.enabled") and eff("payroll.task_pay.enabled") is True \
            and eff("tasks.enabled") is not True:
        add("V-08", "ERROR", ("payroll.task_pay.enabled", "tasks.enabled"),
            "Task-based pay needs the tasks module. Turn on \"Tasks module\" first.")
    if usable("payslip.email_attach_pdf", "payslip.pdf_protection") and eff("payslip.email_attach_pdf") is True \
            and eff("payslip.pdf_protection") != "EMPLOYEE_SECRET":
        add("V-09", "ERROR", ("payslip.email_attach_pdf", "payslip.pdf_protection"),
            "Payslip PDFs can be emailed only when PDF protection is EMPLOYEE_SECRET.")
    issues.extend(_protected_net_issues(eff, usable))

    for constraint in constraints:
        if constraint.is_policy_target or constraint.target not in REGISTRY or not usable(constraint.target):
            continue
        issue = check_legal_value(constraint, eff(constraint.target), label=REGISTRY[constraint.target].label)
        if issue is not None:
            issues.append(issue)
    for ptype, pname, fields in checked_policies:
        issues.extend(policy_legal_issues(ptype, fields, constraints, name=pname))

    if rules is not None and employees:
        issues.extend(_assignment_issues(eff, rules, employees, as_of))

    if usable("payroll.rounding.net_pay_increment", "payroll.currency"):
        issues.extend(_v13(eff("payroll.rounding.net_pay_increment"), eff("payroll.currency"), currencies, minor_units))
    if usable("payroll.attendance_cycle.mode", "payroll.pay_frequency", "payroll.calendar.pay_day_rule",
              "payroll.calendar.pay_day_value", "payroll.calendar.monthly_start_day") \
            and eff("payroll.attendance_cycle.mode") == "SAME_AS_PERIOD":
        early = [(end, pay) for end, pay in periods if pay < end]
        if early or _v14_static(eff):
            add("V-14", "WARNING", ("payroll.attendance_cycle.mode", "payroll.calendar.pay_day_rule"),
                "Attendance after the pay date will be corrected next period. Consider a 26th-25th attendance cycle.",
                periods=[{"period_end": e.isoformat(), "pay_date": p.isoformat()} for e, p in early[:5]])
    if usable("payroll.bank.exclude_unverified_from_payout", "payroll.bank.require_verification") \
            and eff("payroll.bank.exclude_unverified_from_payout") is False \
            and eff("payroll.bank.require_verification") is True:
        add("V-15", "WARNING", ("payroll.bank.exclude_unverified_from_payout", "payroll.bank.require_verification"),
            "Unverified accounts will be flagged but still paid.")

    rule_day = ("payroll.calendar.pay_day_rule", "payroll.calendar.pay_day_value")
    if usable(*rule_day) and eff(rule_day[0]) in ("DAY_OF_MONTH", "DAYS_AFTER_PERIOD_END"):
        day = eff(rule_day[1])
        if day is None or (eff(rule_day[0]) == "DAY_OF_MONTH" and day < 1):
            add("REQUIRED_PAY_DAY_VALUE", "ERROR", rule_day[::-1],
                "Enter the pay day number: day 1-31 for DAY_OF_MONTH, or 0-31 days for DAYS_AFTER_PERIOD_END.")
    if usable("payroll.pay_frequency", "payroll.calendar.biweekly_anchor_date") \
            and eff("payroll.pay_frequency") == "BI_WEEKLY" and eff("payroll.calendar.biweekly_anchor_date") is None:
        add("REQUIRED_BIWEEKLY_ANCHOR", "ERROR", ("payroll.calendar.biweekly_anchor_date", "payroll.pay_frequency"),
            "Bi-weekly pay groups need the start date of their first period.")

    rank = {rule: n for n, rule in enumerate(_RULE_ORDER)}
    return sorted(issues, key=lambda i: rank.get(i.rule, len(rank)))


def _v13(
    increment: Any, pay_group_currency: Any, currencies: Iterable[str], minor_units: Mapping[str, int] | None
) -> list[Issue]:
    keys = ("payroll.rounding.net_pay_increment", "payroll.currency")
    if increment is None or isinstance(increment, Derived):
        return []
    if not Decimal(0) < increment <= Decimal(100):
        return [Issue("V-13", "ERROR", keys, "The net pay rounding increment must be above 0 and at most 100 units.")]
    codes = list(dict.fromkeys([*([pay_group_currency] if isinstance(pay_group_currency, str) else []), *currencies]))
    issues: list[Issue] = []
    for code in codes:
        minor = _minor_unit(code, minor_units)
        if minor is None:
            issues.append(Issue("V-13", "ERROR", keys, f"The minor unit of {code} is unknown, so the net pay "
                                "rounding increment cannot be checked.", {"currency": code}))
            continue
        step = Decimal(1).scaleb(-minor)
        if increment % step != 0:
            issues.append(Issue("V-13", "ERROR", keys, f"The net pay rounding increment {decimal_str(increment)} is "
                                f"not a multiple of the {code} minor unit {decimal_str(step)}.", {"currency": code}))
    return issues


_PROTECTED_NET: Final = "payroll.deductions.min_net_pay_percent"


def _protected_net_issues(eff: Callable[[str], Any], usable: Callable[..., bool]) -> list[Issue]:
    """Automated recoveries need an explicit protected net pay percentage (G-23)."""
    if not usable(_PROTECTED_NET) or eff(_PROTECTED_NET) is not None:
        return []
    issues: list[Issue] = []
    for key in ("payroll.loans.enabled", "payroll.advances.enabled"):
        if usable(key) and eff(key) is True:
            issues.append(Issue(
                "PROTECTED_NET_REQUIRED", "ERROR", (key, _PROTECTED_NET),
                f"{REGISTRY[key].label} are recovered from net pay: set the protected net pay percentage first.",
            ))
    mode = "payroll.arrears.recovery_mode"
    if usable("payroll.arrears.auto_compute", mode) and eff("payroll.arrears.auto_compute") is True \
            and eff(mode) != "MANUAL":
        issues.append(Issue(
            "PROTECTED_NET_REQUIRED", "WARNING", (mode, _PROTECTED_NET),
            "Negative arrears will not be recovered automatically until the protected net pay percentage is set; "
            "they are raised as exceptions instead.",
        ))
    return issues


def _checked_policies(
    policies: Iterable[tuple[str, str, Mapping[str, Any]]], issues: list[Issue], switched_on: Collection[str]
) -> list[tuple[str, str, dict[str, Any]]]:
    """Policies in force are validated as for activation, with the switches that are ON."""
    entries = list(policies)
    if not entries:
        return []
    from app.payroll.policy_types import PolicyValidationError, validate_fields

    checked: list[tuple[str, str, dict[str, Any]]] = []
    for ptype, pname, fields in entries:
        try:
            normalized = validate_fields(ptype, fields, for_activation=True, enabled_switches=switched_on)
            checked.append((ptype, pname, normalized))
        except PolicyValidationError as error:
            issues.append(Issue("POLICY_INVALID", "ERROR", (ptype,), f"Policy \"{pname}\": {error.message}",
                                {"code": error.code, "policy": pname}))
    return checked


def _assignment_issues(
    eff: Callable[[str], Any],
    rules: Mapping[str, Sequence[AssignmentRule]],
    employees: Sequence[EmployeeAttributes],
    as_of: date | None,
) -> list[Issue]:
    from app.payroll.policy_types import POLICY_TYPES, find_priority_conflicts, resolve_assignment

    issues: list[Issue] = []
    for code, ptype in POLICY_TYPES.items():
        if ptype.module != "payroll":  # attendance types fall back to their LEGACY_V1 mapping (JC-AS-22)
            continue
        type_rules = list(rules.get(code, ()))
        for conflict in find_priority_conflicts(type_rules, employees, on=as_of):
            issues.append(Issue(
                "V-12", "ERROR", (code,),
                f"{code}: rules {', '.join(str(r) for r in conflict.rule_ids)} have the same priority "
                f"({conflict.priority}) and match the same employees: "
                f"{', '.join(str(e) for e in conflict.employee_ids)}.",
                {"policy_type": code, "priority": conflict.priority, "rule_ids": list(conflict.rule_ids),
                 "employee_ids": list(conflict.employee_ids)},
            ))
        if not ptype.assignable:
            continue
        gates = ptype.gate_switches
        if gates and not any(eff(gate) is True for gate in gates):
            continue
        missing = [e.employee_id for e in employees if resolve_assignment(type_rules, e, on=as_of).status == "NO_POLICY"]
        if missing:
            issues.append(Issue(
                "V-11", "ERROR", gates or (code,),
                f"{code}: {len(missing)} employee(s) in scope match no policy: {', '.join(map(str, missing))}.",
                {"policy_type": code, "employee_ids": missing}, precheck_only=True,
            ))
    return issues


# --------------------------------------------------------------------------- registration


def _check_definition(defn: SettingDef) -> None:
    def bad(message: str) -> SettingValidationError:
        return SettingValidationError("INVALID_DEFINITION", f"{defn.key}: {message}", key=defn.key)

    if not _KEY_RE.fullmatch(defn.key):
        raise bad("key must be dot-separated lower snake case")
    if defn.type not in SETTING_TYPES:
        raise bad(f"unknown type {defn.type}")
    if defn.section not in SECTION_BY_TITLE:
        raise bad(f"unknown section {defn.section}")
    if defn.resolution_grain not in GRAINS:
        raise bad(f"unknown grain {defn.resolution_grain}")
    if defn.sensitivity not in SENSITIVITIES:
        raise bad(f"unknown sensitivity {defn.sensitivity}")
    if defn.support_status not in SUPPORT_STATUSES:
        raise bad(f"unknown support status {defn.support_status}")
    if defn.legal_sensitivity not in LEGAL_SENSITIVITIES:
        raise bad(f"unknown legal sensitivity {defn.legal_sensitivity}")
    if (defn.legal_sensitivity == "STATUTORY") != (defn.statutory_scheme is not None):
        raise bad("statutory settings, and only they, name a statutory scheme")
    if not set(defn.allowed_levels) <= set(SCOPE_LEVELS):
        raise bad("allowed levels must be tenant scope levels")
    if not defn.label or not defn.help:
        raise bad("label and help are required")
    if defn.type == "bool" and not ("ON:" in defn.help and "OFF:" in defn.help):
        raise bad("help must explain ON and OFF")
    if defn.type in ("enum", "set") and not defn.constraints.get("choices"):
        raise bad("choices are required")
    if isinstance(defn.default, Derived):
        if defn.default.source not in DERIVED_SOURCES:
            raise bad(f"unknown derived default {defn.default.source}")
    elif defn.default is None:
        if not defn.nullable:
            raise bad("a None default needs a nullable setting")
    else:
        if to_json_safe(defn, defn.default) is None:
            raise bad("the default does not validate")
    if defn.new_tenant_default is not UNSET and to_json_safe(defn, defn.new_tenant_default) is None:
        raise bad("the new-tenant default does not validate")


def to_json_safe(defn: SettingDef, value: Any) -> Any:
    previous = REGISTRY.get(defn.key)
    REGISTRY[defn.key] = defn
    try:
        return to_json(defn.key, validate_value(defn.key, value))
    except SettingValidationError:
        return None
    finally:
        if previous is None:
            del REGISTRY[defn.key]
        else:
            REGISTRY[defn.key] = previous


def _add(defn: SettingDef) -> None:
    _check_definition(defn)
    if defn.key in REGISTRY or defn.key in ALIASES:
        raise SettingValidationError("DUPLICATE_SETTING", f"{defn.key} is already registered", key=defn.key)
    for alias in defn.aliases:
        if alias in REGISTRY or alias in ALIASES:
            raise SettingValidationError("DUPLICATE_SETTING", f"alias {alias} is already registered", key=defn.key)
    order = 10 * (1 + sum(1 for d in REGISTRY.values() if d.section == defn.section))
    if "order" not in defn.ui:
        object.__setattr__(defn, "ui", MappingProxyType({**defn.ui, "order": order}))
    REGISTRY[defn.key] = defn
    for alias in defn.aliases:
        ALIASES[alias] = defn.key


def register_pack_settings(defs: Iterable[SettingDef]) -> tuple[str, ...]:
    """Add a country pack's employer choices (``payroll.statutory.<CC>.*``, 3.1 §5.12, 3.2 §7.1).

    Re-registering an identical definition is a no-op; a different definition for an existing
    key is rejected. Pack settings live in the Statutory & tax section of the Payroll module,
    name their ``statutory_scheme``, and may be set only at legal entity, branch or employee
    level; a definition with no levels is pack-owned and read-only.
    """
    added: list[str] = []
    for defn in defs:
        if defn.legal_sensitivity == "NONE":
            defn = replace(defn, legal_sensitivity="STATUTORY")
        match = _STATUTORY_KEY_RE.fullmatch(defn.key)
        if not match:
            raise SettingValidationError(
                "INVALID_DEFINITION", f"{defn.key}: pack settings use payroll.statutory.<CC>.<scheme>…", key=defn.key
            )
        if not defn.statutory_scheme or not defn.statutory_scheme.startswith(f"{match.group(1)}."):
            raise SettingValidationError(
                "INVALID_DEFINITION", f"{defn.key}: statutory_scheme must be '<CC>.<scheme>'", key=defn.key
            )
        if defn.module != PAYROLL_MODULE or defn.section != S_STATUTORY:
            raise SettingValidationError(
                "INVALID_DEFINITION", f"{defn.key}: pack settings belong to Payroll › {S_STATUTORY}", key=defn.key
            )
        if not set(defn.allowed_levels) <= {LE, B, E}:
            raise SettingValidationError(
                "INVALID_DEFINITION", f"{defn.key}: pack settings are set per legal entity, branch or employee",
                key=defn.key,
            )
        existing = REGISTRY.get(defn.key)
        if existing is not None:
            if definition_json(existing) != definition_json(_with_order(defn, existing.ui.get("order"))):
                raise SettingValidationError(
                    "DUPLICATE_SETTING", f"{defn.key} is already registered differently", key=defn.key
                )
            continue
        _add(defn)
        _PACK_KEYS.add(defn.key)
        added.append(defn.key)
    return tuple(added)


def _with_order(defn: SettingDef, order: Any) -> SettingDef:
    if order is None or "order" in defn.ui:
        return defn
    clone = SettingDef(**{f: getattr(defn, f) for f in defn.__dataclass_fields__})
    object.__setattr__(clone, "ui", MappingProxyType({**defn.ui, "order": order}))
    return clone


def unregister_pack_settings(country_code: str) -> tuple[str, ...]:
    """Remove a pack's settings (pack disabled, or test isolation)."""
    prefix = f"payroll.statutory.{country_code}."
    removed = tuple(sorted(k for k in _PACK_KEYS if k.startswith(prefix)))
    for key in removed:
        for alias in REGISTRY[key].aliases:
            ALIASES.pop(alias, None)
        del REGISTRY[key]
        _PACK_KEYS.discard(key)
    return removed


def pack_setting_keys() -> tuple[str, ...]:
    return tuple(sorted(_PACK_KEYS))


for _definition in _catalogue():
    _add(_definition)
for _definition in REGISTRY.values():
    for _dependency in _definition.depends_on:
        if _dependency not in REGISTRY:
            raise SettingValidationError(
                "INVALID_DEFINITION", f"{_definition.key}: unknown dependency {_dependency}", key=_definition.key
            )
del _definition


# --------------------------------------------------------------------------- configuration profiles

PROFILE_SOURCES: Final = ("STARTER_PROFILE", "LEGACY_MIGRATION")


@dataclass(frozen=True)
class ProfilePolicy:
    """A catch-all starter policy (everyone, priority 0) of a profile.

    It is created ACTIVE when one of its type's gate switches is ON in the profile (or the type
    is always in force) and DRAFT otherwise. ``fields`` are overrides on top of the type's
    defaults; ``rows_from_leave_types`` builds LEAVE_PAY_POLICY rows from the tenant's leave types.
    """

    type: str
    name: str
    fields: Mapping[str, Any] = field(default_factory=dict, hash=False)
    rows_from_leave_types: bool = False
    note: str | None = None


@dataclass(frozen=True)
class Profile:
    """A materialization profile: explicit TENANT values, locks, checklist decisions and policies."""

    name: str
    source: str
    reason: str
    description: str
    values: Mapping[str, Any] = field(hash=False)
    locks: frozenset[str] = frozenset()
    decisions: Mapping[str, str] = field(default_factory=dict, hash=False)
    legacy_keys: tuple[str, ...] = ()
    policies: tuple[ProfilePolicy, ...] = ()
    acknowledgments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileRow:
    """One TENANT-level ``setting_values`` row written by a profile (value in JSON form)."""

    key: str
    value: Any
    locked: bool
    source: str
    profile_version: str
    reason: str


_STARTER_VALUES: Final[Mapping[str, Any]] = MappingProxyType({
    "payroll.enabled": True,
    "payroll.attendance.enabled": True,
    "payroll.attendance.half_day_value": "0.5",
    "payroll.lop.proration_basis": "CALENDAR_DAYS",
    "payroll.lop.sandwich_rule": "OFF",
    "payroll.lop.zero_paid_days": "EXCEPTION",
    "payroll.leave.enabled": True,
    "payroll.leave.when_disabled_treat_approved_leave_as": "PAID_EXCUSED",
    "payroll.leave.unapproved_at_cutoff": "EXCEPTION",
    "payroll.leave.negative_balance_on_exit": "EXCEPTION",
    "payroll.leave.encashment.enabled": False,
    "payroll.holiday_pay.non_salaried.enabled": False,
    "payroll.holiday_work_premium.enabled": False,
    "payroll.weekly_off_pay.enabled": False,
    "payroll.overtime.enabled": False,
    "tasks.enabled": True,
    "payroll.task_pay.enabled": True,
    "payroll.late_penalty.enabled": False,
    "payroll.shift_allowance.enabled": False,
    "payroll.wage_inputs.enabled": True,
    "payroll.wage_inputs.require_approval_above": {"amount": "0", "currency": None},
    "payroll.incentives.enabled": False,
    "payroll.arrears.auto_compute": True,
    "payroll.arrears.payment_mode": "NEXT_REGULAR_RUN",
    "payroll.arrears.recovery_mode": "MANUAL",
    "payroll.reimbursements.enabled": True,
    "payroll.reimbursements.payout_route": "WITH_PAYROLL",
    "payroll.loans.enabled": False,
    "payroll.advances.enabled": False,
    "payroll.deductions.min_net_pay_percent": None,
    "payroll.accounting.enabled": False,
    "payroll.structures.net_based_enabled": False,
    "payroll.fx.manual_rate_requires_approval": True,
    "payroll.approval.separation_of_duties": True,
    "payroll.approval.payment_dual_control": True,
    "payroll.approval.step_up_for_finalize": True,
    "settings.change_approval.enabled": False,
    "payslip.publish_trigger": "ON_FINALIZE",
    "payslip.email_notify": True,
    "payslip.email_include_net_pay": False,
    "payroll.payments.reissue_requires_approval": True,
    "payroll.bank.self_service_enabled": True,
    "payroll.bank.change_requires_approval": True,
    "payroll.bank.cooling_off_hours": 48,
    "payroll.bank.require_verification": True,
    "payroll.bank.exclude_unverified_from_payout": True,
    "payroll.bank.verification_methods": ["FORMAT", "DOCUMENT_REVIEW"],
    "payroll.bank.split_payments_enabled": False,
    "org.allow_concurrent_assignments": False,
})

_STARTER_DECISIONS: Final[Mapping[str, str]] = MappingProxyType({
    "payroll.attendance.enabled": "Do you record attendance in this system? If you do not, turn attendance off; "
    "otherwise every working day without attendance becomes a review exception.",
    "payroll.overtime.enabled": "Overtime pay is off. To pay overtime, enter the multipliers in the overtime "
    "policy (none is assumed) and turn it on. The law may require it for some workers.",
    "payroll.holiday_pay.non_salaried.enabled": "Are hourly or daily workers paid for holidays? If so, choose the "
    "holiday pay method in the holiday pay policy and turn it on.",
    "payroll.holiday_work_premium.enabled": "Is work on a holiday paid a premium? If so, enter the multiplier in "
    "the holiday pay policy and turn it on.",
    "payroll.weekly_off_pay.enabled": "Is work on a weekly off paid a premium? If so, enter the multiplier in the "
    "holiday pay policy and turn it on.",
    "payroll.shift_allowance.enabled": "Do night or weekend shifts earn an allowance? If so, enter the amounts "
    "in the shift allowance policy and turn it on.",
    "payroll.late_penalty.enabled": "Late penalties are off. Turn them on only where the law allows deductions "
    "for late marks.",
    "payroll.incentives.enabled": "Automatic incentives are off. Define a plan (measures, formula, cap) before "
    "turning them on.",
    "payroll.loans.enabled": "Loans are off. Set the protected net pay percentage before turning them on.",
    "payroll.advances.enabled": "Salary advances are off. Set the protected net pay percentage before turning "
    "them on.",
    "payroll.leave.encashment.enabled": "Is unused leave paid out? If so, enter the encashment formula of each "
    "encashable leave type and turn encashment on.",
    "payroll.accounting.enabled": "Accounting journals are off; they need a GL account for every component.",
    "payroll.bank.split_payments_enabled": "Split payments are off; turn them on only if your bank files support "
    "several accounts per employee.",
})

_LEGACY_VALUES: Final[Mapping[str, Any]] = MappingProxyType({
    "payroll.enabled": True,
    "payroll.attendance.enabled": True,
    "payroll.attendance.half_day_value": "0.5",
    "payroll.lop.proration_basis": "WORKING_DAYS",
    "payroll.leave.enabled": False,
    "payroll.overtime.enabled": False,
    "payroll.wage_inputs.enabled": True,
    "payroll.approval.separation_of_duties": True,
})

PROFILES: Final[Mapping[str, Profile]] = MappingProxyType({
    "STARTER_V1": Profile(
        name="STARTER_V1",
        source="STARTER_PROFILE",
        reason=STARTER_REASON,
        description="New tenants: features that pay only approved records or raise review exceptions are ON; "
        "features that need a legal constant or a company decision are OFF with a draft policy.",
        values=_STARTER_VALUES,
        locks=frozenset({
            "payroll.approval.separation_of_duties",
            "payroll.approval.payment_dual_control",
            "payroll.fx.manual_rate_requires_approval",
            "payroll.bank.change_requires_approval",
        }),
        decisions=_STARTER_DECISIONS,
        policies=(
            ProfilePolicy("ATTENDANCE_PAY_POLICY", "Default attendance pay", {
                "unmarked_day": "EXCEPTION", "missing_punch_out": "EXCEPTION",
                "derive_status_from_hours": False, "count_on_duty_as_present": True,
            }),
            ProfilePolicy("LEAVE_PAY_POLICY", "Default leave pay", rows_from_leave_types=True,
                          note="Paid leave types are paid in full and the others are unpaid; review statutory "
                          "leave (maternity, paternity, sick)."),
            ProfilePolicy("TASK_PAY_POLICY", "Default task pay", {
                "attribution": "APPROVAL_DATE", "rate_source": "TASK_THEN_RATE_CARD", "cap_action": "EXCEPTION",
                "minimum_wage_floor": "WARN", "requires_evidence": True, "clawback": "MANUAL", "allow_split": False,
            }),
            ProfilePolicy("REIMBURSEMENT_POLICY", "Default reimbursements", {
                "claim_types": [{
                    "code": "GENERAL", "name": "General expense",
                    "receipt_required_above": {"amount": "0", "currency": None},
                }],
                "submission_window_days": 60, "duplicate_receipt_check": True,
            }),
            ProfilePolicy("PAYSLIP_TEMPLATE", "Default payslip"),
            ProfilePolicy("OVERTIME_POLICY", "Default overtime", {
                "source": "APPROVED_REQUESTS", "daily_threshold": "SHIFT_DURATION", "min_minutes": 30,
                "rounding": {"increment_minutes": 15, "mode": "DOWN"}, "cap_action": "EXCEPTION",
                "comp_off_allowed": False,
            }, note="Enter the multipliers before activating: none is assumed."),
            ProfilePolicy("HOLIDAY_PAY_POLICY", "Default holiday pay", {
                "holiday_work_requires_approval": True, "holiday_work_min_hours": "4.0",
            }, note="Enter the holiday-work and weekly-off multipliers before activating: none is assumed."),
            ProfilePolicy("SHIFT_ALLOWANCE_POLICY", "Default shift allowance"),
            ProfilePolicy("LATE_PENALTY_POLICY", "Default late penalty"),
            ProfilePolicy("INCENTIVE_PLAN", "Default incentive plan"),
            ProfilePolicy("LOAN_POLICY", "Default loans"),
            ProfilePolicy("ADVANCE_POLICY", "Default advances"),
        ),
    ),
    "LEGACY_V1": Profile(
        name="LEGACY_V1",
        source="LEGACY_MIGRATION",
        reason=LEGACY_REASON,
        description="Existing tenants: reproduces the legacy payroll (unmarked days paid, absences and half days "
        "deducted over working days, leave and overtime not used by payroll).",
        values=_LEGACY_VALUES,
        legacy_keys=(
            "payroll.approval.separation_of_duties",
            "attendance.grace_minutes",
            "attendance.lunch_is_paid",
        ),
        policies=(
            ProfilePolicy("ATTENDANCE_PAY_POLICY", "Legacy attendance pay", {
                "unmarked_day": "PRESENT", "missing_punch_out": "FULL_DAY",
            }),
            ProfilePolicy("PAYSLIP_TEMPLATE", "Default payslip"),
            ProfilePolicy("OVERTIME_POLICY", "Legacy overtime", {"source": "APPROVED_REQUESTS"},
                          note="The legacy overtime multiplier, if any, was copied into this draft; overtime "
                          "stays off until it is reviewed (EC-MIG-04)."),
            ProfilePolicy("LATE_PENALTY_POLICY", "Legacy late penalty", {
                "penalty_type": "FIXED_AMOUNT", "free_late_marks": 0,
            }, note="Legacy payroll deducted a fixed amount per late mark; it is not applied unless you enter an "
               "amount in your currency and turn penalties on (EC-MIG-03)."),
        ),
        acknowledgments=(
            "The legacy fixed penalty per late mark is no longer applied.",
            "Net pay is no longer clipped at zero; a negative net pay becomes a review exception.",
            "Statutory deductions are not calculated unless a verified country pack is active.",
        ),
    ),
})


def _profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise SettingValidationError("UNKNOWN_PROFILE", f"{name!r} is not a configuration profile") from None


def _legacy_overrides(profile: Profile, legacy_values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Values carried over from today's settings (3.1 §2.4); other legacy keys are ignored here."""
    if not legacy_values:
        return {}
    if not profile.legacy_keys:
        raise SettingValidationError("LEGACY_VALUES_NOT_ALLOWED", f"{profile.name} does not take legacy values")
    found: dict[str, Any] = {}
    for given, value in legacy_values.items():
        key = given if given in REGISTRY else ALIASES.get(given)
        if key not in profile.legacy_keys:
            continue
        if key in found:
            raise SettingValidationError("LEGACY_VALUE_INVALID", f"{key} is given twice", key=key)
        try:
            found[key] = validate_value(key, value)
        except SettingValidationError as error:
            raise SettingValidationError(
                "LEGACY_VALUE_INVALID", f"The legacy value of {given} is invalid: {error.message}", key=key
            ) from None
    return found


def profile_values(
    name: str, *, leave_types: Iterable[Any], legacy_values: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Every TENANT-level value a profile materializes, normalized (3.1 §9, decisions I-13, I-14).

    Keys the profile does not choose take their registry default, so a later registry change
    never alters a materialized tenant. Keys with a context-derived default and pack keys are
    left to resolution. ``leave_types`` are validated as for ``profile_policies``;
    ``legacy_values`` (today's settings, by legacy or new key name) apply to ``LEGACY_V1`` only.
    """
    profile = _profile(name)
    from app.payroll.policy_types import normalize_leave_types

    normalize_leave_types(leave_types)
    return _materialize(profile, _legacy_overrides(profile, legacy_values))


def _materialize(profile: Profile, legacy: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for defn in definitions():
        if Level.TENANT not in defn.allowed_levels or defn.key in _PACK_KEYS or (not defn.legacy_v1 and profile.name == "LEGACY_V1"):
            continue
        if defn.key in legacy:
            values[defn.key] = legacy[defn.key]
        elif defn.key in profile.values:
            values[defn.key] = validate_value(defn.key, profile.values[defn.key])
        elif not isinstance(defn.default, Derived):
            values[defn.key] = validate_value(defn.key, defn.default)
    return values


def profile_rows(
    name: str, *, leave_types: Iterable[Any], legacy_values: Mapping[str, Any] | None = None
) -> list[ProfileRow]:
    """The ``setting_values`` rows of a profile, with lock, source and profile version."""
    profile = _profile(name)
    values = profile_values(name, leave_types=leave_types, legacy_values=legacy_values)
    return [
        ProfileRow(key, to_json(key, value), key in profile.locks, profile.source, profile.name, profile.reason)
        for key, value in values.items()
    ]


def profile_locks(name: str) -> frozenset[str]:
    return _profile(name).locks


def profile_policies(
    name: str, *, leave_types: Iterable[Any], legacy_values: Mapping[str, Any] | None = None
) -> list[Any]:
    """The profile's catch-all starter policies (``policy_types.StarterPolicy``)."""
    from app.payroll.policy_types import starter_policies

    return list(starter_policies(name, leave_types=leave_types, legacy_values=legacy_values))


def _check_profile(profile: Profile) -> None:
    def bad(message: str) -> SettingValidationError:
        return SettingValidationError("INVALID_PROFILE", f"{profile.name}: {message}")

    if profile.source not in PROFILE_SOURCES:
        raise bad(f"unknown source {profile.source}")
    for key, value in profile.values.items():
        defn = REGISTRY.get(key)
        if defn is None or Level.TENANT not in defn.allowed_levels:
            raise bad(f"{key} cannot be set at TENANT level")
        if key.startswith("payroll.statutory."):
            raise bad(f"{key}: statutory schemes are never set by a profile")
        if validate_value(key, value) is True and defn.support_status == "NOT_IMPLEMENTED":
            raise bad(f"{key} is not implemented and cannot be ON")
    for key in profile.locks:
        if key not in profile.values or not REGISTRY[key].lockable:
            raise bad(f"{key} cannot be locked by the profile")
    for key in (*profile.decisions, *profile.legacy_keys):
        if key not in REGISTRY or Level.TENANT not in REGISTRY[key].allowed_levels:
            raise bad(f"unknown key {key}")


for _profile_def in PROFILES.values():
    _check_profile(_profile_def)
for _key, _value in _materialize(PROFILES["STARTER_V1"], {}).items():
    object.__setattr__(REGISTRY[_key], "new_tenant_default", _value)
for _key in PROFILES["STARTER_V1"].decisions:
    object.__setattr__(REGISTRY[_key], "decision_required", True)
del _profile_def, _key, _value
