"""Payroll policy types: field schemas, activation rules, hashing and assignment (3.1 §3, §5).

A policy is a named, versioned set of fields of one type (overtime, leave pay…) assigned to
employees by eligibility rules. Every type is a strict pydantic model: unknown fields, floats and
loosely typed values are rejected, decimals are strings in JSON, money is ``{"amount",
"currency"}``, and every documented default is filled in. A version may be saved as a DRAFT with
legally sensitive fields blank (overtime, holiday-work and weekly-off multipliers, encashment
formulas…); activation rejects them while they are blank, so no legal constant is ever assumed
(decision I-13, contract K:157).

``policy_hash`` is the SHA-256 of the canonical JSON frozen into run snapshots. Assignment rules
match employees on their attributes on a date; ``resolve_assignment`` takes the highest priority
and reports same-priority ambiguity instead of guessing (EC-CFG-05). ``starter_policies`` builds
the catch-all policies of a configuration profile: ACTIVE where the profile turns a gate switch
ON, DRAFT otherwise.
"""

import hashlib
import json
import re
import types
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, TypeAdapter, ValidationError, model_validator
from pydantic_core import CoreSchema, core_schema

from app.payroll import currencies
from app.payroll.settings_registry import (
    CODE_RE,
    LOCALE_RE,
    PROFILES,
    REGISTRY,
    Profile,
    SettingValidationError,
    decimal_str,
    parse_decimal,
)

GRAINS: Final = ("DAY", "PERIOD")
VERSION_STATUSES: Final = ("DRAFT", "ACTIVE")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_LEAVE_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,29}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FORMULA_MAX_LENGTH = 2000


class PolicyValidationError(ValueError):
    """Rejected policy fields, rules or inputs. ``code`` is stable; ``field`` is a path such as ``buckets[0].multiplier``."""

    def __init__(
        self, code: str, message: str, *, field: str | None = None, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details: dict[str, Any] = dict(details or {})

    def __repr__(self) -> str:
        return f"PolicyValidationError({self.code!r}, {self.message!r}, field={self.field!r})"


# --------------------------------------------------------------------------- field types


@dataclass(frozen=True)
class FieldMeta:
    """A field rule: validates strictly, serializes to JSON and describes itself for the frontend.

    Used as ``Annotated`` metadata; pydantic calls ``check`` instead of its own (lax) coercion.
    """

    kind: str = "value"
    minimum: Decimal | int | None = None
    maximum: Decimal | int | None = None
    exclusive_minimum: bool = False
    max_places: int | None = None
    max_length: int | None = None
    unit: str | None = None
    choices: tuple[str, ...] | None = None

    def check(self, value: object) -> Any:
        raise NotImplementedError

    def serialize(self, value: Any) -> Any:
        return value

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(
            self.check, serialization=core_schema.plain_serializer_function_ser_schema(self.serialize)
        )

    def json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        for name in ("minimum", "maximum", "max_places", "max_length", "unit"):
            value = getattr(self, name)
            if value is not None:
                data[name] = decimal_str(value) if isinstance(value, Decimal) else value
        if self.exclusive_minimum:
            data["exclusive_minimum"] = True
        if self.choices is not None:
            data["choices"] = list(self.choices)
        return data


def _places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


@dataclass(frozen=True)
class DecimalRule(FieldMeta):
    kind: str = "decimal"
    max_places: int | None = 4

    def check(self, value: object) -> Decimal:
        number = parse_decimal(value, what="the value")
        low, high = self.minimum, self.maximum
        if low is not None and (number <= low if self.exclusive_minimum else number < low):
            raise ValueError(f"must be {'greater than' if self.exclusive_minimum else 'at least'} {decimal_str(Decimal(low))}")
        if high is not None and number > high:
            raise ValueError(f"must be at most {decimal_str(Decimal(high))}")
        if self.max_places is not None and _places(number) > self.max_places:
            raise ValueError(f"allows at most {self.max_places} decimal places")
        return number

    def serialize(self, value: Any) -> str:
        return decimal_str(value)


@dataclass(frozen=True)
class IntRule(FieldMeta):
    kind: str = "int"

    def check(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be a whole number")
        if (self.minimum is not None and value < self.minimum) or (self.maximum is not None and value > self.maximum):
            raise ValueError(f"must be between {self.minimum} and {self.maximum}")
        return value


@dataclass(frozen=True)
class TextRule(FieldMeta):
    kind: str = "text"

    def check(self, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be non-empty text")
        if self.max_length is not None and len(value) > self.max_length:
            raise ValueError(f"must be at most {self.max_length} characters")
        if _CONTROL_RE.search(value):
            raise ValueError("must not contain control characters")
        return value


@dataclass(frozen=True)
class PatternRule(FieldMeta):
    pattern: re.Pattern[str] | None = None
    hint: str = ""

    def check(self, value: object) -> str:
        if not isinstance(value, str) or self.pattern is None or not self.pattern.fullmatch(value):
            raise ValueError(f"must be {self.hint}")
        return value

    def json(self) -> dict[str, Any]:
        return super().json() | {"hint": self.hint}


@dataclass(frozen=True)
class FormulaRule(FieldMeta):
    kind: str = "formula"
    max_length: int | None = _FORMULA_MAX_LENGTH

    def check(self, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a formula")
        if len(value) > _FORMULA_MAX_LENGTH:
            raise ValueError(f"must be at most {_FORMULA_MAX_LENGTH} characters")
        from app.payroll.formula import FormulaError, parse

        try:
            parse(value)
        except FormulaError as error:
            raise ValueError(f"is not a valid formula: {error.message}") from None
        return value


@dataclass(frozen=True)
class RefRule(FieldMeta):
    kind: str = "ref"

    def check(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("must be a record id")
        return value


@dataclass(frozen=True)
class ChoiceSetRule(FieldMeta):
    """A set of choices, stored as a list in choice order."""

    kind: str = "set"
    min_items: int = 0

    def check(self, value: object) -> tuple[str, ...]:
        choices = self.choices or ()
        if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("must be a list")
        items = list(value)
        for item in items:
            if not isinstance(item, str) or item not in choices:
                raise ValueError(f"{item!r} is not one of {', '.join(choices)}")
        if len(set(items)) != len(items):
            raise ValueError("lists the same value twice")
        if len(items) < self.min_items:
            raise ValueError(f"needs at least {self.min_items} value(s)")
        return tuple(sorted(items, key=choices.index))

    def serialize(self, value: Any) -> list[str]:
        return list(value)


@dataclass(frozen=True)
class ThresholdRule(FieldMeta):
    """``SHIFT_DURATION`` or a number of hours (a whole number of minutes)."""

    kind: str = "hours_or_shift"
    minimum: Decimal | int | None = Decimal(0)
    maximum: Decimal | int | None = Decimal(24)
    exclusive_minimum: bool = True
    unit: str | None = "hours"
    choices: tuple[str, ...] | None = ("SHIFT_DURATION",)

    def check(self, value: object) -> str | Decimal:
        if value == "SHIFT_DURATION":
            return "SHIFT_DURATION"
        number = parse_decimal(value, what="the threshold")
        if not Decimal(0) < number <= Decimal(24):
            raise ValueError("must be SHIFT_DURATION or a number of hours above 0 and at most 24")
        if (number * 60) % 1 != 0:
            raise ValueError("must be a whole number of minutes")
        return number

    def serialize(self, value: Any) -> str:
        return value if isinstance(value, str) else decimal_str(value)


@dataclass(frozen=True)
class IdListRule(FieldMeta):
    kind: str = "ids"

    def check(self, value: object) -> tuple[int, ...]:
        if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("must be a list of record ids")
        if not value:
            raise ValueError("must not be empty; leave the criterion out instead")
        return tuple(sorted({RefRule().check(item) for item in value}))

    def serialize(self, value: Any) -> list[int]:
        return list(value)


@dataclass(frozen=True)
class CodeListRule(FieldMeta):
    kind: str = "codes"

    def check(self, value: object) -> tuple[str, ...]:
        if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("must be a list of codes")
        items = [item for item in value if isinstance(item, str) and item.strip() and len(item) <= 50]
        if not items or len(items) != len(value):
            raise ValueError("must be a non-empty list of codes of at most 50 characters")
        return tuple(sorted(set(items)))

    def serialize(self, value: Any) -> list[str]:
        return list(value)


_D0, _D24 = Decimal(0), Decimal(24)
Hours = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=_D24, exclusive_minimum=True, unit="hours")]
HoursOrZero = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=_D24, unit="hours")]
WeekHours = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(168), exclusive_minimum=True, unit="hours")]
MonthHours = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(744), exclusive_minimum=True, unit="hours")]
QuarterHours = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(2208), exclusive_minimum=True, unit="hours")]
Percent = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(100), max_places=6, unit="%")]
PositivePercent = Annotated[
    Decimal, DecimalRule(minimum=_D0, maximum=Decimal(100), exclusive_minimum=True, max_places=6, unit="%")]
AllowancePercent = Annotated[
    Decimal, DecimalRule(minimum=_D0, maximum=Decimal(1000), exclusive_minimum=True, max_places=6, unit="%")]
Multiplier = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(10), unit="x")]
OvertimeMultiplier = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(10), exclusive_minimum=True, unit="x")]
Ratio = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(10), exclusive_minimum=True, unit="x")]
Days = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(366), unit="days")]
PenaltyDays = Annotated[
    Decimal, DecimalRule(minimum=_D0, maximum=Decimal(31), exclusive_minimum=True, max_places=2, unit="days")]
CapDays = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(31), max_places=2, unit="days")]
Measure = Annotated[Decimal, DecimalRule(minimum=_D0)]
Amount = Annotated[Decimal, DecimalRule(minimum=_D0)]
DayNumber = Annotated[int, IntRule(minimum=1, maximum=366, unit="day")]
StepDays = Annotated[int, IntRule(minimum=1, maximum=366, unit="days")]
WindowMonths = Annotated[int, IntRule(minimum=1, maximum=60, unit="months")]
DayCount = Annotated[int, IntRule(minimum=1, maximum=3650, unit="days")]
ClawbackDays = Annotated[int, IntRule(minimum=0, maximum=3650, unit="days")]
IncrementMinutes = Annotated[int, IntRule(minimum=1, maximum=60, unit="minutes")]
DayMinutes = Annotated[int, IntRule(minimum=0, maximum=1440, unit="minutes")]
WindowMinutes = Annotated[int, IntRule(minimum=1, maximum=1440, unit="minutes")]
GraceMinutes = Annotated[int, IntRule(minimum=0, maximum=240, unit="minutes")]
MarkCount = Annotated[int, IntRule(minimum=0, maximum=100)]
MarksPerUnit = Annotated[int, IntRule(minimum=1, maximum=100)]
TenureMonths = Annotated[int, IntRule(minimum=1, maximum=360, unit="months")]
ServiceMonths = Annotated[int, IntRule(minimum=0, maximum=600, unit="months")]
LoanCount = Annotated[int, IntRule(minimum=1, maximum=10)]
RecoveryRuns = Annotated[int, IntRule(minimum=1, maximum=3, unit="runs")]
PerYearCount = Annotated[int, IntRule(minimum=1, maximum=24)]
Code = Annotated[str, PatternRule(kind="code", max_length=50, pattern=CODE_RE, hint="an upper-case code such as TASK_EARNING")]
LeaveCode = Annotated[
    str, PatternRule(kind="leave_type_code", max_length=30, pattern=_LEAVE_CODE_RE, hint="a leave type code such as casual")]
LocaleCode = Annotated[str, PatternRule(kind="locale", max_length=20, pattern=LOCALE_RE, hint="a locale such as en-IN")]
LocalTime = Annotated[str, PatternRule(kind="time", max_length=5, pattern=_TIME_RE, hint="a time in HH:MM")]
Formula = Annotated[str, FormulaRule()]
Ref = Annotated[int, RefRule()]
Name60 = Annotated[str, TextRule(max_length=60)]
Name100 = Annotated[str, TextRule(max_length=100)]
Note500 = Annotated[str, TextRule(max_length=500)]
DailyThreshold = Annotated[Union[str, Decimal], ThresholdRule()]
IdList = Annotated[tuple[int, ...], IdListRule()]
CodeList = Annotated[tuple[str, ...], CodeListRule()]


def _f(default: Any = ..., help_: str = "", *, legal: bool = False, factory: Callable[[], Any] | None = None) -> Any:
    extra = {"legal_sensitivity": "LEGAL_FLOOR"} if legal else None
    if factory is not None:
        return Field(default_factory=factory, description=help_, json_schema_extra=extra)
    return Field(default=default, description=help_, json_schema_extra=extra)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Money(_Strict):
    """A non-negative amount. The currency may be omitted only for zero, which is the same in every currency."""

    amount: Amount
    currency: str | None = None

    @model_validator(mode="after")
    def _currency(self) -> "Money":
        if self.currency is None:
            if self.amount != 0:
                raise ValueError("a non-zero amount needs a currency")
            return self
        info = currencies.require_payroll_currency(self.currency)
        if info.minor_unit is not None and _places(self.amount) > info.minor_unit:
            raise ValueError(f"{info.code} amounts have at most {info.minor_unit} decimal places")
        return self


def _same_currency(*amounts: Money | None) -> bool:
    found = {m.currency for m in amounts if m is not None and m.currency is not None}
    return len(found) <= 1


@dataclass(frozen=True)
class ActivationProblem:
    """A field that must be filled in before a policy version can be activated."""

    field: str
    message: str
    legal_sensitivity: str = "NONE"


class PolicyFields(_Strict):
    """Base of every policy type's fields."""

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        return []


def _needs(switches: frozenset[str] | None, *keys: str) -> bool:
    """Whether a feature gated by ``keys`` may be in force; ``None`` means assume every switch ON."""
    return switches is None or any(key in switches for key in keys)


# --------------------------------------------------------------------------- 3.1 §5.2 attendance


class AttendancePayPolicy(PolicyFields):
    unmarked_day: Literal["EXCEPTION", "ABSENT", "PRESENT"] = _f(
        "EXCEPTION", "A working day with no attendance and no leave: a review exception, absent (LOP) or present.")
    missing_punch_out: Literal["EXCEPTION", "HALF_DAY", "FULL_DAY", "ABSENT"] = _f(
        "EXCEPTION", "A session closed without a punch-out: a review exception, a half day, a full day or absent.")
    derive_status_from_hours: bool = _f(False, "Recompute full or half day from canonical net hours.")
    full_day_min_hours: Hours = _f(Decimal("8.0"), "Net hours for a full day when deriving the status.")
    half_day_min_hours: Hours = _f(Decimal("4.0"), "Net hours for a half day when deriving; below this, absent.")
    count_on_duty_as_present: bool = _f(True, "Approved on-duty or field work counts as present.")
    lop_component_tag: Code = _f("LOP_APPLICABLE", "Only components with this tag are reduced by LOP.")
    break_type_pay_map: dict[Code, bool] = _f(
        help_="Paid (true) or unpaid (false) per break type, for example {\"LUNCH\": false}. Unlisted break "
        "types keep the choice recorded on the break when it started.", factory=dict)

    @model_validator(mode="after")
    def _hours(self) -> "AttendancePayPolicy":
        if self.half_day_min_hours > self.full_day_min_hours:
            raise ValueError("half_day_min_hours must not exceed full_day_min_hours")
        return self


# --------------------------------------------------------------------------- 3.1 §5.3 leave

LEAVE_PAY_TREATMENTS: Final = ("FULL", "UNPAID", "PARTIAL", "TIERED", "STATUTORY")
ENCASH_ON: Final = ("EXIT", "YEAR_END", "ON_REQUEST")
STATUTORY_REVIEW_WORDS: Final = ("maternity", "paternity", "parental", "adoption", "sick")
EncashOn = Annotated[tuple[str, ...], ChoiceSetRule(choices=ENCASH_ON, min_items=1)]


class LeaveTier(_Strict):
    from_day: DayNumber
    to_day: DayNumber
    percent: Percent


class LeavePayRow(_Strict):
    leave_type_code: LeaveCode = _f(help_="The leave type (leave_types.code).")
    pay_treatment: Literal["FULL", "UNPAID", "PARTIAL", "TIERED", "STATUTORY"] | None = _f(
        None, "How a leave day is paid. Blank: FULL when the leave type is paid, else UNPAID.")
    partial_percent: Percent | None = _f(None, "Paid percentage for PARTIAL.")
    tiers: list[LeaveTier] | None = _f(
        None, "For TIERED: consecutive day ranges from day 1 with their paid percentage, counted in a rolling "
        "window across periods (for example days 1-10 at 100%, 11-30 at 50%).")
    tiers_window_months: WindowMonths = _f(
        12, "Length of the rolling window in which TIERED leave days are counted.")
    counts_toward_ot_threshold: bool = _f(False, "Leave hours count as worked for weekly overtime thresholds.")
    encashable: bool = _f(False, "Unused balance of this leave type may be paid out.")
    encashment_formula: Formula | None = _f(
        None, "Rate per encashed day, for example (BASIC + DA) / 26. Required to activate an encashable type; "
        "no formula is assumed.", legal=True)
    encash_on: EncashOn = _f(
        ("EXIT",), "When encashment happens: on exit, at year end (the excess) or on request.")
    encashment_max_days_per_year: Days | None = _f(None, "Maximum days encashed per year.")
    year_end_excess_action: Literal["LAPSE", "CARRY_FORWARD", "ENCASH"] = _f(
        "CARRY_FORWARD", "What happens to the balance above the carry-forward limit at year end.")

    @model_validator(mode="after")
    def _rules(self) -> "LeavePayRow":
        if self.partial_percent is not None and self.pay_treatment != "PARTIAL":
            raise ValueError("partial_percent applies only to PARTIAL")
        if self.tiers is not None:
            if self.pay_treatment != "TIERED":
                raise ValueError("tiers apply only to TIERED")
            expected = 1
            for tier in self.tiers:
                if tier.from_day != expected or tier.to_day < tier.from_day:
                    raise ValueError("tiers must be consecutive day ranges starting at day 1")
                expected = tier.to_day + 1
        return self

    def problems(self, prefix: str, switches: frozenset[str] | None) -> list[ActivationProblem]:
        found: list[ActivationProblem] = []
        if self.pay_treatment == "PARTIAL" and self.partial_percent is None:
            found.append(ActivationProblem(f"{prefix}.partial_percent", "Enter the paid percentage."))
        if self.pay_treatment == "TIERED" and not self.tiers:
            found.append(ActivationProblem(f"{prefix}.tiers", "Enter the tiers."))
        if self.encashable and self.encashment_formula is None and _needs(switches, "payroll.leave.encashment.enabled"):
            found.append(ActivationProblem(
                f"{prefix}.encashment_formula", "Enter the encashment rate formula.", "LEGAL_FLOOR"))
        if self.year_end_excess_action == "ENCASH" and not (self.encashable and "YEAR_END" in self.encash_on):
            found.append(ActivationProblem(
                f"{prefix}.year_end_excess_action", "Year-end encashment needs an encashable type encashed at YEAR_END."))
        return found


class LeavePayPolicy(PolicyFields):
    rows: list[LeavePayRow] = _f(help_="One row per leave type.", factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "LeavePayPolicy":
        codes = [row.leave_type_code for row in self.rows]
        if len(set(codes)) != len(codes):
            raise ValueError("each leave type may appear only once")
        return self

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        return [p for i, row in enumerate(self.rows) for p in row.problems(f"rows[{i}]", switches)]


def leave_pay_treatment(row: Mapping[str, Any], *, is_paid: bool) -> str:
    """The effective treatment of a normalized leave pay row (blank: from the leave type)."""
    treatment = row.get("pay_treatment")
    if treatment is not None:
        return str(treatment)
    return "FULL" if is_paid else "UNPAID"


# --------------------------------------------------------------------------- 3.1 §5.4 holidays

HOLIDAY_WORK_COMPENSATIONS: Final = ("PREMIUM", "COMP_OFF", "PREMIUM_AND_COMP_OFF", "EMPLOYEE_CHOICE")
_PREMIUM_PAID: Final = frozenset({"PREMIUM", "PREMIUM_AND_COMP_OFF", "EMPLOYEE_CHOICE"})
PAID_HOLIDAY_TYPES: Final = ("PUBLIC", "COMPANY", "OPTIONAL_AVAILED")
PaidHolidayTypes = Annotated[tuple[str, ...], ChoiceSetRule(choices=PAID_HOLIDAY_TYPES)]
WorkCompensation = Literal["PREMIUM", "COMP_OFF", "PREMIUM_AND_COMP_OFF", "EMPLOYEE_CHOICE"]


class HolidayPayPolicy(PolicyFields):
    paid_holiday_types: PaidHolidayTypes = _f(
        PAID_HOLIDAY_TYPES, "Holiday types that count as paid days.")
    non_salaried_holiday_pay: Literal["OFF", "STANDARD_HOURS", "AVERAGE_DAILY_EARNINGS"] = _f(
        "STANDARD_HOURS", "Holiday pay for hourly and daily workers.")
    eligibility_adjacent_days: Literal["NONE", "DAY_BEFORE", "DAY_AFTER", "BOTH"] = _f(
        "NONE", "Presence required on the working day before and/or after to earn holiday pay (needs attendance).")
    holiday_work_compensation: WorkCompensation = _f("PREMIUM", "Compensation for work on a holiday.")
    holiday_work_multiplier: Multiplier | None = _f(
        None, "Extra pay per holiday hour (up to standard hours) as a multiple of the hourly rate, on top of the "
        "normal pay for the day: 1.0 means double pay in total. Required to activate; none is assumed.", legal=True)
    holiday_work_min_hours: HoursOrZero = _f(Decimal("4.0"), "Minimum hours worked on a holiday to qualify.")
    holiday_work_requires_approval: bool = _f(True, "Holiday work must be approved before it is paid.")
    holiday_hours_beyond_standard: Literal["OVERTIME_HOLIDAY_BUCKET", "SAME_PREMIUM"] = _f(
        "OVERTIME_HOLIDAY_BUCKET", "Hours beyond standard hours: the overtime HOLIDAY bucket, or the same premium.")
    comp_off_validity_days: DayCount = _f(60, "Comp-off expires after this.")
    holiday_on_weekly_off: Literal["NONE", "SUBSTITUTE_DAY", "EXTRA_DAY_PAY"] = _f(
        "NONE", "A holiday that falls on a weekly off.")
    weekly_off_work_compensation: WorkCompensation = _f("PREMIUM", "Compensation for work on a weekly off.")
    weekly_off_work_multiplier: Multiplier | None = _f(
        None, "Extra pay per weekly-off hour on top of the normal pay for the day: 0.5 means time and a half in "
        "total. Required to activate; none is assumed.", legal=True)
    weekly_off_work_min_hours: HoursOrZero = _f(Decimal("4.0"), "Minimum hours worked on a weekly off to qualify.")

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        found: list[ActivationProblem] = []
        if (_needs(switches, "payroll.holiday_work_premium.enabled") and self.holiday_work_multiplier is None
                and self.holiday_work_compensation in _PREMIUM_PAID):
            found.append(ActivationProblem("holiday_work_multiplier", "Enter the holiday-work multiplier.", "LEGAL_FLOOR"))
        if (_needs(switches, "payroll.weekly_off_pay.enabled") and self.weekly_off_work_multiplier is None
                and self.weekly_off_work_compensation in _PREMIUM_PAID):
            found.append(ActivationProblem("weekly_off_work_multiplier", "Enter the weekly-off multiplier.", "LEGAL_FLOOR"))
        return found


# --------------------------------------------------------------------------- 3.1 §5.5 overtime

OT_DAY_TYPES: Final = ("WORKDAY", "WEEKLY_OFF", "HOLIDAY")
# SUM_TAG_HOURLY converts each OT_BASE rate by its unit: period amounts ÷ period.basis_days ÷
# emp.standard_hours_per_day (for a salaried employee exactly the 3.1 §5.5 formula
# SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day), per-day rates ÷ standard hours and
# per-hour (RATE_X_UNITS per hour) rates as they are, so an hourly worker's 48.00/h is not divided again.
DEFAULT_OT_RATE_FORMULA: Final = 'SUM_TAG_HOURLY("OT_BASE")'
_SIXTY = Decimal(60)


class OvertimeBucket(_Strict):
    day_type: Literal["WORKDAY", "WEEKLY_OFF", "HOLIDAY"]
    after_hours: HoursOrZero = _f(Decimal(0), "The tier starts after this many overtime hours on the day.")
    multiplier: OvertimeMultiplier | None = _f(
        None, "Pay per overtime hour as a multiple of the hourly rate. Required to activate; none is assumed.",
        legal=True)

    @model_validator(mode="after")
    def _minutes(self) -> "OvertimeBucket":
        if (self.after_hours * _SIXTY) % 1 != 0:
            raise ValueError("after_hours must be a whole number of minutes")
        return self


def _blank_buckets() -> list[OvertimeBucket]:
    return [OvertimeBucket(day_type=day_type, after_hours=Decimal(0)) for day_type in OT_DAY_TYPES]


class OvertimeRounding(_Strict):
    increment_minutes: IncrementMinutes = 15
    mode: Literal["DOWN", "NEAREST", "UP"] = "DOWN"


class OvertimeCaps(_Strict):
    per_day: Hours | None = None
    per_week: WeekHours | None = None
    per_month: MonthHours | None = None
    per_quarter: QuarterHours | None = None


class OvertimePolicy(PolicyFields):
    exempt: bool = _f(False, "For workers who never earn overtime (blocked where the law covers them).")
    source: Literal["APPROVED_REQUESTS", "ATTENDANCE_WITH_APPROVAL", "ATTENDANCE_AUTO"] = _f(
        "APPROVED_REQUESTS", "Where overtime hours come from; attendance sources need attendance ON.")
    daily_threshold: DailyThreshold = _f("SHIFT_DURATION", "Hours above this in a day are overtime.")
    weekly_threshold_hours: WeekHours | None = _f(None, "Hours above this in a week are overtime.")
    threshold_combination: Literal["DAILY", "WEEKLY", "DAILY_THEN_WEEKLY"] = _f(
        "DAILY", "DAILY_THEN_WEEKLY counts each hour once.")
    min_minutes: DayMinutes = _f(30, "Shorter overtime is ignored.")
    rounding: OvertimeRounding = _f(help_="Rounding of overtime minutes.", factory=OvertimeRounding)
    buckets: list[OvertimeBucket] = _f(
        help_="Multiplier tiers per day type, for example the first 2 hours at 1.5x, then 2x. Each day type needs a "
        "tier starting after 0 hours.", factory=_blank_buckets)
    rate_formula: Formula = _f(
        DEFAULT_OT_RATE_FORMULA, "Hourly base rate from full-period rates, so LOP does not lower it. "
        "SUM_TAG_HOURLY divides period amounts by the basis days and standard hours, per-day rates by standard "
        "hours, and uses per-hour rates as they are.")
    caps: OvertimeCaps = _f(help_="Caps per day, week, month and quarter, in hours.", factory=OvertimeCaps)
    cap_action: Literal["EXCEPTION", "TRUNCATE"] = _f("EXCEPTION", "What happens above a cap.")
    cross_midnight: Literal["SHIFT_START_DATE", "SPLIT_AT_MIDNIGHT"] = _f(
        "SHIFT_START_DATE", "Which day's bucket applies to work after midnight.")
    comp_off_allowed: bool = _f(False, "The employee may choose time off instead of pay.")
    comp_off_ratio: Ratio = _f(Decimal("1.0"), "Leave hours per overtime hour.")
    approval_chain_id: Ref | None = _f(None, "Approval chain for overtime requests; blank: the default chain.")

    @model_validator(mode="after")
    def _buckets(self) -> "OvertimePolicy":
        if not self.buckets:
            raise ValueError("buckets must not be empty")
        keys = [(b.day_type, b.after_hours) for b in self.buckets]
        if len(set(keys)) != len(keys):
            raise ValueError("buckets repeat a day type and starting hour")
        for day_type in OT_DAY_TYPES:
            if (day_type, Decimal(0)) not in keys:
                raise ValueError(f"buckets need a {day_type} tier starting after 0 hours")
        return self

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        found: list[ActivationProblem] = []
        if not self.exempt:
            for index, bucket in enumerate(self.buckets):
                if bucket.multiplier is None:
                    found.append(ActivationProblem(
                        f"buckets[{index}].multiplier", f"Enter the {bucket.day_type} overtime multiplier.",
                        "LEGAL_FLOOR"))
        if self.threshold_combination != "DAILY" and self.weekly_threshold_hours is None:
            found.append(ActivationProblem("weekly_threshold_hours", "Enter the weekly threshold."))
        return found


# --------------------------------------------------------------------------- 3.1 §5.6 task pay


class TaskPayPolicy(PolicyFields):
    attribution: Literal["APPROVAL_DATE", "COMPLETION_DATE"] = _f("APPROVAL_DATE", "Which period pays the task.")
    rate_source: Literal["TASK", "RATE_CARD", "TASK_THEN_RATE_CARD"] = _f(
        "TASK_THEN_RATE_CARD", "Where the rate comes from.")
    earning_component: Code = _f("TASK_EARNING", "Component of task earnings.")
    cap_per_period: Money | None = _f(None, "Maximum task earnings per period.")
    cap_action: Literal["EXCEPTION", "TRUNCATE_AND_CARRY", "TRUNCATE"] = _f("EXCEPTION", "What happens above the cap.")
    minimum_wage_floor: Literal["OFF", "WARN", "TOP_UP"] = _f(
        "WARN", "Comparison with the legal minimum wage; the country pack may force TOP_UP.", legal=True)
    allow_split: bool = _f(False, "One task may be paid to several assignees by percentage.")
    requires_evidence: bool = _f(False, "An attachment is required before approval.")
    clawback: Literal["NEXT_RUN", "MANUAL"] = _f("NEXT_RUN", "Reversal of a task that was already paid.")
    contractor_document: Literal["INVOICE", "SELF_BILLED_INVOICE"] = _f("INVOICE", "Document type for contractors.")
    approval_chain_id: Ref | None = _f(None, "Approval chain for tasks; blank: the default chain.")


# --------------------------------------------------------------------------- 3.1 §5.7 late penalties


class LatePenaltyPolicy(PolicyFields):
    free_late_marks: MarkCount = _f(2, "Late marks per period without penalty.")
    penalty_type: Literal["DAY_FRACTION", "FIXED_AMOUNT"] = _f("DAY_FRACTION", "LOP fraction or fixed amount.")
    late_marks_per_unit: MarksPerUnit = _f(3, "Every this many chargeable late marks…")
    unit_days: PenaltyDays = _f(Decimal("0.5"), "…equal this many days of LOP.")
    amount_per_late: Money | None = _f(
        None, "For FIXED_AMOUNT, in the pay group currency. Required to activate; none is assumed.", legal=True)
    count_early_exit: bool = _f(False, "Early exits count as late marks.")
    early_exit_grace_minutes: GraceMinutes = _f(15, "Grace before an early exit counts.")
    cap_days_per_period: CapDays = _f(Decimal("2.0"), "Maximum penalty LOP per period.")
    exempt_if_regularized: bool = _f(True, "An approved regularisation removes the mark.")

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        if self.penalty_type == "FIXED_AMOUNT" and (self.amount_per_late is None or self.amount_per_late.amount == 0):
            return [ActivationProblem("amount_per_late", "Enter the amount per late mark.", "LEGAL_FLOOR")]
        return []


# --------------------------------------------------------------------------- 3.1 §5.8 shift allowance


def _minutes_of(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


class NightWindow(_Strict):
    start: LocalTime = "22:00"
    end: LocalTime = "06:00"

    @model_validator(mode="after")
    def _distinct(self) -> "NightWindow":
        if self.start == self.end:
            raise ValueError("the night window must not be empty")
        return self

    @property
    def minutes(self) -> int:
        return (_minutes_of(self.end) - _minutes_of(self.start)) % 1440


class ShiftAllowancePolicy(PolicyFields):
    night_window: NightWindow = _f(help_="Local time window of night work.", factory=NightWindow)
    min_minutes_in_window: WindowMinutes = _f(
        120, "Minutes inside the night window needed for the allowance.")
    allowance_type: Literal["PER_SHIFT_AMOUNT", "PERCENT_OF_HOURLY", "FIXED_MONTHLY_IF_ANY"] = _f(
        "PER_SHIFT_AMOUNT", "An amount per qualifying shift, a percentage of the hourly rate per night hour, or a "
        "fixed monthly amount if any shift qualifies.")
    amount: Money | None = _f(None, "For PER_SHIFT_AMOUNT and FIXED_MONTHLY_IF_ANY.", legal=True)
    percent: AllowancePercent | None = _f(None, "For PERCENT_OF_HOURLY.", legal=True)
    weekend_shift_amount: Money | None = _f(None, "Per shift worked on a weekend.")
    component: Code = _f("SHIFT_ALLOWANCE", "Component of the allowance.")

    @model_validator(mode="after")
    def _rules(self) -> "ShiftAllowancePolicy":
        if self.min_minutes_in_window > self.night_window.minutes:
            raise ValueError("min_minutes_in_window is longer than the night window")
        if not _same_currency(self.amount, self.weekend_shift_amount):
            raise ValueError("amount and weekend_shift_amount must use the same currency")
        return self

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        if self.allowance_type == "PERCENT_OF_HOURLY":
            if self.percent is None:
                return [ActivationProblem("percent", "Enter the percentage of the hourly rate.", "LEGAL_FLOOR")]
        elif self.amount is None or self.amount.amount == 0:
            return [ActivationProblem("amount", "Enter the allowance amount.", "LEGAL_FLOOR")]
        return []


# --------------------------------------------------------------------------- 3.1 §5.9 incentives


class IncentiveTarget(_Strict):
    grade_id: Ref | None = None
    employee_id: Ref | None = None
    target: Measure

    @model_validator(mode="after")
    def _one(self) -> "IncentiveTarget":
        if (self.grade_id is None) == (self.employee_id is None):
            raise ValueError("a target is for exactly one grade or one employee")
        return self


class IncentivePlan(PolicyFields):
    measure_source: Literal["IMPORT", "TASKS"] = _f("IMPORT", "Where measures come from.")
    formula: Formula | None = _f(None, "Incentive amount from the measures and targets. Required to activate.")
    targets: list[IncentiveTarget] = _f(help_="Targets by grade or employee.", factory=list)
    payout_frequency: Literal["MONTHLY", "QUARTERLY", "ANNUAL"] = _f("MONTHLY", "How often incentives are paid.")
    cap: Money | None = _f(None, "Maximum incentive per payout.")
    clawback_window_days: ClawbackDays = _f(
        0, "Days after payout in which an incentive may be clawed back; 0 means never.")
    component: Code = _f("INCENTIVE", "Component of the incentive.")

    @model_validator(mode="after")
    def _unique(self) -> "IncentivePlan":
        keys = [(t.grade_id, t.employee_id) for t in self.targets]
        if len(set(keys)) != len(keys):
            raise ValueError("each grade or employee may have only one target")
        return self

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        """The formula is required and may read only the plan's ``inc.*`` variables in that namespace; task-based
        measures are not supported yet (decision I-22: no exactly-once consumption of task measures)."""
        from app.payroll import incentives

        found: list[ActivationProblem] = []
        if self.formula is None:
            found.append(ActivationProblem("formula", "Enter the incentive formula."))
        else:
            found.extend(ActivationProblem("formula", text) for text in incentives.formula_problems(self.formula))
        if self.measure_source not in incentives.SUPPORTED_MEASURE_SOURCES:
            found.append(ActivationProblem(
                "measure_source", "Task-based measures are not supported yet; use IMPORT (approved measures entered "
                "or imported as wage inputs)."))
        return found


# --------------------------------------------------------------------------- 3.1 §5.10 reimbursements


class ClaimType(_Strict):
    code: Code
    name: Name100
    limit_per_claim: Money | None = None
    limit_per_month: Money | None = None
    limit_per_year: Money | None = None
    receipt_required_above: Money | None = _f(None, "A receipt is required above this amount; blank: never.")
    taxability: Literal["TAXABLE", "EXEMPT", "PARTIAL", "UNCLASSIFIED"] = _f(
        "UNCLASSIFIED", "From the country pack; UNCLASSIFIED until a verified pack classifies it.")
    approval_chain_id: Ref | None = None

    @model_validator(mode="after")
    def _limits(self) -> "ClaimType":
        amounts = (self.limit_per_claim, self.limit_per_month, self.limit_per_year, self.receipt_required_above)
        if not _same_currency(*amounts):
            raise ValueError("the limits of a claim type must use one currency")
        ordered = [m.amount for m in (self.limit_per_claim, self.limit_per_month, self.limit_per_year) if m is not None]
        if ordered != sorted(ordered):
            raise ValueError("limits must not decrease from per claim to per month to per year")
        return self


class ReimbursementPolicy(PolicyFields):
    claim_types: list[ClaimType] = _f(help_="Claim types with their limits.", factory=list)
    submission_window_days: DayCount = _f(
        60, "Claims must be submitted within this many days of the expense.")
    duplicate_receipt_check: bool = _f(True, "Possible duplicate receipts are flagged for the approver.")

    @model_validator(mode="after")
    def _unique(self) -> "ReimbursementPolicy":
        codes = [c.code for c in self.claim_types]
        if len(set(codes)) != len(codes):
            raise ValueError("claim type codes must be unique")
        return self

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        return [] if self.claim_types else [ActivationProblem("claim_types", "Add at least one claim type.")]


# --------------------------------------------------------------------------- 3.1 §5.11 loans and advances


class LoanPolicy(PolicyFields):
    max_amount_formula: Formula | None = _f(
        None, "Maximum loan, for example 3 * MONTHLY_GROSS. Required to activate.")
    max_tenure_months: TenureMonths = _f(24, "Longest repayment period.")
    interest_rate_percent: Percent = _f(Decimal("0"), "Annual interest rate.")
    interest_method: Literal["FLAT", "REDUCING"] = _f("FLAT", "Interest on the original or the reducing balance.")
    max_instalment_percent_of_net: PositivePercent = _f(Decimal("50"), "Largest instalment as a share of net pay.")
    min_service_months: ServiceMonths = _f(6, "Service needed before applying.")
    max_active_loans: LoanCount = _f(1, "Loans an employee may hold at once.")
    recover_balance_on_exit: bool = _f(True, "The outstanding balance is recovered in the final settlement.")
    approval_chain_id: Ref | None = _f(None, "Approval chain for loans; blank: the default chain.")

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        if self.max_amount_formula is None:
            return [ActivationProblem("max_amount_formula", "Enter the maximum loan formula.")]
        return []


class AdvancePolicy(PolicyFields):
    max_percent_of_monthly_net: PositivePercent = _f(Decimal("50"), "Largest advance as a share of monthly net pay.")
    recover_in_runs: RecoveryRuns = _f(1, "Runs over which an advance is recovered.")
    max_per_year: PerYearCount = _f(2, "Advances per employee per year.")
    approval_chain_id: Ref | None = _f(None, "Approval chain for advances; blank: the default chain.")


# --------------------------------------------------------------------------- 3.1 §5.15 payslip template

PAYSLIP_SECTIONS: Final = (
    "HEADER", "EMPLOYEE", "ATTENDANCE_SUMMARY", "EARNINGS", "DEDUCTIONS", "REIMBURSEMENTS",
    "EMPLOYER_CONTRIBUTIONS", "TOTALS", "PAYMENT", "LEAVE_BALANCES", "TAX_SUMMARY", "FOOTER",
)
MANDATORY_PAYSLIP_SECTIONS: Final = frozenset({"HEADER", "EMPLOYEE", "EARNINGS", "DEDUCTIONS", "TOTALS", "PAYMENT", "FOOTER"})
SectionCode = Literal[
    "HEADER", "EMPLOYEE", "ATTENDANCE_SUMMARY", "EARNINGS", "DEDUCTIONS", "REIMBURSEMENTS",
    "EMPLOYER_CONTRIBUTIONS", "TOTALS", "PAYMENT", "LEAVE_BALANCES", "TAX_SUMMARY", "FOOTER",
]


class PayslipSection(_Strict):
    code: SectionCode
    visible: bool = True


class ComponentGroup(_Strict):
    name: Name60
    components: list[Code] = Field(min_length=1)


class SignatureBlock(_Strict):
    name: Name100 | None = None
    title: Name100 | None = None

    @model_validator(mode="after")
    def _some(self) -> "SignatureBlock":
        if self.name is None and self.title is None:
            raise ValueError("a signature block needs a name or a title")
        return self


class LocaleOverride(_Strict):
    footer_note: Note500 | None = None
    section_titles: dict[SectionCode, Name60] = Field(default_factory=dict)


def _all_sections() -> list[PayslipSection]:
    return [PayslipSection(code=code) for code in PAYSLIP_SECTIONS]


class PayslipTemplate(PolicyFields):
    layout: Literal["CLASSIC", "MODERN", "COMPACT"] = _f("CLASSIC", "Page layout.")
    sections: list[PayslipSection] = _f(
        help_="Every section once, in display order; mandatory sections cannot be hidden.", factory=_all_sections)
    component_groups: list[ComponentGroup] = _f(help_="Named groups of components on the payslip.", factory=list)
    footer_note: Note500 | None = _f(None, "Note printed in the footer.")
    signature_block: SignatureBlock | None = _f(None, "Signatory printed on the payslip.")
    locale_overrides: dict[LocaleCode, LocaleOverride] = _f(
        help_="Per-language footer note and section titles.", factory=dict)

    @model_validator(mode="after")
    def _rules(self) -> "PayslipTemplate":
        codes = [s.code for s in self.sections]
        if sorted(codes) != sorted(PAYSLIP_SECTIONS):
            raise ValueError("sections must list every payslip section exactly once")
        hidden = sorted(s.code for s in self.sections if not s.visible and s.code in MANDATORY_PAYSLIP_SECTIONS)
        if hidden:
            raise ValueError(f"mandatory sections cannot be hidden: {', '.join(hidden)}")
        names = [g.name for g in self.component_groups]
        members = [c for g in self.component_groups for c in g.components]
        if len(set(names)) != len(names) or len(set(members)) != len(members):
            raise ValueError("group names are unique and a component belongs to at most one group")
        return self


# --------------------------------------------------------------------------- 3.1 §5.14 approval chains

APPROVAL_MODULES: Final = (
    "leave", "payroll_run", "compensation", "wage_input", "overtime", "task", "reimbursement", "loan",
    "bank_change", "payment_batch", "off_cycle", "final_settlement",
)
APPROVER_TYPES: Final = (
    "primary_manager", "skip_level_manager", "department_head", "role", "finance_role", "specific_user",
)


class ApprovalStep(_Strict):
    approver_type: Literal[
        "primary_manager", "skip_level_manager", "department_head", "role", "finance_role", "specific_user"
    ]
    role_id: Ref | None = _f(None, "The role, for role steps.")
    specific_user_id: Ref | None = _f(None, "The user, for specific_user steps.")
    min_days: StepDays | None = _f(
        None, "Leave only: the step applies from this many days.")
    min_amount: Money | None = _f(None, "The step applies from this amount (in its currency).")

    @model_validator(mode="after")
    def _targets(self) -> "ApprovalStep":
        if (self.approver_type == "role") != (self.role_id is not None):
            raise ValueError("role steps, and only they, name a role_id")
        if (self.approver_type == "specific_user") != (self.specific_user_id is not None):
            raise ValueError("specific_user steps, and only they, name a specific_user_id")
        return self


class ApprovalChain(PolicyFields):
    module: Literal[
        "leave", "payroll_run", "compensation", "wage_input", "overtime", "task", "reimbursement", "loan",
        "bank_change", "payment_batch", "off_cycle", "final_settlement",
    ] = _f(help_="What the chain approves.")
    steps: list[ApprovalStep] = _f(help_="Approval steps in order.", factory=list)

    @model_validator(mode="after")
    def _steps(self) -> "ApprovalChain":
        if len(self.steps) > 10:
            raise ValueError("a chain has at most 10 steps")
        for step in self.steps:
            if step.min_days is not None and self.module != "leave":
                raise ValueError("min_days applies only to leave chains")
            if step.min_amount is not None and self.module == "leave":
                raise ValueError("leave chains use min_days, not min_amount")
        return self

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        return [] if self.steps else [ActivationProblem("steps", "Add at least one approval step.")]


# --------------------------------------------------------------------------- the type catalogue


@dataclass(frozen=True)
class PolicyTypeDef:
    """One policy type (3.1 §3.2): its gate switches (empty = always in force), grain and schema."""

    code: str
    label: str
    gate_switches: tuple[str, ...]
    grain: str
    model: type[PolicyFields]
    section: str
    spec: str
    assignable: bool = True


POLICY_TYPES: Final[Mapping[str, PolicyTypeDef]] = types.MappingProxyType({
    d.code: d for d in (
        PolicyTypeDef("ATTENDANCE_PAY_POLICY", "Attendance pay policy", ("payroll.attendance.enabled",), "PERIOD",
                      AttendancePayPolicy, "Attendance & LOP", "3.1 §5.2"),
        PolicyTypeDef("LEAVE_PAY_POLICY", "Leave pay policy",
                      ("payroll.leave.enabled", "payroll.leave.encashment.enabled"), "PERIOD",
                      LeavePayPolicy, "Leave pay", "3.1 §5.3"),
        PolicyTypeDef("HOLIDAY_PAY_POLICY", "Holiday pay policy",
                      ("payroll.holiday_pay.non_salaried.enabled", "payroll.holiday_work_premium.enabled",
                       "payroll.weekly_off_pay.enabled"), "DAY",
                      HolidayPayPolicy, "Holidays & weekly offs", "3.1 §5.4"),
        PolicyTypeDef("OVERTIME_POLICY", "Overtime policy", ("payroll.overtime.enabled",), "DAY",
                      OvertimePolicy, "Overtime", "3.1 §5.5"),
        PolicyTypeDef("TASK_PAY_POLICY", "Task pay policy", ("payroll.task_pay.enabled",), "DAY",
                      TaskPayPolicy, "Task pay", "3.1 §5.6"),
        PolicyTypeDef("LATE_PENALTY_POLICY", "Late penalty policy", ("payroll.late_penalty.enabled",), "DAY",
                      LatePenaltyPolicy, "Late & penalties", "3.1 §5.7"),
        PolicyTypeDef("SHIFT_ALLOWANCE_POLICY", "Shift allowance policy", ("payroll.shift_allowance.enabled",), "DAY",
                      ShiftAllowancePolicy, "Shifts & allowances", "3.1 §5.8"),
        PolicyTypeDef("INCENTIVE_PLAN", "Incentive plan", ("payroll.incentives.enabled",), "PERIOD",
                      IncentivePlan, "Bonus, incentives & arrears", "3.1 §5.9"),
        PolicyTypeDef("REIMBURSEMENT_POLICY", "Reimbursement policy", ("payroll.reimbursements.enabled",), "PERIOD",
                      ReimbursementPolicy, "Reimbursements", "3.1 §5.10"),
        PolicyTypeDef("LOAN_POLICY", "Loan policy", ("payroll.loans.enabled",), "PERIOD",
                      LoanPolicy, "Loans & advances", "3.1 §5.11"),
        PolicyTypeDef("ADVANCE_POLICY", "Advance policy", ("payroll.advances.enabled",), "PERIOD",
                      AdvancePolicy, "Loans & advances", "3.1 §5.11"),
        PolicyTypeDef("PAYSLIP_TEMPLATE", "Payslip template", (), "PERIOD",
                      PayslipTemplate, "Payslips & documents", "3.1 §5.15"),
        PolicyTypeDef("APPROVAL_CHAIN", "Approval chain", (), "PERIOD",
                      ApprovalChain, "Approvals & controls", "3.1 §5.14", assignable=False),
    )
})


def policy_type(code: str) -> PolicyTypeDef:
    try:
        return POLICY_TYPES[code]
    except KeyError:
        raise PolicyValidationError("UNKNOWN_POLICY_TYPE", f"{code!r} is not a policy type") from None


def _path(loc: Sequence[Any]) -> str:
    text = ""
    for part in loc:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text


def _parse(code: str, fields: Mapping[str, Any]) -> PolicyFields:
    ptype = policy_type(code)
    if not isinstance(fields, Mapping):
        raise PolicyValidationError("INVALID_FIELDS", "policy fields must be an object")
    try:
        return ptype.model.model_validate(dict(fields))
    except ValidationError as error:
        errors = [
            {"field": _path(e["loc"]), "message": str(e["msg"]).removeprefix("Value error, ")}
            for e in error.errors()
        ]
        first = errors[0]
        where = f"{first['field']}: " if first["field"] else ""
        raise PolicyValidationError(
            "INVALID_FIELDS", f"{ptype.label}: {where}{first['message']}", field=first["field"] or None,
            details={"errors": errors},
        ) from None


def activation_problems(
    code: str, fields: Mapping[str, Any], *, enabled_switches: Collection[str] | None = None
) -> list[ActivationProblem]:
    """Fields still blank for activation, as data (setup checklist). ``None``: assume every switch ON."""
    model = _parse(code, fields)
    return model.activation_problems(None if enabled_switches is None else frozenset(enabled_switches))


def validate_fields(
    code: str,
    fields: Mapping[str, Any],
    *,
    for_activation: bool = False,
    enabled_switches: Collection[str] | None = None,
) -> dict[str, Any]:
    """The normalized JSON fields of a policy version, with every default filled in.

    Raises ``PolicyValidationError`` (``INVALID_FIELDS``) for wrong types, values or combinations.
    With ``for_activation`` it also raises ``FIELD_REQUIRED_FOR_ACTIVATION`` while a field that
    activation needs is blank; ``enabled_switches`` limits that check to the features whose gate
    switches are ON (``None`` assumes all of them).
    """
    model = _parse(code, fields)
    if for_activation:
        problems = model.activation_problems(None if enabled_switches is None else frozenset(enabled_switches))
        if problems:
            label = POLICY_TYPES[code].label
            raise PolicyValidationError(
                "FIELD_REQUIRED_FOR_ACTIVATION",
                f"{label} cannot be activated: " + " ".join(f"{p.field}: {p.message}" for p in problems),
                field=problems[0].field,
                details={"fields": [{"field": p.field, "message": p.message, "legal_sensitivity": p.legal_sensitivity}
                                    for p in problems]},
            )
    return model.model_dump(mode="json")


def default_fields(code: str) -> dict[str, Any]:
    """The fields of a new draft of ``code`` (every default; APPROVAL_CHAIN needs its module)."""
    return validate_fields(code, {})


# --------------------------------------------------------------------------- hashing


def _canonical(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PolicyValidationError("INVALID_FIELDS", "decimals in policy fields must be finite")
        return decimal_str(value)
    if isinstance(value, float):
        raise PolicyValidationError("INVALID_FIELDS", "policy fields must not contain floating-point numbers")
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise PolicyValidationError("INVALID_FIELDS", "policy field names must be strings")
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    raise PolicyValidationError("INVALID_FIELDS", f"unsupported value of type {type(value).__name__}")


def canonical_json(fields: Mapping[str, Any]) -> str:
    """Sorted keys, no whitespace, decimals as canonical strings, UTF-8 text."""
    return json.dumps(_canonical(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def policy_hash(fields: Mapping[str, Any]) -> str:
    """SHA-256 (hex) of the canonical JSON of policy fields (3.1 §2.6)."""
    if not isinstance(fields, Mapping):
        raise PolicyValidationError("INVALID_FIELDS", "policy fields must be an object")
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- assignment rules

CRITERIA_DIMENSIONS: Final = (
    ("legal_entity_ids", "legal_entity_id"),
    ("branch_ids", "branch_id"),
    ("pay_group_ids", "pay_group_id"),
    ("department_ids", "department_id"),
    ("grade_ids", "grade_id"),
    ("employment_types", "employment_type"),
    ("designation_ids", "designation_id"),
    ("employee_ids", "employee_id"),
)


class AssignmentCriteria(_Strict):
    """Who a rule covers: every given dimension must match (any listed value); ``everyone`` is the catch-all."""

    legal_entity_ids: IdList | None = None
    branch_ids: IdList | None = None
    pay_group_ids: IdList | None = None
    department_ids: IdList | None = None
    grade_ids: IdList | None = None
    employment_types: CodeList | None = None
    designation_ids: IdList | None = None
    employee_ids: IdList | None = None
    everyone: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _everyone(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        given = [name for name, _ in CRITERIA_DIMENSIONS if data.get(name) is not None]
        flag = data.get("everyone")
        if given and flag is True:
            raise ValueError("everyone cannot be combined with other criteria")
        if not given and flag is False:
            raise ValueError("criteria must name at least one dimension, or everyone")
        return {**data, "everyone": not given}

    def matches(self, employee: "EmployeeAttributes") -> bool:
        if self.everyone:
            return True
        for name, attribute in CRITERIA_DIMENSIONS:
            allowed = getattr(self, name)
            if allowed is not None and getattr(employee, attribute) not in allowed:
                return False
        return True

    def may_overlap(self, other: "AssignmentCriteria") -> bool:
        """Whether some employee could match both (dimensions named by both must share a value)."""
        for name, _ in CRITERIA_DIMENSIONS:
            mine, theirs = getattr(self, name), getattr(other, name)
            if mine is not None and theirs is not None and not set(mine) & set(theirs):
                return False
        return True


def parse_criteria(criteria: Mapping[str, Any] | AssignmentCriteria) -> AssignmentCriteria:
    if isinstance(criteria, AssignmentCriteria):
        return criteria
    if not isinstance(criteria, Mapping):
        raise PolicyValidationError("INVALID_CRITERIA", "criteria must be an object")
    try:
        return AssignmentCriteria.model_validate(dict(criteria))
    except ValidationError as error:
        first = error.errors()[0]
        raise PolicyValidationError(
            "INVALID_CRITERIA", f"{_path(first['loc']) or 'criteria'}: {str(first['msg']).removeprefix('Value error, ')}",
            field=_path(first["loc"]) or None,
        ) from None


@dataclass(frozen=True)
class EmployeeAttributes:
    """An employee's assignment on one date, as the criteria see it."""

    employee_id: int
    legal_entity_id: int | None = None
    branch_id: int | None = None
    pay_group_id: int | None = None
    department_id: int | None = None
    grade_id: int | None = None
    employment_type: str | None = None
    designation_id: int | None = None


@dataclass(frozen=True)
class AssignmentRule:
    """An eligibility rule of one policy: criteria, priority (higher wins) and effective window."""

    rule_id: int | str
    policy_id: int | str | None
    priority: int
    criteria: AssignmentCriteria = field(hash=False)
    effective_from: date | None = None
    effective_to: date | None = None
    status: str = "ACTIVE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "criteria", parse_criteria(self.criteria))
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not 0 <= self.priority <= 1_000_000:
            raise PolicyValidationError("INVALID_RULE", "priority must be a whole number from 0 to 1000000")
        if self.status not in ("ACTIVE", "INACTIVE"):
            raise PolicyValidationError("INVALID_RULE", "status must be ACTIVE or INACTIVE")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise PolicyValidationError("INVALID_RULE", "effective_to is before effective_from")

    def active_on(self, day: date | None) -> bool:
        if self.status != "ACTIVE":
            return False
        if day is None:
            return True
        return (self.effective_from is None or self.effective_from <= day) and (
            self.effective_to is None or day <= self.effective_to)

    def overlaps_in_time(self, other: "AssignmentRule") -> bool:
        starts = [d for d in (self.effective_from, other.effective_from) if d is not None]
        ends = [d for d in (self.effective_to, other.effective_to) if d is not None]
        return not starts or not ends or max(starts) <= min(ends)


@dataclass(frozen=True)
class AssignmentResolution:
    """RESOLVED (one rule), NO_POLICY (pre-check exception, EC-CFG-07) or AMBIGUOUS (EC-CFG-05)."""

    status: str
    rule: AssignmentRule | None
    candidates: tuple[AssignmentRule, ...]
    matched: tuple[AssignmentRule, ...]

    @property
    def policy_id(self) -> int | str | None:
        return self.rule.policy_id if self.rule is not None else None


def resolve_assignment(
    rules: Iterable[AssignmentRule], employee: EmployeeAttributes, *, on: date | None = None
) -> AssignmentResolution:
    """The rule that applies to ``employee`` on ``on``: the highest priority among matching rules."""
    matched = tuple(sorted(
        (r for r in rules if r.active_on(on) and r.criteria.matches(employee)),
        key=lambda r: (-r.priority, str(r.rule_id)),
    ))
    if not matched:
        return AssignmentResolution("NO_POLICY", None, (), ())
    top = tuple(r for r in matched if r.priority == matched[0].priority)
    if len(top) > 1:
        return AssignmentResolution("AMBIGUOUS", None, top, matched)
    return AssignmentResolution("RESOLVED", top[0], top, matched)


@dataclass(frozen=True)
class PriorityConflict:
    priority: int
    rule_ids: tuple[int | str, ...]
    employee_ids: tuple[int, ...]


def find_priority_conflicts(
    rules: Iterable[AssignmentRule], employees: Iterable[EmployeeAttributes], *, on: date | None = None
) -> list[PriorityConflict]:
    """Pairs of same-priority rules matching the same employee at the same time (V-12, EC-CFG-05).

    Without employees, two rules conflict when they certainly cover the same people: both catch-all,
    or identical criteria, with overlapping effective windows.
    """
    active = [r for r in rules if r.active_on(on)]
    found: dict[tuple[int, tuple[int | str, int | str]], set[int]] = {}
    people = list(employees)
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if a.priority != b.priority or (on is None and not a.overlaps_in_time(b)):
                continue
            pair = tuple(sorted((a.rule_id, b.rule_id), key=str))
            if people:
                hits = {e.employee_id for e in people if a.criteria.matches(e) and b.criteria.matches(e)}
            else:
                hits = set() if a.criteria != b.criteria else {-1}
            if hits:
                found.setdefault((a.priority, pair), set()).update(hits)
    return [
        PriorityConflict(priority, pair, tuple(sorted(e for e in ids if e >= 0)))
        for (priority, pair), ids in sorted(found.items(), key=lambda item: (-item[0][0], [str(x) for x in item[0][1]]))
    ]


def check_assignment_rules(
    rules: Iterable[AssignmentRule], employees: Iterable[EmployeeAttributes] = (), *, on: date | None = None
) -> None:
    """Reject a rule set in which same-priority rules match the same employee (EC-CFG-05)."""
    conflicts = find_priority_conflicts(rules, employees, on=on)
    if not conflicts:
        return
    first = conflicts[0]
    who = f" for employees {', '.join(map(str, first.employee_ids))}" if first.employee_ids else ""
    raise PolicyValidationError(
        "PRIORITY_CONFLICT",
        f"Rules {' and '.join(map(str, first.rule_ids))} have the same priority ({first.priority}) and match the "
        f"same people{who}. Give one of them a different priority.",
        details={"conflicts": [
            {"priority": c.priority, "rule_ids": list(c.rule_ids), "employee_ids": list(c.employee_ids)}
            for c in conflicts
        ]},
    )


# --------------------------------------------------------------------------- starter policies


@dataclass(frozen=True)
class LeaveTypeInput:
    code: str
    is_paid: bool
    name: str | None = None


def normalize_leave_types(leave_types: Iterable[Any]) -> tuple[LeaveTypeInput, ...]:
    """Leave types as mappings or objects with ``code`` and ``is_paid`` (and optionally ``name``)."""
    if isinstance(leave_types, (str, bytes, Mapping)):
        raise PolicyValidationError("INVALID_LEAVE_TYPE", "leave_types must be a list of leave types")
    found: list[LeaveTypeInput] = []
    for item in leave_types:
        get = item.get if isinstance(item, Mapping) else lambda name, item=item: getattr(item, name, None)
        code, is_paid, name = get("code"), get("is_paid"), get("name")
        if not isinstance(code, str) or not _LEAVE_CODE_RE.fullmatch(code):
            raise PolicyValidationError("INVALID_LEAVE_TYPE", f"{code!r} is not a leave type code")
        if not isinstance(is_paid, bool):
            raise PolicyValidationError("INVALID_LEAVE_TYPE", f"leave type {code}: is_paid must be true or false")
        found.append(LeaveTypeInput(code, is_paid, name if isinstance(name, str) else None))
    codes = [lt.code for lt in found]
    if len(set(codes)) != len(codes):
        raise PolicyValidationError("INVALID_LEAVE_TYPE", "leave type codes must be unique")
    return tuple(found)


@dataclass(frozen=True)
class StarterPolicy:
    """A catch-all (everyone, priority 0) policy written when a profile is applied."""

    type: str
    name: str
    fields: Mapping[str, Any] = field(hash=False)
    status: str
    priority: int = 0
    criteria: Mapping[str, Any] = field(default_factory=lambda: {"everyone": True}, hash=False)
    policy_hash: str | None = None
    decision_required: bool = False
    review_items: tuple[str, ...] = ()
    note: str | None = None


def _legacy_multiplier(value: object) -> Decimal:
    """The legacy ``overtime`` setting (stored as a float by the old settings service)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PolicyValidationError("LEGACY_VALUE_INVALID", "the legacy overtime multiplier is not a number")
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        raise PolicyValidationError("LEGACY_VALUE_INVALID", "the legacy overtime multiplier is not a number") from None
    if not number.is_finite() or not Decimal(0) < number <= Decimal(10):
        raise PolicyValidationError("LEGACY_VALUE_INVALID", "the legacy overtime multiplier must be above 0 and at most 10")
    return number


def _leave_rows(leave_types: Sequence[LeaveTypeInput]) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    rows: list[dict[str, Any]] = []
    review: list[str] = []
    for lt in leave_types:
        rows.append({"leave_type_code": lt.code, "pay_treatment": "FULL" if lt.is_paid else "UNPAID", "encashable": False})
        label = f"{lt.code} ({lt.name})" if lt.name else lt.code
        if any(word in f"{lt.code} {lt.name or ''}".lower() for word in STATUTORY_REVIEW_WORDS):
            review.append(f"Leave type {label} is {'paid in full' if lt.is_paid else 'unpaid'}; check whether the "
                          "law requires statutory pay for it.")
    return rows, tuple(review)


def starter_policies(
    profile: str = "STARTER_V1",
    *,
    leave_types: Iterable[Any] = (),
    legacy_values: Mapping[str, Any] | None = None,
) -> list[StarterPolicy]:
    """The catch-all policies of a configuration profile (decisions I-13, I-14).

    A policy is ACTIVE (validated for activation, with its hash) when one of its gate switches is
    ON in the profile, or its type is always in force, and DRAFT otherwise. LEAVE_PAY_POLICY rows
    come from ``leave_types`` (paid: FULL, else UNPAID; never STATUTORY). For ``LEGACY_V1`` the
    legacy ``overtime`` multiplier, when given, is copied into the draft overtime policy (EC-MIG-04).
    """
    if profile not in PROFILES:
        raise PolicyValidationError("UNKNOWN_PROFILE", f"{profile!r} is not a configuration profile")
    prof: Profile = PROFILES[profile]
    leave = normalize_leave_types(leave_types)
    from app.payroll.settings_registry import profile_values

    try:
        values = profile_values(profile, leave_types=leave, legacy_values=legacy_values)
    except SettingValidationError as error:
        raise PolicyValidationError(error.code, error.message) from None
    switched_on = frozenset(k for k, v in values.items() if v is True and REGISTRY[k].type == "bool")
    result: list[StarterPolicy] = []
    for spec in prof.policies:
        ptype = POLICY_TYPES[spec.type]
        fields = dict(spec.fields)
        review: tuple[str, ...] = ()
        if spec.rows_from_leave_types:
            fields["rows"], review = _leave_rows(leave)
        if spec.type == "OVERTIME_POLICY" and legacy_values and "overtime" in legacy_values and prof.legacy_keys:
            multiplier = decimal_str(_legacy_multiplier(legacy_values["overtime"]))
            fields["buckets"] = [
                {"day_type": day_type, "after_hours": "0", **({"multiplier": multiplier} if day_type == "WORKDAY" else {})}
                for day_type in OT_DAY_TYPES
            ]
        active = not ptype.gate_switches or any(g in switched_on for g in ptype.gate_switches)
        normalized = validate_fields(spec.type, fields, for_activation=active, enabled_switches=switched_on)
        result.append(StarterPolicy(
            type=spec.type,
            name=spec.name,
            fields=normalized,
            status="ACTIVE" if active else "DRAFT",
            policy_hash=policy_hash(normalized) if active else None,
            decision_required=any(g in prof.decisions for g in ptype.gate_switches),
            review_items=review,
            note=spec.note,
        ))
    return result


# --------------------------------------------------------------------------- metadata for the frontend


def _describe(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    if origin is Annotated:
        base, *extras = get_args(annotation)
        meta = next((m for m in extras if isinstance(m, FieldMeta)), None)
        return meta.json() if meta is not None else _describe(base)
    if origin in (Union, types.UnionType):
        options = [a for a in get_args(annotation) if a is not type(None)]
        described = _describe(options[0]) if len(options) == 1 else {"kind": "union", "options": [_describe(o) for o in options]}
        return {**described, "nullable": True} if type(None) in get_args(annotation) else described
    if origin is Literal:
        return {"kind": "enum", "choices": list(get_args(annotation))}
    if origin is list:
        return {"kind": "list", "item": _describe(get_args(annotation)[0])}
    if origin is dict:
        key, value = get_args(annotation)
        return {"kind": "map", "key": _describe(key), "value": _describe(value)}
    if annotation is bool:
        return {"kind": "bool"}
    if annotation is str:
        return {"kind": "text"}
    if annotation is Money:
        return {"kind": "money"}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {"kind": "object", "fields": describe_model(annotation)}
    raise TypeError(f"undescribed field type {annotation!r}")


def describe_model(model: type[BaseModel]) -> list[dict[str, Any]]:
    """Field metadata of a policy model: name, type, default (JSON), help, legal sensitivity."""
    described: list[dict[str, Any]] = []
    for name, info in model.model_fields.items():
        annotation = Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
        entry: dict[str, Any] = {"name": name, **_describe(annotation)}
        if info.is_required():
            entry["required"] = True
        else:
            default = info.default_factory() if info.default_factory is not None else info.default
            entry["default"] = TypeAdapter(annotation).dump_python(default, mode="json")
        if info.description:
            entry["help"] = info.description
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        entry["legal_sensitivity"] = extra.get("legal_sensitivity", "NONE")
        described.append(entry)
    return described


def policy_type_metadata() -> list[dict[str, Any]]:
    """Every policy type with its gates, grain, section and field metadata (generated TypeScript)."""
    return [
        {
            "code": d.code,
            "label": d.label,
            "gateSwitches": list(d.gate_switches),
            "grain": d.grain,
            "section": d.section,
            "spec": d.spec,
            "assignable": d.assignable,
            "fields": describe_model(d.model),
        }
        for d in POLICY_TYPES.values()
    ]


def _check_catalogue() -> None:
    for d in POLICY_TYPES.values():
        if d.grain not in GRAINS:
            raise ValueError(f"{d.code}: unknown grain {d.grain}")
        for gate in d.gate_switches:
            if gate not in REGISTRY or REGISTRY[gate].type != "bool":
                raise ValueError(f"{d.code}: unknown gate switch {gate}")
            if REGISTRY[gate].ui.get("policy_type") != d.code:
                raise ValueError(f"{d.code}: switch {gate} names another policy type")


_check_catalogue()
