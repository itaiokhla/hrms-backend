"""The frozen calculation input of one run employee (3.5 §8.1) and its canonical hash.

The database snapshot builder fills a ``Snapshot`` (or its JSON form) when a run
locks its inputs; the engine then calculates from it alone (no database, clock or
network, 3.5 §8.2). ``Snapshot.from_dict`` is strict: unknown keys, floats, naive
datetimes and malformed values raise ``SnapshotError`` with the JSON path, so a
snapshot either means exactly one thing or is refused. ``to_dict`` returns the
normalized JSON form (dates ISO 8601, decimals as strings, sorted collections), so
``from_dict(to_dict(s)) == s`` and ``content_hash`` is stable across processes.

JSON shape (every key optional unless marked *required*; defaults in brackets)::

    schema_version        1 [1]
    run                   run_id, run_employee_id, run_type [REGULAR], environment [production]
                          ("production" never allows fixture packs; anything else is a test/demo stack)
    employee *            employee_id *, code, worker_class [EMPLOYEE], employment_type [PERMANENT], grade,
                          designation, hire_date *, exit_date, country * (ISO 3166-1 alpha-2), subdivision
                          (ISO 3166-2), fte [1], standard_hours_per_day (None: the setting), salaried [true]
                          (hourly/daily workers false: holiday pay per HOLIDAY_PAY_POLICY), attributes
                          ({name: value} custom emp.attr.*), protected ({gender, nationality, date_of_birth, ...},
                          country packs only), average_daily_earnings (AVERAGE_DAILY_EARNINGS holiday pay)
    period *              period_id, start *, end *, pay_date *, frequency [MONTHLY], periods_per_year [12],
                          number_in_tax_year, is_last_in_tax_year [false], tax_year_start
    currency *            pay currency (ISO 4217, payroll-allowed)
    settings              {setting key: resolved JSON value} at PERIOD grain (3.1 §2.2); missing keys take the
                          registry default (settings_registry), never an invented one
    switch_ranges         {DAY-grain switch key: [[start, end | null], ...]} dates on which the switch is ON
                          (3.1 §2.2, EC-OT-01); a DAY-grain switch absent here follows ``settings``
    policies              {policy type: {id, version, policy_hash, fields}} resolved policy versions (3.1 §3)
    components            [{component version}] catalogue of every component the period may produce:
                          code *, version_id, version, name, kind *, category *, calc_method [SYSTEM], tags [],
                          proration [NONE], proration_basis (override of payroll.lop.proration_basis, I-16),
                          frequency [EVERY_PERIOD] + months (SPECIFIC_MONTHS), rounding (line-mode override),
                          cap, floor, eligibility (boolean formula), visibility [EMPLOYEE_VISIBLE],
                          sort_order [0], owner [COMPANY], stage (default from kind)
    segments *            [{key *, start *, end *, assignment_id, compensation}] assignment segments in date order;
                          compensation null = no active salary in the segment (NO_SALARY, EC-CMP-14)
      compensation        id, version, structure_id, structure_version_id, structure_type * (CTC_BASED,
                          GROSS_BASED, NET_BASED, HOURLY, DAILY, TASK_BASED), currency *, pay_basis_amount
                          (annual CTC, monthly gross, target net, hourly or daily rate), ctc_employer_contributions
                          ({code: monthly amount} employer contributions inside CTC that BALANCING subtracts,
                          from the compensation preview), lines * [{component *, method *, amount (FIXED per
                          period), annual_amount (FIXED, ÷ periods_per_year), params (method parameters of
                          formula.method_formula: percent, bases, tag, formula, rate, units, table, base,
                          progressive, others, total), order}]
    days *                [{day ledger input}] every date of the period (F-04), the ledger.DayInput fields:
                          date *, employed, suspended_unpaid, leaves, holiday, weekly_off, manual_lop, attendance,
                          on_duty, segment (segment key), standard_hours, assignment_id, suspension_id, work_week_id
    ledger_overrides      {date: {action, source_id}} reviewer resolutions of days held for review
    overtime_entries      [{entry_id *, date *, minutes *, status [APPROVED], comp_off, start, end, origin}]
    non_working_day_work  [{date *, day_type * (HOLIDAY | WEEKLY_OFF), worked_minutes *, approved, choice, ref}]
    shifts                [{date *, start *, end *, unpaid_breaks [[start, end]], weekend, ref}]
    tasks                 records [{task_id *, task_type *, status *, quantity *, assignees * [{employee_id,
                          split_percent}], accepted_quantity, rate, rate_currency, approved_on, completed_on,
                          evidence_count, external_ref, paid_in_run_id, unit}], rate_card [{task_type, rate,
                          currency, effective_from, effective_to, grade, skill, location}], carried_in,
                          skill, location, minimum_wage {min_hourly_rate, verified, pack_forces_top_up}
    wage_inputs           [{id *, component *, amount, units, currency, source [MANUAL], external_ref, reason,
                          content_hash}] approved inputs targeting the period (EC-VAR-01); source INCENTIVE_PLAN
                          items are achievement measures (units = the measure) of the payout window
    incentive             window_start *, window_end *, target, target_source (EMPLOYEE | GRADE), grade_id: the
                          payout window this run pays and the employee's resolved target (``app.payroll.incentives``,
                          I-22); present only when payroll.incentives.enabled is ON, a plan applies and the period
                          ends a window (omitted from to_dict when absent, so older snapshots keep their hash)
    arrears               [{id *, component * (original), amount *, period_ref *, tags (inherited), reason}]
    reimbursements        [{id *, claim_type *, amount *, currency, taxability}] approved WITH_PAYROLL claims
    loans, advances       [{...}] instalments due, passed to the deductions stage as plain mappings
    court_orders          [{...}] passed to the deductions stage as plain mappings
    previous              period_ref, currency, gross, net, components {code: amount}, history {code: [amounts
                          of the last finalized periods, newest first]}, payment_currency, bank_account_ids
    ytd                   {code: amount} tax-year-to-date before this period (EC-CMP-20)
    pack                  frozen country-pack reference for the statutory stages (``packs.pack_snapshot``: code,
                          version, country, status, is_fixture, fingerprint); null = no pack
    pack_state            determinations of earlier runs the pack reads (contribution periods, EC-STA-17)
    tax_profile           frozen tax profile version (opaque mapping) or null
    attestation           active statutory attestation (decision I-15) or null
    fx                    snapshot_id, rates {currency: pay-currency value of one unit}
    bank_accounts         [{account_id *, type * (PRIMARY, PERCENT, FIXED, REMAINDER), value, order, currency,
                          verified [true], first_payment [false]}] beneficiary data payments will use
    tables                [{code *, kind *, rows *, key_currency, value_currency, upper_inclusive}] formula tables

Money amounts are decimal strings in the pay currency unless a ``currency`` sits beside them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Callable, TypeVar

from app.payroll import ledger as lg
from app.payroll.time_pay import (
    NonWorkingDayWork,
    OvertimeEntry,
    RateCardRow,
    ShiftWorked,
    TaskAssignee,
    TaskRecord,
)

SCHEMA_VERSION = 1
KINDS = ("EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "BENEFIT_IN_KIND", "INFORMATIONAL",
         "ROUNDING")
METHODS = ("FIXED", "PERCENT_OF", "FORMULA", "RATE_X_UNITS", "SLAB", "INPUT", "BALANCING", "SYSTEM")
PRORATIONS = ("NONE", "BY_PAID_DAYS", "BY_PAID_HOURS", "BY_EMPLOYMENT_DAYS")
STRUCTURE_TYPES = ("CTC_BASED", "GROSS_BASED", "NET_BASED", "HOURLY", "DAILY", "TASK_BASED")
FREQUENCIES = ("EVERY_PERIOD", "SPECIFIC_MONTHS", "ONE_TIME", "ON_EXIT")
ALLOCATION_TYPES = ("PRIMARY", "PERCENT", "FIXED", "REMAINDER")
DEFAULT_STAGE = {"EARNING": 4, "REIMBURSEMENT": 4, "BENEFIT_IN_KIND": 4, "INFORMATIONAL": 4, "DEDUCTION": 8,
                 "EMPLOYER_CONTRIBUTION": 6, "ROUNDING": 9}

T = TypeVar("T")


class SnapshotError(ValueError):
    """A snapshot that does not follow the schema; ``path`` points at the offending JSON value."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"{code} at {path or '$'}: {message}")
        self.code = code
        self.path = path
        self.message = message


# --------------------------------------------------------------------------- JSON helpers


def _fail(path: str, message: str, code: str = "INVALID_SNAPSHOT") -> SnapshotError:
    return SnapshotError(code, path, message)


def _mapping(raw: Any, path: str, allowed: Sequence[str] | None = None, required: Sequence[str] = ()) -> Mapping:
    if not isinstance(raw, Mapping):
        raise _fail(path, f"expected an object, got {type(raw).__name__}")
    if allowed is not None:
        unknown = sorted(str(k) for k in raw if k not in allowed)
        if unknown:
            raise _fail(path, f"unknown keys: {', '.join(unknown)}", "UNKNOWN_KEY")
    for key in required:
        if raw.get(key) is None:
            raise _fail(f"{path}.{key}", "is required", "MISSING_KEY")
    return raw


def _list(raw: Any, path: str) -> list:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise _fail(path, f"expected a list, got {type(raw).__name__}")
    return list(raw)


def _str(raw: Any, path: str, *, optional: bool = False) -> str | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str) or not raw:
        raise _fail(path, f"expected a non-empty string, got {raw!r}")
    return raw


def _bool(raw: Any, path: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise _fail(path, f"expected true or false, got {raw!r}")
    return raw


def _int(raw: Any, path: str, *, optional: bool = False, default: int | None = None) -> int | None:
    if raw is None:
        if optional or default is not None:
            return default
        raise _fail(path, "is required", "MISSING_KEY")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _fail(path, f"expected an integer, got {raw!r}")
    return raw


def _ident(raw: Any, path: str, *, optional: bool = True) -> int | str | None:
    if raw is None and optional:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)) or raw == "":
        raise _fail(path, f"expected an integer or string id, got {raw!r}")
    return raw


def _dec(raw: Any, path: str, *, optional: bool = False) -> Decimal | None:
    if raw is None:
        if optional:
            return None
        raise _fail(path, "is required", "MISSING_KEY")
    if isinstance(raw, bool) or isinstance(raw, float) or not isinstance(raw, (str, int, Decimal)):
        raise _fail(path, f"expected a decimal string, got {type(raw).__name__}", "INVALID_NUMBER")
    try:
        value = Decimal(raw.strip()) if isinstance(raw, str) else Decimal(raw)
    except InvalidOperation:
        raise _fail(path, f"not a decimal number: {raw!r}", "INVALID_NUMBER") from None
    if not value.is_finite():
        raise _fail(path, "must be finite", "INVALID_NUMBER")
    return value


def _date(raw: Any, path: str, *, optional: bool = False) -> dt.date | None:
    if raw is None and optional:
        return None
    if isinstance(raw, dt.date) and not isinstance(raw, dt.datetime):
        return raw
    if not isinstance(raw, str):
        raise _fail(path, f"expected an ISO date, got {raw!r}", "INVALID_DATE")
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise _fail(path, f"not an ISO date: {raw!r}", "INVALID_DATE") from None


def _datetime(raw: Any, path: str, *, optional: bool = False) -> dt.datetime | None:
    if raw is None and optional:
        return None
    if isinstance(raw, dt.datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = dt.datetime.fromisoformat(raw)
        except ValueError:
            raise _fail(path, f"not an ISO datetime: {raw!r}", "INVALID_DATE") from None
    else:
        raise _fail(path, f"expected an ISO datetime, got {raw!r}", "INVALID_DATE")
    if value.tzinfo is None or value.utcoffset() is None:
        raise _fail(path, "datetimes need a UTC offset", "INVALID_DATE")
    return value


def _choice(raw: Any, path: str, allowed: Sequence[str], default: str | None = None) -> str:
    value = default if raw is None else raw
    if value not in allowed:
        raise _fail(path, f"must be one of {', '.join(allowed)}, got {raw!r}", "INVALID_CHOICE")
    return value


def _json_value(raw: Any, path: str) -> Any:
    """A plain JSON value (settings, policy fields, opaque pack data); floats are refused (money is never a float)."""
    if raw is None or isinstance(raw, (bool, str, int)):
        return raw
    if isinstance(raw, float):
        raise _fail(path, "floats are not allowed; use a decimal string", "INVALID_NUMBER")
    if isinstance(raw, Decimal):
        return format(raw, "f")
    if isinstance(raw, dt.datetime):
        return raw.isoformat()
    if isinstance(raw, dt.date):
        return raw.isoformat()
    if isinstance(raw, Mapping):
        return MappingProxyType({str(k): _json_value(v, f"{path}.{k}") for k, v in sorted(raw.items(), key=lambda i: str(i[0]))})
    if isinstance(raw, (list, tuple)):
        return tuple(_json_value(v, f"{path}[{i}]") for i, v in enumerate(raw))
    raise _fail(path, f"unsupported value of type {type(raw).__name__}")


def _items(raw: Any, path: str, parse: Callable[[Any, str], T]) -> tuple[T, ...]:
    return tuple(parse(item, f"{path}[{index}]") for index, item in enumerate(_list(raw, path)))


def _guard(path: str, build: Callable[[], T]) -> T:
    """Build a domain object; its own validation errors become SnapshotError at ``path``."""
    try:
        return build()
    except SnapshotError:
        raise
    except (ValueError, TypeError) as exc:
        raise _fail(path, str(exc), getattr(exc, "code", "INVALID_SNAPSHOT")) from None


def dec_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def to_plain(value: Any) -> Any:
    """JSON-ready copy of a value: Decimals and dates as strings, mappings as dicts, tuples as lists."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, Decimals and dates as strings; floats are refused."""

    def default(item: Any) -> Any:
        if isinstance(item, (Decimal, dt.date)):
            return to_plain(item)
        if isinstance(item, Mapping):
            return dict(item)
        if isinstance(item, tuple):
            return list(item)
        raise TypeError(f"{type(item).__name__} is not JSON serializable")

    def check(item: Any) -> Any:
        if isinstance(item, float):
            raise SnapshotError("INVALID_NUMBER", "", "floats cannot be hashed; use Decimal")
        if isinstance(item, Mapping):
            return {str(k): check(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [check(v) for v in item]
        return item

    return json.dumps(check(to_plain(value)), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=default)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- sections


@dataclass(frozen=True)
class RunInfo:
    """Which run this snapshot belongs to; ``environment`` decides whether fixture packs may run (I-07)."""

    run_id: int | str | None = None
    run_employee_id: int | str | None = None
    run_type: str = "REGULAR"
    environment: str = "production"

    @classmethod
    def from_dict(cls, raw: Any, path: str = "run") -> RunInfo:
        data = _mapping(raw or {}, path, ("run_id", "run_employee_id", "run_type", "environment"))
        return cls(_ident(data.get("run_id"), f"{path}.run_id"),
                   _ident(data.get("run_employee_id"), f"{path}.run_employee_id"),
                   _str(data.get("run_type", "REGULAR"), f"{path}.run_type"),
                   _str(data.get("environment", "production"), f"{path}.environment"))

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "run_employee_id": self.run_employee_id, "run_type": self.run_type,
                "environment": self.environment}


@dataclass(frozen=True)
class EmployeeInfo:
    """The employee and assignment facts on the period end date (formula ``emp.*``)."""

    employee_id: int | str
    hire_date: dt.date
    country: str
    code: str | None = None
    worker_class: str = "EMPLOYEE"
    employment_type: str = "PERMANENT"
    grade: str | None = None
    designation: str | None = None
    exit_date: dt.date | None = None
    subdivision: str | None = None
    fte: Decimal = Decimal(1)
    standard_hours_per_day: Decimal | None = None
    salaried: bool = True
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    protected: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    average_daily_earnings: Decimal | None = None

    _KEYS = ("employee_id", "code", "worker_class", "employment_type", "grade", "designation", "hire_date",
             "exit_date", "country", "subdivision", "fte", "standard_hours_per_day", "salaried", "attributes",
             "protected", "average_daily_earnings")

    @classmethod
    def from_dict(cls, raw: Any, path: str = "employee") -> EmployeeInfo:
        data = _mapping(raw, path, cls._KEYS, ("employee_id", "hire_date", "country"))
        attributes = _mapping(data.get("attributes") or {}, f"{path}.attributes")
        protected = _mapping(data.get("protected") or {}, f"{path}.protected")
        return cls(
            employee_id=_ident(data["employee_id"], f"{path}.employee_id", optional=False),
            hire_date=_date(data["hire_date"], f"{path}.hire_date"),
            country=_str(data["country"], f"{path}.country"),
            code=_str(data.get("code"), f"{path}.code", optional=True),
            worker_class=_str(data.get("worker_class", "EMPLOYEE"), f"{path}.worker_class"),
            employment_type=_str(data.get("employment_type", "PERMANENT"), f"{path}.employment_type"),
            grade=_str(data.get("grade"), f"{path}.grade", optional=True),
            designation=_str(data.get("designation"), f"{path}.designation", optional=True),
            exit_date=_date(data.get("exit_date"), f"{path}.exit_date", optional=True),
            subdivision=_str(data.get("subdivision"), f"{path}.subdivision", optional=True),
            fte=_dec(data.get("fte", "1"), f"{path}.fte"),
            standard_hours_per_day=_dec(data.get("standard_hours_per_day"), f"{path}.standard_hours_per_day",
                                        optional=True),
            salaried=_bool(data.get("salaried"), f"{path}.salaried", True),
            attributes=_json_value(attributes, f"{path}.attributes"),
            protected=_json_value(protected, f"{path}.protected"),
            average_daily_earnings=_dec(data.get("average_daily_earnings"), f"{path}.average_daily_earnings",
                                        optional=True),
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Pack-readable facts (``PackContext.employee``): branch_subdivision, date_of_joining, worker_class and
        the protected attributes the law may depend on (gender, disability, nationality, date_of_birth, ...)."""
        facts = {"branch_subdivision": self.subdivision, "date_of_joining": self.hire_date,
                 "date_of_exit": self.exit_date, "worker_class": self.worker_class,
                 "employment_type": self.employment_type, "country": self.country}
        if key in facts:
            value = facts[key]
        else:
            value = self.protected.get(key)
        if key == "date_of_birth" and isinstance(value, str):
            value = dt.date.fromisoformat(value)
        return default if value is None else value

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id, "code": self.code, "worker_class": self.worker_class,
            "employment_type": self.employment_type, "grade": self.grade, "designation": self.designation,
            "hire_date": self.hire_date.isoformat(), "exit_date": self.exit_date.isoformat() if self.exit_date else None,
            "country": self.country, "subdivision": self.subdivision, "fte": dec_str(self.fte),
            "standard_hours_per_day": dec_str(self.standard_hours_per_day), "salaried": self.salaried,
            "attributes": to_plain(self.attributes), "protected": to_plain(self.protected),
            "average_daily_earnings": dec_str(self.average_daily_earnings),
        }


@dataclass(frozen=True)
class PeriodInfo:
    """The pay period (3.5 §2); the engine never reads a clock, these dates are the only dates it knows."""

    start: dt.date
    end: dt.date
    pay_date: dt.date
    period_id: int | str | None = None
    frequency: str = "MONTHLY"
    periods_per_year: int = 12
    number_in_tax_year: int | None = None
    is_last_in_tax_year: bool = False
    tax_year_start: dt.date | None = None

    _KEYS = ("period_id", "start", "end", "pay_date", "frequency", "periods_per_year", "number_in_tax_year",
             "is_last_in_tax_year", "tax_year_start")

    @classmethod
    def from_dict(cls, raw: Any, path: str = "period") -> PeriodInfo:
        data = _mapping(raw, path, cls._KEYS, ("start", "end", "pay_date"))
        start, end = _date(data["start"], f"{path}.start"), _date(data["end"], f"{path}.end")
        if end < start:
            raise _fail(f"{path}.end", "the period ends before it starts")
        per_year = _int(data.get("periods_per_year"), f"{path}.periods_per_year", default=12)
        if per_year < 1:
            raise _fail(f"{path}.periods_per_year", "must be at least 1")
        return cls(start, end, _date(data["pay_date"], f"{path}.pay_date"),
                   _ident(data.get("period_id"), f"{path}.period_id"),
                   _str(data.get("frequency", "MONTHLY"), f"{path}.frequency"), per_year,
                   _int(data.get("number_in_tax_year"), f"{path}.number_in_tax_year", optional=True),
                   _bool(data.get("is_last_in_tax_year"), f"{path}.is_last_in_tax_year", False),
                   _date(data.get("tax_year_start"), f"{path}.tax_year_start", optional=True))

    @property
    def ref(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_id": self.period_id, "start": self.start.isoformat(), "end": self.end.isoformat(),
            "pay_date": self.pay_date.isoformat(), "frequency": self.frequency,
            "periods_per_year": self.periods_per_year, "number_in_tax_year": self.number_in_tax_year,
            "is_last_in_tax_year": self.is_last_in_tax_year,
            "tax_year_start": self.tax_year_start.isoformat() if self.tax_year_start else None,
        }


@dataclass(frozen=True)
class PolicySnapshot:
    """A resolved policy version (3.1 §3): ``fields`` are the validated policy fields as plain JSON."""

    type: str
    fields: Mapping[str, Any]
    id: int | str | None = None
    version: int | None = None
    policy_hash: str | None = None

    @classmethod
    def from_dict(cls, policy_type: str, raw: Any, path: str) -> PolicySnapshot:
        data = _mapping(raw, path, ("id", "version", "policy_hash", "fields"))
        return cls(policy_type, _json_value(_mapping(data.get("fields") or {}, f"{path}.fields"), f"{path}.fields"),
                   _ident(data.get("id"), f"{path}.id"), _int(data.get("version"), f"{path}.version", optional=True),
                   _str(data.get("policy_hash"), f"{path}.policy_hash", optional=True))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "policy_hash": self.policy_hash,
                "fields": to_plain(self.fields)}


@dataclass(frozen=True)
class ComponentDef:
    """One component version from the catalogue (3.3 §2.2)."""

    code: str
    kind: str
    category: str
    version_id: int | None = None
    version: int | None = None
    name: str | None = None
    calc_method: str = "SYSTEM"
    tags: tuple[str, ...] = ()
    proration: str = "NONE"
    proration_basis: str | None = None
    frequency: str = "EVERY_PERIOD"
    months: tuple[int, ...] = ()
    rounding: str | None = None
    cap: Decimal | None = None
    floor: Decimal | None = None
    eligibility: str | None = None
    visibility: str = "EMPLOYEE_VISIBLE"
    sort_order: int = 0
    owner: str = "COMPANY"
    stage: int = 4

    _KEYS = ("code", "kind", "category", "version_id", "version", "name", "calc_method", "tags", "proration",
             "proration_basis", "frequency", "months", "rounding", "cap", "floor", "eligibility", "visibility",
             "sort_order", "owner", "stage")

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> ComponentDef:
        data = _mapping(raw, path, cls._KEYS, ("code", "kind", "category"))
        kind = _choice(data["kind"], f"{path}.kind", KINDS)
        months = tuple(sorted({_int(m, f"{path}.months[{i}]") for i, m in enumerate(_list(data.get("months"), f"{path}.months"))}))
        if any(not 1 <= m <= 12 for m in months):
            raise _fail(f"{path}.months", "months are 1-12")
        frequency = _choice(data.get("frequency"), f"{path}.frequency", FREQUENCIES, "EVERY_PERIOD")
        if frequency == "SPECIFIC_MONTHS" and not months:
            raise _fail(f"{path}.months", "SPECIFIC_MONTHS needs months")
        return cls(
            code=_str(data["code"], f"{path}.code"), kind=kind, category=_str(data["category"], f"{path}.category"),
            version_id=_int(data.get("version_id"), f"{path}.version_id", optional=True),
            version=_int(data.get("version"), f"{path}.version", optional=True),
            name=_str(data.get("name"), f"{path}.name", optional=True),
            calc_method=_choice(data.get("calc_method"), f"{path}.calc_method", METHODS, "SYSTEM"),
            tags=tuple(sorted({_str(t, f"{path}.tags[{i}]") for i, t in enumerate(_list(data.get("tags"), f"{path}.tags"))})),
            proration=_choice(data.get("proration"), f"{path}.proration", PRORATIONS, "NONE"),
            proration_basis=_str(data.get("proration_basis"), f"{path}.proration_basis", optional=True),
            frequency=frequency, months=months,
            rounding=_str(data.get("rounding"), f"{path}.rounding", optional=True),
            cap=_dec(data.get("cap"), f"{path}.cap", optional=True),
            floor=_dec(data.get("floor"), f"{path}.floor", optional=True),
            eligibility=_str(data.get("eligibility"), f"{path}.eligibility", optional=True),
            visibility=_choice(data.get("visibility"), f"{path}.visibility", ("EMPLOYEE_VISIBLE", "HR_ONLY"),
                               "EMPLOYEE_VISIBLE"),
            sort_order=_int(data.get("sort_order"), f"{path}.sort_order", default=0),
            owner=_str(data.get("owner", "COMPANY"), f"{path}.owner"),
            stage=_int(data.get("stage"), f"{path}.stage", default=DEFAULT_STAGE[kind]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "kind": self.kind, "category": self.category, "version_id": self.version_id,
            "version": self.version, "name": self.name, "calc_method": self.calc_method, "tags": list(self.tags),
            "proration": self.proration, "proration_basis": self.proration_basis, "frequency": self.frequency,
            "months": list(self.months), "rounding": self.rounding, "cap": dec_str(self.cap),
            "floor": dec_str(self.floor), "eligibility": self.eligibility, "visibility": self.visibility,
            "sort_order": self.sort_order, "owner": self.owner, "stage": self.stage,
        }


@dataclass(frozen=True)
class StructureLine:
    """One line of the employee's structure version with the assignment's overrides applied (3.3 §6.2)."""

    component: str
    method: str
    amount: Decimal | None = None
    annual_amount: Decimal | None = None
    params: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    order: int = 0

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> StructureLine:
        data = _mapping(raw, path, ("component", "method", "amount", "annual_amount", "params", "order"),
                        ("component", "method"))
        method = _choice(data["method"], f"{path}.method", METHODS)
        line = cls(_str(data["component"], f"{path}.component"), method,
                   _dec(data.get("amount"), f"{path}.amount", optional=True),
                   _dec(data.get("annual_amount"), f"{path}.annual_amount", optional=True),
                   _json_value(_mapping(data.get("params") or {}, f"{path}.params"), f"{path}.params"),
                   _int(data.get("order"), f"{path}.order", default=0))
        if method == "FIXED" and (line.amount is None) == (line.annual_amount is None):
            raise _fail(path, "a FIXED line needs exactly one of amount and annual_amount")
        return line

    def to_dict(self) -> dict[str, Any]:
        return {"component": self.component, "method": self.method, "amount": dec_str(self.amount),
                "annual_amount": dec_str(self.annual_amount), "params": to_plain(self.params), "order": self.order}


@dataclass(frozen=True)
class Compensation:
    """The compensation assignment in force in a segment (3.3 §7) with its structure version."""

    structure_type: str
    currency: str
    lines: tuple[StructureLine, ...]
    id: int | str | None = None
    version: int | None = None
    structure_id: int | str | None = None
    structure_version_id: int | str | None = None
    pay_basis_amount: Decimal | None = None
    ctc_employer_contributions: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))

    _KEYS = ("id", "version", "structure_id", "structure_version_id", "structure_type", "currency",
             "pay_basis_amount", "ctc_employer_contributions", "lines")

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> Compensation:
        data = _mapping(raw, path, cls._KEYS, ("structure_type", "currency"))
        lines = _items(data.get("lines"), f"{path}.lines", StructureLine.from_dict)
        codes = [line.component for line in lines]
        if len(set(codes)) != len(codes):
            raise _fail(f"{path}.lines", "a component appears twice in the structure")
        employer = _mapping(data.get("ctc_employer_contributions") or {}, f"{path}.ctc_employer_contributions")
        return cls(
            structure_type=_choice(data["structure_type"], f"{path}.structure_type", STRUCTURE_TYPES),
            currency=_str(data["currency"], f"{path}.currency"),
            lines=tuple(sorted(lines, key=lambda line: (line.order, line.component))),
            id=_ident(data.get("id"), f"{path}.id"), version=_int(data.get("version"), f"{path}.version", optional=True),
            structure_id=_ident(data.get("structure_id"), f"{path}.structure_id"),
            structure_version_id=_ident(data.get("structure_version_id"), f"{path}.structure_version_id"),
            pay_basis_amount=_dec(data.get("pay_basis_amount"), f"{path}.pay_basis_amount", optional=True),
            ctc_employer_contributions=MappingProxyType({
                str(code): _dec(value, f"{path}.ctc_employer_contributions.{code}")
                for code, value in sorted(employer.items())
            }),
        )

    def line(self, code: str) -> StructureLine | None:
        return next((line for line in self.lines if line.component == code), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "version": self.version, "structure_id": self.structure_id,
            "structure_version_id": self.structure_version_id, "structure_type": self.structure_type,
            "currency": self.currency, "pay_basis_amount": dec_str(self.pay_basis_amount),
            "ctc_employer_contributions": {k: dec_str(v) for k, v in self.ctc_employer_contributions.items()},
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class Segment:
    """An assignment segment of the period (3.4 §5.4): its own dates, assignment and compensation."""

    key: str
    start: dt.date
    end: dt.date
    assignment_id: int | str | None = None
    compensation: Compensation | None = None

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> Segment:
        data = _mapping(raw, path, ("key", "start", "end", "assignment_id", "compensation"), ("key", "start", "end"))
        start, end = _date(data["start"], f"{path}.start"), _date(data["end"], f"{path}.end")
        if end < start:
            raise _fail(f"{path}.end", "the segment ends before it starts")
        compensation = data.get("compensation")
        return cls(_str(data["key"], f"{path}.key"), start, end, _ident(data.get("assignment_id"), f"{path}.assignment_id"),
                   None if compensation is None else Compensation.from_dict(compensation, f"{path}.compensation"))

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "assignment_id": self.assignment_id,
                "compensation": None if self.compensation is None else self.compensation.to_dict()}


@dataclass(frozen=True)
class WageInput:
    """An approved wage input targeting the period (3.4 §13); consumed once by a finalized run (EC-VAR-01)."""

    id: int | str
    component: str
    amount: Decimal | None = None
    units: Decimal | None = None
    currency: str | None = None
    source: str = "MANUAL"
    external_ref: str | None = None
    reason: str | None = None
    content_hash: str | None = None

    _KEYS = ("id", "component", "amount", "units", "currency", "source", "external_ref", "reason", "content_hash")

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> WageInput:
        data = _mapping(raw, path, cls._KEYS, ("id", "component"))
        item = cls(_ident(data["id"], f"{path}.id", optional=False), _str(data["component"], f"{path}.component"),
                   _dec(data.get("amount"), f"{path}.amount", optional=True),
                   _dec(data.get("units"), f"{path}.units", optional=True),
                   _str(data.get("currency"), f"{path}.currency", optional=True),
                   _str(data.get("source", "MANUAL"), f"{path}.source"),
                   _str(data.get("external_ref"), f"{path}.external_ref", optional=True),
                   _str(data.get("reason"), f"{path}.reason", optional=True),
                   _str(data.get("content_hash"), f"{path}.content_hash", optional=True))
        if item.amount is None and item.units is None:
            raise _fail(path, "a wage input needs an amount or units")
        return item

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "component": self.component, "amount": dec_str(self.amount),
                "units": dec_str(self.units), "currency": self.currency, "source": self.source,
                "external_ref": self.external_ref, "reason": self.reason, "content_hash": self.content_hash}


@dataclass(frozen=True)
class ArrearsItem:
    """An approved arrears amount for an earlier period (3.3 §8.3); ``tags`` are the original component's."""

    id: int | str
    component: str
    amount: Decimal
    period_ref: str
    tags: tuple[str, ...] = ()
    reason: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> ArrearsItem:
        data = _mapping(raw, path, ("id", "component", "amount", "period_ref", "tags", "reason"),
                        ("id", "component", "amount", "period_ref"))
        return cls(_ident(data["id"], f"{path}.id", optional=False), _str(data["component"], f"{path}.component"),
                   _dec(data["amount"], f"{path}.amount"), _str(data["period_ref"], f"{path}.period_ref"),
                   tuple(sorted({_str(t, f"{path}.tags[{i}]") for i, t in enumerate(_list(data.get("tags"), f"{path}.tags"))})),
                   _str(data.get("reason"), f"{path}.reason", optional=True))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "component": self.component, "amount": dec_str(self.amount),
                "period_ref": self.period_ref, "tags": list(self.tags), "reason": self.reason}


@dataclass(frozen=True)
class Reimbursement:
    """An approved claim paid with payroll (3.4 §14), already converted into the pay currency."""

    id: int | str
    claim_type: str
    amount: Decimal
    currency: str | None = None
    taxability: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> Reimbursement:
        data = _mapping(raw, path, ("id", "claim_type", "amount", "currency", "taxability"), ("id", "claim_type", "amount"))
        return cls(_ident(data["id"], f"{path}.id", optional=False), _str(data["claim_type"], f"{path}.claim_type"),
                   _dec(data["amount"], f"{path}.amount"), _str(data.get("currency"), f"{path}.currency", optional=True),
                   _str(data.get("taxability"), f"{path}.taxability", optional=True))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "claim_type": self.claim_type, "amount": dec_str(self.amount),
                "currency": self.currency, "taxability": self.taxability}


@dataclass(frozen=True)
class Previous:
    """The last finalized period of this employee, for variance and outlier checks (3.5 §10)."""

    period_ref: str | None = None
    currency: str | None = None
    gross: Decimal | None = None
    net: Decimal | None = None
    components: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    history: Mapping[str, tuple[Decimal, ...]] = field(default_factory=lambda: MappingProxyType({}))
    payment_currency: str | None = None
    bank_account_ids: tuple[int | str, ...] = ()

    _KEYS = ("period_ref", "currency", "gross", "net", "components", "history", "payment_currency",
             "bank_account_ids")

    @classmethod
    def from_dict(cls, raw: Any, path: str = "previous") -> Previous:
        data = _mapping(raw or {}, path, cls._KEYS)
        components = _mapping(data.get("components") or {}, f"{path}.components")
        history = _mapping(data.get("history") or {}, f"{path}.history")
        accounts = _items(data.get("bank_account_ids"), f"{path}.bank_account_ids",
                          lambda v, p: _ident(v, p, optional=False))
        return cls(
            _str(data.get("period_ref"), f"{path}.period_ref", optional=True),
            _str(data.get("currency"), f"{path}.currency", optional=True),
            _dec(data.get("gross"), f"{path}.gross", optional=True), _dec(data.get("net"), f"{path}.net", optional=True),
            MappingProxyType({str(k): _dec(v, f"{path}.components.{k}") for k, v in sorted(components.items())}),
            MappingProxyType({str(k): _items(v, f"{path}.history.{k}", lambda x, p: _dec(x, p))
                              for k, v in sorted(history.items())}),
            _str(data.get("payment_currency"), f"{path}.payment_currency", optional=True),
            tuple(sorted(accounts, key=str)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"period_ref": self.period_ref, "currency": self.currency, "gross": dec_str(self.gross),
                "net": dec_str(self.net), "components": {k: dec_str(v) for k, v in self.components.items()},
                "history": {k: [dec_str(v) for v in values] for k, values in self.history.items()},
                "payment_currency": self.payment_currency, "bank_account_ids": list(self.bank_account_ids)}


@dataclass(frozen=True)
class BankAccount:
    """The beneficiary data payments will use (3.7 §5), with its split rule."""

    account_id: int | str
    type: str
    value: Decimal | None = None
    order: int = 0
    currency: str | None = None
    verified: bool = True
    first_payment: bool = False

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> BankAccount:
        data = _mapping(raw, path, ("account_id", "type", "value", "order", "currency", "verified", "first_payment"),
                        ("account_id", "type"))
        return cls(_ident(data["account_id"], f"{path}.account_id", optional=False),
                   _choice(data["type"], f"{path}.type", ALLOCATION_TYPES), _dec(data.get("value"), f"{path}.value", optional=True),
                   _int(data.get("order"), f"{path}.order", default=0),
                   _str(data.get("currency"), f"{path}.currency", optional=True),
                   _bool(data.get("verified"), f"{path}.verified", True),
                   _bool(data.get("first_payment"), f"{path}.first_payment", False))

    def to_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "type": self.type, "value": dec_str(self.value), "order": self.order,
                "currency": self.currency, "verified": self.verified, "first_payment": self.first_payment}


@dataclass(frozen=True)
class IncentiveInputs:
    """The payout window this run pays and the employee's resolved incentive target (3.1 §5.9, decision I-22)."""

    window_start: dt.date
    window_end: dt.date
    target: Decimal | None = None
    target_source: str | None = None
    grade_id: int | str | None = None

    _KEYS = ("window_start", "window_end", "target", "target_source", "grade_id")

    @classmethod
    def from_dict(cls, raw: Any, path: str = "incentive") -> IncentiveInputs | None:
        if raw is None:
            return None
        data = _mapping(raw, path, cls._KEYS, ("window_start", "window_end"))
        item = cls(_date(data["window_start"], f"{path}.window_start"),
                   _date(data["window_end"], f"{path}.window_end"),
                   _dec(data.get("target"), f"{path}.target", optional=True),
                   None if data.get("target_source") is None else _choice(
                       data.get("target_source"), f"{path}.target_source", ("EMPLOYEE", "GRADE")),
                   _ident(data.get("grade_id"), f"{path}.grade_id"))
        if item.window_end < item.window_start:
            raise _fail(path, "the payout window ends before it starts")
        if (item.target is None) != (item.target_source is None):
            raise _fail(path, "a target needs its source and a source needs its target")
        return item

    def to_dict(self) -> dict[str, Any]:
        return {"window_start": self.window_start.isoformat(), "window_end": self.window_end.isoformat(),
                "target": dec_str(self.target), "target_source": self.target_source, "grade_id": self.grade_id}


@dataclass(frozen=True)
class TaskInputs:
    """Approved tasks attributed to the period, the rate card and the minimum-wage data (3.4 §10)."""

    records: tuple[TaskRecord, ...] = ()
    rate_card: tuple[RateCardRow, ...] = ()
    carried_in: Decimal = Decimal(0)
    skill: str | None = None
    location: str | None = None
    min_hourly_rate: Decimal | None = None
    min_wage_verified: bool = False
    pack_forces_top_up: bool = False

    @classmethod
    def from_dict(cls, raw: Any, path: str = "tasks") -> TaskInputs:
        data = _mapping(raw or {}, path, ("records", "rate_card", "carried_in", "skill", "location", "minimum_wage"))
        wage = _mapping(data.get("minimum_wage") or {}, f"{path}.minimum_wage",
                        ("min_hourly_rate", "verified", "pack_forces_top_up"))
        return cls(
            _items(data.get("records"), f"{path}.records", _task_record),
            _items(data.get("rate_card"), f"{path}.rate_card", _rate_card_row),
            _dec(data.get("carried_in", "0"), f"{path}.carried_in"),
            _str(data.get("skill"), f"{path}.skill", optional=True),
            _str(data.get("location"), f"{path}.location", optional=True),
            _dec(wage.get("min_hourly_rate"), f"{path}.minimum_wage.min_hourly_rate", optional=True),
            _bool(wage.get("verified"), f"{path}.minimum_wage.verified", False),
            _bool(wage.get("pack_forces_top_up"), f"{path}.minimum_wage.pack_forces_top_up", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [_task_record_dict(t) for t in self.records],
            "rate_card": [_rate_card_dict(r) for r in self.rate_card],
            "carried_in": dec_str(self.carried_in), "skill": self.skill, "location": self.location,
            "minimum_wage": {"min_hourly_rate": dec_str(self.min_hourly_rate), "verified": self.min_wage_verified,
                             "pack_forces_top_up": self.pack_forces_top_up},
        }


# --------------------------------------------------------------------------- domain records (ledger, time pay)


def _source_id(raw: Any, path: str, *, optional: bool = False) -> int | str | None:
    return _ident(raw, path, optional=optional)


def _portion(raw: Any) -> str:
    return "FULL" if raw is None else raw


def _leave(raw: Any, path: str) -> lg.LeavePortion:
    data = _mapping(raw, path, ("leave_id", "leave_type_code", "portion", "pay_treatment", "partial_percent", "tiered",
                                "counts_non_working_days", "content_hash"), ("leave_id", "leave_type_code"))
    tiered = None
    if data.get("tiered") is not None:
        t = _mapping(data["tiered"], f"{path}.tiered", ("tiers", "days_before_period", "rolls_off"))
        tiers = tuple(
            _guard(f"{path}.tiered.tiers[{i}]", lambda r=r, i=i: lg.Tier(
                _int(_mapping(r, f"{path}.tiered.tiers[{i}]")["from_day"], f"{path}.tiered.tiers[{i}].from_day"),
                _int(r.get("to_day"), f"{path}.tiered.tiers[{i}].to_day", optional=True),
                _dec(r.get("percent"), f"{path}.tiered.tiers[{i}].percent")))
            for i, r in enumerate(_list(t.get("tiers"), f"{path}.tiered.tiers")))
        rolls = tuple((_date(_list(r, f"{path}.tiered.rolls_off[{i}]")[0], f"{path}.tiered.rolls_off[{i}]"),
                       _dec(_list(r, f"{path}.tiered.rolls_off[{i}]")[1], f"{path}.tiered.rolls_off[{i}]"))
                      for i, r in enumerate(_list(t.get("rolls_off"), f"{path}.tiered.rolls_off")))
        tiered = _guard(f"{path}.tiered", lambda: lg.TieredPay(tiers, _dec(t.get("days_before_period", "0"),
                                                                             f"{path}.tiered.days_before_period"), rolls))
    return _guard(path, lambda: lg.LeavePortion(
        _source_id(data["leave_id"], f"{path}.leave_id"), _str(data["leave_type_code"], f"{path}.leave_type_code"),
        _portion(data.get("portion")), data.get("pay_treatment") or "FULL",
        _dec(data.get("partial_percent"), f"{path}.partial_percent", optional=True), tiered,
        _bool(data.get("counts_non_working_days"), f"{path}.counts_non_working_days", False),
        _str(data.get("content_hash"), f"{path}.content_hash", optional=True)))


def _leave_dict(item: lg.LeavePortion) -> dict[str, Any]:
    tiered = None
    if item.tiered is not None:
        tiered = {"tiers": [{"from_day": t.from_day, "to_day": t.to_day, "percent": dec_str(t.percent)}
                            for t in item.tiered.tiers],
                  "days_before_period": dec_str(item.tiered.days_before_period),
                  "rolls_off": [[d.isoformat(), dec_str(n)] for d, n in item.tiered.rolls_off]}
    return {"leave_id": item.leave_id, "leave_type_code": item.leave_type_code, "portion": item.portion.value,
            "pay_treatment": item.pay_treatment.value, "partial_percent": dec_str(item.partial_percent),
            "tiered": tiered, "counts_non_working_days": item.counts_non_working_days,
            "content_hash": item.content_hash}


def _simple(cls: type, id_key: str, raw: Any, path: str) -> Any:
    data = _mapping(raw, path, (id_key, "portion", "content_hash"), (id_key,))
    return _guard(path, lambda: cls(_source_id(data[id_key], f"{path}.{id_key}"), _portion(data.get("portion")),
                                    _str(data.get("content_hash"), f"{path}.content_hash", optional=True)))


def _attendance(raw: Any, path: str) -> lg.AttendanceInput:
    data = _mapping(raw, path, ("status", "session_id", "closed_without_punch_out", "worked_seconds", "late",
                                "early_exit", "regularised", "night_seconds", "content_hash") + _STANDARD_ATTENDANCE_KEYS,
                    ("status",))
    return _guard(path, lambda: lg.AttendanceInput(
        data["status"], _source_id(data.get("session_id"), f"{path}.session_id", optional=True),
        _bool(data.get("closed_without_punch_out"), f"{path}.closed_without_punch_out", False),
        _int(data.get("worked_seconds"), f"{path}.worked_seconds", optional=True),
        _bool(data.get("late"), f"{path}.late", False), _bool(data.get("early_exit"), f"{path}.early_exit", False),
        _bool(data.get("regularised"), f"{path}.regularised", False),
        _int(data.get("night_seconds"), f"{path}.night_seconds", default=0),
        _str(data.get("content_hash"), f"{path}.content_hash", optional=True),
        rules_mode=_str(data.get("rules_mode"), f"{path}.rules_mode", optional=True) or "LEGACY_V1",
        present_portion=_str(data.get("present_portion"), f"{path}.present_portion", optional=True),
        other_half=_str(data.get("other_half"), f"{path}.other_half", optional=True),
        late_regularised=_bool(data.get("late_regularised"), f"{path}.late_regularised", False),
        early_exit_regularised=_bool(data.get("early_exit_regularised"), f"{path}.early_exit_regularised", False)))


# JC-AS-01: written only for STANDARD inputs, so v1 / LEGACY_V1 snapshots stay byte-identical (golden T-3).
_STANDARD_ATTENDANCE_KEYS = ("rules_mode", "present_portion", "other_half", "late_regularised",
                             "early_exit_regularised")


def _attendance_dict(att: lg.AttendanceInput) -> dict[str, Any]:
    body = {"status": att.status.value, "session_id": att.session_id,
            "closed_without_punch_out": att.closed_without_punch_out, "worked_seconds": att.worked_seconds,
            "late": att.late, "early_exit": att.early_exit, "regularised": att.regularised,
            "night_seconds": att.night_seconds, "content_hash": att.content_hash}
    if att.standard:
        body.update(rules_mode=att.rules_mode,
                    present_portion=att.present_portion.value if att.present_portion is not None else None,
                    other_half=att.other_half, late_regularised=att.late_regularised,
                    early_exit_regularised=att.early_exit_regularised)
    return body


_DAY_KEYS = ("date", "employed", "suspended_unpaid", "leaves", "holiday", "weekly_off", "manual_lop", "attendance",
             "on_duty", "segment", "standard_hours", "assignment_id", "suspension_id", "work_week_id")


def day_from_dict(raw: Any, path: str) -> lg.DayInput:
    data = _mapping(raw, path, _DAY_KEYS, ("date",))
    holiday = None
    if data.get("holiday") is not None:
        h = _mapping(data["holiday"], f"{path}.holiday", ("holiday_id", "type", "name", "content_hash"),
                     ("holiday_id", "type"))
        holiday = _guard(f"{path}.holiday", lambda: lg.HolidayInput(
            _source_id(h["holiday_id"], f"{path}.holiday.holiday_id"), h["type"], h.get("name") or "",
            _str(h.get("content_hash"), f"{path}.holiday.content_hash", optional=True)))
    attendance = None if data.get("attendance") is None else _attendance(data["attendance"], f"{path}.attendance")
    return _guard(path, lambda: lg.DayInput(
        date=_date(data["date"], f"{path}.date"),
        employed=_bool(data.get("employed"), f"{path}.employed", True),
        suspended_unpaid=_bool(data.get("suspended_unpaid"), f"{path}.suspended_unpaid", False),
        leaves=_items(data.get("leaves"), f"{path}.leaves", _leave),
        holiday=holiday,
        weekly_off=_bool(data.get("weekly_off"), f"{path}.weekly_off", False),
        manual_lop=_items(data.get("manual_lop"), f"{path}.manual_lop", lambda r, p: _simple(lg.ManualLop, "source_id", r, p)),
        attendance=attendance,
        on_duty=_items(data.get("on_duty"), f"{path}.on_duty", lambda r, p: _simple(lg.OnDuty, "source_id", r, p)),
        segment=data.get("segment") or "",
        standard_hours=_dec(data.get("standard_hours"), f"{path}.standard_hours", optional=True),
        assignment_id=_source_id(data.get("assignment_id"), f"{path}.assignment_id", optional=True),
        suspension_id=_source_id(data.get("suspension_id"), f"{path}.suspension_id", optional=True),
        work_week_id=_source_id(data.get("work_week_id"), f"{path}.work_week_id", optional=True),
    ))


def day_to_dict(day: lg.DayInput) -> dict[str, Any]:
    att = day.attendance
    return {
        "date": day.date.isoformat(), "employed": day.employed, "suspended_unpaid": day.suspended_unpaid,
        "leaves": [_leave_dict(item) for item in day.leaves],
        "holiday": None if day.holiday is None else {
            "holiday_id": day.holiday.holiday_id, "type": day.holiday.type.value, "name": day.holiday.name,
            "content_hash": day.holiday.content_hash},
        "weekly_off": day.weekly_off,
        "manual_lop": [{"source_id": m.source_id, "portion": m.portion.value, "content_hash": m.content_hash}
                       for m in day.manual_lop],
        "attendance": None if att is None else _attendance_dict(att),
        "on_duty": [{"source_id": o.source_id, "portion": o.portion.value, "content_hash": o.content_hash}
                    for o in day.on_duty],
        "segment": day.segment, "standard_hours": dec_str(day.standard_hours), "assignment_id": day.assignment_id,
        "suspension_id": day.suspension_id, "work_week_id": day.work_week_id,
    }


def _overtime_entry(raw: Any, path: str) -> OvertimeEntry:
    data = _mapping(raw, path, ("entry_id", "date", "minutes", "status", "comp_off", "start", "end", "origin"),
                    ("entry_id", "date", "minutes"))
    return OvertimeEntry(str(_ident(data["entry_id"], f"{path}.entry_id", optional=False)), _date(data["date"], f"{path}.date"),
                         _int(data["minutes"], f"{path}.minutes"), _str(data.get("status", "APPROVED"), f"{path}.status"),
                         _bool(data.get("comp_off"), f"{path}.comp_off", False),
                         _datetime(data.get("start"), f"{path}.start", optional=True),
                         _datetime(data.get("end"), f"{path}.end", optional=True),
                         _str(data.get("origin", "REQUEST"), f"{path}.origin"))


def _overtime_dict(e: OvertimeEntry) -> dict[str, Any]:
    return {"entry_id": e.entry_id, "date": e.date.isoformat(), "minutes": e.minutes, "status": e.status,
            "comp_off": e.comp_off, "start": e.start.isoformat() if e.start else None,
            "end": e.end.isoformat() if e.end else None, "origin": e.origin}


def _nwd(raw: Any, path: str) -> NonWorkingDayWork:
    data = _mapping(raw, path, ("date", "day_type", "worked_minutes", "approved", "choice", "ref"),
                    ("date", "day_type", "worked_minutes"))
    return NonWorkingDayWork(_date(data["date"], f"{path}.date"),
                             _choice(data["day_type"], f"{path}.day_type", ("HOLIDAY", "WEEKLY_OFF")),
                             _int(data["worked_minutes"], f"{path}.worked_minutes"),
                             _bool(data.get("approved"), f"{path}.approved", True),
                             _str(data.get("choice"), f"{path}.choice", optional=True),
                             _str(data.get("ref"), f"{path}.ref", optional=True))


def _nwd_dict(w: NonWorkingDayWork) -> dict[str, Any]:
    return {"date": w.date.isoformat(), "day_type": w.day_type, "worked_minutes": w.worked_minutes,
            "approved": w.approved, "choice": w.choice, "ref": w.ref}


def _shift(raw: Any, path: str) -> ShiftWorked:
    data = _mapping(raw, path, ("date", "start", "end", "unpaid_breaks", "weekend", "ref"), ("date", "start", "end"))
    breaks = tuple((_datetime(_list(b, f"{path}.unpaid_breaks[{i}]")[0], f"{path}.unpaid_breaks[{i}]"),
                    _datetime(_list(b, f"{path}.unpaid_breaks[{i}]")[1], f"{path}.unpaid_breaks[{i}]"))
                   for i, b in enumerate(_list(data.get("unpaid_breaks"), f"{path}.unpaid_breaks")))
    return ShiftWorked(_date(data["date"], f"{path}.date"), _datetime(data["start"], f"{path}.start"),
                       _datetime(data["end"], f"{path}.end"), breaks, _bool(data.get("weekend"), f"{path}.weekend", False),
                       _str(data.get("ref"), f"{path}.ref", optional=True))


def _shift_dict(s: ShiftWorked) -> dict[str, Any]:
    return {"date": s.date.isoformat(), "start": s.start.isoformat(), "end": s.end.isoformat(),
            "unpaid_breaks": [[a.isoformat(), b.isoformat()] for a, b in s.unpaid_breaks], "weekend": s.weekend,
            "ref": s.ref}


_TASK_KEYS = ("task_id", "task_type", "status", "quantity", "assignees", "accepted_quantity", "rate", "rate_currency",
              "approved_on", "completed_on", "evidence_count", "external_ref", "paid_in_run_id", "unit")


def _task_record(raw: Any, path: str) -> TaskRecord:
    data = _mapping(raw, path, _TASK_KEYS, ("task_id", "task_type", "status", "quantity"))
    assignees = tuple(
        TaskAssignee(str(_ident(_mapping(a, f"{path}.assignees[{i}]", ("employee_id", "split_percent"),
                                         ("employee_id",))["employee_id"], f"{path}.assignees[{i}].employee_id",
                                optional=False)),
                     _dec(a.get("split_percent", "100"), f"{path}.assignees[{i}].split_percent"))
        for i, a in enumerate(_list(data.get("assignees"), f"{path}.assignees")))
    return TaskRecord(
        str(_ident(data["task_id"], f"{path}.task_id", optional=False)), _str(data["task_type"], f"{path}.task_type"),
        _str(data["status"], f"{path}.status"), _dec(data["quantity"], f"{path}.quantity"), assignees,
        _dec(data.get("accepted_quantity"), f"{path}.accepted_quantity", optional=True),
        _dec(data.get("rate"), f"{path}.rate", optional=True),
        _str(data.get("rate_currency"), f"{path}.rate_currency", optional=True),
        _date(data.get("approved_on"), f"{path}.approved_on", optional=True),
        _date(data.get("completed_on"), f"{path}.completed_on", optional=True),
        _int(data.get("evidence_count"), f"{path}.evidence_count", default=0),
        _str(data.get("external_ref"), f"{path}.external_ref", optional=True),
        None if data.get("paid_in_run_id") is None else str(data["paid_in_run_id"]),
        _str(data.get("unit"), f"{path}.unit", optional=True),
    )


def _task_record_dict(t: TaskRecord) -> dict[str, Any]:
    return {"task_id": t.task_id, "task_type": t.task_type, "status": t.status, "quantity": dec_str(t.quantity),
            "assignees": [{"employee_id": a.employee_id, "split_percent": dec_str(a.split_percent)} for a in t.assignees],
            "accepted_quantity": dec_str(t.accepted_quantity), "rate": dec_str(t.rate), "rate_currency": t.rate_currency,
            "approved_on": t.approved_on.isoformat() if t.approved_on else None,
            "completed_on": t.completed_on.isoformat() if t.completed_on else None,
            "evidence_count": t.evidence_count, "external_ref": t.external_ref, "paid_in_run_id": t.paid_in_run_id,
            "unit": t.unit}


def _rate_card_row(raw: Any, path: str) -> RateCardRow:
    data = _mapping(raw, path, ("task_type", "rate", "currency", "effective_from", "effective_to", "grade", "skill",
                                "location"), ("task_type", "rate", "currency", "effective_from"))
    return RateCardRow(_str(data["task_type"], f"{path}.task_type"), _dec(data["rate"], f"{path}.rate"),
                       _str(data["currency"], f"{path}.currency"), _date(data["effective_from"], f"{path}.effective_from"),
                       _date(data.get("effective_to"), f"{path}.effective_to", optional=True),
                       _str(data.get("grade"), f"{path}.grade", optional=True),
                       _str(data.get("skill"), f"{path}.skill", optional=True),
                       _str(data.get("location"), f"{path}.location", optional=True))


def _rate_card_dict(r: RateCardRow) -> dict[str, Any]:
    return {"task_type": r.task_type, "rate": dec_str(r.rate), "currency": r.currency,
            "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None,
            "grade": r.grade, "skill": r.skill, "location": r.location}


def _ranges(raw: Any, path: str) -> tuple[tuple[dt.date, dt.date | None], ...]:
    result = []
    for index, item in enumerate(_list(raw, path)):
        pair = _list(item, f"{path}[{index}]")
        if len(pair) != 2:
            raise _fail(f"{path}[{index}]", "a range is [start, end | null]")
        start = _date(pair[0], f"{path}[{index}][0]")
        end = _date(pair[1], f"{path}[{index}][1]", optional=True)
        if end is not None and end < start:
            raise _fail(f"{path}[{index}]", "the range ends before it starts")
        result.append((start, end))
    return tuple(sorted(result, key=lambda r: (r[0], r[1] or dt.date.max)))


def _table(raw: Any, path: str) -> Mapping[str, Any]:
    data = _mapping(raw, path, ("code", "kind", "rows", "key_currency", "value_currency", "upper_inclusive"),
                    ("code", "kind", "rows"))
    return _json_value(data, path)


# --------------------------------------------------------------------------- the snapshot


_TOP_KEYS = ("schema_version", "run", "employee", "period", "currency", "settings", "switch_ranges", "policies",
             "components", "segments", "days", "ledger_overrides", "overtime_entries", "non_working_day_work", "shifts",
             "tasks", "wage_inputs", "incentive", "arrears", "reimbursements", "loans", "advances", "court_orders", "previous",
             "ytd", "pack", "pack_state", "tax_profile", "attestation", "fx", "bank_accounts", "tables")


def _frozen(mapping: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType(dict(sorted(mapping.items())))


@dataclass(frozen=True)
class Snapshot:
    """Everything one employee's calculation may read (3.5 §8.1); see the module docstring for the JSON form."""

    employee: EmployeeInfo
    period: PeriodInfo
    currency: str
    segments: tuple[Segment, ...]
    days: tuple[lg.DayInput, ...]
    run: RunInfo = field(default_factory=RunInfo)
    settings: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    switch_ranges: Mapping[str, tuple[tuple[dt.date, dt.date | None], ...]] = field(
        default_factory=lambda: MappingProxyType({}))
    policies: Mapping[str, PolicySnapshot] = field(default_factory=lambda: MappingProxyType({}))
    components: Mapping[str, ComponentDef] = field(default_factory=lambda: MappingProxyType({}))
    ledger_overrides: Mapping[dt.date, lg.LedgerOverride] = field(default_factory=lambda: MappingProxyType({}))
    overtime_entries: tuple[OvertimeEntry, ...] = ()
    non_working_day_work: tuple[NonWorkingDayWork, ...] = ()
    shifts: tuple[ShiftWorked, ...] = ()
    tasks: TaskInputs = field(default_factory=TaskInputs)
    wage_inputs: tuple[WageInput, ...] = ()
    incentive: IncentiveInputs | None = None
    arrears: tuple[ArrearsItem, ...] = ()
    reimbursements: tuple[Reimbursement, ...] = ()
    loans: tuple[Mapping[str, Any], ...] = ()
    advances: tuple[Mapping[str, Any], ...] = ()
    court_orders: tuple[Mapping[str, Any], ...] = ()
    previous: Previous = field(default_factory=Previous)
    ytd: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    pack: Mapping[str, Any] | None = None
    pack_state: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    tax_profile: Mapping[str, Any] | None = None
    attestation: Mapping[str, Any] | None = None
    fx: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    bank_accounts: tuple[BankAccount, ...] = ()
    tables: tuple[Mapping[str, Any], ...] = ()
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Snapshot:
        data = _mapping(raw, "", _TOP_KEYS, ("employee", "period", "currency", "segments", "days"))
        version = _int(data.get("schema_version"), "schema_version", default=SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise _fail("schema_version", f"unsupported schema version {version}", "UNSUPPORTED_SCHEMA")
        policies = _mapping(data.get("policies") or {}, "policies")
        components = _items(data.get("components"), "components", ComponentDef.from_dict)
        by_code: dict[str, ComponentDef] = {}
        for index, component in enumerate(components):
            if component.code in by_code:
                raise _fail(f"components[{index}].code", f"{component.code} appears twice", "DUPLICATE_COMPONENT")
            by_code[component.code] = component
        overrides = _mapping(data.get("ledger_overrides") or {}, "ledger_overrides")
        segments = _items(data.get("segments"), "segments", Segment.from_dict)
        if not segments:
            raise _fail("segments", "at least one segment is required", "MISSING_KEY")
        keys = [s.key for s in segments]
        if len(set(keys)) != len(keys):
            raise _fail("segments", "segment keys must be unique")
        segments = tuple(sorted(segments, key=lambda s: (s.start, s.key)))
        for earlier, later in zip(segments, segments[1:]):
            if later.start <= earlier.end:
                raise _fail("segments", f"segments {earlier.key} and {later.key} overlap", "OVERLAPPING_SEGMENTS")
        days = tuple(sorted(_items(data.get("days"), "days", day_from_dict), key=lambda d: d.date))
        ytd = _mapping(data.get("ytd") or {}, "ytd")
        switch = _mapping(data.get("switch_ranges") or {}, "switch_ranges")
        opaque = {name: (None if data.get(name) is None else _json_value(_mapping(data[name], name), name))
                  for name in ("pack", "tax_profile", "attestation")}
        return cls(
            employee=EmployeeInfo.from_dict(data["employee"]),
            period=PeriodInfo.from_dict(data["period"]),
            currency=_str(data["currency"], "currency"),
            segments=segments,
            days=days,
            run=RunInfo.from_dict(data.get("run")),
            settings=_json_value(_mapping(data.get("settings") or {}, "settings"), "settings"),
            switch_ranges=_frozen({str(k): _ranges(v, f"switch_ranges.{k}") for k, v in switch.items()}),
            policies=_frozen({str(k): PolicySnapshot.from_dict(str(k), v, f"policies.{k}") for k, v in policies.items()}),
            components=_frozen(by_code),
            ledger_overrides=MappingProxyType(dict(sorted(
                (_date(k, f"ledger_overrides.{k}"), _guard(f"ledger_overrides.{k}", lambda v=v, k=k: lg.LedgerOverride(
                    _mapping(v, f"ledger_overrides.{k}", ("action", "source_id"), ("action",))["action"],
                    _ident(v.get("source_id"), f"ledger_overrides.{k}.source_id"))))
                for k, v in overrides.items()))),
            overtime_entries=_items(data.get("overtime_entries"), "overtime_entries", _overtime_entry),
            non_working_day_work=tuple(sorted(_items(data.get("non_working_day_work"), "non_working_day_work", _nwd),
                                              key=lambda w: w.date)),
            shifts=_items(data.get("shifts"), "shifts", _shift),
            tasks=TaskInputs.from_dict(data.get("tasks")),
            wage_inputs=_items(data.get("wage_inputs"), "wage_inputs", WageInput.from_dict),
            incentive=IncentiveInputs.from_dict(data.get("incentive")),
            arrears=_items(data.get("arrears"), "arrears", ArrearsItem.from_dict),
            reimbursements=_items(data.get("reimbursements"), "reimbursements", Reimbursement.from_dict),
            loans=_items(data.get("loans"), "loans", lambda v, p: _json_value(_mapping(v, p), p)),
            advances=_items(data.get("advances"), "advances", lambda v, p: _json_value(_mapping(v, p), p)),
            court_orders=_items(data.get("court_orders"), "court_orders", lambda v, p: _json_value(_mapping(v, p), p)),
            previous=Previous.from_dict(data.get("previous")),
            ytd=_frozen({str(k): _dec(v, f"ytd.{k}") for k, v in ytd.items()}),
            pack=opaque["pack"], tax_profile=opaque["tax_profile"], attestation=opaque["attestation"],
            pack_state=_json_value(_mapping(data.get("pack_state") or {}, "pack_state"), "pack_state"),
            fx=_json_value(_mapping(data.get("fx") or {}, "fx"), "fx"),
            bank_accounts=_items(data.get("bank_accounts"), "bank_accounts", BankAccount.from_dict),
            tables=tuple(sorted(_items(data.get("tables"), "tables", _table), key=lambda t: t["code"])),
            schema_version=version,
        )

    def to_dict(self) -> dict[str, Any]:
        return self._with_optional({
            "schema_version": self.schema_version,
            "run": self.run.to_dict(),
            "employee": self.employee.to_dict(),
            "period": self.period.to_dict(),
            "currency": self.currency,
            "settings": to_plain(self.settings),
            "switch_ranges": {k: [[s.isoformat(), e.isoformat() if e else None] for s, e in v]
                              for k, v in self.switch_ranges.items()},
            "policies": {k: v.to_dict() for k, v in self.policies.items()},
            "components": [c.to_dict() for c in self.components.values()],
            "segments": [s.to_dict() for s in self.segments],
            "days": [day_to_dict(d) for d in self.days],
            "ledger_overrides": {d.isoformat(): {"action": o.action.value, "source_id": o.source_id}
                                 for d, o in self.ledger_overrides.items()},
            "overtime_entries": [_overtime_dict(e) for e in self.overtime_entries],
            "non_working_day_work": [_nwd_dict(w) for w in self.non_working_day_work],
            "shifts": [_shift_dict(s) for s in self.shifts],
            "tasks": self.tasks.to_dict(),
            "wage_inputs": [w.to_dict() for w in self.wage_inputs],
            "arrears": [a.to_dict() for a in self.arrears],
            "reimbursements": [r.to_dict() for r in self.reimbursements],
            "loans": to_plain(self.loans),
            "advances": to_plain(self.advances),
            "court_orders": to_plain(self.court_orders),
            "previous": self.previous.to_dict(),
            "ytd": {k: dec_str(v) for k, v in self.ytd.items()},
            "pack": to_plain(self.pack),
            "pack_state": to_plain(self.pack_state),
            "tax_profile": to_plain(self.tax_profile),
            "attestation": to_plain(self.attestation),
            "fx": to_plain(self.fx),
            "bank_accounts": [b.to_dict() for b in self.bank_accounts],
            "tables": to_plain(self.tables),
        })

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def _with_optional(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.incentive is not None:
            data["incentive"] = self.incentive.to_dict()
        return data

    @property
    def content_hash(self) -> str:
        """SHA-256 of the canonical JSON (3.5 §8.2); stored with the run and part of ``calc_hash``."""
        return sha256_hex(self.canonical_json())


def content_hash(snapshot: Snapshot | Mapping[str, Any]) -> str:
    """The content hash of a snapshot or of its JSON form (normalized first, so equal meaning gives equal hash)."""
    parsed = snapshot if isinstance(snapshot, Snapshot) else Snapshot.from_dict(snapshot)
    return parsed.content_hash


def load(snapshot: Snapshot | Mapping[str, Any]) -> Snapshot:
    return snapshot if isinstance(snapshot, Snapshot) else Snapshot.from_dict(snapshot)


__all__ = [
    "ArrearsItem", "BankAccount", "Compensation", "ComponentDef", "EmployeeInfo", "IncentiveInputs", "PeriodInfo", "PolicySnapshot",
    "Previous", "Reimbursement", "RunInfo", "SCHEMA_VERSION", "Segment", "Snapshot", "SnapshotError",
    "StructureLine", "TaskInputs", "WageInput", "canonical_json", "content_hash", "day_from_dict", "day_to_dict",
    "load", "sha256_hex", "to_plain",
]

