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


# --------------------------------------------------------------------------- AS spine §6.2.1 attendance capture
# Registered here by JC-AS-22 (attendance owns the semantics: docs/attendance-shift/04-attendance/02 and spine §6.2.1);
# write gate attendance:manage_policy (``write_permission``), never in payroll snapshots.

CAPTURE_SOURCE_CHOICES = ("WEB", "MOBILE", "KIOSK", "DEVICE", "IMPORT", "SUPERVISOR")
KIOSK_METHOD_CHOICES = ("FACE", "PIN", "QR", "CARD")
CaptureSources = Annotated[tuple[str, ...], ChoiceSetRule(choices=CAPTURE_SOURCE_CHOICES, min_items=1)]
KioskMethods = Annotated[tuple[str, ...], ChoiceSetRule(choices=KIOSK_METHOD_CHOICES, min_items=1)]
LocationMode = Literal["OFF", "OPTIONAL", "REQUIRED"]
GeofenceMode = Literal["OFF", "FLAG", "REQUIRE_REASON", "BLOCK"]
Text100 = Annotated[str, TextRule(max_length=100)]


class CaptureGps(_Strict):
    preferred_accuracy_m: Annotated[int, IntRule(minimum=5, maximum=1000, unit="m")] = 50
    acceptable_accuracy_m: Annotated[int, IntRule(minimum=10, maximum=5000, unit="m")] = 100
    timeout_seconds: Annotated[int, IntRule(minimum=5, maximum=60, unit="seconds")] = 20
    max_attempts: Annotated[int, IntRule(minimum=1, maximum=5)] = 3
    max_fix_age_seconds: Annotated[int, IntRule(minimum=30, maximum=600, unit="seconds")] = 120

    @model_validator(mode="after")
    def _order(self) -> "CaptureGps":
        if self.acceptable_accuracy_m < self.preferred_accuracy_m:
            raise ValueError("the acceptable accuracy cannot be finer than the preferred accuracy")
        return self


class CaptureTracking(_Strict):
    enabled: bool = False
    interval_minutes: Annotated[int, IntRule(minimum=5, maximum=480, unit="minutes")] = 120
    grace_minutes: Annotated[int, IntRule(minimum=1, maximum=60, unit="minutes")] = 15
    pause_on_break: bool = True
    movement_threshold_m: Annotated[int, IntRule(minimum=50, maximum=50000, unit="m")] = 1000


class SupervisorMuster(_Strict):
    enabled: bool = False
    photo_required: bool = False
    geofence_mode: GeofenceMode = "OFF"
    offline_allowed: bool = False


class AttendanceCapturePolicy(PolicyFields):
    """Spine §6.2.1. ``profile`` is set by ``attendance.v2_prepare`` (LEGACY_V1) or the editor (STANDARD), never typed."""

    profile: Literal["LEGACY_V1", "STANDARD"] = _f("STANDARD", "LEGACY_V1 follows the legacy attendance settings.")
    allowed_sources: CaptureSources = _f(("WEB", "MOBILE"), "Where people may punch from.")
    shift_required: bool = _f(False, "Punches need a scheduled shift.")
    capture_required: bool = _f(True, "False: the covered people need not punch (presence is presumed).")
    face_on_punch: bool = _f(False, "Face match at check in and out (no liveness check).")
    face_on_break: bool = _f(False, "Face match at break start and end (no liveness check).")
    punch_photo: bool = _f(False, "A photo is taken with each punch.")
    kiosk_methods: KioskMethods = _f(("PIN",), "How people identify at a kiosk.")
    dpia_reference: Text100 | None = _f(None, "Privacy impact assessment reference, required with any face option.")
    location_mode: LocationMode = _f("OFF", "Location at check in and out.")
    location_on_breaks: LocationMode = _f("OFF", "Location at break start and end.")
    location_failure_action: Literal["ALLOW_WITH_REASON", "BLOCK"] = _f(
        "ALLOW_WITH_REASON", "Without a usable location: continue with a reason, or refuse.")
    reject_low_accuracy: bool = _f(False, "Refuse readings coarser than the acceptable accuracy.")
    gps: CaptureGps = _f(help_="Browser location settings.", factory=CaptureGps)
    geofence_mode: GeofenceMode = _f("OFF", "What happens outside the allowed sites.")
    allowed_site_ids: IdList | None = _f(None, "Allowed sites; empty = the active sites of the person's branch.")
    ip_mode: Literal["OFF", "FLAG", "BLOCK"] = _f("OFF", "Office network check.")
    qr_mode: Literal["OFF", "REQUIRED"] = _f("OFF", "A site QR poster must be scanned.")
    tracking: CaptureTracking = _f(help_="Location during a live session only.", factory=CaptureTracking)
    multiple_sessions_allowed: bool = _f(True, "Several sessions per shift date.")
    allow_multiple_breaks: bool = _f(True, "More than one break per session.")
    break_open_at_punch_out: Literal["CONFIRM_AUTO_CLOSE", "BLOCK"] = _f(
        "CONFIRM_AUTO_CLOSE", "Check out with an open break: confirm and close it, or refuse.")
    min_session_minutes: Annotated[int, IntRule(minimum=1, maximum=60, unit="minutes")] = _f(
        1, "Minimum time between check in and check out.")
    auto_close_mode: Literal["MAX_DURATION", "SHIFT_END_PLUS"] = _f("SHIFT_END_PLUS", "How a forgotten check out closes.")
    max_session_hours: Annotated[int, IntRule(minimum=4, maximum=24, unit="hours")] = _f(16, "Longest session.")
    auto_close_after_minutes: Annotated[int, IntRule(minimum=30, maximum=720, unit="minutes")] = _f(
        240, "Close this long after the shift end (SHIFT_END_PLUS).")
    infer_breaks_from_punches: bool = _f(False, "Device punches between IN and OUT are breaks.")
    max_break_gap_minutes: Annotated[int, IntRule(minimum=5, maximum=240, unit="minutes")] = _f(
        90, "Longest gap read as a break.")
    offline_punches: Literal["OFF", "ALLOW"] = _f("OFF", "Queue punches while offline (AS-M12).")
    offline_max_age_hours: Annotated[int, IntRule(minimum=1, maximum=168, unit="hours")] = _f(
        24, "Oldest offline punch accepted.")
    supervisor_muster: SupervisorMuster = _f(help_="Crew punches by a supervisor.", factory=SupervisorMuster)

    @model_validator(mode="after")
    def _combinations(self) -> "AttendanceCapturePolicy":
        face = self.face_on_punch or self.face_on_break or "FACE" in self.kiosk_methods
        if face and self.profile == "STANDARD" and not self.dpia_reference:
            raise ValueError("dpia_reference: enter the privacy impact assessment reference to use face matching")
        # EC AS-GEO-05 (V-AS-13): a blocking geofence needs a location fix at every punch.
        if self.geofence_mode in ("REQUIRE_REASON", "BLOCK") and self.location_mode != "REQUIRED":
            raise ValueError("location_mode: a geofence that blocks needs location required")
        return self


DayMinutesValue = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(1440), max_places=2, unit="minutes")]
HalfDayLimitMinutes = Annotated[int, IntRule(minimum=1, maximum=720, unit="minutes")]
WholePercent = Annotated[int, IntRule(minimum=0, maximum=100, unit="%")]
WaiverMode = Literal["NONE", "NET_GE_EXPECTED", "GROSS_GE_EXPECTED"]
ROUNDING_INCREMENTS = (1, 5, 6, 10, 15)


class AttendanceRounding(_Strict):
    mode: Literal["NONE", "NEAREST", "UP", "DOWN"] = "NONE"
    increment_minutes: Annotated[int, IntRule(minimum=1, maximum=15, unit="minutes")] = 1

    @model_validator(mode="after")
    def _increment(self) -> "AttendanceRounding":
        if self.increment_minutes not in ROUNDING_INCREMENTS:
            raise ValueError(f"increment_minutes must be one of {', '.join(map(str, ROUNDING_INCREMENTS))}")
        return self


class AttendanceBreakRule(_Strict):
    """One BreakRule (AS 03-shifts/01 §5.1) of ``break_defaults``: the breaks of a custom-time roster entry without a
    shift (§5.4). The engine checks the combinations (``shift_engine.validate_break_defaults``)."""

    kind: Literal["LUNCH", "REST", "PERSONAL", "OTHER"] = "LUNCH"
    mode: Literal["PUNCHED", "FIXED_WINDOW", "FLOATING_WINDOW", "AUTO_DEDUCT"] = "AUTO_DEDUCT"
    minutes: Annotated[int, IntRule(minimum=1, maximum=240, unit="minutes")] = 30
    paid: bool = False
    start: LocalTime | None = None
    end: LocalTime | None = None
    window_start: LocalTime | None = None
    window_end: LocalTime | None = None
    next_day: bool = False
    auto_deduct_after_minutes: Annotated[int, IntRule(minimum=0, maximum=1440, unit="minutes")] | None = None


class AttendanceRulesPolicy(PolicyFields):
    """Spine §6.2.2; AS 04-attendance/02 §4.3. Read by the day evaluation for STANDARD days only (payroll reads the
    evaluations, never this policy: ``SKIPPED_POLICY_TYPES``, JC-AS-18)."""

    profile: Literal["LEGACY_V1", "STANDARD"] = _f("STANDARD", "LEGACY_V1 follows payroll's attendance pay policy.")
    day_basis: Literal["PRESENCE", "HOURS"] = _f("HOURS", "A half is present by any work, or by worked hours.")
    hours_basis: Literal["NET", "GROSS"] = _f("NET", "Hours compared without or with unpaid breaks.")
    threshold_mode: Literal["ABSOLUTE", "PERCENT_OF_EXPECTED"] = _f(
        "PERCENT_OF_EXPECTED", "Fixed minutes, or a share of the expected minutes.")
    full_day_min_minutes: DayMinutesValue = _f(Decimal(480), "Minutes for a full day (ABSOLUTE).")
    half_day_min_minutes: DayMinutesValue = _f(Decimal(240), "Minutes for a half day (ABSOLUTE).")
    full_day_min_percent: WholePercent = _f(90, "Share of the expected minutes for a full day.")
    half_day_min_percent: WholePercent = _f(50, "Share of the expected minutes for a half day.")
    work_time_basis: Literal["SUM_OF_SESSIONS", "FIRST_IN_LAST_OUT"] = _f(
        "SUM_OF_SESSIONS", "Gaps between sessions count as work only with first in / last out.")
    absent_marking: Literal["AUTO_ABSENT", "LEAVE_UNMARKED"] = _f(
        "AUTO_ABSENT", "An unworked expected half is absent, or left unmarked for leave.")
    non_working_day_min_minutes: DayMinutesValue | None = _f(
        None, "Least work that makes a holiday or weekly off a worked day; empty = the half-day minimum.")
    grace_in_minutes: GraceMinutes | None = _f(None, "Grace after the start; empty = the grace-time setting.")
    grace_out_minutes: GraceMinutes = _f(0, "Grace before the end.")
    flexible_late_basis: Literal["NONE", "CORE_START"] = _f("NONE", "Flexible shifts are late only against core time.")
    late_half_day_after_minutes: HalfDayLimitMinutes | None = _f(
        None, "Later than this, the first half is lost instead of a late mark.")
    early_half_day_before_minutes: HalfDayLimitMinutes | None = _f(
        None, "Earlier than this, the last half is lost instead of an early mark.")
    late_waiver: WaiverMode = _f("NONE", "Excuse a late mark when the day's hours are complete.")
    early_waiver: WaiverMode = _f("NONE", "Excuse an early mark when the day's hours are complete.")
    short_hours_enabled: bool = _f(False, "Mark days shorter than expected.")
    short_tolerance_minutes: GraceMinutes = _f(0, "Short minutes tolerated before a short mark.")
    break_attestation: bool = _f(False, "Ask at check out whether the break was taken.")
    break_defaults: list[AttendanceBreakRule] = _f(
        help_="Breaks of roster entries with custom times and no shift (at most 4).", factory=list)
    rounding: AttendanceRounding = _f(help_="Rounding of worked time (never breaks, never marks).",
                                      factory=AttendanceRounding)
    open_day_start_time: LocalTime = _f("04:00", "Start of the attendance day when there is no shift.")
    ot_early_arrival: Literal["NEVER", "WITH_APPROVAL", "ALWAYS"] = _f(
        "NEVER", "Whether time before the shift counts toward overtime.")
    ot_post_shift_buffer_minutes: Annotated[int, IntRule(minimum=0, maximum=120, unit="minutes")] = _f(
        0, "Time after the shift up to this is not overtime.")
    ot_requires_pre_approval: bool = _f(False, "Overtime without approval raises an exception.")
    time_bank_enabled: bool = _f(False, "Not available yet (JC-AS-47).")

    @model_validator(mode="after")
    def _combinations(self) -> "AttendanceRulesPolicy":
        if self.half_day_min_minutes > self.full_day_min_minutes:
            raise ValueError("half_day_min_minutes: the half-day minimum cannot exceed the full-day minimum")
        if self.half_day_min_percent > self.full_day_min_percent:
            raise ValueError("half_day_min_percent: the half-day share cannot exceed the full-day share")
        if (self.late_half_day_after_minutes is not None and self.grace_in_minutes is not None
                and self.late_half_day_after_minutes <= self.grace_in_minutes):
            raise ValueError("late_half_day_after_minutes: must be greater than the grace in")
        if self.time_bank_enabled:
            raise ValueError("time_bank_enabled: Time bank is not available yet")
        if self.break_defaults:
            from app.services import shift_engine

            issues = shift_engine.validate_break_defaults([r.model_dump() for r in self.break_defaults])
            if issues:
                first = issues[0]
                raise ValueError(f"{first.field.replace('break_rules', 'break_defaults')}: {first.message}")
        return self


ATTENDANCE_REQUEST_KINDS = ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "VOID_SESSION", "MARK_EXCUSE",
                            "STATUS_OVERRIDE")
SELF_REQUEST_KINDS = ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "VOID_SESSION", "MARK_EXCUSE")
DAY_RESULT_VALUES = ("PRESENT", "HALF_DAY", "ABSENT", "MISSING_PUNCH", "UNMARKED", "PENDING", "OFF")
WEEKDAY_CHOICES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
RequestKinds = Annotated[tuple[str, ...], ChoiceSetRule(choices=ATTENDANCE_REQUEST_KINDS)]
SelfKinds = Annotated[tuple[str, ...], ChoiceSetRule(choices=SELF_REQUEST_KINDS)]
DayResultSet = Annotated[tuple[str, ...], ChoiceSetRule(choices=DAY_RESULT_VALUES)]
WeekdaySet = Annotated[tuple[str, ...], ChoiceSetRule(choices=WEEKDAY_CHOICES)]
BackdateDays = Annotated[int, IntRule(minimum=0, maximum=90, unit="days")]
CutoffHours = Annotated[int, IntRule(minimum=0, maximum=336, unit="hours")]


class RequestLimit(_Strict):
    kinds: RequestKinds
    period: Literal["DAY", "WEEK", "CYCLE"] = "CYCLE"
    max_count: Annotated[int, IntRule(minimum=1, maximum=100)] | None = None
    max_minutes: Annotated[int, IntRule(minimum=1, maximum=2400, unit="minutes")] | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "RequestLimit":
        if not self.kinds:
            raise ValueError("kinds: name at least one request kind")
        if self.max_count is None and self.max_minutes is None:
            raise ValueError("a limit needs max_count or max_minutes")
        return self


class RequestExcuse(_Strict):
    max_minutes_per_request: Annotated[int, IntRule(minimum=1, maximum=480, unit="minutes")] | None = None


class RequestOnDuty(_Strict):
    enabled: bool = True
    remote_work_enabled: bool = False
    remote_work_counts_as_holiday_work: bool = False
    backdate_days: BackdateDays = 7
    future_days: Annotated[int, IntRule(minimum=0, maximum=365, unit="days")] = 60
    max_days_per_cycle: Annotated[int, IntRule(minimum=1, maximum=31, unit="days")] | None = None
    partial_day_allowed: bool = True
    requires_punch: bool = False


def _default_limits() -> list:
    return [RequestLimit(kinds=("MISSED_PUNCH", "ADD_SESSION", "CORRECTION"), period="CYCLE", max_count=4),
            RequestLimit(kinds=("MARK_EXCUSE",), period="CYCLE", max_count=2)]


class AttendanceRequestPolicy(PolicyFields):
    """Spine §6.2.3; AS 04-attendance/02 §4.4 (semantics 04-attendance/04). Consumed by attendance requests (AS-M4)."""

    profile: Literal["LEGACY_V1", "STANDARD"] = _f("STANDARD", "LEGACY_V1 = no self-service requests (today).")
    self_kinds: SelfKinds = _f(("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "MARK_EXCUSE"),
                               "What employees may request for themselves (never a status override).")
    limits: list[RequestLimit] = _f(help_="Request limits per day, week or attendance cycle.",
                                          factory=_default_limits)
    excuse: RequestExcuse = _f(help_="Late / early excuses.", factory=RequestExcuse)
    blocked_day_results: DayResultSet = _f((), "Day results on which an excuse cannot be filed.")
    backdate_days: BackdateDays = _f(7, "How far back a request may go.")
    window_mode: Literal["ROLLING", "UNTIL_CUTOFF", "ROLLING_AND_CUTOFF"] = _f(
        "ROLLING_AND_CUTOFF", "Requests close after the backdate days, the payroll cutoff, or both.")
    future_days: Annotated[int, IntRule(minimum=0, maximum=31, unit="days")] = _f(0, "How far ahead a request may go.")
    comment_required: bool = _f(False, "Every request needs a comment.")
    attachment_required_kinds: RequestKinds = _f((), "Request kinds that need an attachment.")
    on_duty: RequestOnDuty = _f(help_="On-duty and remote work.", factory=RequestOnDuty)

    @model_validator(mode="after")
    def _combinations(self) -> "AttendanceRequestPolicy":
        if len(self.limits) > 10:
            raise ValueError("limits: at most 10 limits")
        return self


class SwapRequestRules(_Strict):
    enabled: bool = False
    cutoff_hours: CutoffHours = 24
    approval_required: bool = True
    max_per_period: Annotated[int, IntRule(minimum=1, maximum=1000)] | None = None
    limit_window: Literal["WEEK", "MONTH", "ATTENDANCE_CYCLE"] = "MONTH"
    approver_may_extend: bool = False


class GiveAwayRequestRules(SwapRequestRules):
    pay_effect: Literal["PAID_OFF", "LEAVE_REQUIRED", "UNPAID_OFF"] | None = None  # None = per giver (JC-AS-49)


class SchedulingRequests(_Strict):
    swap: SwapRequestRules = Field(default_factory=SwapRequestRules)
    drop: GiveAwayRequestRules = Field(default_factory=GiveAwayRequestRules)
    release: GiveAwayRequestRules = Field(default_factory=lambda: GiveAwayRequestRules(cutoff_hours=48))
    pickup_approval_required: bool = True
    change_enabled: bool = True
    change_cutoff_hours: CutoffHours = 48


class CoverageRule(_Strict):
    team_id: Annotated[int, IntRule(minimum=1)] | None = None
    branch_id: Annotated[int, IntRule(minimum=1)] | None = None
    site_id: Annotated[int, IntRule(minimum=1)] | None = None
    shift_id: Annotated[int, IntRule(minimum=1)]
    weekdays: WeekdaySet = ()
    min_count: Annotated[int, IntRule(minimum=1, maximum=500)] = 1
    required_tags: list[str] = Field(default_factory=list)


class OpenShiftEligibility(_Strict):
    same_team: bool = True
    same_branch: bool = True
    priority: Literal["NONE", "EXISTING_PART_TIME_FIRST"] = "NONE"
    exclusive_window_hours: Annotated[int, IntRule(minimum=0, maximum=168, unit="hours")] = 0


class Availability(_Strict):
    enabled: bool = False
    change_notice_days: Annotated[int, IntRule(minimum=0, maximum=60, unit="days")] = 7
    approval_required: bool = True


class WorkLocationPlan(_Strict):
    mode: Literal["NONE", "PATTERN", "ROSTER"] = "NONE"
    pattern: dict[Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                  Literal["ON_SITE", "REMOTE", "EMPLOYEE_CHOICE"]] = Field(default_factory=dict)


class SchedulingPolicy(PolicyFields):
    """Spine §6.2.4; AS 04-attendance/02 §4.5 (semantics 03-shifts/02, /03). Consumed by rosters (AS-M5) and shift
    requests (AS-M6); write gate ``shift:manage``."""

    profile: Literal["LEGACY_V1", "STANDARD"] = _f("STANDARD", "LEGACY_V1 = placement shifts, no rosters (today).")
    schedule_source: Literal["PLACEMENT", "ROSTER"] = _f("PLACEMENT", "Shifts come from placements or rosters.")
    holiday_mode: Literal["HOLIDAY_OFF", "WORK_THROUGH", "HOLIDAY_SKELETON"] = _f(
        "HOLIDAY_OFF", "Holidays are off, worked through, or covered by a skeleton crew.")
    skeleton_coverage: dict[str, Annotated[int, IntRule(minimum=1, maximum=50)]] = _f(
        help_="Skeleton crew per shift id on holidays.", factory=dict)
    publish_notice_days: Annotated[int, IntRule(minimum=0, maximum=28, unit="days")] = _f(
        7, "Rosters are published this many days ahead.")
    rule_severity: dict[str, Literal["WARN", "REQUIRE_REASON", "BLOCK"]] = _f(
        help_="Raise the severity of roster rules (never lower a pack's).", factory=dict)
    min_rest_hours: Annotated[int, IntRule(minimum=1, maximum=48, unit="hours")] | None = _f(
        None, "Least rest between shifts.")
    max_consecutive_days: Annotated[int, IntRule(minimum=1, maximum=31, unit="days")] | None = _f(
        None, "Most working days in a row.")
    max_days_per_week: Annotated[int, IntRule(minimum=1, maximum=7, unit="days")] | None = _f(
        None, "Most working days per week.")
    max_hours_per_week: Annotated[int, IntRule(minimum=1, maximum=168, unit="hours")] | None = _f(
        None, "Most hours per week.")
    coverage_rules: list[CoverageRule] = _f(help_="Least staff per shift.", factory=list)
    requests: SchedulingRequests = _f(help_="Swaps, drops, releases, pickups and changes.", factory=SchedulingRequests)
    open_shift_eligibility: OpenShiftEligibility = _f(help_="Who may pick up open shifts.", factory=OpenShiftEligibility)
    availability: Availability = _f(help_="Employee availability.", factory=Availability)
    acknowledge_published_shifts: bool = _f(False, "Employees acknowledge published shifts.")
    work_location_plan: WorkLocationPlan = _f(help_="Office, home or choice per day.", factory=WorkLocationPlan)
    lints_enabled: bool = _f(True, "Show roster lints while planning.")

    @model_validator(mode="after")
    def _combinations(self) -> "SchedulingPolicy":
        if self.holiday_mode == "HOLIDAY_SKELETON" and not self.skeleton_coverage:
            raise ValueError("skeleton_coverage: a skeleton crew needs at least one shift count")
        if len(self.coverage_rules) > 50:
            raise ValueError("coverage_rules: at most 50 rules")
        if self.work_location_plan.mode == "ROSTER" and self.schedule_source != "ROSTER":
            raise ValueError("work_location_plan: a roster-based plan needs schedule_source ROSTER")
        return self


# --------------------------------------------------------------------------- the type catalogue


# --------------------------------------------------------------------------- LEAVE_PLAN (LH 03-leave/01 §5)
# Per-type leave rules (eligibility, entitlement, accrual, carry-forward, encashment units, request rules, documents)
# as data, versioned and assigned by criteria like every policy. Plans never enter payroll snapshots
# (``SKIPPED_POLICY_TYPES``); payroll reads leave only through ``leave_days`` and the ledger. Names follow
# 03-leave/01 §5.2-§5.5 and §8.1; the leave services evaluate them (``app/services/leave_plans.py``).

LeaveUnits = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(8784), max_places=2, unit="units")]
PositiveUnits = Annotated[
    Decimal, DecimalRule(minimum=_D0, maximum=Decimal(8784), exclusive_minimum=True, max_places=2, unit="units")]
UnitStep = Annotated[Decimal, DecimalRule(minimum=Decimal("0.01"), maximum=Decimal(1), max_places=2, unit="units")]
Fte = Annotated[Decimal, DecimalRule(minimum=_D0, maximum=Decimal(1), exclusive_minimum=True, max_places=4)]
LeaveMonths = Annotated[int, IntRule(minimum=1, maximum=60, unit="months")]
NoticeValue = Annotated[int, IntRule(minimum=0, maximum=365)]
BackdateDays = Annotated[int, IntRule(minimum=0, maximum=90, unit="days")]
FutureDays = Annotated[int, IntRule(minimum=1, maximum=730, unit="days")]
GapDays = Annotated[int, IntRule(minimum=0, maximum=5, unit="days")]
DueDays = Annotated[int, IntRule(minimum=0, maximum=90, unit="days")]
SlabMinutes = Annotated[int, IntRule(minimum=1, maximum=1440, unit="minutes")]
MaxRequests = Annotated[int, IntRule(minimum=1, maximum=1000)]
MonthDay = Annotated[str, PatternRule(kind="month_day", max_length=5,
                                      pattern=re.compile(r"(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"),
                                      hint="a day of the year as MM-DD")]
LEAVE_GENDERS: Final = ("FEMALE", "MALE", "OTHER", "UNSPECIFIED")
WORKER_CLASS_CHOICES: Final = ("EMPLOYEE", "CONTRACTOR", "INTERN", "APPRENTICE")
ATTACHMENT_CATEGORIES: Final = ("MEDICAL_CERTIFICATE", "FIT_TO_WORK", "PARENTAL_EVIDENCE", "BEREAVEMENT_EVIDENCE",
                                "MARRIAGE_EVIDENCE", "OFFICIAL_SUMMONS", "STUDY_EVIDENCE", "TRAVEL_DOCUMENT",
                                "SUPPORTING_DOCUMENT")
LEAVE_EVENT_TYPES: Final = ("EXPECTED_DELIVERY", "BIRTH", "ADOPTION_HANDOVER", "SURROGACY_HANDOVER", "PREGNANCY_LOSS",
                            "REPRODUCTIVE_PROCEDURE", "DEATH", "MARRIAGE", "PILGRIMAGE", "SABBATICAL_ELIGIBILITY",
                            "LONG_ILLNESS", "OTHER")
ENCASH_TRIGGERS: Final = ("EXIT", "YEAR_END", "ON_REQUEST")
GenderSet = Annotated[tuple[str, ...], ChoiceSetRule(choices=LEAVE_GENDERS, min_items=1)]
WorkerClassSet = Annotated[tuple[str, ...], ChoiceSetRule(choices=WORKER_CLASS_CHOICES, min_items=1)]
EncashTriggers = Annotated[tuple[str, ...], ChoiceSetRule(choices=ENCASH_TRIGGERS)]
EventTypeSet = Annotated[tuple[str, ...], ChoiceSetRule(choices=LEAVE_EVENT_TYPES, min_items=1)]
EarnOn = Annotated[tuple[str, ...], ChoiceSetRule(choices=("HOLIDAY", "WEEKLY_OFF"), min_items=1)]


class LeaveCycle(_Strict):
    kind: Literal["CALENDAR_YEAR", "FISCAL_YEAR", "ANNIVERSARY", "CUSTOM"] = _f(
        "CALENDAR_YEAR", "The leave year: calendar year, the legal entity's fiscal year, the work anniversary or a "
        "custom start day.")
    custom_start: MonthDay | None = _f(None, "First day of a CUSTOM leave year (MM-DD, never 02-29).")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveCycle":
        if (self.kind == "CUSTOM") != (self.custom_start is not None):
            raise ValueError("custom_start is required for a CUSTOM cycle and only for it")
        if self.custom_start == "02-29":
            raise ValueError("a leave year cannot start on 29 February")
        return self


class LeavePlanDefaults(_Strict):
    cycle: LeaveCycle = _f(help_="The leave year of every type without its own cycle.", factory=LeaveCycle)


class LeaveBalanceScope(_Strict):
    kind: Literal["CYCLE", "LOTS", "EVENT"] = _f(
        "CYCLE", "CYCLE: one balance per leave year. LOTS: expiring credits (comp-off). EVENT: per occurrence.")


class LeaveCountingBasis(_Strict):
    basis: Literal["WORKING_DAYS", "CALENDAR_DAYS", "CALENDAR_EXCL_HOLIDAYS", "FIXED_WEEKDAYS"] = _f(
        "WORKING_DAYS", "Which dates inside a request are charged.")
    weekdays: list[Annotated[int, IntRule(minimum=0, maximum=6)]] | None = _f(
        None, "FIXED_WEEKDAYS only: the charged weekdays (Mon=0..Sun=6).")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveCountingBasis":
        if (self.basis == "FIXED_WEEKDAYS") != (self.weekdays is not None):
            raise ValueError("weekdays are required for FIXED_WEEKDAYS and only for it")
        if self.weekdays is not None and (not self.weekdays or len(set(self.weekdays)) != len(self.weekdays)):
            raise ValueError("weekdays must be 1-7 distinct days")
        return self


class LeaveMinTenure(_Strict):
    value: Annotated[int, IntRule(minimum=1, maximum=600)] = _f(help_="How long.")
    unit: Literal["DAYS", "MONTHS", "YEARS"] = _f("MONTHS", "Unit of the value.")
    basis: Literal["SERVICE_START", "HIRE_DATE", "CONFIRMATION"] = _f("SERVICE_START", "Counted from.")
    mode: Literal["BLOCK_USE", "NO_ENTITLEMENT"] = _f(
        "BLOCK_USE", "BLOCK_USE: accrues but cannot be used yet. NO_ENTITLEMENT: nothing until reached.")


class LeaveEligibility(_Strict):
    genders: GenderSet | None = _f(None, "Only these (mapped) genders; empty = everyone.")
    unknown_gender: Literal["ELIGIBLE_VERIFY", "NOT_ELIGIBLE"] = _f(
        "ELIGIBLE_VERIFY", "Unknown gender: HR verifies, or not eligible.")
    worker_classes: WorkerClassSet | None = _f(None, "Only these worker classes.")
    employment_types: CodeList | None = _f(None, "Only these employment type codes.")
    countries: CodeList | None = _f(None, "Only branches in these countries (ISO codes).")
    branch_ids: IdList | None = _f(None, "Only these branches.")
    min_fte: Fte | None = _f(None, "Least full-time equivalent.")
    min_tenure: LeaveMinTenure | None = _f(None, "Waiting period.")


class LeaveTenureSlab(_Strict):
    from_months: Annotated[int, IntRule(minimum=0, maximum=600, unit="months")]
    amount: LeaveUnits


class LeaveEntitlement(_Strict):
    method: Literal["FRONT_LOADED", "PERIODIC", "TENURE_SLABS", "PER_DAYS_WORKED", "EVENT_GRANT", "NONE"] = _f(
        "FRONT_LOADED", "How the yearly amount is credited.")
    amount: LeaveUnits = _f(Decimal(0), "Yearly amount (days or hours).")
    frequency: Literal["MONTHLY", "QUARTERLY", "HALF_YEARLY"] | None = _f(None, "PERIODIC: how often.")
    post_at: Literal["START", "END"] | None = _f(None, "PERIODIC: credit at the start or the end of each period.")
    per_days_worked: Annotated[int, IntRule(minimum=1, maximum=365)] | None = _f(
        None, "PER_DAYS_WORKED: one unit per this many days worked.")
    tenure_slabs: list[LeaveTenureSlab] | None = _f(None, "TENURE_SLABS: yearly amounts by months of service.")
    credit_expiry_months: LeaveMonths | None = _f(None, "Credits expire this many months after posting.")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveEntitlement":
        if (self.method == "PERIODIC") != (self.frequency is not None and self.post_at is not None):
            raise ValueError("PERIODIC needs frequency and post_at (and only PERIODIC takes them)")
        if (self.method == "PER_DAYS_WORKED") != (self.per_days_worked is not None):
            raise ValueError("per_days_worked is required for PER_DAYS_WORKED and only for it")
        if (self.method == "TENURE_SLABS") != bool(self.tenure_slabs):
            raise ValueError("tenure_slabs are required for TENURE_SLABS and only for it")
        if self.tenure_slabs:
            months = [s.from_months for s in self.tenure_slabs]
            if months[0] != 0 or any(b <= a for a, b in zip(months, months[1:])):
                raise ValueError("tenure_slabs must start at 0 months and increase")
        return self


class LeaveProration(_Strict):
    joiner: Literal["PRORATE", "FULL"] = _f("PRORATE", "Joiners get a share of the year, or the full amount.")
    leaver: Literal["PRORATE", "FULL"] = _f("PRORATE", "Leavers keep a share of the year, or the full amount.")


class LeaveRounding(_Strict):
    mode: Literal["NONE_2DP", "NEAREST_HALF", "UP_HALF", "DOWN_HALF", "NEAREST_QUARTER", "NEAREST_WHOLE"] = _f(
        "NONE_2DP", "Rounding of credited amounts (applied to the cumulative amount earned).")


class LeaveCarryForward(_Strict):
    cap: LeaveUnits | None = _f(Decimal(0), "Most units carried into the next year; empty = everything.")
    expiry_months: LeaveMonths | None = _f(None, "Carried units expire after this many months; empty = never.")


class LeaveEncashment(_Strict):
    eligible: bool = _f(False, "Unused units may be paid out.")
    triggers: EncashTriggers = _f((), "When: on exit, at year end (the part above the carry cap) or on request.")
    max_units_per_cycle: PositiveUnits | None = _f(None, "Most units paid out per leave year.")
    min_balance_after: LeaveUnits | None = _f(None, "Units that must remain after an on-request encashment.")
    step: UnitStep = _f(Decimal("0.5"), "Encashed units are a multiple of this.")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveEncashment":
        if self.triggers and not self.eligible:
            raise ValueError("triggers need eligible = true")
        return self


class LeaveSandwich(_Strict):
    mode: Literal["OFF", "WEEKLY_OFFS", "HOLIDAYS", "BOTH"] = _f(
        "OFF", "Count weekly offs and/or holidays between two leave days as leave.")
    half_day_edges: Literal["IGNORE", "COUNT"] = _f("IGNORE", "Half-day leave next to the gap closes the sandwich.")


class LeaveNoticeStep(_Strict):
    min_units: PositiveUnits
    value: NoticeValue


class LeaveNotice(_Strict):
    value: NoticeValue = _f(0, "Notice before the first leave day.")
    unit: Literal["CALENDAR_DAYS", "WORKING_DAYS"] = _f("CALENDAR_DAYS", "Unit of the notice.")
    scale: list[LeaveNoticeStep] = _f(help_="Longer requests need longer notice.", factory=list)


class LeaveEmergency(_Strict):
    enabled: bool = _f(True, "Short notice is allowed when the employee marks the request as an emergency.")
    max_units: PositiveUnits = _f(Decimal(1), "Largest emergency request.")
    reason_required: bool = _f(True, "Emergency requests need a reason.")


class LeaveCap(_Strict):
    period: Literal["MONTH", "QUARTER", "CYCLE", "ROLLING_12M"]
    max_units: PositiveUnits


class LeaveInstanceCap(_Strict):
    period: Literal["MONTH", "QUARTER", "CYCLE", "ROLLING_12M", "LIFETIME"]
    max_requests: MaxRequests


class LeaveClubbing(_Strict):
    not_with: list[LeaveCode] = _f(help_="Types this leave may not be joined to.", factory=list)
    gap_days: GapDays = _f(0, "Working days allowed between them.")


class LeaveRequestRules(_Strict):
    self_service: bool = _f(True, "Employees apply themselves.")
    on_behalf: bool = _f(True, "Managers and HR may apply for them.")
    portions: Literal["INHERIT", "FULL_ONLY", "HOURS_ALLOWED"] = _f(
        "INHERIT", "INHERIT follows the type's half-day setting; FULL_ONLY refuses half days.")
    min_units: PositiveUnits | None = _f(None, "Smallest request.")
    max_units: PositiveUnits | None = _f(None, "Largest request.")
    max_consecutive_units: PositiveUnits | None = _f(None, "Most consecutive units with adjoining requests.")
    caps: list[LeaveCap] = _f(help_="Most units per period.", factory=list)
    max_instances: list[LeaveInstanceCap] = _f(help_="Most requests per period.", factory=list)
    min_notice: LeaveNotice | None = _f(None, "Advance notice.")
    emergency_exception: LeaveEmergency | None = _f(None, "Emergency requests may skip the notice.")
    backdate_limit_days: BackdateDays = _f(0, "How many days back a request may start.")
    future_limit_days: FutureDays = _f(365, "How far ahead a request may end.")
    clubbing: LeaveClubbing = _f(help_="Types that may not be joined to this one.", factory=LeaveClubbing)
    adjacent_non_working: Literal["ALLOW", "WARN", "BLOCK"] = _f(
        "ALLOW", "Leave next to a weekly off or holiday.")
    blackouts: Literal["APPLY", "EXEMPT"] = _f("APPLY", "Blackout periods apply to this type.")
    coverage: Literal["APPLY", "EXEMPT"] = _f("APPLY", "Minimum-staffing rules apply to this type.")
    reason: Literal["OPTIONAL", "REQUIRED", "HIDDEN"] = _f("OPTIONAL", "The reason field.")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveRequestRules":
        if self.min_units is not None and self.max_units is not None and self.min_units > self.max_units:
            raise ValueError("min_units must not exceed max_units")
        return self


class LeaveDocumentNeed(_Strict):
    when: Literal["ALWAYS", "MIN_UNITS", "MIN_CALENDAR_DAYS", "BACKDATED", "ADJACENT_NON_WORKING"]
    min_units: PositiveUnits | None = None
    category: Literal[ATTACHMENT_CATEGORIES] = "SUPPORTING_DOCUMENT"  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _rules(self) -> "LeaveDocumentNeed":
        if (self.when in ("MIN_UNITS", "MIN_CALENDAR_DAYS")) != (self.min_units is not None):
            raise ValueError("min_units is required for MIN_UNITS / MIN_CALENDAR_DAYS and only for them")
        return self


class LeaveDocuments(_Strict):
    allowed: bool = _f(True, "Employees may attach documents.")
    required: list[LeaveDocumentNeed] = _f(help_="When a document is required.", factory=list)
    due: Literal["AT_SUBMIT", "BEFORE_DECISION", "WITHIN_DAYS_AFTER_RETURN", "WITHIN_DAYS_AFTER_SUBMIT"] = _f(
        "AT_SUBMIT", "When the document must be there.")
    due_days: DueDays | None = _f(None, "Days allowed for WITHIN_DAYS_*.")
    verification: Literal["NONE", "AFTER_APPROVAL", "BEFORE_APPROVAL"] = _f("NONE", "Whether HR verifies it.")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveDocuments":
        if self.due.startswith("WITHIN_DAYS") != (self.due_days is not None):
            raise ValueError("due_days is required for WITHIN_DAYS_* and only for it")
        if not self.allowed and self.required:
            raise ValueError("documents cannot be required when uploads are not allowed")
        return self


class LeaveApprovalRules(_Strict):
    auto_approve_eligible: bool = _f(False, "Requests without warnings are approved automatically.")


class LeaveChangeRules(_Strict):
    cancellation_requires_approval: Literal["INHERIT", "ALWAYS", "NEVER"] = _f(
        "INHERIT", "INHERIT follows the leave.cancellation.requires_approval setting.")
    recall_allowed: bool = _f(True, "A manager may recall the employee from approved leave.")


class LeaveEventRules(_Strict):
    event_types: EventTypeSet = _f(help_="Events that give this leave.")
    entitlement_units: PositiveUnits = _f(help_="Units per event.")
    duration_unit: Literal["WORKING_DAYS", "CALENDAR_DAYS", "WEEKS"] = _f("CALENDAR_DAYS", "Unit of the units.")
    earliest_start_offset_days: Annotated[int, IntRule(minimum=-365, maximum=0)] = _f(
        0, "How many days before the event the leave may start (negative).")
    take_within_days: Annotated[int, IntRule(minimum=1, maximum=1095)] | None = _f(
        None, "The leave must end within this many days of the event.")
    max_occurrences: MaxRequests | None = _f(None, "Events per employment.")


class LeaveTypeRule(_Strict):
    leave_type_code: LeaveCode = _f(help_="The leave type (leave_types.code).")
    frozen: bool = _f(False, "System-written: kept only so year end can settle an inactive type.")
    keep_on_plan_exit: bool = _f(False, "The balance stays bookable after moving to a plan without this type.")
    cycle: LeaveCycle | None = _f(None, "Own leave year; empty = the plan default.")
    balance_scope: LeaveBalanceScope = _f(help_="How the balance is kept.", factory=LeaveBalanceScope)
    counting_basis: LeaveCountingBasis = _f(help_="Charged dates.", factory=LeaveCountingBasis)
    eligibility: LeaveEligibility = _f(help_="Who can take it.", factory=LeaveEligibility)
    during_probation: Literal["ALLOW", "ACCRUE_ONLY", "BLOCK"] = _f("ALLOW", "During probation.")
    during_notice: Literal["ALLOW", "BLOCK", "ALLOW_EXTENDS_LWD"] = _f("ALLOW", "During the notice period.")
    entitlement: LeaveEntitlement = _f(help_="How much and how it is credited.", factory=LeaveEntitlement)
    accrual_cap: PositiveUnits | None = _f(None, "No accrual while the balance is at least this.")
    max_balance: PositiveUnits | None = _f(None, "Ceiling of the balance.")
    proration: LeaveProration = _f(help_="Joiners and leavers.", factory=LeaveProration)
    rounding: LeaveRounding = _f(help_="Rounding of credits.", factory=LeaveRounding)
    carry_forward: LeaveCarryForward = _f(help_="Year end.", factory=LeaveCarryForward)
    encashment: LeaveEncashment = _f(help_="Pay-out of unused units (money is the leave pay policy's).",
                                     factory=LeaveEncashment)
    balance_check: Literal["CURRENT", "PROJECTED"] = _f("CURRENT", "Balance compared at booking.")
    insufficient_balance_action: Literal["BLOCK", "ALLOW_NEGATIVE", "SPLIT_TO_UNPAID"] = _f(
        "BLOCK", "When the balance is short.")
    negative_allowance: LeaveUnits = _f(Decimal(0), "How far the balance may go below zero (ALLOW_NEGATIVE).")
    negative_at_cycle_end: Literal["CARRY_NEGATIVE", "WRITE_OFF"] = _f(
        "CARRY_NEGATIVE", "A negative balance at year end.")
    sandwich: LeaveSandwich = _f(help_="Leave sandwich.", factory=LeaveSandwich)
    request: LeaveRequestRules = _f(help_="Request rules.", factory=LeaveRequestRules)
    documents: LeaveDocuments = _f(help_="Documents.", factory=LeaveDocuments)
    approval: LeaveApprovalRules = _f(help_="Approval.", factory=LeaveApprovalRules)
    change: LeaveChangeRules = _f(help_="Changes to approved leave.", factory=LeaveChangeRules)
    event: LeaveEventRules | None = _f(None, "Per-event leave (kind EVENT types only).")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveTypeRule":
        if (self.insufficient_balance_action == "ALLOW_NEGATIVE") != (self.negative_allowance > 0):
            raise ValueError("negative_allowance > 0 goes with insufficient_balance_action ALLOW_NEGATIVE only")
        if self.balance_scope.kind == "EVENT" and self.event is None:
            raise ValueError("an EVENT balance needs the event block")
        if self.event is not None and self.request.min_notice is not None and self.request.min_notice.value > 365:
            raise ValueError("notice of an event leave is at most 365 days")
        return self


class LeaveCreditSlab(_Strict):
    min_worked_minutes: SlabMinutes
    units: PositiveUnits


def _default_slabs() -> list["LeaveCreditSlab"]:
    return [LeaveCreditSlab(min_worked_minutes=240, units=Decimal("0.5")),
            LeaveCreditSlab(min_worked_minutes=480, units=Decimal("1.0"))]


class LeaveCompOffRules(_Strict):
    earn_on: EarnOn = _f(("HOLIDAY", "WEEKLY_OFF"), "Work on these days can be claimed.")
    claim_window_days: Annotated[int, IntRule(minimum=1, maximum=90, unit="days")] = _f(
        30, "Days after the worked day within which a claim is filed.")
    credit_slabs: list[LeaveCreditSlab] = _f(help_="Worked minutes to credited units.", factory=_default_slabs)
    validity_days: Annotated[int, IntRule(minimum=1, maximum=365, unit="days")] = _f(
        60, "Days after the worked day the credit can be used.")
    min_usable_days: Annotated[int, IntRule(minimum=0, maximum=90, unit="days")] = _f(
        14, "A credit is usable at least this long after it is credited.")
    max_units_per_month: PositiveUnits | None = _f(None, "Most units credited per month.")

    @model_validator(mode="after")
    def _rules(self) -> "LeaveCompOffRules":
        minutes = [s.min_worked_minutes for s in self.credit_slabs]
        if not minutes or any(b <= a for a, b in zip(minutes, minutes[1:])):
            raise ValueError("credit_slabs must list increasing minutes")
        return self


class LeavePlan(PolicyFields):
    """LH 03-leave/01 §5.2: ``defaults``, ``comp_off`` and one rule per leave type (1-60)."""

    schema_version: Literal[1] = _f(1, "Schema version.")
    defaults: LeavePlanDefaults = _f(help_="Plan defaults.", factory=LeavePlanDefaults)
    comp_off: LeaveCompOffRules | None = _f(None, "Comp-off claim rules (needed when a COMP_OFF type is in the plan).")
    types: list[LeaveTypeRule] = _f(help_="One rule per leave type.", factory=list)

    @model_validator(mode="after")
    def _rules(self) -> "LeavePlan":
        codes = [t.leave_type_code for t in self.types]
        if len(codes) > 60:
            raise ValueError("a plan has at most 60 leave types")
        if len(set(codes)) != len(codes):
            raise ValueError("each leave type appears once in a plan")
        return self

    def activation_problems(self, switches: frozenset[str] | None) -> list[ActivationProblem]:
        if not self.types:
            return [ActivationProblem("types", "Add at least one leave type.")]
        return []


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
    # JC-AS-22: the write gate of a non-payroll type (None = payroll:configuration:write) and its owning module
    # (``/api/v1/payroll/policies`` lists payroll types only; ``/api/v1/attendance/policies`` the attendance ones).
    write_permission: str | None = None
    module: str = "payroll"
    # LH-SEC-02 (07 §5.2 "activate plan version"): the tenant setting that, when on, makes activating a version of
    # this type need a fresh step-up (None = never).
    activation_step_up_setting: str | None = None


POLICY_TYPES: Final[Mapping[str, PolicyTypeDef]] = types.MappingProxyType({
    d.code: d for d in (
        PolicyTypeDef("ATTENDANCE_PAY_POLICY", "Attendance pay policy", ("payroll.attendance.enabled",), "PERIOD",
                      AttendancePayPolicy, "Attendance & LOP", "3.1 §5.2"),
        PolicyTypeDef("LEAVE_PAY_POLICY", "Leave pay policy",
                      ("payroll.leave.enabled", "payroll.leave.encashment.enabled"), "PERIOD",
                      LeavePayPolicy, "Leave pay", "3.1 §5.3",
                      activation_step_up_setting="leave.plan_activation.requires_step_up"),
        PolicyTypeDef("HOLIDAY_PAY_POLICY", "Holiday pay policy",
                      ("payroll.holiday_pay.non_salaried.enabled", "payroll.holiday_work_premium.enabled",
                       "payroll.weekly_off_pay.enabled"), "DAY",
                      HolidayPayPolicy, "Holidays & weekly offs", "3.1 §5.4",
                      activation_step_up_setting="leave.plan_activation.requires_step_up"),
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
        PolicyTypeDef("ATTENDANCE_CAPTURE_POLICY", "Capture policy", (), "DAY", AttendanceCapturePolicy,
                      "Capture methods & devices", "AS spine §6.2.1", write_permission="attendance:manage_policy",
                      module="attendance"),
        PolicyTypeDef("ATTENDANCE_RULES_POLICY", "Attendance rules", (), "DAY", AttendanceRulesPolicy,
                      "Attendance rules", "AS spine §6.2.2", write_permission="attendance:manage_policy",
                      module="attendance"),
        PolicyTypeDef("ATTENDANCE_REQUEST_POLICY", "Requests & approvals", (), "DAY", AttendanceRequestPolicy,
                      "Requests & approvals", "AS spine §6.2.3", write_permission="attendance:manage_policy",
                      module="attendance"),
        PolicyTypeDef("SCHEDULING_POLICY", "Shifts & schedules", (), "DAY", SchedulingPolicy,
                      "Shifts & schedules", "AS spine §6.2.4", write_permission="shift:manage",
                      module="attendance"),
        # LH-M2 (03-leave/01 §5.1): no gate switch, the builder is GLOBAL (13 §2.3); balances read it from the cut-over.
        PolicyTypeDef("LEAVE_PLAN", "Leave plan", (), "DAY", LeavePlan, "Leave", "LH 03-leave/01 §5",
                      write_permission="leave:manage_policy", module="leave",
                      activation_step_up_setting="leave.plan_activation.requires_step_up"),
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
