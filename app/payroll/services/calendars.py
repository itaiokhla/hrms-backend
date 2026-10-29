"""Pay calendars and pay periods of pay groups (3.5 §2, 3.9 §3.6 and §4 Calendars, §6 jobs).

* A pay group has one open calendar (frequency + rules) at a time. Saving a calendar validates the
  rules with ``app.payroll.calendar`` and generates the periods 18 months ahead; the periodic job
  ``payroll.generate_periods`` keeps every open calendar 18 months ahead (it re-schedules itself
  daily, idempotent per date).
* Rules are materialized on the calendar row: what the request gives, else the pay group's
  ``calendar_rules``, else the resolved PAY_GROUP settings (``payroll.calendar.*``,
  ``payroll.attendance_cycle.*``, ``payroll.cutoff.*``); an invalid setting fails closed (409
  CONFIG_INVALID, EC-CFG-09). The tax year start is the legal entity's country's.
* Pay dates are shifted with the banking calendar (EC-CAL-03): the resolved
  ``payroll.calendar.banking_calendar_id``, else the country default (the tenant's national holiday
  calendar of the entity's country), over the country's CLDR weekend. ``recheck_pay_date`` re-validates
  a stored pay date against today's banking calendar for payment batch creation (EC-CAL-08).
* Changing the frequency or rules from a date ends the current calendar the day before: the period
  that straddles the date is shortened into a closing transition period, the new calendar starts the
  next day (with an opening transition period when needed) and attendance windows are chained, so no
  date is left out or counted twice (EC-CAL-07). Periods that have runs, or are PROCESSING/CLOSED,
  never change (CHANGE_IN_LOCKED_PERIOD). The database refuses overlapping periods (EC-CAL-12).
* Period status OPEN -> PROCESSING -> CLOSED follows ``state_machines.PAY_PERIOD``; ``set_period_status``
  is its only writer (used by ``services.runs``).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.org import LegalEntity
from app.models.pay_org import HolidayCalendar, PayGroup
from app.models.pay_runs import PayCalendar, PayPeriod, PayrollRun
from app.payroll import audit as payroll_audit
from app.payroll import calendar as paycal
from app.payroll import countries
from app.payroll import state_machines as sm
from app.payroll.services import access, jobs
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService

READ = "payroll:configuration:read"
WRITE = "payroll:configuration:write"
RUN_READ = "payroll:run:read"
AUDIT_MODULE = payroll_audit.PAYROLL_SETTINGS
GENERATE_JOB = "payroll.generate_periods"
ACTIVATE_COMPENSATION_JOB = "compensation.activate_scheduled"  # compensation.ACTIVATE_JOB (handler there)
HORIZON_MONTHS = 18
MAX_PREVIEW = 36
_ONE_DAY = timedelta(days=1)

CALENDAR_SAVED = "PAY_CALENDAR_SAVED"
CALENDAR_CHANGED = "PAY_CALENDAR_CHANGED"
PERIODS_GENERATED = "PAY_PERIODS_GENERATED"
CALENDAR_IN_FORCE = "PAY_CALENDAR_IN_FORCE"

# CalendarRules fields stored on a calendar row (the banking calendar is resolved at generation).
RULE_FIELDS = (
    "monthly_start_day", "semi_monthly_split_day", "week_start_day", "biweekly_anchor_date", "pay_day_rule",
    "pay_day_value", "pay_day_shift", "attendance_cycle_mode", "attendance_cycle_start_day",
    "input_cutoff_days_before_pay_day", "approval_days_before_pay_day", "payment_lead_business_days",
    "tax_year_start", "tax_year_basis",
)
_FIELD_OF_SETTING = dict(paycal.SETTING_FIELDS)
_ENUM_FIELDS = ("week_start_day", "pay_day_rule", "pay_day_shift", "attendance_cycle_mode", "tax_year_basis")
# Statuses of a period whose dates may still change with a calendar change.
_LOCKED_PERIOD_STATUSES = (sm.PeriodStatus.PROCESSING.value, sm.PeriodStatus.CLOSED.value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def horizon(today: date) -> date:
    """The last date periods are generated for: 18 months ahead (3.5 §2.1)."""
    month = today.month - 1 + HORIZON_MONTHS
    year, month = today.year + month // 12, month % 12 + 1
    day = min(today.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30,
                          31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _audit(db: Session, principal: Optional[Principal], event: str, description: str, entity_type: str,
           entity_id: int, company_id: int, /, **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, event, description[:500], entity_type, entity_id,
                        company_id=company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def _calendar_error(exc: paycal.CalendarError) -> AppError:
    return bad_request(exc.message, exc.code)


def load_pay_group(db: Session, company_id: int, pay_group_id: int, *, lock: bool = False) -> PayGroup:
    query = db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.id == pay_group_id)
    if lock:
        query = query.with_for_update(of=PayGroup).populate_existing()
    group = query.first()
    if group is None:
        raise not_found("Pay group")
    return group


def scoped_pay_group(db: Session, principal: Principal, permission: str, pay_group_id: int, *,
                     lock: bool = False) -> PayGroup:
    """The pay group if the caller's ``permission`` reaches it (403 without it, 404 outside scope)."""
    if not principal.has(permission):
        raise forbidden()
    group = db.query(PayGroup).filter(PayGroup.company_id == principal.company_id, PayGroup.id == pay_group_id).first()
    if group is None or not access.target_scope(principal, permission).covers(group.legal_entity_id, group.id):
        raise not_found("Pay group")
    return load_pay_group(db, principal.company_id, pay_group_id, lock=True) if lock else group


def _entity(db: Session, group: PayGroup) -> LegalEntity:
    return db.query(LegalEntity).filter(LegalEntity.company_id == group.company_id,
                                        LegalEntity.id == group.legal_entity_id).one()


def open_calendar(db: Session, company_id: int, pay_group_id: int, *, lock: bool = False) -> Optional[PayCalendar]:
    query = db.query(PayCalendar).filter(PayCalendar.company_id == company_id, PayCalendar.pay_group_id == pay_group_id,
                                         PayCalendar.effective_to.is_(None))
    if lock:
        query = query.with_for_update().populate_existing()
    return query.first()


def _stored_periods(db: Session, company_id: int, pay_group_id: int) -> List[PayPeriod]:
    return (db.query(PayPeriod)
            .filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id == pay_group_id)
            .order_by(PayPeriod.period_start)
            .all())


# ---------------------------------------------------------------------------
# Banking calendar (EC-CAL-03, EC-CAL-08)
# ---------------------------------------------------------------------------
def banking_calendar_id(db: Session, group: PayGroup, entity: LegalEntity, on: date) -> Optional[int]:
    """The resolved ``payroll.calendar.banking_calendar_id`` of the pay group, else the country default:
    the tenant's national holiday calendar of the entity's country (no subdivision)."""
    try:
        value = settings_engine.resolve(db, group.company_id, "payroll.calendar.banking_calendar_id", on=on,
                                        pay_group_id=group.id).value
    except settings_engine.SettingConfigError:
        value = None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    found = (db.query(HolidayCalendar.id)
             .filter(HolidayCalendar.company_id == group.company_id, HolidayCalendar.country_code == entity.country_code,
                     HolidayCalendar.subdivision_code.is_(None), HolidayCalendar.parent_calendar_id.is_(None),
                     HolidayCalendar.status == "ACTIVE")
             .order_by(HolidayCalendar.id)
             .first())
    return found[0] if found else None


def banking_calendar(db: Session, group: PayGroup, entity: LegalEntity, start: date, end: date,
                     calendar_id: Optional[int] = None) -> paycal.BankingCalendar:
    """Business days between ``start`` and ``end`` (with a margin for shifting): the banking calendar's effective
    weekend and every non-optional FULL-day occurrence of its flattened calendar (LH 04-holiday/01 §7 B-2, B-3:
    includes minus exclusions through ``holiday_calendar_service.flatten``)."""
    from app.services import holiday_calendar_service

    try:
        weekend = frozenset(countries.default_weekend(entity.country_code))
    except Exception:  # an unknown or inactive country: Saturday and Sunday (fail safe for shifting)
        weekend = frozenset({5, 6})
    calendar_id = calendar_id if calendar_id is not None else banking_calendar_id(db, group, entity, start)
    holidays: frozenset = frozenset()
    if calendar_id is not None:
        calendar = db.get(HolidayCalendar, calendar_id)
        if calendar is not None and (calendar.weekend_days or calendar.country_code):
            weekend = frozenset(holiday_calendar_service.effective_weekend(db, calendar))
        margin = timedelta(days=paycal.MAX_SHIFT_SEARCH_DAYS + 5)
        flat = holiday_calendar_service.flatten(db, group.company_id, calendar_id, start - margin, end + margin)
        holidays = frozenset(f.row.date for f in flat.rows
                             if f.row.holiday_type != "OPTIONAL" and f.row.portion == "FULL"
                             and f.row.status in ("CONFIRMED", "PROVISIONAL"))
    return paycal.BankingCalendar(weekend, holidays)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _from_settings(db: Session, group: PayGroup, on: date) -> Dict[str, Any]:
    keys = list(paycal.SETTING_FIELDS)
    try:
        resolved = settings_engine.resolve_many(db, group.company_id, keys, pay_group_id=group.id, on=on)
    except settings_engine.SettingConfigError as exc:
        raise conflict(f"Calendar setting {exc.key} is invalid: {exc.message}", "CONFIG_INVALID")
    values: Dict[str, Any] = {}
    for key, found in resolved.items():
        value = found.json_value
        if value is not None:
            values[paycal.SETTING_FIELDS[key]] = value
    return values


def _normalize_rules(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Field names of CalendarRules (registry keys accepted too); unknown keys are refused."""
    result: Dict[str, Any] = {}
    for key, value in raw.items():
        name = _FIELD_OF_SETTING.get(key, key)
        if name not in RULE_FIELDS:
            raise bad_request(f"{key!r} is not a pay calendar rule.", "INVALID_RULES")
        result[name] = value
    return result


def materialize_rules(db: Session, group: PayGroup, entity: LegalEntity, requested: Mapping[str, Any],
                      on: date) -> Dict[str, Any]:
    """Every rule field: the request, else the pay group's calendar_rules, else the resolved settings."""
    rules = _from_settings(db, group, on)
    group_rules = {k: v for k, v in _normalize_rules(
        {k: v for k, v in (group.calendar_rules or {}).items() if _FIELD_OF_SETTING.get(k, k) in RULE_FIELDS}
    ).items() if v is not None}
    rules.update(group_rules)
    rules.update(_normalize_rules(requested))
    if not rules.get("tax_year_start"):
        try:
            rules["tax_year_start"] = countries.tax_year_start(entity.country_code)
        except Exception:
            rules["tax_year_start"] = "01-01"
    rules.setdefault("tax_year_basis", paycal.TaxYearBasis.PAY_DATE.value)
    for name in _ENUM_FIELDS:
        if name in rules and rules[name] is not None:
            rules[name] = str(rules[name])
    anchor = rules.get("biweekly_anchor_date")
    if isinstance(anchor, date):
        rules["biweekly_anchor_date"] = anchor.isoformat()
    return {name: rules[name] for name in RULE_FIELDS if name in rules}


def calendar_rules(rules: Mapping[str, Any], banking: paycal.BankingCalendar) -> paycal.CalendarRules:
    fields = dict(rules)
    anchor = fields.get("biweekly_anchor_date")
    if isinstance(anchor, str):
        try:
            fields["biweekly_anchor_date"] = date.fromisoformat(anchor)
        except ValueError:
            raise bad_request(f"biweekly_anchor_date {anchor!r} is not an ISO date.", "NOT_A_LOCAL_DATE") from None
    try:
        return paycal.CalendarRules(banking_calendar=banking, **fields)
    except TypeError as exc:
        raise bad_request(f"Invalid pay calendar rules: {exc}", "INVALID_RULES") from None
    except paycal.CalendarError as exc:
        raise _calendar_error(exc) from None


def pay_calendar(frequency: str, rules: Mapping[str, Any], banking: paycal.BankingCalendar) -> paycal.PayCalendar:
    try:
        return paycal.PayCalendar(frequency, calendar_rules(rules, banking))
    except paycal.CalendarError as exc:
        raise _calendar_error(exc) from None


# ---------------------------------------------------------------------------
# Conversion between stored rows and calendar.Period
# ---------------------------------------------------------------------------
def to_period(row: PayPeriod) -> paycal.Period:
    return paycal.Period(
        start=row.period_start, end=row.period_end, attendance_start=row.attendance_start,
        attendance_end=row.attendance_end, pay_date=row.pay_date, nominal_pay_date=row.nominal_pay_date,
        input_cutoff=row.input_cutoff_on, approval_deadline=row.approval_deadline_on,
        payment_submit_by=row.payment_submit_by, tax_year=row.tax_year, number_in_tax_year=row.number_in_tax_year,
        is_last_in_tax_year=row.is_last_in_tax_year, frequency=row.frequency, is_transition=row.is_transition,
    )


def _apply(row: PayPeriod, period: paycal.Period) -> None:
    row.frequency = period.frequency.value
    row.period_start, row.period_end = period.start, period.end
    row.attendance_start, row.attendance_end = period.attendance_start, period.attendance_end
    row.pay_date, row.nominal_pay_date = period.pay_date, period.nominal_pay_date
    row.input_cutoff_on, row.approval_deadline_on = period.input_cutoff, period.approval_deadline
    row.payment_submit_by = period.payment_submit_by
    row.tax_year, row.number_in_tax_year = period.tax_year, period.number_in_tax_year
    row.is_last_in_tax_year, row.is_transition = period.is_last_in_tax_year, period.is_transition
    row.pay_date_warnings = [_issue_json(i) for i in paycal.pay_date_warnings([period])]


def _issue_json(issue: paycal.CalendarIssue) -> dict:
    return {"code": issue.code, "severity": issue.severity, "message": issue.message, "rule": issue.rule}


def _new_row(calendar: PayCalendar, period: paycal.Period) -> PayPeriod:
    row = PayPeriod(company_id=calendar.company_id, pay_calendar_id=calendar.id, pay_group_id=calendar.pay_group_id,
                    status=sm.PeriodStatus.OPEN.value, row_version=1)
    _apply(row, period)
    return row


def period_json(row: PayPeriod) -> dict:
    return {
        "id": row.id, "pay_group_id": row.pay_group_id, "pay_calendar_id": row.pay_calendar_id,
        "frequency": row.frequency, "start": row.period_start.isoformat(), "end": row.period_end.isoformat(),
        "days": (row.period_end - row.period_start).days + 1,
        "attendance_start": row.attendance_start.isoformat(), "attendance_end": row.attendance_end.isoformat(),
        "pay_date": row.pay_date.isoformat(), "nominal_pay_date": row.nominal_pay_date.isoformat(),
        "input_cutoff": row.input_cutoff_on.isoformat(), "approval_deadline": row.approval_deadline_on.isoformat(),
        "payment_submit_by": row.payment_submit_by.isoformat(), "tax_year": row.tax_year,
        "number_in_tax_year": row.number_in_tax_year, "is_last_in_tax_year": row.is_last_in_tax_year,
        "is_transition": row.is_transition, "status": row.status, "warnings": list(row.pay_date_warnings or []),
        "row_version": row.row_version,
    }


def calendar_json(row: PayCalendar) -> dict:
    return {
        "id": row.id, "pay_group_id": row.pay_group_id, "frequency": row.frequency, "rules": dict(row.rules or {}),
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None, "status": row.status,
        "generated_until": row.generated_until.isoformat() if row.generated_until else None,
        "banking_calendar_id": row.banking_calendar_id, "row_version": row.row_version,
    }


def _preview_json(period: paycal.Period) -> dict:
    data = period.to_dict()
    data["warnings"] = [_issue_json(i) for i in paycal.pay_date_warnings([period])]
    return data


# ---------------------------------------------------------------------------
# Locks, generation and change
# ---------------------------------------------------------------------------
def _locked_until(db: Session, company_id: int, periods: Sequence[PayPeriod]) -> Optional[date]:
    """The last date covered by a period that may not change: PROCESSING, CLOSED, or with any run."""
    ids = [p.id for p in periods]
    with_runs = set()
    if ids:
        with_runs = {pid for (pid,) in db.query(PayrollRun.pay_period_id).filter(
            PayrollRun.company_id == company_id, PayrollRun.pay_period_id.in_(ids)).distinct()}
    locked = [p.period_end for p in periods if p.status in _LOCKED_PERIOD_STATUSES or p.id in with_runs]
    return max(locked) if locked else None


def _chain(previous: Optional[PayPeriod], period: paycal.Period) -> paycal.Period:
    """Continue numbering and attendance from the stored previous period (after a calendar change the
    natural numbering of ``generate_periods`` assumes an unchanged calendar)."""
    if previous is None:
        return period
    number = previous.number_in_tax_year + 1 if previous.tax_year == period.tax_year else 1
    attendance_start = previous.attendance_end + _ONE_DAY
    attendance_end = max(period.attendance_end, previous.attendance_end)
    return replace(period, number_in_tax_year=number, attendance_start=attendance_start, attendance_end=attendance_end)


def _extend(db: Session, calendar: PayCalendar, group: PayGroup, entity: LegalEntity, until: date) -> int:
    """Append the calendar's natural periods after the last stored one through ``until``."""
    stored = _stored_periods(db, calendar.company_id, group.id)
    last = stored[-1] if stored else None
    start = last.period_end + _ONE_DAY if last is not None else calendar.effective_from
    if start > until:
        calendar.generated_until = max(calendar.generated_until or until, until)
        return 0
    banking = banking_calendar(db, group, entity, start, until + timedelta(days=120), calendar.banking_calendar_id)
    cal = pay_calendar(calendar.frequency, calendar.rules, banking)
    try:
        natural = [p for p in cal.periods(start, until) if p.end >= start]
    except paycal.CalendarError as exc:
        raise _calendar_error(exc) from None
    if natural and natural[0].start != start:
        if last is None:
            raise bad_request(f"The calendar must start on a period start ({natural[0].start.isoformat()}).",
                              "CALENDAR_START_NOT_PERIOD_START")
        raise conflict(f"The stored periods end on {last.period_end.isoformat()} but the calendar's next period "
                       f"starts on {natural[0].start.isoformat()}; change the calendar from {start.isoformat()}.",
                       "PERIODS_NOT_CONTIGUOUS")
    created = 0
    previous = last
    for period in natural:
        # Chaining reproduces the natural numbering and attendance of an unchanged calendar and keeps
        # both continuous after a change.
        row = _new_row(calendar, _chain(previous, period))
        db.add(row)
        db.flush()
        previous = row
        created += 1
    calendar.generated_until = max(calendar.generated_until or until, until)
    return created


def sync_pay_group(db: Session, group: PayGroup, calendar: PayCalendar, today: date) -> Optional[dict]:
    """Once a calendar is in force (``effective_from`` <= today), the pay group's ``pay_frequency`` and
    ``calendar_rules`` follow it, so services that derive natural periods from the pay group (wage
    inputs, compensation) agree with the stored periods. Returns the change, or None."""
    if calendar.effective_from > today or calendar.effective_to is not None:
        return None
    kept = {k: v for k, v in (group.calendar_rules or {}).items() if _FIELD_OF_SETTING.get(k, k) not in RULE_FIELDS}
    rules = {**kept, **{k: v for k, v in (calendar.rules or {}).items() if v is not None}}
    if group.pay_frequency == calendar.frequency and dict(group.calendar_rules or {}) == rules:
        return None
    change = {"pay_group_id": group.id, "frequency": [group.pay_frequency, calendar.frequency]}
    group.pay_frequency, group.calendar_rules = calendar.frequency, rules
    db.flush()
    return change


def _check_save_permission(principal: Principal, group: PayGroup) -> None:
    if not principal.has(WRITE):
        raise forbidden()
    if not access.target_scope(principal, WRITE).covers(group.legal_entity_id, group.id):
        raise not_found("Pay group")


def validate_calendar(db: Session, principal: Principal, pay_group_id: int, *, frequency: str,
                      rules: Mapping[str, Any], effective_from: Optional[date] = None) -> dict:
    """Dry run of ``save_calendar``: the materialized rules, the first periods and warnings (nothing written)."""
    group = scoped_pay_group(db, principal, WRITE, pay_group_id)
    entity = _entity(db, group)
    today = _today(db, principal.company_id)
    materialized = materialize_rules(db, group, entity, rules, effective_from or today)
    current = open_calendar(db, principal.company_id, group.id)
    start = effective_from or today
    banking = banking_calendar(db, group, entity, start, start + timedelta(days=400))
    cal = pay_calendar(frequency, materialized, banking)
    issues: List[dict] = []
    if current is not None:
        stored = _stored_periods(db, principal.company_id, group.id)
        if stored:
            if effective_from is None:
                raise bad_request("Give effective_from to change an existing calendar.", "EFFECTIVE_FROM_REQUIRED")
            old = pay_calendar(current.frequency, current.rules, banking)
            try:
                change = paycal.plan_calendar_change(
                    old, [to_period(p) for p in stored], cal, effective_from, effective_from + timedelta(days=370),
                    locked_until=_locked_until(db, principal.company_id, stored))
            except paycal.CalendarError as exc:
                raise _calendar_error(exc) from None
            preview = [p for p in change.periods if p.end >= effective_from - timedelta(days=40)][:MAX_PREVIEW]
            return {"frequency": frequency, "rules": materialized, "change": True,
                    "periods": [_preview_json(p) for p in preview], "issues": issues,
                    "dropped": len(change.dropped)}
    try:
        first = cal.period_containing(start)
        periods = cal.periods(first.start, first.start + timedelta(days=370))[:MAX_PREVIEW]
    except paycal.CalendarError as exc:
        raise _calendar_error(exc) from None
    return {"frequency": frequency, "rules": materialized, "change": False,
            "periods": [_preview_json(p) for p in periods], "issues": issues, "dropped": 0}


def save_calendar(db: Session, principal: Principal, pay_group_id: int, *, frequency: str, rules: Mapping[str, Any],
                  effective_from: Optional[date] = None, row_version: Optional[int] = None) -> PayCalendar:
    """Create the pay group's calendar or change it from ``effective_from``, and generate periods 18 months ahead."""
    group = scoped_pay_group(db, principal, WRITE, pay_group_id, lock=True)
    if group.status != "ACTIVE":
        raise conflict("The pay group is inactive.", "PAY_GROUP_INACTIVE")
    entity = _entity(db, group)
    cid = principal.company_id
    today = _today(db, cid)
    until = horizon(today)
    current = open_calendar(db, cid, group.id, lock=True)
    if current is not None and row_version is not None and current.row_version != row_version:
        raise conflict("The calendar was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    start = effective_from or today
    materialized = materialize_rules(db, group, entity, rules, start)
    banking_id = banking_calendar_id(db, group, entity, start)
    banking = banking_calendar(db, group, entity, start - timedelta(days=400), until + timedelta(days=120), banking_id)
    new_cal = pay_calendar(frequency, materialized, banking)
    stored = _stored_periods(db, cid, group.id)

    if current is None or not stored:
        if current is not None:  # an open calendar without periods: replace it
            current.status, current.effective_to = "ENDED", max(current.effective_from, start - _ONE_DAY)
            db.flush()
        try:
            first = new_cal.period_containing(start)
        except paycal.CalendarError as exc:
            raise _calendar_error(exc) from None
        calendar = PayCalendar(company_id=cid, pay_group_id=group.id, frequency=new_cal.frequency.value,
                               rules=materialized, effective_from=first.start, status="ACTIVE",
                               banking_calendar_id=banking_id, created_by_user_id=principal.user_id, row_version=1)
        db.add(calendar)
        db.flush()
        created = _extend(db, calendar, group, entity, until)
        synced = sync_pay_group(db, group, calendar, today)
        _audit(db, principal, CALENDAR_SAVED, f"Pay calendar of {group.code}: {calendar.frequency} from "
               f"{calendar.effective_from.isoformat()}, {created} periods", "pay_calendar", calendar.id, cid,
               pay_group_id=group.id, frequency=calendar.frequency, rules=materialized, periods_created=created,
               pay_group_synced=synced)
        schedule_generation(db, cid)
        return calendar

    if effective_from is None:
        raise bad_request("Give effective_from to change an existing calendar.", "EFFECTIVE_FROM_REQUIRED")
    if current.frequency == new_cal.frequency.value and dict(current.rules or {}) == materialized:
        created = _extend(db, current, group, entity, until)
        return current
    old_cal = pay_calendar(current.frequency, current.rules, banking)
    try:
        change = paycal.plan_calendar_change(old_cal, [to_period(p) for p in stored], new_cal, effective_from, until,
                                             locked_until=_locked_until(db, cid, stored))
    except paycal.CalendarError as exc:
        raise _calendar_error(exc) from None
    by_start = {p.period_start: p for p in stored}
    truncated_start = change.closing_transition.start if change.closing_transition is not None else None
    for period in change.dropped:
        row = by_start[period.start]
        if period.start != truncated_start:
            db.delete(row)
    db.flush()
    if change.kept:
        _apply(by_start[change.kept[-1].start], change.kept[-1])
    if change.closing_transition is not None:
        row = by_start[change.closing_transition.start]
        _apply(row, change.closing_transition)
        row.row_version += 1
    db.flush()
    current.status, current.effective_to = "ENDED", effective_from - _ONE_DAY
    current.row_version += 1
    db.flush()
    calendar = PayCalendar(company_id=cid, pay_group_id=group.id, frequency=new_cal.frequency.value,
                           rules=materialized, effective_from=effective_from, status="ACTIVE",
                           banking_calendar_id=banking_id, created_by_user_id=principal.user_id, row_version=1,
                           generated_until=until)
    db.add(calendar)
    db.flush()
    for period in ((change.opening_transition,) if change.opening_transition is not None else ()) + change.new_periods:
        db.add(_new_row(calendar, period))
        db.flush()
    synced = sync_pay_group(db, group, calendar, today)  # later changes follow through the daily job
    _audit(db, principal, CALENDAR_CHANGED,
           f"Pay calendar of {group.code} changed from {current.frequency} to {calendar.frequency} on "
           f"{effective_from.isoformat()}", "pay_calendar", calendar.id, cid, pay_group_id=group.id,
           previous_calendar_id=current.id, frequency=calendar.frequency, rules=materialized,
           dropped=len(change.dropped), closing_transition=change.closing_transition is not None,
           opening_transition=change.opening_transition is not None, periods_created=len(change.new_periods),
           pay_group_synced=synced)
    schedule_generation(db, cid)
    return calendar


def generate_ahead(db: Session, company_id: int, *, pay_group_id: Optional[int] = None,
                   until: Optional[date] = None) -> int:
    """Extend every open calendar (of ACTIVE pay groups) to ``until`` (default: 18 months ahead)."""
    until = until or horizon(_today(db, company_id))
    query = (db.query(PayCalendar).join(PayGroup, and_(PayGroup.company_id == PayCalendar.company_id,
                                                       PayGroup.id == PayCalendar.pay_group_id))
             .filter(PayCalendar.company_id == company_id, PayCalendar.effective_to.is_(None),
                     PayGroup.status == "ACTIVE"))
    if pay_group_id is not None:
        query = query.filter(PayCalendar.pay_group_id == pay_group_id)
    created = 0
    today = _today(db, company_id)
    for calendar in query.order_by(PayCalendar.id).all():
        group = load_pay_group(db, company_id, calendar.pay_group_id, lock=True)
        created += _extend(db, calendar, group, _entity(db, group), until)
        synced = sync_pay_group(db, group, calendar, today)
        if synced:
            _audit(db, None, CALENDAR_IN_FORCE, f"Pay group {group.code} now follows its {calendar.frequency} calendar",
                   "pay_calendar", calendar.id, company_id, **synced)
    if created:
        _audit(db, None, PERIODS_GENERATED, f"{created} pay periods generated through {until.isoformat()}",
               "company", company_id, company_id, until=until.isoformat(), created=created)
    return created


def schedule_generation(db: Session, company_id: int, *, on: Optional[date] = None,
                        include_generation: bool = True) -> None:
    """Queue the tenant's daily payroll maintenance (one job per type and date, idempotent): period
    generation 18 months ahead, and the activation of compensation assignments whose start date has
    come (``compensation.activate_scheduled``, FOLLOW-UPS F-38), and the daily payroll notifications (input
    cutoff reminders and the exceptions digest, ``notifications.schedule_daily``, F-68). The periodic scheduler
    runs the generation job daily (scheduled type ``payroll.generate_periods``, BUG-29); the job re-queues the
    others for the next day, and only a new tenant's first run queues the generation itself."""
    day = on or (_today(db, company_id) + _ONE_DAY)
    run_at = datetime.combine(day, time(1, 0), tzinfo=timezone.utc)
    if include_generation:
        jobs.enqueue(db, company_id=company_id, type=GENERATE_JOB, payload={"run_date": day.isoformat()},
                     idempotency_key=f"generate-periods:{day.isoformat()}", run_at=run_at, priority=200)
    jobs.enqueue(db, company_id=company_id, type=ACTIVATE_COMPENSATION_JOB, payload={"run_date": day.isoformat()},
                 idempotency_key=f"activate-scheduled:{day.isoformat()}", run_at=run_at, priority=150)
    # Input cutoff reminders and the exceptions digest (notify.payroll.*, F-68) follow the same daily rhythm.
    from app.payroll.services import notifications

    notifications.schedule_daily(db, company_id, on=day)
    # The retention sweep (payroll.retention_sweep, legal holds F-73) re-queues itself; this keeps the chain alive.
    from app.payroll.services import legal_holds

    legal_holds.schedule_sweep(db, company_id, at=datetime.combine(day, legal_holds.SWEEP_AT, tzinfo=timezone.utc))


@jobs.register(GENERATE_JOB)
def _generate_job(ctx: jobs.JobContext) -> dict:
    created = generate_ahead(ctx.db, ctx.company_id)
    schedule_generation(ctx.db, ctx.company_id, on=_today(ctx.db, ctx.company_id) + _ONE_DAY, include_generation=False)
    return {"created": created}


# ---------------------------------------------------------------------------
# Reads and lookups
# ---------------------------------------------------------------------------
def get_calendar(db: Session, principal: Principal, pay_group_id: int) -> dict:
    group = scoped_pay_group(db, principal, READ, pay_group_id)
    rows = (db.query(PayCalendar).filter(PayCalendar.company_id == principal.company_id,
                                         PayCalendar.pay_group_id == group.id)
            .order_by(PayCalendar.effective_from).all())
    current = next((r for r in rows if r.effective_to is None), None)
    return {"pay_group_id": group.id, "current": calendar_json(current) if current else None,
            "history": [calendar_json(r) for r in rows]}


def _readable_group(db: Session, principal: Principal, pay_group_id: int) -> PayGroup:
    for permission in (RUN_READ, READ):
        if principal.has(permission):
            group = db.query(PayGroup).filter(PayGroup.company_id == principal.company_id,
                                              PayGroup.id == pay_group_id).first()
            if group is not None and access.target_scope(principal, permission).covers(group.legal_entity_id, group.id):
                return group
    if not (principal.has(RUN_READ) or principal.has(READ)):
        raise forbidden()
    raise not_found("Pay group")


def list_periods(db: Session, principal: Principal, pay_group_id: int, *, start: Optional[date] = None,
                 end: Optional[date] = None, status: Optional[str] = None, skip: int = 0,
                 limit: int = 100) -> Tuple[int, List[dict]]:
    group = _readable_group(db, principal, pay_group_id)
    query = db.query(PayPeriod).filter(PayPeriod.company_id == principal.company_id, PayPeriod.pay_group_id == group.id)
    if start is not None:
        query = query.filter(PayPeriod.period_end >= start)
    if end is not None:
        query = query.filter(PayPeriod.period_start <= end)
    if status is not None:
        query = query.filter(PayPeriod.status == status)
    total = query.count()
    rows = query.order_by(PayPeriod.period_start).offset(skip).limit(limit).all()
    return total, [period_json(r) for r in rows]


def load_period(db: Session, company_id: int, period_id: int, *, lock: bool = False) -> PayPeriod:
    query = db.query(PayPeriod).filter(PayPeriod.company_id == company_id, PayPeriod.id == period_id)
    if lock:
        query = query.with_for_update(of=PayPeriod).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Pay period")
    return row


def period_containing(db: Session, company_id: int, pay_group_id: int, day: date) -> Optional[PayPeriod]:
    return (db.query(PayPeriod)
            .filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id == pay_group_id,
                    PayPeriod.period_start <= day, PayPeriod.period_end >= day)
            .first())


PERIOD_STATE = {"PROCESSING": "INPUT_LOCKED", "CLOSED": "FINALIZED"}


def period_state_on(db: Session, company_id: int, employee_id: int, day: date, *,
                    now: Optional[datetime] = None) -> str:
    """OPEN / INPUT_LOCKED / FINALIZED for an employee's date (SPINE V3-069; LH 05 §5.6; LH-TZ-03).

    The pay period of the employee's pay group (placement valid on ``day``) that contains the date: CLOSED is
    FINALIZED, PROCESSING is INPUT_LOCKED, and an OPEN period becomes INPUT_LOCKED once the **employee's** local today
    is after its ``input_cutoff_on`` (a New York employee of a Dubai pay group locks later than a Dubai colleague).
    No pay group or no stored period: OPEN. Payroll's own snapshot lock always decides what a run contains.
    """
    from app.models.employee import Employee
    from app.models.org import EmployeeAssignment
    from app.services import org_structure_service

    placement = (db.query(EmployeeAssignment)
                 .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                         EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= day,
                         or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= day))
                 .order_by(EmployeeAssignment.effective_from.desc()).first())
    if placement is None or not placement.pay_group_id:
        return "OPEN"
    period = period_containing(db, company_id, placement.pay_group_id, day)
    from app.services import attendance_access

    if attendance_access.v2_enabled(db, company_id):
        # JC-AS-10 (G-16): with a split attendance cycle the date belongs to the period whose window holds it
        # (27 Nov of a 26th-25th cycle is December's input), so its state is that period's state.
        from app.services import payroll_sync

        period = payroll_sync.period_of_window(db, company_id, placement.pay_group_id, day) or period
    if period is None:
        return "OPEN"
    if period.status in PERIOD_STATE:
        return PERIOD_STATE[period.status]
    employee = db.get(Employee, employee_id)
    local_today = org_structure_service.employee_local_date(db, employee, now or clock.utcnow())
    return "INPUT_LOCKED" if local_today > period.input_cutoff_on else "OPEN"


def period_accepts_inputs(db: Session, company_id: int, pay_group_id: int, start: date, end: date) -> bool:
    """Whether the stored pay period [start, end] of the pay group accepts inputs (EC-VAR-03): OPEN and
    PROCESSING do, CLOSED does not; a range with no stored period is open (not generated yet).
    Installed as ``wage_inputs.period_is_open`` by ``services.runs.install_hooks`` (F-35)."""
    row = (db.query(PayPeriod.status)
           .filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id == pay_group_id,
                   PayPeriod.period_start <= end, PayPeriod.period_end >= start)
           .all())
    return all(sm.accepts_inputs(status) for (status,) in row)


def set_period_status(db: Session, period: PayPeriod, action: str) -> PayPeriod:
    """The only writer of ``pay_periods.status`` (``state_machines.PAY_PERIOD``); 409 on an invalid move."""
    try:
        period.status = sm.next_state(sm.PAY_PERIOD, period.status, action)
    except sm.InvalidTransition as exc:
        raise conflict(exc.message, exc.code)
    period.row_version += 1
    db.flush()
    return period


def recheck_pay_date(db: Session, company_id: int, period_id: int) -> dict:
    """Re-validate a stored pay date against today's banking calendar (payment batch creation, EC-CAL-08)."""
    period = load_period(db, company_id, period_id)
    group = load_pay_group(db, company_id, period.pay_group_id)
    entity = _entity(db, group)
    calendar = db.query(PayCalendar).filter(PayCalendar.company_id == company_id,
                                            PayCalendar.id == period.pay_calendar_id).one()
    banking = banking_calendar(db, group, entity, period.period_start, period.pay_date + timedelta(days=60),
                               calendar.banking_calendar_id or banking_calendar_id(db, group, entity, period.pay_date))
    try:
        check = paycal.recheck_pay_date(to_period(period), calendar_rules(calendar.rules, banking))
    except paycal.CalendarError as exc:
        raise _calendar_error(exc) from None
    return {"period_id": period.id, "scheduled": check.scheduled.isoformat(), "current": check.current.isoformat(),
            "payment_submit_by": check.payment_submit_by.isoformat(), "changed": check.changed,
            "issues": [_issue_json(i) for i in check.issues]}


def pay_date_check(db: Session, principal: Principal, period_id: int) -> dict:
    """``recheck_pay_date`` for a caller who may read the period's pay group (404 otherwise). Without any read
    right the answer is 403 before any lookup, so it never tells which period ids exist (EC-SEC-02)."""
    if not (principal.has(RUN_READ) or principal.has(READ)):
        raise forbidden()
    period = load_period(db, principal.company_id, period_id)
    _readable_group(db, principal, period.pay_group_id)
    return recheck_pay_date(db, principal.company_id, period.id)


def period_count(db: Session, company_id: int, pay_group_id: int) -> int:
    return db.query(PayPeriod.id).filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id == pay_group_id).count()
