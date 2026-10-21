"""Holiday and weekly-off pay: work premiums, stacking with overtime, holiday pay for
hourly and daily workers, holidays on weekly offs, comp-off (3.4 §8, §9.5; 3.1 §5.4, §6.2).

Who pays an hour worked on a non-working day (3.1 §6.2), per date (DAY grain):

    premium switch  overtime  hours up to standard          hours beyond standard
    ON              OFF       premium (or comp-off)          same premium
    ON              ON        premium (or comp-off)          overtime HOLIDAY / WEEKLY_OFF bucket
                                                             (or the same premium with SAME_PREMIUM)
    OFF             ON        overtime bucket                overtime bucket
    OFF             OFF       nothing extra                  nothing extra

``extra = hourly_rate × multiplier × min(hours, standard_hours)`` is paid on top of
the normal pay for the day. The multipliers have no default (contract K:157,
decision I-13): a premium to pay with no multiplier entered raises INVALID_POLICY. Premiums add and multipliers never compound
(EC-HOL-02): overtime hours are returned as entries for ``overtime_hours`` and are
paid only at their bucket multiplier, never also at the premium.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.payroll.time_pay_common import (
    BLOCKING,
    EARNING,
    HOLIDAY,
    INVALID_INPUT,
    INVALID_POLICY,
    INVALID_RATE,
    WARNING,
    WEEKLY_OFF,
    DayRanges,
    Issue,
    TimePayError,
    TimePayLine,
    check_ranges,
    check_rounding_mode,
    field_value,
    in_calc_context,
    make_line,
    policy_bool,
    policy_choice,
    policy_decimal,
    policy_int,
    require_decimal,
    require_int,
    resolve_minor_unit,
    show,
    show_hours,
    switch_on,
    units_4dp,
)
from app.payroll.time_pay_overtime import (
    APPROVED,
    ORIGIN_HOLIDAY_WORK,
    ORIGIN_WEEKLY_OFF_WORK,
    OvertimeEntry,
)

COMPENSATIONS = ("PREMIUM", "COMP_OFF", "PREMIUM_AND_COMP_OFF", "EMPLOYEE_CHOICE")
BEYOND_STANDARD = ("OVERTIME_HOLIDAY_BUCKET", "SAME_PREMIUM")
HOLIDAY_ON_WEEKLY_OFF = ("NONE", "SUBSTITUTE_DAY", "EXTRA_DAY_PAY")
NON_SALARIED_MODES = ("OFF", "STANDARD_HOURS", "AVERAGE_DAILY_EARNINGS")
ADJACENT_RULES = ("NONE", "DAY_BEFORE", "DAY_AFTER", "BOTH")
PAID_HOLIDAY_TYPES = ("PUBLIC", "COMPANY", "OPTIONAL_AVAILED")
HOLIDAY_TYPES = ("PUBLIC", "COMPANY", "OPTIONAL")
PENDING = "PENDING"

PREMIUM_COMPONENTS = {HOLIDAY: "HOLIDAY_PREMIUM", WEEKLY_OFF: "WEEKLY_OFF_PREMIUM"}


def _standard_minutes(standard_hours: Decimal) -> int:
    value = require_decimal(standard_hours, "standard_hours", positive=True, maximum=Decimal(24)) * 60
    if value != value.to_integral_value():
        raise TimePayError(INVALID_INPUT, "standard_hours must be a whole number of minutes", "standard_hours")
    return int(value)


def _rate(hourly_rate: Optional[Decimal], name: str = "hourly_rate") -> Decimal:
    if hourly_rate is None:
        raise TimePayError(INVALID_RATE, f"{name} is required to pay this premium", name)
    return require_decimal(hourly_rate, name, positive=True, code=INVALID_RATE)


@dataclass(frozen=True)
class CompOffGrant:
    """A COMP_OFF leave credit that expires after ``comp_off_validity_days`` (EC-HOL-07)."""

    date: date
    source: str  # HOLIDAY_WORK | WEEKLY_OFF_WORK
    hours: Decimal
    days: Decimal
    expires_on: date


@dataclass(frozen=True)
class NonWorkingDayWork:
    """Net hours worked on a holiday or weekly off (the slot keeps its classification, 3.4 §4.2)."""

    date: date
    day_type: str  # HOLIDAY | WEEKLY_OFF
    worked_minutes: int
    approved: bool = True
    choice: Optional[str] = None  # PREMIUM or COMP_OFF, for EMPLOYEE_CHOICE
    ref: Optional[str] = None


@dataclass(frozen=True)
class DayOutcome:
    date: date
    day_type: str
    worked_minutes: int
    premium_minutes: int
    comp_off_minutes: int
    overtime_minutes: int
    rule: str


@dataclass(frozen=True)
class HolidayPremiums:
    lines: Tuple[TimePayLine, ...]
    overtime_entries: Tuple[OvertimeEntry, ...]
    comp_off: Tuple[CompOffGrant, ...]
    days: Tuple[DayOutcome, ...]
    issues: Tuple[Issue, ...]

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


@dataclass(frozen=True)
class _WorkRules:
    multiplier: Optional[Decimal]  # None: not entered; required before a premium is paid (contract K:157)
    min_minutes: Decimal
    compensation: str
    requires_approval: bool
    beyond: str
    validity_days: int


def _work_rules(policy: Any, day_type: str) -> _WorkRules:
    prefix = "holiday_work" if day_type == HOLIDAY else "weekly_off_work"
    multiplier = policy_decimal(policy, f"{prefix}_multiplier", None, minimum=Decimal(0), maximum=Decimal(10))
    min_hours = policy_decimal(policy, f"{prefix}_min_hours", "4.0", minimum=Decimal(0), maximum=Decimal(24))
    return _WorkRules(
        multiplier=multiplier,
        min_minutes=(min_hours or Decimal(0)) * 60,
        compensation=policy_choice(policy, f"{prefix}_compensation", "PREMIUM", COMPENSATIONS),
        requires_approval=policy_bool(policy, "holiday_work_requires_approval", True),
        beyond=policy_choice(policy, "holiday_hours_beyond_standard", "OVERTIME_HOLIDAY_BUCKET", BEYOND_STANDARD),
        validity_days=policy_int(policy, "comp_off_validity_days", 60, minimum=0) or 0,
    )


@in_calc_context
def holiday_premiums(
    days: Sequence[NonWorkingDayWork],
    policy: Any,
    *,
    hourly_rate: Optional[Decimal],
    standard_hours: Decimal,
    currency: str,
    holiday_pay_enabled: Optional[DayRanges],
    weekly_off_pay_enabled: Optional[DayRanges],
    overtime_enabled: Optional[DayRanges],
    minor_unit: Optional[int] = None,
    rounding: str = "HALF_UP",
) -> HolidayPremiums:
    """Premiums, comp-off and overtime entries for work on holidays and weekly offs (§8, §9.5).

    Weekly-off work uses the ``weekly_off_work_*`` fields with the same mechanics; approval
    (``holiday_work_requires_approval``) and the beyond-standard rule apply to both day types.
    The returned ``overtime_entries`` must be passed to ``overtime_hours`` (and those dates
    must have no other overtime entries). Switch arguments list the dates each switch is ON
    (``None`` = every date, ``()`` = never).
    """
    minor_unit = resolve_minor_unit(currency, minor_unit)
    check_rounding_mode(rounding)
    for name, ranges in (("holiday_pay_enabled", holiday_pay_enabled),
                         ("weekly_off_pay_enabled", weekly_off_pay_enabled), ("overtime_enabled", overtime_enabled)):
        check_ranges(ranges, name)
    standard = _standard_minutes(standard_hours)
    rules = {HOLIDAY: _work_rules(policy, HOLIDAY), WEEKLY_OFF: _work_rules(policy, WEEKLY_OFF)}
    seen = set()
    outcomes: List[DayOutcome] = []
    entries: List[OvertimeEntry] = []
    grants: List[CompOffGrant] = []
    issues: List[Issue] = []
    premium_minutes: Dict[str, List[Tuple[date, int]]] = {HOLIDAY: [], WEEKLY_OFF: []}

    for day in sorted(days, key=lambda d: d.date):
        if day.day_type not in (HOLIDAY, WEEKLY_OFF):
            raise TimePayError(INVALID_INPUT, f"{day.date}: day_type must be HOLIDAY or WEEKLY_OFF", "day_type")
        if day.date in seen:
            raise TimePayError(INVALID_INPUT, f"work on {day.date} given twice", "days")
        seen.add(day.date)
        worked = require_int(day.worked_minutes, f"worked_minutes on {day.date}", minimum=0, maximum=25 * 60)
        if not worked:
            continue
        rule = rules[day.day_type]
        label = "holiday" if day.day_type == HOLIDAY else "weekly off"
        origin = ORIGIN_HOLIDAY_WORK if day.day_type == HOLIDAY else ORIGIN_WEEKLY_OFF_WORK
        entry_id = f"{origin.lower()}:{day.date.isoformat()}"
        premium_on = switch_on(day.date, holiday_pay_enabled if day.day_type == HOLIDAY else weekly_off_pay_enabled)
        overtime_on = switch_on(day.date, overtime_enabled)

        if not premium_on:
            if overtime_on:
                status = APPROVED if day.approved else PENDING
                entries.append(OvertimeEntry(entry_id, day.date, worked, status, origin=origin))
                outcomes.append(DayOutcome(day.date, day.day_type, worked, 0, 0, worked,
                                           f"{label.capitalize()} pay OFF, overtime ON: all hours to the overtime "
                                           f"{day.day_type} bucket"))
            else:
                outcomes.append(DayOutcome(day.date, day.day_type, worked, 0, 0, 0,
                                           f"{label.capitalize()} pay and overtime OFF: nothing extra"))
            continue
        if rule.requires_approval and not day.approved:
            issues.append(Issue("HOLIDAY_WORK_NOT_APPROVED", WARNING,
                                f"Work on a {label} is not approved; no premium until it is", day.date, day.ref))
            outcomes.append(DayOutcome(day.date, day.day_type, worked, 0, 0, 0, "Not approved: nothing extra"))
            continue
        if worked < rule.min_minutes:
            outcomes.append(DayOutcome(day.date, day.day_type, worked, 0, 0, 0,
                                       f"Below the minimum of {show(rule.min_minutes / 60)} h: nothing extra"))
            continue

        within = min(worked, standard)
        beyond = worked - within
        to_overtime = beyond if (beyond and overtime_on and rule.beyond == "OVERTIME_HOLIDAY_BUCKET") else 0
        eligible = worked - to_overtime
        compensation = rule.compensation
        if compensation == "EMPLOYEE_CHOICE":
            if day.choice not in ("PREMIUM", "COMP_OFF"):
                issues.append(Issue("HOLIDAY_WORK_CHOICE_MISSING", BLOCKING,
                                    f"The employee has not chosen premium or comp-off for work on a {label}",
                                    day.date, day.ref))
                compensation = "NONE"
            else:
                compensation = day.choice
        paid = eligible if compensation in ("PREMIUM", "PREMIUM_AND_COMP_OFF") else 0
        credited = eligible if compensation in ("COMP_OFF", "PREMIUM_AND_COMP_OFF") else 0
        if paid:
            premium_minutes[day.day_type].append((day.date, paid))
        if credited:
            grants.append(CompOffGrant(day.date, origin, Decimal(credited) / 60, Decimal(credited) / standard,
                                       day.date + timedelta(days=rule.validity_days)))
        if to_overtime:
            entries.append(OvertimeEntry(entry_id, day.date, to_overtime, APPROVED, origin=origin))
        beyond_text = (f"; {show_hours(to_overtime)} beyond standard to the overtime {day.day_type} bucket"
                       if to_overtime else (f"; {show_hours(beyond)} beyond standard at the same premium"
                                            if beyond else ""))
        outcomes.append(DayOutcome(day.date, day.day_type, worked, paid, credited, to_overtime,
                                   f"{compensation.replace('_', ' ').lower()} for {show_hours(eligible)}{beyond_text}"))

    lines: List[TimePayLine] = []
    for day_type in (HOLIDAY, WEEKLY_OFF):
        items = premium_minutes[day_type]
        if not items:
            continue
        rate = _rate(hourly_rate)
        multiplier = rules[day_type].multiplier
        if multiplier is None:
            prefix = "holiday_work" if day_type == HOLIDAY else "weekly_off_work"
            raise TimePayError(INVALID_POLICY, f"{prefix}_multiplier is required to pay the premium; none is assumed",
                               f"{prefix}_multiplier")
        minutes = sum(m for _, m in items)
        lines.append(make_line(
            PREMIUM_COMPONENTS[day_type], EARNING, "HOLIDAY_PREMIUM", rate * multiplier * minutes / 60, currency,
            minor_unit=minor_unit, rounding=rounding, method="RATE_X_UNITS",
            explanation=f"{show(rate)} × {multiplier} × {show_hours(minutes)} on top of the normal pay for the day",
            units=units_4dp(minutes), rate=rate,
            sources=[{"type": "day_ledger", "date": d.isoformat(), "minutes": str(m)} for d, m in items],
        ))
    return HolidayPremiums(tuple(lines), tuple(entries), tuple(grants), tuple(outcomes), tuple(issues))


# --------------------------------------------------------------------------- holiday on a weekly off (EC-HOL-01)


@dataclass(frozen=True)
class HolidayOnWeeklyOff:
    rule: str
    lines: Tuple[TimePayLine, ...]
    substitute_days: Tuple[date, ...]  # holidays for which a substitute day off is owed
    unaffected: Tuple[date, ...]

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


@in_calc_context
def holiday_on_weekly_off(
    dates: Sequence[date],
    policy: Any,
    *,
    daily_rate: Optional[Decimal],
    currency: str,
    holiday_pay_enabled: Optional[DayRanges],
    minor_unit: Optional[int] = None,
    rounding: str = "HALF_UP",
    component: str = "HOLIDAY_PREMIUM",
) -> HolidayOnWeeklyOff:
    """A paid holiday falling on a weekly off: nothing, a substitute day off, or an extra day's pay."""
    minor_unit = resolve_minor_unit(currency, minor_unit)
    check_ranges(holiday_pay_enabled, "holiday_pay_enabled")
    rule = policy_choice(policy, "holiday_on_weekly_off", "NONE", HOLIDAY_ON_WEEKLY_OFF)
    covered = tuple(sorted(d for d in set(dates) if switch_on(d, holiday_pay_enabled)))
    unaffected = tuple(sorted(d for d in set(dates) if d not in covered))
    if rule == "NONE" or not covered:
        return HolidayOnWeeklyOff(rule, (), (), tuple(sorted(set(dates))))
    if rule == "SUBSTITUTE_DAY":
        return HolidayOnWeeklyOff(rule, (), covered, unaffected)
    rate = _rate(daily_rate, "daily_rate")
    line = make_line(component, EARNING, "HOLIDAY_PREMIUM", rate * len(covered), currency,
                     minor_unit=minor_unit, rounding=rounding, method="RATE_X_UNITS",
                     explanation=f"{show(rate)} × {len(covered)} day(s): extra day's pay for a holiday on a weekly off",
                     units=Decimal(len(covered)), rate=rate,
                     sources=[{"type": "holiday", "date": d.isoformat()} for d in covered])
    return HolidayOnWeeklyOff(rule, (line,), (), unaffected)


# --------------------------------------------------------------------------- hourly and daily workers (EC-HOL-03, EC-HOL-10)


@dataclass(frozen=True)
class HolidayDay:
    date: date
    holiday_type: str  # PUBLIC | COMPANY | OPTIONAL
    availed: bool = False  # an OPTIONAL holiday counts only when the employee availed it
    present_before: Optional[bool] = None  # on the scheduled working day before; None = not known yet
    present_after: Optional[bool] = None
    ref: Optional[str] = None


@dataclass(frozen=True)
class SkippedHoliday:
    date: date
    reason: str


@dataclass(frozen=True)
class NonSalariedHolidayPay:
    mode: str
    lines: Tuple[TimePayLine, ...]
    paid: Tuple[date, ...]
    payable_minutes: int  # standard hours credited as payable hours (STANDARD_HOURS)
    skipped: Tuple[SkippedHoliday, ...]
    issues: Tuple[Issue, ...]

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


def _paid_types(policy: Any) -> frozenset:
    raw = field_value(policy, "paid_holiday_types", PAID_HOLIDAY_TYPES)
    if isinstance(raw, str) or not isinstance(raw, Iterable):
        raise TimePayError(INVALID_POLICY, "paid_holiday_types must be a set of holiday types", "paid_holiday_types")
    values = frozenset(raw)
    if not values <= set(PAID_HOLIDAY_TYPES):
        raise TimePayError(INVALID_POLICY, f"paid_holiday_types must be within {', '.join(PAID_HOLIDAY_TYPES)}",
                           "paid_holiday_types")
    return values


@in_calc_context
def non_salaried_holiday_pay(
    holidays: Sequence[HolidayDay],
    policy: Any,
    *,
    hourly_rate: Optional[Decimal],
    standard_hours: Decimal,
    currency: str,
    enabled: Optional[DayRanges],
    average_daily_earnings: Optional[Decimal] = None,
    employment_start: Optional[date] = None,
    employment_end: Optional[date] = None,
    pack_requires: bool = False,
    minor_unit: Optional[int] = None,
    rounding: str = "HALF_UP",
    component: str = "HOLIDAY_PAY",
) -> NonSalariedHolidayPay:
    """Holiday pay for hourly and daily workers (§8): rate × standard hours, or average daily earnings.

    Salaried employees never use this: a paid holiday is a paid day inside salary. A holiday
    outside employment is not paid (EC-HOL-10). With ``eligibility_adjacent_days`` the worker
    must be present on the working day before and/or after (EC-HOL-03); presence that is not
    known yet withholds the pay with a review item instead of guessing.
    """
    minor_unit = resolve_minor_unit(currency, minor_unit)
    check_ranges(enabled, "enabled")
    mode = policy_choice(policy, "non_salaried_holiday_pay", "STANDARD_HOURS", NON_SALARIED_MODES)
    adjacency = policy_choice(policy, "eligibility_adjacent_days", "NONE", ADJACENT_RULES)
    paid_types = _paid_types(policy)
    standard = _standard_minutes(standard_hours)
    issues: List[Issue] = []
    if mode == "OFF":
        if pack_requires and holidays:
            issues.append(Issue("HOLIDAY_PAY_REQUIRED_BY_LAW", BLOCKING,
                                "The country pack requires holiday pay for these workers but the policy turns it off"))
        return NonSalariedHolidayPay(mode, (), (), 0, tuple(SkippedHoliday(h.date, "POLICY_OFF") for h in holidays),
                                     tuple(issues))
    skipped: List[SkippedHoliday] = []
    paid: List[HolidayDay] = []
    for holiday in sorted(holidays, key=lambda h: h.date):
        if holiday.holiday_type not in HOLIDAY_TYPES:
            raise TimePayError(INVALID_INPUT, f"{holiday.date}: unknown holiday type {holiday.holiday_type!r}",
                               "holiday_type")
        reason = None
        if not (pack_requires or switch_on(holiday.date, enabled)):
            reason = "SWITCH_OFF"
        elif (employment_start and holiday.date < employment_start) or (employment_end and holiday.date > employment_end):
            reason = "NOT_EMPLOYED"
        elif holiday.holiday_type == "OPTIONAL" and not holiday.availed:
            reason = "OPTIONAL_NOT_AVAILED"
        elif (holiday.holiday_type if holiday.holiday_type != "OPTIONAL" else "OPTIONAL_AVAILED") not in paid_types:
            reason = "TYPE_NOT_PAID"
        else:
            needed = []
            if adjacency in ("DAY_BEFORE", "BOTH"):
                needed.append(holiday.present_before)
            if adjacency in ("DAY_AFTER", "BOTH"):
                needed.append(holiday.present_after)
            if any(flag is False for flag in needed):
                reason = "ABSENT_ON_ADJACENT_DAY"
            elif any(flag is None for flag in needed):
                reason = "ADJACENT_DAY_UNKNOWN"
                issues.append(Issue("HOLIDAY_PAY_ELIGIBILITY_UNKNOWN", WARNING,
                                    "Attendance on the adjacent working day is not known yet; holiday pay withheld "
                                    "until it is", holiday.date, holiday.ref))
        if reason:
            skipped.append(SkippedHoliday(holiday.date, reason))
        else:
            paid.append(holiday)
    if not paid:
        return NonSalariedHolidayPay(mode, (), (), 0, tuple(skipped), tuple(issues))
    sources = [{"type": "holiday", "date": h.date.isoformat(), "holiday_type": h.holiday_type} for h in paid]
    if mode == "STANDARD_HOURS":
        rate = _rate(hourly_rate)
        minutes = standard * len(paid)
        line = make_line(component, EARNING, "HOLIDAY_PREMIUM", rate * minutes / 60, currency,
                         minor_unit=minor_unit, rounding=rounding, method="RATE_X_UNITS",
                         explanation=f"{show(rate)} × {show_hours(standard)} × {len(paid)} holiday(s)",
                         units=units_4dp(minutes), rate=rate, sources=sources)
        return NonSalariedHolidayPay(mode, (line,), tuple(h.date for h in paid), minutes, tuple(skipped), tuple(issues))
    if average_daily_earnings is None:
        issues.append(Issue("AVERAGE_DAILY_EARNINGS_MISSING", BLOCKING,
                            "Average daily earnings are needed to pay these holidays"))
        return NonSalariedHolidayPay(mode, (), (), 0, tuple(skipped), tuple(issues))
    average = require_decimal(average_daily_earnings, "average_daily_earnings", minimum=Decimal(0))
    line = make_line(component, EARNING, "HOLIDAY_PREMIUM", average * len(paid), currency,
                     minor_unit=minor_unit, rounding=rounding, method="RATE_X_UNITS",
                     explanation=f"Average daily earnings {show(average)} × {len(paid)} holiday(s)",
                     units=Decimal(len(paid)), rate=average, sources=sources)
    return NonSalariedHolidayPay(mode, (line,), tuple(h.date for h in paid), 0, tuple(skipped), tuple(issues))
