"""Country-pack framework: protocol, version metadata, capability levels, rule data, sources (3.2 §6).

A pack version is code plus effective-dated rule data. Rates, ceilings and slabs are data rows
(``EffectiveTable``, ``BandTable``) that never change once a version is reviewed; algorithms are
methods of the pack class. Every rule names its sources in the ``SourceManifest`` and its golden
cases in ``TestVector``s. Packs are deterministic: no clock, no network, no database; they see
only the lines calculated so far and a ``PackContext`` built from the frozen snapshot.

Statutory honesty (decision I-07): only a ``VERIFIED_FOR_PERIOD`` version produces final lines;
a fixture version (``is_fixture``) can never be verified and runs only outside production; a
test-only pack (``ZZ``) runs only in the test environment. The gate lives in the registry and
in ``engine.statutory``; packs just calculate.
"""

from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from app.payroll.engine.lines import (
    STAGE_POST_TAX,
    STAGE_PRE_TAX,
    STAGE_STATUTORY,
    STAGE_TAX,
    CalcIssue,
    Line,
)
from app.payroll.money import CALC_CONTEXT, STATUTORY_ROUNDING, decimal_str, quantize_amount, quantum
from app.payroll.packs.tax_profile import TaxProfileField
from app.payroll.settings_registry import PAYROLL_MODULE, S_STATUTORY, UNSET, Level, LegalConstraint, SettingDef
from app.payroll.state_machines import PackVersionStatus

ZERO = Decimal(0)
HUNDRED = Decimal(100)

PORTIONS = ("EMPLOYEE", "EMPLOYER")
MINOR_UNIT = "MINOR_UNIT"  # statutory rounding to the currency minor unit, half up
ROUNDING_RULES = (MINOR_UNIT, *STATUTORY_ROUNDING)
FIXTURE_LABEL = "FIXTURE — not law"
NOT_VERIFIED_LABEL = "Compliance not verified"


class PackError(ValueError):
    """A pack definition or rule-data error (a programming error, never a run-time data problem)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- capability levels (3.2 §6.2)


class Capability(str, Enum):
    UNSUPPORTED = "UNSUPPORTED"
    DISPLAY_ONLY = "DISPLAY_ONLY"
    CONFIG_ONLY = "CONFIG_ONLY"
    CALC_SUPPORTED = "CALC_SUPPORTED"
    VERIFIED = "VERIFIED"
    FILING_INTEGRATED = "FILING_INTEGRATED"

    @property
    def rank(self) -> int:
        return _CAPABILITY_ORDER.index(self)

    @property
    def label(self) -> str:
        return CAPABILITY_LABELS[self]


_CAPABILITY_ORDER: Tuple[Capability, ...] = tuple(Capability)
CAPABILITY_LABELS: Mapping[Capability, str] = {
    Capability.UNSUPPORTED: "Country not offered",
    Capability.DISPLAY_ONLY: "Names, currency and formatting only",
    Capability.CONFIG_ONLY: f"Non-statutory payroll only — {NOT_VERIFIED_LABEL}",
    Capability.CALC_SUPPORTED: "Statutory calculations implemented, not verified for the period",
    Capability.VERIFIED: "Verified for the period",
    Capability.FILING_INTEGRATED: "Verified, with tested filing integration",
}
VERIFIED_STATUSES = (PackVersionStatus.VERIFIED_FOR_PERIOD, PackVersionStatus.EXPIRED)


# --------------------------------------------------------------------------- version metadata (3.2 §6.3)


@dataclass(frozen=True)
class PackVersion:
    """One pack version as shipped in code. The live status is kept in ``country_pack_versions``.

    ``capability`` is what the code implements; the effective level also depends on the status
    and the environment (``packs.capability``). An ``EXPIRED`` version stays valid for the periods
    it covers (replays and retro runs, EC-STA-04).
    """

    code: str                      # pack code, the country for national packs: "IN", "ZZ"
    version: str                   # "IN-2026.09.17-FIXTURE"
    country: str                   # ISO 3166 alpha-2 ("ZZ" is user-assigned: test only)
    name: str
    currency: str                  # currency of every amount in the rule data
    effective_from: date
    effective_to: Optional[date]
    status: PackVersionStatus
    capability: Capability
    tax_year_start: str = "01-01"  # "MM-DD"
    is_fixture: bool = False
    test_only: bool = False
    reviewer: Optional[str] = None
    reviewed_on: Optional[date] = None
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PackVersionStatus(self.status))
        object.__setattr__(self, "capability", Capability(self.capability))
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise PackError("INVALID_VERSION", f"{self.version}: effective_to is before effective_from")
        if self.is_fixture and self.status in VERIFIED_STATUSES:
            raise PackError("FIXTURE_NOT_VERIFIABLE", f"{self.version}: a fixture version is never verified (I-07)")
        if self.status in VERIFIED_STATUSES and not (self.reviewer and self.reviewed_on):
            raise PackError("REVIEW_MISSING", f"{self.version}: a verified version needs its reviewer and review date")
        month, _, day = self.tax_year_start.partition("-")
        if not (month.isdigit() and day.isdigit() and 1 <= int(month) <= 12 and 1 <= int(day) <= 28):
            raise PackError("INVALID_VERSION", f"{self.version}: tax_year_start must be MM-DD with a day 1-28")

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)

    def covers_period(self, start: date, end: date) -> bool:
        return self.covers(start) and self.covers(end)

    def to_json(self) -> Dict[str, Any]:
        return {
            "code": self.code, "version": self.version, "country": self.country, "name": self.name,
            "currency": self.currency, "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "status": self.status.value, "capability": self.capability.value, "tax_year_start": self.tax_year_start,
            "is_fixture": self.is_fixture, "test_only": self.test_only, "reviewer": self.reviewer,
            "reviewed_on": self.reviewed_on.isoformat() if self.reviewed_on else None, "notes": self.notes,
        }


# --------------------------------------------------------------------------- source manifest (3.2 §6.4)

SOURCE_KINDS = ("PRIMARY", "SECONDARY", "FICTIONAL")
_MANIFEST_REQUIRED = ("title", "url", "publisher", "retrieved", "effective_from", "test_vectors", "next_review_due")
_MANIFEST_REVIEW = ("reviewer", "reviewed_on", "approval")


@dataclass(frozen=True)
class SourceEntry:
    """Where one rule comes from. ``confidence`` is the research rating ("HIGH" or "MEDIUM")."""

    rule: str                       # rule id, e.g. "IN.epf.ceiling"
    title: str
    url: Optional[str]
    publisher: str
    kind: str
    retrieved: date
    effective_from: Optional[date]
    test_vectors: Tuple[str, ...] = ()
    next_review_due: Optional[date] = None
    published: Optional[date] = None
    effective_to: Optional[date] = None
    reviewer: Optional[str] = None
    reviewed_on: Optional[date] = None
    approval: Optional[str] = None
    confidence: str = "HIGH"
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise PackError("INVALID_SOURCE", f"{self.rule}: kind must be one of {SOURCE_KINDS}")
        object.__setattr__(self, "test_vectors", tuple(self.test_vectors))

    def missing(self, *, for_verification: bool = False) -> List[str]:
        names = _MANIFEST_REQUIRED + (_MANIFEST_REVIEW if for_verification else ())
        return [name for name in names if getattr(self, name) in (None, "", ())]

    def to_json(self) -> Dict[str, Any]:
        def iso(value: Optional[date]) -> Optional[str]:
            return value.isoformat() if value else None

        return {
            "rule": self.rule, "title": self.title, "url": self.url, "publisher": self.publisher, "kind": self.kind,
            "retrieved": iso(self.retrieved), "effective_from": iso(self.effective_from),
            "effective_to": iso(self.effective_to), "published": iso(self.published),
            "test_vectors": list(self.test_vectors), "next_review_due": iso(self.next_review_due),
            "reviewer": self.reviewer, "reviewed_on": iso(self.reviewed_on), "approval": self.approval,
            "confidence": self.confidence, "note": self.note,
        }


@dataclass(frozen=True)
class SourceManifest:
    entries: Tuple[SourceEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))

    def for_rule(self, rule: str) -> Tuple[SourceEntry, ...]:
        return tuple(e for e in self.entries if e.rule == rule)

    def rules(self) -> Tuple[str, ...]:
        return tuple(sorted({e.rule for e in self.entries}))

    def problems(self, rules: Iterable[str] = (), vector_ids: Iterable[str] = (), *,
                 for_verification: bool = False) -> List[str]:
        """What keeps the manifest from being complete (guard SOURCE_MANIFEST_COMPLETE, 3.5 §6.3).

        Every rule the pack uses needs a source; every source needs the contract §19 fields and
        names test vectors that exist; verification also needs the reviewer fields.
        """
        problems: List[str] = []
        known_vectors = set(vector_ids)
        for rule in sorted(set(rules)):
            if not self.for_rule(rule):
                problems.append(f"{rule}: no source")
        for entry in self.entries:
            problems.extend(f"{entry.rule}: missing {name}" for name in entry.missing(for_verification=for_verification))
            if known_vectors:
                problems.extend(f"{entry.rule}: unknown test vector {v}" for v in entry.test_vectors
                                if v not in known_vectors)
        return problems

    def to_json(self) -> List[Dict[str, Any]]:
        return [e.to_json() for e in self.entries]


# --------------------------------------------------------------------------- rule data (3.2 §6.6)


@dataclass(frozen=True)
class RuleRow:
    """One effective-dated row of rule data; ``values`` are Decimals, ``BandTable``s or strings."""

    effective_from: date
    effective_to: Optional[date]
    values: Mapping[str, Any]
    source: str = ""                # rule id in the manifest

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


@dataclass(frozen=True)
class Segment:
    """A part of the period under one rule row, with its share of the period's calendar days."""

    start: date
    end: date
    row: RuleRow
    days: int
    period_days: int

    @property
    def fraction(self) -> Decimal:
        with localcontext(CALC_CONTEXT):
            return Decimal(self.days) / Decimal(self.period_days)


SPLIT_RULES = ("BY_DAYS", "PERIOD_END", "PERIOD_START")


@dataclass(frozen=True)
class EffectiveTable:
    """Effective-dated rows of one rule; rows never overlap and are never edited (a change is a new row)."""

    name: str
    rows: Tuple[RuleRow, ...]

    def __post_init__(self) -> None:
        rows = tuple(sorted(self.rows, key=lambda r: r.effective_from))
        for earlier, later in zip(rows, rows[1:]):
            if earlier.effective_to is None or earlier.effective_to >= later.effective_from:
                raise PackError("OVERLAPPING_ROWS", f"{self.name}: rows from {earlier.effective_from} and "
                                f"{later.effective_from} overlap")
        object.__setattr__(self, "rows", rows)

    def on(self, day: date) -> Optional[RuleRow]:
        return next((row for row in self.rows if row.covers(day)), None)

    def segments(self, start: date, end: date, rule: str = "BY_DAYS") -> Tuple[Segment, ...]:
        """The period split by rule changes (EC-STA-05). ``PERIOD_END``/``PERIOD_START`` apply one row
        to the whole period; ``BY_DAYS`` gives each row its calendar days. A gap raises (fail closed)."""
        if rule not in SPLIT_RULES:
            raise PackError("INVALID_SPLIT_RULE", f"{self.name}: split rule must be one of {SPLIT_RULES}")
        total = (end - start).days + 1
        if rule != "BY_DAYS":
            row = self.on(end if rule == "PERIOD_END" else start)
            if row is None:
                raise PackError("NO_RULE_ROW", f"{self.name}: no row on {end if rule == 'PERIOD_END' else start}")
            return (Segment(start, end, row, total, total),)
        result: List[Segment] = []
        day = start
        while day <= end:
            row = self.on(day)
            if row is None:
                raise PackError("NO_RULE_ROW", f"{self.name}: no row on {day}")
            seg_end = end if row.effective_to is None else min(end, row.effective_to)
            result.append(Segment(day, seg_end, row, (seg_end - day).days + 1, total))
            day = seg_end + timedelta(days=1)
        return tuple(result)

    def to_json(self) -> List[Dict[str, Any]]:
        return [{"effective_from": r.effective_from.isoformat(),
                 "effective_to": r.effective_to.isoformat() if r.effective_to else None,
                 "values": {k: _data_json(v) for k, v in sorted(r.values.items())}, "source": r.source}
                for r in self.rows]


BAND_METHODS = ("PROGRESSIVE", "FLAT_RATE", "FIXED_AMOUNT")


@dataclass(frozen=True)
class Band:
    """A band up to ``upper`` (None = no upper limit). ``inclusive`` decides whether ``upper`` itself
    belongs to this band. ``rate`` is a percentage; ``amount`` a fixed amount (FIXED_AMOUNT tables)."""

    upper: Optional[Decimal]
    rate: Decimal = ZERO
    amount: Decimal = ZERO
    inclusive: bool = True
    label: str = ""


@dataclass(frozen=True)
class BandResult:
    value: Decimal                  # unrounded
    band: int                       # index of the band the base falls in
    steps: Tuple[str, ...]


@dataclass(frozen=True)
class BandTable:
    """A slab table: PROGRESSIVE (each slice at its rate), FLAT_RATE (the whole base at the rate of
    its band) or FIXED_AMOUNT (the amount of the band the base falls in)."""

    name: str
    method: str
    bands: Tuple[Band, ...]

    def __post_init__(self) -> None:
        if self.method not in BAND_METHODS:
            raise PackError("INVALID_TABLE", f"{self.name}: method must be one of {BAND_METHODS}")
        bands = tuple(self.bands)
        if not bands or bands[-1].upper is not None:
            raise PackError("INVALID_TABLE", f"{self.name}: the last band has no upper limit")
        uppers = [b.upper for b in bands[:-1]]
        if any(u is None for u in uppers) or uppers != sorted(uppers) or len(set(uppers)) != len(uppers):
            raise PackError("INVALID_TABLE", f"{self.name}: band limits must increase")
        object.__setattr__(self, "bands", bands)

    def band_of(self, base: Decimal) -> int:
        for index, band in enumerate(self.bands):
            if band.upper is None or base < band.upper or (band.inclusive and base == band.upper):
                return index
        raise AssertionError("unreachable: the last band is open")

    def apply(self, base: Decimal) -> BandResult:
        with localcontext(CALC_CONTEXT):
            index = self.band_of(base)
            band = self.bands[index]
            if self.method == "FIXED_AMOUNT":
                return BandResult(band.amount, index, (f"band {_band_text(self.bands, index)}: {fmt(band.amount)}",))
            if self.method == "FLAT_RATE":
                value = base * band.rate / HUNDRED
                return BandResult(value, index, (f"{pct(band.rate)} × {fmt(base)} (band {_band_text(self.bands, index)})",))
            total, lower, steps = ZERO, ZERO, []
            for band in self.bands:
                if base <= lower:
                    break
                top = base if band.upper is None else min(base, band.upper)
                part = (top - lower) * band.rate / HUNDRED
                if band.rate:
                    steps.append(f"{pct(band.rate)} × ({fmt(top)} − {fmt(lower)}) = {fmt(part)}")
                total += part
                if band.upper is None:
                    break
                lower = band.upper
            return BandResult(total, index, tuple(steps))

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "method": self.method,
                "bands": [{"upper": None if b.upper is None else decimal_str(b.upper), "rate": decimal_str(b.rate),
                           "amount": decimal_str(b.amount), "inclusive": b.inclusive, "label": b.label}
                          for b in self.bands]}


def _band_text(bands: Sequence[Band], index: int) -> str:
    band = bands[index]
    lower = bands[index - 1].upper if index else None
    if band.upper is None:
        return f"above {fmt(lower)}" if lower is not None else "all amounts"
    return f"{'up to' if band.inclusive else 'below'} {fmt(band.upper)}" + (f" (from {fmt(lower)})" if lower else "")


def _data_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, (BandTable, EffectiveTable)):
        return value.to_json()
    if isinstance(value, Mapping):
        return {str(k): _data_json(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_data_json(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def canonical_hash(data: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace, Decimals and dates as strings)."""
    text = json.dumps(_data_json(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- components, schemes, liabilities

DUE_RULES = ("DAY_OF_NEXT_MONTH", "LAST_DAY_OF_NEXT_MONTH", "DAYS_AFTER_PERIOD_END", "NEXT_QUARTER_MONTH_DAY")


@dataclass(frozen=True)
class DueRule:
    """When a liability is due, from the period end (3.2 §8.3)."""

    kind: str
    day: int = 0

    def __post_init__(self) -> None:
        if self.kind not in DUE_RULES:
            raise PackError("INVALID_DUE_RULE", f"due rule must be one of {DUE_RULES}")

    def due_date(self, period_end: date) -> date:
        if self.kind == "DAYS_AFTER_PERIOD_END":
            return period_end + timedelta(days=self.day)
        year, month = (period_end.year + 1, 1) if period_end.month == 12 else (period_end.year, period_end.month + 1)
        if self.kind == "NEXT_QUARTER_MONTH_DAY":
            month_index = (period_end.month - 1) // 3 * 3 + 3  # last month of the quarter (1-based)
            year, month = (period_end.year + 1, 1) if month_index == 12 else (period_end.year, month_index + 1)
        last = monthrange(year, month)[1]
        if self.kind == "LAST_DAY_OF_NEXT_MONTH":
            return date(year, month, last)
        return date(year, month, min(self.day, last))

    def describe(self) -> str:
        return {
            "DAY_OF_NEXT_MONTH": f"day {self.day} of the next month",
            "LAST_DAY_OF_NEXT_MONTH": "last day of the next month",
            "DAYS_AFTER_PERIOD_END": f"{self.day} days after the period end",
            "NEXT_QUARTER_MONTH_DAY": f"day {self.day} of the month after the quarter",
        }[self.kind]


@dataclass(frozen=True)
class ComponentDef:
    """A pack-owned statutory component (read-only for tenants, EC-STA-18)."""

    code: str
    name: str
    kind: str                       # DEDUCTION | EMPLOYER_CONTRIBUTION | INFORMATIONAL
    category: str                   # EMPLOYEE_CONTRIBUTION | TAX | EMPLOYER_CONTRIBUTION | ...
    stage: int
    scheme: str                     # "IN.epf"
    rounding: str = MINOR_UNIT
    portion: Optional[str] = None   # EMPLOYEE | EMPLOYER (EC-STA-10)
    payee: Optional[str] = None     # the authority or fund the liability is owed to
    due: Optional[DueRule] = None
    tags: Tuple[str, ...] = ()
    visibility: str = "EMPLOYEE_VISIBLE"
    sort_order: int = 0
    reduces_taxable: bool = False   # a pre-tax item: the pack's taxable income excludes it

    def __post_init__(self) -> None:
        if self.rounding not in ROUNDING_RULES:
            raise PackError("INVALID_COMPONENT", f"{self.code}: rounding must be one of {ROUNDING_RULES}")
        if self.stage not in (STAGE_PRE_TAX, STAGE_STATUTORY, STAGE_TAX, STAGE_POST_TAX):
            raise PackError("INVALID_COMPONENT", f"{self.code}: packs produce lines in stages 5-8")
        if self.portion is not None and self.portion not in PORTIONS:
            raise PackError("INVALID_COMPONENT", f"{self.code}: portion must be one of {PORTIONS}")
        object.__setattr__(self, "tags", tuple(self.tags))

    def to_json(self) -> Dict[str, Any]:
        return {"code": self.code, "name": self.name, "kind": self.kind, "category": self.category,
                "stage": self.stage, "scheme": self.scheme, "rounding": self.rounding, "portion": self.portion,
                "payee": self.payee, "due": self.due.describe() if self.due else None, "tags": list(self.tags),
                "visibility": self.visibility, "owner": "PACK"}


@dataclass(frozen=True)
class Scheme:
    """A statutory scheme with its employer switch ``payroll.statutory.<CC>.<scheme>.enabled``."""

    code: str                       # "IN.epf"
    name: str
    mandatory: bool
    legal_source: str
    components: Tuple[str, ...] = ()
    note: str = ""

    @property
    def switch_key(self) -> str:
        country, _, scheme = self.code.partition(".")
        return f"payroll.statutory.{country}.{scheme}.enabled"


@dataclass(frozen=True)
class Liability:
    """What a statutory line owes, to whom and when (3.2 §8.1). Employee and employer portions are
    always separate liabilities (EC-STA-10)."""

    component: str
    scheme: str
    portion: Optional[str]
    payee: str
    amount: Decimal
    currency: str
    due_date: Optional[date]
    period_end: date

    def to_json(self) -> Dict[str, Any]:
        from app.payroll.money import amount_str

        return {"component": self.component, "scheme": self.scheme, "portion": self.portion, "payee": self.payee,
                "amount": amount_str(self.amount, self.currency), "currency": self.currency,
                "due_date": self.due_date.isoformat() if self.due_date else None, "period_end": self.period_end.isoformat(),
                "remittance_status": "PENDING"}


@dataclass(frozen=True)
class ReportDef:
    code: str
    name: str
    frequency: str                  # MONTHLY | QUARTERLY | ANNUAL
    due: Optional[DueRule] = None
    note: str = ""


@dataclass(frozen=True)
class PaymentRules:
    pay_deadline: Optional[str] = None          # plain rule text with its source
    mandated_system: Optional[str] = None       # for example a wage protection system
    bank_file_formats: Tuple[str, ...] = ()
    final_settlement_deadline: Optional[str] = None


# --------------------------------------------------------------------------- what a pack sees


def _as_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PackError("INVALID_INPUT", f"{name} must be a decimal")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        try:
            return Decimal(value)
        except ArithmeticError:
            raise PackError("INVALID_INPUT", f"{name} must be a decimal") from None
    raise PackError("INVALID_INPUT", f"{name} must be a decimal string, never {type(value).__name__}")


@dataclass
class PackContext:
    """The frozen facts a pack calculates from, built by ``engine.statutory`` from the run context.

    ``employee`` holds the facts packs may read (``branch_subdivision`` on the period end,
    ``gender``, ``disability``, ``nationality``, ``date_of_birth``, ``date_of_joining``,
    ``worker_class``); some are protected attributes no company formula may use (EC-CMP-19).
    ``ytd`` holds per legal entity and tax year amounts from finalized runs, keyed by component code
    or wage-base name (3.2 §7.4). ``pack_state`` carries determinations from earlier runs, such as
    contribution-period eligibility (EC-STA-17); new determinations that later runs must read go to
    ``state_out``; values shared between the stages of one calculation go to ``scratch``.
    """

    version: PackVersion
    currency: str
    period_start: date
    period_end: date
    settings: Mapping[str, Any] = field(default_factory=dict)
    employee: Mapping[str, Any] = field(default_factory=dict)
    tax_profile: Optional[Mapping[str, Any]] = None
    ytd: Mapping[str, Any] = field(default_factory=dict)
    pack_state: Mapping[str, Any] = field(default_factory=dict)
    pay_date: Optional[date] = None
    frequency: str = "MONTHLY"
    tax_periods_remaining: Optional[int] = None   # periods left in the tax year, this one included
    paid_days: Optional[Decimal] = None
    period_days: Optional[Decimal] = None
    final_settlement: bool = False
    issues: List[CalcIssue] = field(default_factory=list)
    state_out: Dict[str, Any] = field(default_factory=dict)
    scratch: Dict[str, Any] = field(default_factory=dict)     # values shared between stages, never persisted
    choice_defaults: Mapping[str, Any] = field(default_factory=dict)

    @property
    def calendar_days(self) -> int:
        return (self.period_end - self.period_start).days + 1

    def setting(self, key: str, default: Any = UNSET) -> Any:
        if key in self.settings:
            return self.settings[key]
        if key in self.choice_defaults:
            return self.choice_defaults[key]
        return default

    def scheme_on(self, scheme: Scheme) -> bool:
        return self.setting(scheme.switch_key, False) is True

    def ytd_amount(self, key: str) -> Decimal:
        value = self.ytd.get(key)
        return ZERO if value is None else _as_decimal(value, f"ytd[{key}]")

    def profile(self, key: str, default: Any = None) -> Any:
        return default if self.tax_profile is None else self.tax_profile.get(key, default)

    def issue(self, code: str, severity: str, message: str, *, component: Optional[str] = None,
              waivable: bool = False, **details: Any) -> None:
        self.issues.append(CalcIssue(code, severity, message, dict(details), component, waivable))

    def tax_year_bounds(self) -> Tuple[date, date]:
        month, day = (int(p) for p in self.version.tax_year_start.split("-"))
        start = date(self.period_end.year, month, day)
        if start > self.period_end:
            start = date(self.period_end.year - 1, month, day)
        return start, date(start.year + 1, month, day) - timedelta(days=1)

    def periods_remaining(self) -> Optional[int]:
        """Periods left in the tax year including this one: given by the snapshot, or derived for
        monthly payroll; ``None`` when unknown (the pack then raises an issue, never guesses)."""
        if self.tax_periods_remaining is not None:
            return self.tax_periods_remaining
        if self.frequency != "MONTHLY":
            return None
        _, year_end = self.tax_year_bounds()
        return (year_end.year - self.period_end.year) * 12 + year_end.month - self.period_end.month + 1


# --------------------------------------------------------------------------- the protocol (3.2 §6.5)


@runtime_checkable
class CountryPack(Protocol):
    """The contract interface (3.2 §6.5), adapted: amounts are Decimals in ``ctx.currency``, issues are
    reported through ``ctx.issue``, and stage 5 (pre-tax items) and the taxable income are explicit."""

    code: str
    version: str
    meta: PackVersion
    manifest: SourceManifest

    def validate_context(self, ctx: PackContext) -> List[CalcIssue]: ...
    def employer_choices(self) -> List[SettingDef]: ...
    def setting_constraints(self) -> List[LegalConstraint]: ...
    def schemes(self) -> List[Scheme]: ...
    def components(self) -> List[ComponentDef]: ...
    def pre_tax_deductions(self, lines: Sequence[Line], ctx: PackContext) -> List[Line]: ...
    def statutory_bases(self, lines: Sequence[Line], ctx: PackContext) -> Dict[str, Decimal]: ...
    def employee_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]: ...
    def employer_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]: ...
    def taxable_income(self, lines: Sequence[Line], ctx: PackContext) -> Decimal: ...
    def withholding(self, taxable: Decimal, ytd: Mapping[str, Any], profile: Optional[Mapping[str, Any]],
                    ctx: PackContext) -> List[Line]: ...
    def minimum_pay_checks(self, ledger: Any, lines: Sequence[Line], ctx: PackContext) -> List[CalcIssue]: ...
    def termination_entitlements(self, separation: Mapping[str, Any], ctx: PackContext) -> List[Line]: ...
    def protected_net_floor_percent(self, ctx: Optional[PackContext] = None) -> Optional[Decimal]: ...
    def ytd_keys(self) -> Tuple[str, ...]: ...
    def reports(self) -> List[ReportDef]: ...
    def tax_profile_schema(self) -> Tuple[TaxProfileField, ...]: ...
    def payment_rules(self) -> PaymentRules: ...
    def rule_data(self) -> Dict[str, Any]: ...
    def test_vectors(self) -> Tuple["TestVector", ...]: ...
    def fingerprint(self) -> str: ...


# --------------------------------------------------------------------------- test vectors


@dataclass(frozen=True)
class TestVector:
    """A golden case: inputs for one employee-period and the exact expected statutory lines.

    ``expected`` maps component code to the amount string ("0" means no line, or a zero line);
    ``expected_issues`` lists issue codes that must be raised.
    """

    __test__ = False  # not a pytest class

    id: str
    description: str
    period_start: date
    period_end: date
    lines: Tuple[Line, ...]
    expected: Mapping[str, str]
    settings: Mapping[str, Any] = field(default_factory=dict)
    employee: Mapping[str, Any] = field(default_factory=dict)
    tax_profile: Optional[Mapping[str, Any]] = None
    ytd: Mapping[str, Any] = field(default_factory=dict)
    pack_state: Mapping[str, Any] = field(default_factory=dict)
    expected_issues: Tuple[str, ...] = ()
    rules: Tuple[str, ...] = ()
    ec_ids: Tuple[str, ...] = ()
    source: str = ""


# --------------------------------------------------------------------------- a base class for packs


def fmt(value: Optional[Decimal], places: int = 2) -> str:
    """A figure for explanations: grouped digits, rounded half up for display only."""
    if value is None:
        return "—"
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP, context=CALC_CONTEXT), ",")


def pct(rate: Decimal) -> str:
    return f"{decimal_str(rate)}%"


def tagged(lines: Iterable[Line], tag: str, *, kinds: Tuple[str, ...] = ("EARNING",)) -> Decimal:
    """Σ of lines carrying ``tag`` (earnings by default): statutory wage bases come from tags (3.3 §2.3)."""
    with localcontext(CALC_CONTEXT):
        return sum((line.amount for line in lines if tag in line.tags and line.kind in kinds), ZERO)


def gross_cash(lines: Iterable[Line]) -> Decimal:
    with localcontext(CALC_CONTEXT):
        return sum((line.amount for line in lines if line.kind == "EARNING"), ZERO)


class BasePack:
    """Shared machinery: component lookup, statutory rounding, liabilities in the trace, defaults.

    Subclasses set ``meta``, ``manifest`` and override the stages they implement.
    """

    meta: PackVersion
    manifest: SourceManifest = SourceManifest(())
    floor_percent: Optional[Decimal] = None

    @property
    def code(self) -> str:
        return self.meta.code

    @property
    def version(self) -> str:
        return self.meta.version

    # -- declarations
    def schemes(self) -> List[Scheme]:
        return []

    def components(self) -> List[ComponentDef]:
        return []

    def employer_choices(self) -> List[SettingDef]:
        return []

    def setting_constraints(self) -> List[LegalConstraint]:
        return []

    def reports(self) -> List[ReportDef]:
        return []

    def tax_profile_schema(self) -> Tuple[TaxProfileField, ...]:
        """The declaration fields this pack needs from an employee (3.2 §6.1, §7.2); none by default, which means
        the employee has nothing to declare and the pages say so. Override with ``TaxProfileField``s; the
        framework adds no field and gives none a meaning (the pack reads the values from ``ctx.tax_profile``)."""
        return ()

    def payment_rules(self) -> PaymentRules:
        return PaymentRules()

    def rule_data(self) -> Dict[str, Any]:
        return {}

    def test_vectors(self) -> Tuple[TestVector, ...]:
        return ()

    def ytd_keys(self) -> Tuple[str, ...]:
        return ()

    def protected_net_floor_percent(self, ctx: Optional[PackContext] = None) -> Optional[Decimal]:
        return self.floor_percent

    def fingerprint(self) -> str:
        """Hash of the version, its rule data and its component definitions (stale detection, EC-STA-03)."""
        return canonical_hash({"version": self.meta.to_json(), "rules": self.rule_data(),
                               "components": [c.to_json() for c in self.components()]})

    def choice_defaults(self) -> Dict[str, Any]:
        return {d.key: d.default for d in self.employer_choices()}

    def ytd_increments(self, lines: Sequence[Line], *, taxable: Optional[Decimal] = None) -> Dict[str, Decimal]:
        """What one finalized employee-period adds to each ``ytd_keys`` entry: component codes sum their
        lines, wage bases come from the ``base`` recorded in the trace of the line that names them,
        ``TAXABLE_INCOME`` is the period's taxable income."""
        result: Dict[str, Decimal] = {}
        with localcontext(CALC_CONTEXT):
            for key in self.ytd_keys():
                if key == "TAXABLE_INCOME":
                    result[key] = ZERO if taxable is None else taxable
                    continue
                own = [line for line in lines if line.component == key]
                if own:
                    result[key] = sum((line.amount for line in own), ZERO)
                    continue
                based = [line for line in lines if line.trace.get("ytd_base_key") == key]
                result[key] = sum((Decimal(line.trace["base"]) for line in based[:1]), ZERO)
        return result

    # -- stages (no-ops by default)
    def validate_context(self, ctx: PackContext) -> List[CalcIssue]:
        return []

    def pre_tax_deductions(self, lines: Sequence[Line], ctx: PackContext) -> List[Line]:
        return []

    def statutory_bases(self, lines: Sequence[Line], ctx: PackContext) -> Dict[str, Decimal]:
        return {}

    def employee_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        return []

    def employer_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        return []

    def taxable_income(self, lines: Sequence[Line], ctx: PackContext) -> Decimal:
        """Σ ``TAXABLE_INCOME`` earnings and benefits in kind, less pre-tax items that reduce it."""
        with localcontext(CALC_CONTEXT):
            income = tagged(lines, "TAXABLE_INCOME", kinds=("EARNING", "BENEFIT_IN_KIND"))
            reducing = {c.code for c in self.components() if c.reduces_taxable}
            relief = sum((line.amount for line in lines if line.component in reducing and line.statutory), ZERO)
            return income - relief

    def withholding(self, taxable: Decimal, ytd: Mapping[str, Any], profile: Optional[Mapping[str, Any]],
                    ctx: PackContext) -> List[Line]:
        return []

    def minimum_pay_checks(self, ledger: Any, lines: Sequence[Line], ctx: PackContext) -> List[CalcIssue]:
        return []

    def termination_entitlements(self, separation: Mapping[str, Any], ctx: PackContext) -> List[Line]:
        return []

    # -- helpers for subclasses
    def component(self, code: str) -> ComponentDef:
        for definition in self.components():
            if definition.code == code:
                return definition
        raise PackError("UNKNOWN_COMPONENT", f"{self.version}: no component {code}")

    def scheme(self, code: str) -> Scheme:
        for scheme in self.schemes():
            if scheme.code == code:
                return scheme
        raise PackError("UNKNOWN_SCHEME", f"{self.version}: no scheme {code}")

    def line(self, code: str, unrounded: Decimal, ctx: PackContext, explanation: str, *,
             base: Optional[Decimal] = None, rate: Optional[Decimal] = None, rule: str = "",
             details: Optional[Mapping[str, Any]] = None, sources: Sequence[Mapping[str, Any]] = (),
             ytd_base_key: Optional[str] = None) -> Line:
        """A statutory line rounded by the component's pack rule, never by company settings (EC-STA-13),
        with its liability (payee and due date) in the trace. A fixture pack's lines are always
        ``fixture=True`` (I-07), whatever path calls the pack."""
        definition = self.component(code)
        amount, rounding = round_statutory_amount(unrounded, ctx.currency, definition.rounding)
        trace: Dict[str, Any] = {
            "method": "PACK", "pack": self.code, "pack_version": self.version, "rule": rule,
            "scheme": definition.scheme, "portion": definition.portion,
            "unrounded": decimal_str(unrounded), "rounding": rounding,
        }
        if base is not None:
            trace["base"] = decimal_str(base)
        if ytd_base_key:
            trace["ytd_base_key"] = ytd_base_key
        if rate is not None:
            trace["rate_percent"] = decimal_str(rate)
        if details:
            trace["details"] = {k: _data_json(v) for k, v in details.items()}
        if definition.payee:
            due = definition.due
            trace["liability"] = {"payee": definition.payee,
                                  "due_date": due.due_date(ctx.period_end).isoformat() if due else None,
                                  "due_rule": due.describe() if due else "not in the pack data"}
        refs = [dict(s) for s in sources]
        refs.extend({"type": "pack_rule", "rule": entry.rule, "title": entry.title, "url": entry.url}
                    for entry in self.manifest.for_rule(rule)[:1])
        return Line(
            component=definition.code, kind=definition.kind, category=definition.category, stage=definition.stage,
            amount=amount, currency=ctx.currency, units=None, rate=rate,
            tags=definition.tags + (("FIXTURE",) if self.meta.is_fixture else ()),
            visibility=definition.visibility, sources=tuple(refs), trace=trace, explanation=explanation,
            sort_order=definition.sort_order, statutory=True, fixture=self.meta.is_fixture,
        )


def round_statutory_amount(value: Decimal, currency: str, rule: str) -> Tuple[Decimal, Dict[str, str]]:
    """``value`` rounded by a pack rule, with the rounding record for the trace."""
    if rule == MINOR_UNIT:
        mode, increment = "HALF_UP", None
    elif rule in STATUTORY_ROUNDING:
        mode, increment = STATUTORY_ROUNDING[rule]
    else:
        raise PackError("UNKNOWN_ROUNDING_RULE", f"{rule!r} is not one of {ROUNDING_RULES}")
    amount = quantize_amount(value, currency, mode, increment)
    step = quantum(currency) if increment is None else increment
    return amount, {"stage": "STATUTORY", "rule": rule, "mode": mode, "increment": decimal_str(step)}


def with_labels(line: Line, *, fixture: bool, verified: bool, status: str) -> Line:
    """Mark a pack line: statutory, fixture badge (I-07) or "not verified" preview label."""
    tags = list(line.tags)
    explanation = line.explanation
    if fixture:
        tags.append("FIXTURE")
        explanation = f"{FIXTURE_LABEL}: {explanation}"
    elif not verified:
        tags.append("NOT_VERIFIED")
        explanation = f"{NOT_VERIFIED_LABEL} (pack {status}): {explanation}"
    trace = {**line.trace, "pack_status": status, "verified": verified, "fixture": fixture}
    return replace(line, tags=tuple(dict.fromkeys(tags)), explanation=explanation, trace=trace,
                   statutory=True, fixture=fixture)


# --------------------------------------------------------------------------- employer choices


def choice_switch(scheme: Scheme, *, on: str, off: str, levels: Tuple[Level, ...] = (Level.LEGAL_ENTITY,)) -> SettingDef:
    """The scheme's ``enabled`` switch. Statutory schemes default OFF: no profile or default ever turns
    one on (I-13); a pack constraint locks a mandatory scheme ON where the pack is verified."""
    return SettingDef(
        key=scheme.switch_key, type="bool", default=False, allowed_levels=levels, module=PAYROLL_MODULE,
        section=S_STATUTORY, label=scheme.name, help=f"ON: {on} OFF: {off}", statutory_scheme=scheme.code,
        legal_sensitivity="STATUTORY", ui={"control": "switch", "switch_card": True},
    )


def choice_enum(key: str, scheme: Scheme, choices: Tuple[str, ...], default: str, label: str, help_: str, *,
                levels: Tuple[Level, ...] = (Level.LEGAL_ENTITY,)) -> SettingDef:
    return SettingDef(
        key=key, type="enum", default=default, allowed_levels=levels, module=PAYROLL_MODULE, section=S_STATUTORY,
        label=label, help=help_, constraints={"choices": choices}, statutory_scheme=scheme.code,
        legal_sensitivity="STATUTORY", ui={"control": "select"}, depends_on=(scheme.switch_key,),
    )


def choice_flag(key: str, scheme: Scheme, label: str, *, on: str, off: str, default: bool = False,
                levels: Tuple[Level, ...] = (Level.LEGAL_ENTITY,)) -> SettingDef:
    return SettingDef(
        key=key, type="bool", default=default, allowed_levels=levels, module=PAYROLL_MODULE, section=S_STATUTORY,
        label=label, help=f"ON: {on} OFF: {off}", statutory_scheme=scheme.code, legal_sensitivity="STATUTORY",
        ui={"control": "switch"}, depends_on=(scheme.switch_key,),
    )


def liabilities(lines: Iterable[Line], period_end: date) -> List[Liability]:
    """The liabilities of statutory lines, one per line: employee and employer portions stay apart
    (3.2 §8.1, EC-STA-10). Lines whose pack data has no due date get ``due_date`` None."""
    result: List[Liability] = []
    for line in lines:
        info = line.trace.get("liability") if line.statutory else None
        if not info or not line.amount:
            continue
        due = info.get("due_date")
        result.append(Liability(line.component, line.trace.get("scheme", ""), line.trace.get("portion"),
                                info["payee"], line.amount, line.currency,
                                date.fromisoformat(due) if due else None, period_end))
    return result


def earning(code: str, amount: str, currency: str, tags: Iterable[str] = (), *, category: str = "BASE",
            kind: str = "EARNING") -> Line:
    """A stage-4 line for test vectors and previews."""
    from app.payroll.engine.lines import STAGE_EARNINGS

    return Line(component=code, kind=kind, category=category, stage=STAGE_EARNINGS, amount=Decimal(amount),
                currency=currency, tags=tuple(tags), explanation=f"{code} (test vector input)")
