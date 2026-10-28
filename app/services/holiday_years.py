"""Holiday rules, calendar years, publication and amendments (LH 04-holiday/01 §5.5, §6; 04-holiday/02 §3.7, §4.5,
§6.4; LH 11 §6.10; LH 14 WI-1.8).

Year model. A (calendar, year) is DRAFT (``version`` 0, never counts) or PUBLISHED (``version`` >= 1, counts); a year
with rows but no record is a legacy year and is treated as PUBLISHED v1 (IG-LH-15). Each calendar publishes its own
rows; a calendar that includes another sees its rows only once that year is published.

* Rules: ``create_rule`` / ``update_rule`` / ``deactivate_rule`` validate with ``holiday_recurrence.validate``
  (RV-01..09) and, with ``apply_to_draft_years``, regenerate DRAFT years; PUBLISHED years are never touched and come
  back as ``published_years_affected`` for an amendment (§4.6).
* ``publish_year``: dry run (validations V-01..V-10, impact) -> acknowledged confirm-warnings -> optional second
  person (``holiday.publish.requires_second_person``: the first commit returns 202 with a pending request; another
  publisher completes it while the content hash still matches) -> PUBLISHED v1, change log, recount, notifications.
* ``amend_year``: every change to a PUBLISHED year (add / cancel / move / confirm / edit / working days) in one
  version, reason mandatory except a confirmation on the expected date (P-4), date bands of V3-042 per operation.
* DRAFT rows are edited directly (``edit_draft_row``: edit, SKIP, RESET, set expected date).

Dry runs and impact previews apply the change inside a SAVEPOINT and roll it back, so the preview reads exactly what
the commit would write (flattening, day facts, pay periods). Writes to rows of a PUBLISHED year happen only through
``_amending`` (``SET LOCAL app.holiday_amendment``), which the database guard trigger of 0044 requires.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.holiday import CalendarWorkingDay, Holiday, HolidayCalendarYear, HolidayRule
from app.models.org import Branch
from app.models.pay_org import HolidayCalendar
from app.models.user import User
from app.services import holiday_recurrence as rec
from app.services import holiday_rollover
from app.services.audit_log_service import AuditLogService

AUDIT_MODULE = "Holidays"
FUTURE_YEARS = 2  # V-04: a year at most two years after calendar_local_today's year
RULE_PREVIEW_YEARS = 5
HALF_DAY_HOLIDAYS_ENABLED = False  # LH-M5b gate (04-holiday/01 §5.4)
MAX_IMPACT_EMPLOYEES = 40  # V-02 samples per pay period (one day-facts read each)
CONFIRM_WARNINGS = ("HOLIDAYS_COVER_ALL_WORKING_DAYS", "HOLIDAY_DATE_NEEDED", "PAST_DATES_IN_FIRST_PUBLICATION",
                    "HALF_DAY_HOLIDAYS_COMBINED", "BANK_HOLIDAY_ON_EMPLOYEE_CALENDAR")
CATEGORIES = ("NATIONAL", "REGIONAL", "FESTIVAL", "ELECTION", "COMPANY_EVENT", "SHUTDOWN", "BRIDGE", "EMERGENCY",
              "BANK", "OTHER")
TYPES = ("PUBLIC", "COMPANY", "OPTIONAL")
PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
OPERATIONS = ("add", "cancel", "move", "confirm", "edit", "add_working_day", "remove_working_day")
RULE_EDITABLE = ("name", "name_i18n", "holiday_type", "category", "kind", "params", "portion", "duration_days",
                 "duration_range", "observed_rule", "applies_on", "valid_from_year", "valid_to_year", "source")
# Fields whose change alters what the rule generates (``version`` + 1, §3.2).
RULE_GENERATING = ("name", "holiday_type", "category", "kind", "params", "portion", "duration_days", "duration_range",
                   "observed_rule", "applies_on", "valid_from_year", "valid_to_year")


# ============================================================================================== small helpers
def _svc():
    from app.services import holiday_calendar_service

    return holiday_calendar_service


@contextmanager
def _amending(db: Session):
    """Rows of a PUBLISHED year change only inside an amendment (0044 guard trigger)."""
    db.execute(text("SET LOCAL app.holiday_amendment = 'on'"))
    yield


def _simulate(db: Session, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` inside a SAVEPOINT and roll it back: dry runs read exactly what the commit would write."""
    nested = db.begin_nested()
    try:
        return fn()
    finally:
        if nested.is_active:
            nested.rollback()
        db.expire_all()


def _parse_date(value: Any, field: str = "date") -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise bad_request(f"{field} must be YYYY-MM-DD.", "INVALID_DATE") from None


def _user_name(db: Session, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    user = db.get(User, user_id)
    return user.full_name if user is not None else None


def _reason(value: Any) -> Optional[str]:
    reason = " ".join(str(value or "").split())
    if len(reason) > 500:
        raise bad_request("The reason may have at most 500 characters.", "REASON_TOO_LONG")
    return reason or None


def holiday_setting(db: Session, company_id: int, key: str, default: Any, *,
                    legal_entity_id: Optional[int] = None) -> Any:
    """A ``holiday.*`` setting. An invalid stored value fails closed (LH-WL-02, V3-085): 409 CONFIG_INVALID naming the
    key and level, never the default; a key the registry does not know yet keeps the documented default."""
    from app.payroll import settings_registry
    from app.payroll.services import settings as settings_service

    if key not in settings_registry.REGISTRY:
        return default
    try:
        return settings_service.resolve(db, company_id, key, legal_entity_id=legal_entity_id,
                                        on=clock.company_today(db, company_id)).value
    except settings_service.SettingConfigError as exc:
        level = (exc.context or {}).get("scope_type") or (exc.context or {}).get("level") or "a stored level"
        raise AppError(409, f"The setting {key} has an invalid value at {level}; fix it in Settings first.",
                       "CONFIG_INVALID", extra={"details": {"key": key, "context": {
                           k: v for k, v in (exc.context or {}).items() if isinstance(v, (str, int, float))}}})


def _setting(db: Session, company_id: int, key: str, default: Any, *, legal_entity_id: Optional[int] = None) -> Any:
    return holiday_setting(db, company_id, key, default, legal_entity_id=legal_entity_id)


def requires_second_person(db: Session, calendar: HolidayCalendar) -> bool:
    return bool(_setting(db, calendar.company_id, "holiday.publish.requires_second_person", False,
                         legal_entity_id=calendar.legal_entity_id))


def _bands(db: Session, calendar: HolidayCalendar) -> Tuple[date, int]:
    svc = _svc()
    return svc.calendar_local_today(db, calendar.company_id, calendar.id), svc.backdate_days(db, calendar.company_id)


# ============================================================================================== year records
def year_rows(db: Session, company_id: int, calendar_id: int, year: int) -> List[Holiday]:
    return db.query(Holiday).filter(
        Holiday.company_id == company_id, Holiday.calendar_id == calendar_id,
        Holiday.date >= date(year, 1, 1), Holiday.date <= date(year, 12, 31)).order_by(Holiday.date, Holiday.id).all()


def working_days_of(db: Session, company_id: int, calendar_id: int, year: int) -> List[CalendarWorkingDay]:
    return db.query(CalendarWorkingDay).filter(
        CalendarWorkingDay.company_id == company_id, CalendarWorkingDay.calendar_id == calendar_id,
        CalendarWorkingDay.date >= date(year, 1, 1), CalendarWorkingDay.date <= date(year, 12, 31)
    ).order_by(CalendarWorkingDay.date).all()


def content_hash(db: Session, company_id: int, calendar_id: int, year: int) -> str:
    """SHA-256 of the calendar's own rows of the year (occurrences and declared working days, 01 §6.1); the same
    canonical shape as the 0044 backfill, so a legacy year keeps its hash until it changes."""
    from app.services.holiday_service import content

    payload = {
        "holidays": [content(h) for h in year_rows(db, company_id, calendar_id, year)],
        "working_days": [{"date": w.date.isoformat(), "name": w.name, "portion": w.portion}
                         for w in working_days_of(db, company_id, calendar_id, year)],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def get_record(db: Session, company_id: int, calendar_id: int, year: int, *,
               lock: bool = False) -> Optional[HolidayCalendarYear]:
    query = db.query(HolidayCalendarYear).filter(
        HolidayCalendarYear.company_id == company_id, HolidayCalendarYear.calendar_id == calendar_id,
        HolidayCalendarYear.year == year)
    return (query.with_for_update() if lock else query).first()


def ensure_year(db: Session, company_id: int, calendar: HolidayCalendar, year: int, *,
                lock: bool = False) -> HolidayCalendarYear:
    """The year record, created on the first write: PUBLISHED v1 when the year already has (legacy) rows, else
    DRAFT (11 §11.6 steps 2-3; 01 §6.1)."""
    if not 2000 <= int(year) <= 2100:
        raise bad_request("The year must be between 2000 and 2100.", "INVALID_YEAR")
    record = get_record(db, company_id, calendar.id, year, lock=lock)
    if record is not None:
        return record
    has_rows = db.query(Holiday.id).filter(Holiday.company_id == company_id, Holiday.calendar_id == calendar.id,
                                          Holiday.date >= date(year, 1, 1), Holiday.date <= date(year, 12, 31)).first()
    record = HolidayCalendarYear(company_id=company_id, calendar_id=calendar.id, year=year,
                                 status="PUBLISHED" if has_rows else "DRAFT", version=1 if has_rows else 0,
                                 row_version=1)
    if has_rows:
        record.content_hash = content_hash(db, company_id, calendar.id, year)
        record.published_at = clock.utcnow()
    db.add(record)
    db.flush()
    if lock:
        record = get_record(db, company_id, calendar.id, year, lock=True)
    return record


def state_of(db: Session, company_id: int, calendar_id: int, year: int) -> str:
    """PUBLISHED / DRAFT / NONE (no record and no rows); a legacy year with rows is PUBLISHED."""
    record = get_record(db, company_id, calendar_id, year)
    if record is not None:
        return record.status
    has_rows = db.query(Holiday.id).filter(Holiday.company_id == company_id, Holiday.calendar_id == calendar_id,
                                          Holiday.date >= date(year, 1, 1), Holiday.date <= date(year, 12, 31)).first()
    return "PUBLISHED" if has_rows else "NONE"


def touch_draft(db: Session, record: HolidayCalendarYear) -> None:
    """Any edit of a DRAFT year voids its pending publication request (V3-027) and bumps its row version."""
    if record.status != "DRAFT":
        return
    if record.publish_requested_by_user_id is not None:
        record.publish_requested_by_user_id = None
        record.publish_requested_at = None
        record.requested_content_hash = None
        record.publish_request_payload = None
    record.row_version = (record.row_version or 1) + 1


def _clear_request(record: HolidayCalendarYear) -> None:
    record.publish_requested_by_user_id = None
    record.publish_requested_at = None
    record.requested_content_hash = None
    record.publish_request_payload = None


def year_json(db: Session, record: Optional[HolidayCalendarYear], calendar: HolidayCalendar, year: int) -> dict:
    status = record.status if record is not None else state_of(db, calendar.company_id, calendar.id, year)
    pending = None
    if record is not None and record.publish_requested_by_user_id:
        payload = record.publish_request_payload or {}
        pending = {"requested_by_user_id": record.publish_requested_by_user_id,
                   "requested_by": _user_name(db, record.publish_requested_by_user_id),
                   "requested_at": record.publish_requested_at.isoformat() if record.publish_requested_at else None,
                   "content_hash": record.requested_content_hash, "kind": payload.get("kind", "PUBLISH"),
                   "operations": payload.get("operations", []), "reason": payload.get("reason")}
    return {
        "calendar_id": calendar.id, "year": year, "status": status,
        "version": record.version if record is not None else (1 if status == "PUBLISHED" else 0),
        "content_hash": record.content_hash if record is not None else None,
        "published_at": record.published_at.isoformat() if record is not None and record.published_at else None,
        "published_by": _user_name(db, record.published_by_user_id) if record is not None else None,
        "approved_by": _user_name(db, record.approved_by_user_id) if record is not None else None,
        "generated_at": record.generated_at.isoformat() if record is not None and record.generated_at else None,
        "row_version": record.row_version if record is not None else None,
        "pending_request": pending,
    }


# ============================================================================================== serializers
def serialize_occurrence(h: Holiday, *, own: bool = True, source_code: Optional[str] = None,
                         path: Sequence[str] = (), today: Optional[date] = None,
                         window_start: Optional[date] = None, year_status: Optional[str] = None) -> dict:
    ref = h.source_ref or {}
    out = {
        "id": h.id, "calendar_id": h.calendar_id, "date": h.date.isoformat(), "weekday": h.date.weekday(),
        "name": h.name, "name_i18n": h.name_i18n or {}, "holiday_type": h.holiday_type, "category": h.category,
        "portion": h.portion, "status": h.status, "observance": h.observance,
        "observed_from_date": h.observed_from_date.isoformat() if h.observed_from_date else None,
        "sequence_index": h.sequence_index, "rule_id": h.rule_id, "source": h.source,
        "rule_year": ref.get("rule_year"), "occurrence_key": ref.get("occurrence_key"),
        "overridden": bool(ref.get("overridden")), "moved_from": ref.get("moved_from"),
        "expected_source": ref.get("expected_source"), "legal_ref": ref.get("legal_ref"),
        "issued_on": ref.get("issued_on"), "description": h.description, "row_version": h.row_version,
        "own": own, "source_code": source_code, "path": list(path), "year_status": year_status,
        "confirmed_at": h.confirmed_at.isoformat() if h.confirmed_at else None,
    }
    if today is not None and own:
        start = window_start or today
        future = h.date > today
        in_window = start <= h.date <= today
        provisional = h.status == "PROVISIONAL"
        live = h.status != "CANCELLED"
        if year_status == "DRAFT":
            out["actions"] = ["edit", "skip"] + (["reset"] if ref.get("overridden") else []) + \
                (["confirm"] if provisional else [])
        else:
            actions = []
            if live and (future or (in_window and provisional)):
                actions += ["cancel", "move"]
            if provisional:
                actions.append("confirm")
            if live and future:
                actions.append("edit")
            elif live:
                actions.append("describe")
            out["actions"] = actions
    return out


def _rule_summary(row: HolidayRule) -> str:
    p = row.params or {}
    months = rec._MONTH_NAMES  # noqa: SLF001 - display names of the engine
    days = rec._WEEKDAY_NAMES  # noqa: SLF001
    kind = row.kind
    if kind == "FIXED_DATE":
        text_ = f"Every {p.get('day')} {months[int(p.get('month', 1)) - 1]}"
        if p.get("offset_days"):
            text_ += f", shifted by {int(p['offset_days']):+d} days"
    elif kind == "NTH_WEEKDAY":
        text_ = f"{rec._ORDINALS.get(p.get('nth'), p.get('nth'))} {days[int(p.get('weekday', 0))]} of " \
                f"{months[int(p.get('month', 1)) - 1]}"  # noqa: SLF001
        if p.get("offset_days"):
            text_ += f" {int(p['offset_days']):+d} days"
    elif kind == "WEEKDAY_RELATIVE":
        words = {"ON_OR_AFTER": "on or after", "AFTER": "after", "ON_OR_BEFORE": "on or before", "BEFORE": "before"}
        text_ = f"{days[int(p.get('weekday', 0))]} {words.get(p.get('direction'), '')} {p.get('day')} " \
                f"{months[int(p.get('month', 1)) - 1]}"
    elif kind == "EASTER_OFFSET":
        offset = int(p.get("offset_days", 0))
        where = "Easter Sunday" if offset == 0 else f"{abs(offset)} days {'after' if offset > 0 else 'before'} Easter"
        text_ = f"{where} ({(p.get('tradition') or 'WESTERN').title()})"
    elif kind == "ANNUAL_CONFIRM":
        span = row.duration_range or [row.duration_days, row.duration_days]
        length = f"{span[0]}–{span[1]} days" if span[0] != span[1] else f"{span[0]} day{'s' if span[0] > 1 else ''}"
        text_ = f"Announced every year, {length}"
    elif kind == "ONE_OFF":
        text_ = f"Once, {p.get('date')}"
    else:
        text_ = kind
    if row.duration_days and row.duration_days > 1 and kind != "ANNUAL_CONFIRM":
        text_ += f", {row.duration_days} days"
    return text_


def serialize_rule(db: Session, row: HolidayRule, calendar: HolidayCalendar, today: date) -> dict:
    next_dates: List[str] = []
    if row.status == "ACTIVE":
        try:
            spec = holiday_rollover.rule_spec(row)
            weekend = frozenset(_svc().effective_weekend(db, calendar))
            expected = holiday_rollover.expected_dates([row])
            for year in range(max(today.year, row.valid_from_year), min(today.year + 6, 2101)):
                start = expected.get((row.id, year))
                if row.kind == "ANNUAL_CONFIRM" and start is None:
                    continue
                for o in rec.expand(spec, year, weekend, expected_start=start):
                    if o.date >= today and o.sequence_index == 1:
                        next_dates.append(o.date.isoformat())
                if len(next_dates) >= 3:
                    break
        except rec.RecurrenceError:
            next_dates = []
    ref = row.source_ref or {}
    return {
        "id": row.id, "calendar_id": row.calendar_id, "name": row.name, "name_i18n": row.name_i18n or {},
        "holiday_type": row.holiday_type, "category": row.category, "kind": row.kind, "params": row.params or {},
        "portion": row.portion, "duration_days": row.duration_days, "duration_range": row.duration_range,
        "observed_rule": row.observed_rule, "applies_on": row.applies_on, "valid_from_year": row.valid_from_year,
        "valid_to_year": row.valid_to_year, "status": row.status, "version": row.version, "source": row.source,
        "legal_ref": ref.get("legal_ref"), "issued_on": ref.get("issued_on"),
        "expected_dates": ref.get("expected_dates") or {}, "row_version": row.row_version,
        "summary": _rule_summary(row), "next_dates": sorted(next_dates)[:3],
        "generated_rows": db.query(func.count(Holiday.id)).filter(
            Holiday.company_id == row.company_id, Holiday.rule_id == row.id).scalar() or 0,
    }


# ============================================================================================== rules (02 §3)
def _rule_issue_error(issues: Sequence[rec.Issue]) -> AppError:
    errors = [i for i in issues if i.severity == "error"]
    first = errors[0]
    status = 409 if first.code == "HOLIDAY_IN_PAST" else 422
    code = first.code if first.code in ("INVALID_PORTION", "HOLIDAY_IN_PAST") else "RULE_INVALID"
    return AppError(status, first.message, code, extra={"details": [
        {"field": i.field, "message": i.message, "code": i.code, "reason": i.reason} for i in errors]})


def _clean_rule(data: Mapping[str, Any], current: Optional[HolidayRule], default_year: int) -> Dict[str, Any]:
    """Request fields -> column values (types normalised; the engine validates meaning)."""
    def pick(key, default=None):
        if key in data:
            return data[key]
        return getattr(current, key) if current is not None else default

    def as_int(value, field):
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise AppError(422, f"{field} must be a whole number.", "RULE_INVALID",
                           extra={"details": [{"field": field, "message": "must be a whole number"}]}) from None

    name = " ".join(str(pick("name") or "").split())
    params = pick("params") or {}
    if not isinstance(params, Mapping):
        raise AppError(422, "params must be an object.", "RULE_INVALID",
                       extra={"details": [{"field": "params", "message": "must be an object"}]})
    duration_range = pick("duration_range")
    applies_on = pick("applies_on")
    return {
        "name": name, "name_i18n": dict(pick("name_i18n") or {}),
        "holiday_type": pick("holiday_type", "COMPANY") or "COMPANY", "category": pick("category", "OTHER") or "OTHER",
        "kind": pick("kind"), "params": dict(params), "portion": pick("portion", "FULL") or "FULL",
        "duration_days": as_int(pick("duration_days", 1) or 1, "duration_days"),
        "duration_range": [int(x) for x in duration_range] if duration_range else None,
        "observed_rule": pick("observed_rule", "NONE") or "NONE",
        "applies_on": sorted(int(x) for x in applies_on) if applies_on else None,
        "valid_from_year": as_int(pick("valid_from_year", default_year) or default_year, "valid_from_year"),
        "valid_to_year": as_int(pick("valid_to_year"), "valid_to_year"),
        "source": pick("source", "MANUAL") or "MANUAL",
    }


def _spec(values: Mapping[str, Any], rule_id: Optional[int] = None, status: str = "ACTIVE",
          version: int = 1) -> rec.RuleSpec:
    return rec.RuleSpec(
        kind=values["kind"], params=values["params"], name=values["name"], id=rule_id,
        holiday_type=values["holiday_type"], category=values["category"], portion=values["portion"],
        duration_days=values["duration_days"],
        duration_range=tuple(values["duration_range"]) if values["duration_range"] else None,
        observed_rule=values["observed_rule"],
        applies_on=frozenset(values["applies_on"]) if values["applies_on"] else None,
        valid_from_year=values["valid_from_year"], valid_to_year=values["valid_to_year"], status=status,
        version=version, name_i18n=values["name_i18n"])


def _validate_rule(db: Session, calendar: HolidayCalendar, values: Mapping[str, Any], *, rule_id: Optional[int],
                   status: str = "ACTIVE", check_past: bool = True) -> List[rec.Issue]:
    if values["kind"] not in rec.KINDS or values["kind"] == "RRULE":
        message = "Repeating rules in iCalendar format can only be imported." if values["kind"] == "RRULE" \
            else "Choose how the holiday repeats."
        raise AppError(422, message, "RULE_INVALID", extra={"details": [{"field": "kind", "message": message}]})
    if values["source"] not in ("MANUAL", "PACK", "IMPORT", "GOVT_NOTICE"):
        raise AppError(422, "Unknown rule source.", "RULE_INVALID",
                       extra={"details": [{"field": "source", "message": "unknown source"}]})
    svc = _svc()
    today = svc.calendar_local_today(db, calendar.company_id, calendar.id)
    others = [holiday_rollover.rule_spec(r) for r in holiday_rollover.rules_of(db, calendar.company_id, calendar.id)
              if r.id != rule_id]
    try:
        spec = _spec(values, rule_id, status)
        issues = rec.validate(spec, weekend_days=svc.effective_weekend(db, calendar),
                              today=today if check_past else None, half_days_enabled=HALF_DAY_HOLIDAYS_ENABLED,
                              other_rules=others)
    except rec.RecurrenceError as exc:
        raise AppError(422, exc.message, "RULE_INVALID",
                       extra={"details": [{"field": i.field, "message": i.message} for i in exc.issues]}) from None
    if any(i.severity == "error" for i in issues):
        raise _rule_issue_error(issues)
    return issues


def _rule_or_404(db: Session, principal: Principal, rule_id: int) -> Tuple[HolidayRule, HolidayCalendar]:
    rule = db.query(HolidayRule).filter(HolidayRule.company_id == principal.company_id,
                                        HolidayRule.id == int(rule_id)).first()
    if rule is None:
        raise not_found("Holiday rule")
    calendar = _svc()._get_managed(db, principal, rule.calendar_id)  # noqa: SLF001
    return rule, calendar


def _affected_years(db: Session, calendar: HolidayCalendar, first: int, last: Optional[int],
                    apply_to_draft_years: bool, actor: Optional[Principal]) -> Tuple[List[dict], List[int]]:
    """Regenerate DRAFT years of the window; list PUBLISHED years whose rule output differs (§4.6)."""
    cid = calendar.company_id
    today = _svc().calendar_local_today(db, cid, calendar.id)
    low = max(first - 1, 2000)
    high = min((last if last is not None else today.year + FUTURE_YEARS) + 1, 2100)
    draft_changes: List[dict] = []
    if apply_to_draft_years:
        for (year,) in db.query(HolidayCalendarYear.year).filter(
                HolidayCalendarYear.company_id == cid, HolidayCalendarYear.calendar_id == calendar.id,
                HolidayCalendarYear.status == "DRAFT", HolidayCalendarYear.year >= low,
                HolidayCalendarYear.year <= high).order_by(HolidayCalendarYear.year):
            result = holiday_rollover.generate(db, cid, calendar.id, year, actor)
            draft_changes.append({"year": year, "counts": result["counts"]})
    # A published year is reported only when some difference can still be applied: dates before the backdate
    # window are kept for payroll history (V3-042), so offering them would only end in BACKDATE_LIMIT.
    earliest = (today - timedelta(days=_svc().backdate_days(db, cid))).isoformat()
    affected: List[int] = []
    for year in range(max(low, today.year - 1), min(high, today.year + FUTURE_YEARS) + 1):
        if year in holiday_rollover.published_years(db, cid, calendar.id, [year]) and any(
                line["date"] >= earliest for line in holiday_rollover.published_difference(db, cid, calendar, year)):
            affected.append(year)
    return draft_changes, affected


def list_rules(db: Session, principal: Principal, calendar_id: int, status: Optional[str] = None) -> dict:
    calendar = _svc()._get_visible(db, principal, calendar_id)  # noqa: SLF001
    today = _svc().calendar_local_today(db, principal.company_id, calendar.id)
    query = db.query(HolidayRule).filter(HolidayRule.company_id == principal.company_id,
                                         HolidayRule.calendar_id == calendar.id)
    if status:
        if status not in ("ACTIVE", "INACTIVE"):
            raise bad_request("status must be ACTIVE or INACTIVE.", "INVALID_STATUS")
        query = query.filter(HolidayRule.status == status)
    rows = query.order_by(HolidayRule.status, HolidayRule.name).all()
    return {"calendar_id": calendar.id, "local_today": today.isoformat(),
            "items": [serialize_rule(db, r, calendar, today) for r in rows]}


def get_rule(db: Session, principal: Principal, rule_id: int) -> dict:
    rule = db.query(HolidayRule).filter(HolidayRule.company_id == principal.company_id,
                                        HolidayRule.id == int(rule_id)).first()
    if rule is None:
        raise not_found("Holiday rule")
    calendar = _svc()._get_visible(db, principal, rule.calendar_id)  # noqa: SLF001
    return serialize_rule(db, rule, calendar, _svc().calendar_local_today(db, principal.company_id, calendar.id))


def _source_ref(data: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None) -> dict:
    ref = dict(current or {})
    for key in ("legal_ref", "issued_on"):
        if key in data:
            value = (str(data[key]).strip() or None) if data[key] is not None else None
            if key == "legal_ref" and value and len(value) > 100:
                raise AppError(422, "Legal reference may have at most 100 characters.", "RULE_INVALID",
                               extra={"details": [{"field": key, "message": "at most 100 characters"}]})
            if key == "issued_on" and value:
                value = _parse_date(value, "issued_on").isoformat()
            ref[key] = value
    return ref


def create_rule(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]) -> dict:
    """POST /{id}/rules (§3.7): 201 rule, ``draft_changes`` and ``published_years_affected``."""
    svc = _svc()
    calendar = svc._get_managed(db, principal, calendar_id)  # noqa: SLF001
    if calendar.status != "ACTIVE":
        raise bad_request(f"Holiday calendar {calendar.code} is inactive.", "ORG_DIMENSION_INACTIVE")
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    values = _clean_rule(data, None, today.year)
    issues = _validate_rule(db, calendar, values, rule_id=None)
    row = HolidayRule(company_id=principal.company_id, calendar_id=calendar.id, status="ACTIVE", version=1,
                      row_version=1, created_by_user_id=principal.user_id, source_ref=_source_ref(data), **values)
    db.add(row)
    svc._flush(db)  # noqa: SLF001
    draft_changes, affected = _affected_years(db, calendar, row.valid_from_year, row.valid_to_year,
                                              bool(data.get("apply_to_draft_years", True)), principal)
    AuditLogService.log(db, principal, AUDIT_MODULE, "RULE_SAVED", f"{calendar.code}: rule {row.name} created",
                        "holiday_rule", row.id, details={"after": serialize_rule(db, row, calendar, today)})
    return {"rule": serialize_rule(db, row, calendar, today), "draft_changes": draft_changes,
            "published_years_affected": affected,
            "info": [{"code": i.code, "message": i.message} for i in issues if i.severity != "error"]}


def update_rule(db: Session, principal: Principal, rule_id: int, data: Mapping[str, Any], *,
                expected_row_version: Optional[int] = None) -> dict:
    """PATCH /rules/{rid}: ``kind`` is immutable once the rule generated a row (create a new rule instead)."""
    svc = _svc()
    rule, calendar = _rule_or_404(db, principal, rule_id)
    svc._check_version(rule, expected_row_version)  # noqa: SLF001
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    before = serialize_rule(db, rule, calendar, today)
    values = _clean_rule(data, rule, today.year)
    if values["kind"] != rule.kind and db.query(Holiday.id).filter(Holiday.company_id == rule.company_id,
                                                                    Holiday.rule_id == rule.id).first():
        raise AppError(422, "The kind of a rule that has generated holidays cannot change; create a new rule.",
                       "RULE_INVALID", extra={"details": [{"field": "kind", "message": "immutable after generation"}]})
    issues = _validate_rule(db, calendar, values, rule_id=rule.id, status=rule.status,
                            check_past=values["kind"] == "ONE_OFF" and values["params"] != rule.params)
    generating = any(values[k] != getattr(rule, k) for k in RULE_GENERATING)
    for key, value in values.items():
        setattr(rule, key, value)
    rule.source_ref = _source_ref(data, rule.source_ref)
    if generating:
        rule.version = (rule.version or 1) + 1
    rule.row_version = (rule.row_version or 1) + 1
    svc._flush(db)  # noqa: SLF001
    draft_changes, affected = _affected_years(db, calendar, min(rule.valid_from_year, before["valid_from_year"]),
                                              None, bool(data.get("apply_to_draft_years", True)), principal)
    after = serialize_rule(db, rule, calendar, today)
    AuditLogService.log(db, principal, AUDIT_MODULE, "RULE_SAVED", f"{calendar.code}: rule {rule.name} saved",
                        "holiday_rule", rule.id, details={"before": before, "after": after})
    return {"rule": after, "draft_changes": draft_changes, "published_years_affected": affected,
            "info": [{"code": i.code, "message": i.message} for i in issues if i.severity != "error"]}


def deactivate_rule(db: Session, principal: Principal, rule_id: int, *, reason: Optional[str],
                    apply_to_draft_years: bool = True, expected_row_version: Optional[int] = None) -> dict:
    """POST /rules/{rid}/deactivate (V3-096): INACTIVE rules generate nothing; published rows stay."""
    svc = _svc()
    rule, calendar = _rule_or_404(db, principal, rule_id)
    svc._check_version(rule, expected_row_version)  # noqa: SLF001
    if rule.status == "INACTIVE":
        raise conflict("This rule is already inactive.", "INVALID_TRANSITION")
    rule.status = "INACTIVE"
    rule.row_version = (rule.row_version or 1) + 1
    svc._flush(db)  # noqa: SLF001
    draft_changes, affected = _affected_years(db, calendar, rule.valid_from_year, rule.valid_to_year,
                                              apply_to_draft_years, principal)
    published = sorted({h.date.year for h in db.query(Holiday).filter(
        Holiday.company_id == rule.company_id, Holiday.rule_id == rule.id, Holiday.status != "CANCELLED")})
    AuditLogService.log(db, principal, AUDIT_MODULE, "RULE_SAVED", f"{calendar.code}: rule {rule.name} deactivated",
                        "holiday_rule", rule.id, details={"status": "INACTIVE", "reason": _reason(reason)})
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    return {"rule": serialize_rule(db, rule, calendar, today), "draft_changes": draft_changes,
            "published_years_affected": sorted(set(affected) | set(published))}


def set_expected_date(db: Session, principal: Principal, rule_id: int, year: int, day: Optional[Any]) -> dict:
    """The TENANT expected start of an ANNUAL_CONFIRM rule for one year (§6.2, IG-LH-17); regenerates a DRAFT
    year, and reports a PUBLISHED one for an amendment."""
    svc = _svc()
    rule, calendar = _rule_or_404(db, principal, rule_id)
    if rule.kind != "ANNUAL_CONFIRM":
        raise AppError(422, "Only holidays announced every year have expected dates.", "RULE_INVALID",
                       extra={"details": [{"field": "kind", "message": "not ANNUAL_CONFIRM"}]})
    if not rule.valid_from_year <= int(year) <= (rule.valid_to_year or 2100):
        raise AppError(422, f"The rule is not valid in {year}.", "RULE_INVALID",
                       extra={"details": [{"field": "year", "message": "outside the rule's years"}]})
    ref = dict(rule.source_ref or {})
    dates = dict(ref.get("expected_dates") or {})
    if day in (None, ""):
        dates.pop(str(year), None)
    else:
        start = _parse_date(day)
        if start.year not in (int(year) - 1, int(year), int(year) + 1):
            raise AppError(422, "The expected date must be close to its year.", "RULE_INVALID",
                           extra={"details": [{"field": "date", "message": "outside the rule-year window"}]})
        dates[str(year)] = start.isoformat()
    ref["expected_dates"] = dates
    rule.source_ref = ref
    rule.row_version = (rule.row_version or 1) + 1
    svc._flush(db)  # noqa: SLF001
    draft_changes, affected = _affected_years(db, calendar, int(year), int(year), True, principal)
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    return {"rule": serialize_rule(db, rule, calendar, today), "draft_changes": draft_changes,
            "published_years_affected": affected}


def preview_rule(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    """POST /rules/preview (§3.7): pure; the calendar's other rules and fixed rows make observed chains real."""
    svc = _svc()
    if not data.get("calendar_id"):
        raise bad_request("calendar_id is required.", "VALIDATION_ERROR")
    calendar = svc._get_visible(db, principal, int(data["calendar_id"]))  # noqa: SLF001
    if not (principal.is_company_admin or any(principal.scopes(c) for c in ("holiday:manage", "holiday:publish"))):
        raise forbidden("Previewing rules needs the holiday manage permission.")
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    rule_id = data.get("rule_id")
    current = None
    if rule_id:
        current = db.query(HolidayRule).filter(HolidayRule.company_id == principal.company_id,
                                               HolidayRule.id == int(rule_id),
                                               HolidayRule.calendar_id == calendar.id).first()
    values = _clean_rule(data.get("rule") or data, current, today.year)
    years = int(data.get("years") or RULE_PREVIEW_YEARS)
    if not 1 <= years <= rec.MAX_PREVIEW_YEARS:
        raise AppError(422, f"years must be 1-{rec.MAX_PREVIEW_YEARS}.", "RULE_INVALID",
                       extra={"details": [{"field": "years", "message": "1-10"}]})
    from_year = int(data.get("from_year") or max(values["valid_from_year"], today.year))
    issues = _validate_rule(db, calendar, values, rule_id=current.id if current else None, check_past=False)
    weekend = svc.effective_weekend(db, calendar)
    spec = _spec(values, current.id if current else None)
    others = [holiday_rollover.rule_spec(r) for r in holiday_rollover.rules_of(db, principal.company_id, calendar.id)
              if current is None or r.id != current.id]
    out = []
    for year in range(from_year, min(from_year + years, 2101)):
        _, _, fixed = holiday_rollover._inputs(db, principal.company_id, calendar, year)  # noqa: SLF001
        fixed = [f for f in fixed if current is None or f.rule_id != current.id]
        expected = {(spec.id, int(y)): date.fromisoformat(d) for y, d in
                    ((current.source_ref or {}).get("expected_dates") or {}).items()} if current else {}
        result = rec.generate_year([spec, *others], year, weekend, fixed, expected)
        rows = [r for r in result.rows if r.rule is spec]
        notes = [{"code": n.code, "message": n.message, "severity": n.severity} for n in result.notes
                 if n.rule_id == spec.id]
        out.append({"year": year, "rows": [holiday_rollover.occurrence_json(r) for r in rows], "notes": notes})
    return {"calendar_id": calendar.id, "weekend": weekend, "years": out,
            "info": [{"code": i.code, "message": i.message} for i in issues if i.severity != "error"]}


# ============================================================================================== year view
def get_year(db: Session, principal: Principal, calendar_id: int, year: int) -> dict:
    """GET /{id}/years/{year}: status, version, own rows (every status) and inherited published rows, declared
    working days, rule notes (dates still needed) and what the viewer may do."""
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    cid = principal.company_id
    record = get_record(db, cid, calendar.id, year)
    status = record.status if record is not None else state_of(db, cid, calendar.id, year)
    today, window = _bands(db, calendar)
    start = today - timedelta(days=window)
    own = [serialize_occurrence(h, own=True, source_code=calendar.code, today=today, window_start=start,
                                year_status=status) for h in year_rows(db, cid, calendar.id, year)]
    flat = svc.Flattener(db, cid).flatten(calendar.id, date(year, 1, 1), date(year, 12, 31))
    codes: Dict[int, Optional[str]] = {}
    inherited = []
    ids = [f.row.id for f in flat.rows if f.inherited]
    rows = {h.id: h for h in db.query(Holiday).filter(Holiday.company_id == cid, Holiday.id.in_(ids or [-1]))}
    for f in flat.rows:
        if f.inherited and f.row.id in rows:
            for c in f.path:
                if c not in codes:
                    codes[c] = svc._code(db, cid, c)  # noqa: SLF001
            inherited.append(serialize_occurrence(rows[f.row.id], own=False, source_code=codes.get(f.row.calendar_id),
                                                  path=[codes.get(c) or str(c) for c in f.path[1:]]))
    notes: List[dict] = []
    if status != "PUBLISHED":
        try:
            notes = [holiday_rollover._issue_json(n) for n in  # noqa: SLF001
                     holiday_rollover.run_generator(db, cid, calendar, year).notes]
        except rec.RecurrenceError:
            notes = []
    live = [o for o in own if o["status"] != "CANCELLED"]
    can_manage = svc.can_act(db, principal, calendar)
    can_publish = svc.can_act(db, principal, calendar, "holiday:publish")
    second = requires_second_person(db, calendar)
    out = year_json(db, record, calendar, year)
    out.update({
        "calendar_code": calendar.code, "calendar_name": calendar.name, "local_today": today.isoformat(),
        "backdate_days": window, "window_start": start.isoformat(),
        "items": sorted(own + inherited, key=lambda o: (o["date"], not o["own"], o["name"])),
        "working_days": [_working_day_json(w) for w in working_days_of(db, cid, calendar.id, year)],
        "notes": notes,
        "summary": {"own": len(live), "inherited": len(inherited),
                    "expected": sum(1 for o in live if o["status"] == "PROVISIONAL"),
                    "cancelled": len(own) - len(live), "rules": db.query(func.count(HolidayRule.id)).filter(
                        HolidayRule.company_id == cid, HolidayRule.calendar_id == calendar.id,
                        HolidayRule.status == "ACTIVE").scalar() or 0},
        "can_manage": can_manage, "can_publish": can_publish, "requires_second_person": second,
        "current_content_hash": content_hash(db, cid, calendar.id, year),
    })
    return out


def _working_day_json(w: CalendarWorkingDay) -> dict:
    return {"id": w.id, "calendar_id": w.calendar_id, "date": w.date.isoformat(), "weekday": w.date.weekday(),
            "name": w.name, "portion": w.portion, "reason": w.reason, "row_version": w.row_version}


def generate_year(db: Session, principal: Principal, calendar_id: int, year: int) -> dict:
    """POST /{id}/years/{year}/generate: regenerate the DRAFT year from the rules (YEAR_PUBLISHED otherwise)."""
    svc = _svc()
    calendar = svc._get_managed(db, principal, calendar_id)  # noqa: SLF001
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    if year < today.year:
        raise conflict(f"{year} has passed; past years are not generated.", "HOLIDAY_IN_PAST")
    if year > today.year + FUTURE_YEARS:
        raise AppError(422, f"Years can be prepared up to {today.year + FUTURE_YEARS}.", "FUTURE_LIMIT")
    result = holiday_rollover.generate(db, principal.company_id, calendar.id, year, principal)
    result["year_record"] = year_json(db, get_record(db, principal.company_id, calendar.id, year), calendar, year)
    return result


def delete_draft_year(db: Session, principal: Principal, calendar_id: int, year: int) -> dict:
    """DELETE /{id}/years/{year}: discard a DRAFT year and its rows (V3-096)."""
    svc = _svc()
    calendar = svc._get_managed(db, principal, calendar_id)  # noqa: SLF001
    record = get_record(db, principal.company_id, calendar.id, year, lock=True)
    if record is None:
        raise not_found("Holiday year")
    if record.status == "PUBLISHED":
        raise conflict(f"{year} is published; it cannot be discarded.", "YEAR_PUBLISHED")
    removed = 0
    for h in year_rows(db, principal.company_id, calendar.id, year):
        db.delete(h)
        removed += 1
    for w in working_days_of(db, principal.company_id, calendar.id, year):
        db.delete(w)
    db.delete(record)
    svc._flush(db)  # noqa: SLF001
    AuditLogService.log(db, principal, AUDIT_MODULE, "YEAR_DISCARDED", f"{calendar.code} {year}: draft discarded",
                        "holiday_calendar", calendar.id, details={"year": year, "rows": removed})
    return {"year": year, "removed": removed}


# ============================================================================================== impact (01 §6.3)
def _affected_calendars(db: Session, flattener, calendar: HolidayCalendar) -> List[int]:
    svc = _svc()
    return sorted(svc.dependents(flattener.includes, calendar.id))


def _flat_state(db: Session, company_id: int, calendar_ids: Sequence[int], year: int,
                drafts: Iterable[Tuple[int, int]] = ()) -> Dict[int, Dict[date, List[Tuple[int, str, str]]]]:
    svc = _svc()
    flattener = svc.Flattener(db, company_id, drafts=drafts)
    out: Dict[int, Dict[date, List[Tuple[int, str, str]]]] = {}
    for cal in calendar_ids:
        flat = flattener.flatten(cal, date(year - 1, 12, 1), date(year + 1, 1, 31))
        by_date: Dict[date, List[Tuple[int, str, str]]] = defaultdict(list)
        for f in flat.rows:
            by_date[f.row.date].append((f.row.id, f.row.name, f.row.holiday_type))
        out[cal] = by_date
    return out


def _diff_state(before, after) -> Tuple[Dict[int, Dict[date, Tuple[list, list]]], Set[date]]:
    changes: Dict[int, Dict[date, Tuple[list, list]]] = defaultdict(dict)
    dates: Set[date] = set()
    for cal in set(before) | set(after):
        b, a = before.get(cal, {}), after.get(cal, {})
        for day in set(b) | set(a):
            if sorted(b.get(day, [])) != sorted(a.get(day, [])):
                changes[cal][day] = (b.get(day, []), a.get(day, []))
                dates.add(day)
    return changes, dates


def _people(db: Session, company_id: int, calendar_ids: Sequence[int], year: int) -> Tuple[Set[int], Dict[str, int]]:
    """Employees who resolve to the calendars on a date of ``year`` or later, and their count per branch."""
    from app.models.org import EmployeeAssignment

    svc = _svc()
    employees: Set[int] = set()
    for cal in calendar_ids:
        employees |= svc.calendar_users(db, company_id, cal, date(year, 1, 1))[1]
    branch_of: Dict[int, str] = {}
    if employees:
        names = dict(db.query(Branch.id, Branch.name).filter(Branch.company_id == company_id))
        for a in db.query(EmployeeAssignment).filter(
                EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(sorted(employees)),
                EmployeeAssignment.is_primary.is_(True)).order_by(EmployeeAssignment.effective_from):
            branch_of[a.employee_id] = names.get(a.branch_id, "—")
    by_branch: Dict[str, int] = defaultdict(int)
    for name in branch_of.values():
        by_branch[name] += 1
    return employees, dict(sorted(by_branch.items()))


def _leave_impact(db: Session, company_id: int, employees: Set[int], dates: Set[date]) -> dict:
    from app.models.leave import Leave
    from app.models.time_inputs import LeaveDay

    if not employees or not dates:
        return {"approved_requests": 0, "dates": 0, "units": "0.00", "pending_requests": 0}
    rows = db.query(LeaveDay.leave_id, LeaveDay.units).join(
        Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id)).filter(
        LeaveDay.company_id == company_id, LeaveDay.employee_id.in_(sorted(employees)),
        LeaveDay.date.in_(sorted(dates)), LeaveDay.voided_at.is_(None)).all()
    pending = db.query(func.count(Leave.id)).filter(
        Leave.company_id == company_id, Leave.employee_id.in_(sorted(employees)),
        func.lower(Leave.status) == "pending",
        Leave.start_date <= max(dates), Leave.end_date >= min(dates)).scalar() or 0
    units = sum((r.units for r in rows), start=type(rows[0].units)("0")) if rows else 0
    return {"approved_requests": len({r.leave_id for r in rows}), "dates": len(rows), "units": f"{units:.2f}",
            "pending_requests": pending}


def _pay_periods(db: Session, company_id: int, employees: Set[int], dates: Set[date]) -> List[dict]:
    from app.models.org import EmployeeAssignment
    from app.services.holiday_work_weeks import _locked_periods

    if not employees or not dates:
        return []
    assignments = db.query(EmployeeAssignment).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(sorted(employees))).all()
    return _locked_periods(db, company_id, assignments, sorted(dates))


def _covered_periods(db: Session, company_id: int, employees: Set[int], dates: Set[date]) -> List[dict]:
    """V-02: pay periods (stored, of the affected employees' pay groups) containing a changed date in which every
    scheduled working day of an employee is now a holiday (contract §28.2)."""
    from app.models.org import EmployeeAssignment
    from app.models.pay_org import PayGroup
    from app.models.pay_runs import PayPeriod
    from app.payroll.services import time_inputs

    if not employees or not dates:
        return []
    by_group: Dict[int, List[int]] = defaultdict(list)
    for a in db.query(EmployeeAssignment).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(sorted(employees)),
            EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.pay_group_id.isnot(None)):
        if a.employee_id not in by_group[a.pay_group_id]:
            by_group[a.pay_group_id].append(a.employee_id)
    if not by_group:
        return []
    periods = db.query(PayPeriod).filter(
        PayPeriod.company_id == company_id, PayPeriod.pay_group_id.in_(sorted(by_group)),
        PayPeriod.period_start <= max(dates), PayPeriod.period_end >= min(dates)).order_by(PayPeriod.period_start).all()
    names = dict(db.query(PayGroup.id, PayGroup.name).filter(PayGroup.company_id == company_id,
                                                            PayGroup.id.in_(sorted(by_group))))
    out = []
    for period in periods:
        if not any(period.period_start <= d <= period.period_end for d in dates):
            continue
        for employee_id in by_group[period.pay_group_id][:MAX_IMPACT_EMPLOYEES]:
            facts = time_inputs.day_facts(db, company_id, employee_id, period.period_start, period.period_end)
            scheduled = [f for f in facts if f.employed and not f.weekly_off]
            if scheduled and all(f.holiday is not None and f.holiday.paid for f in scheduled):
                out.append({"pay_group_id": period.pay_group_id, "pay_group": names.get(period.pay_group_id),
                            "period_start": period.period_start.isoformat(),
                            "period_end": period.period_end.isoformat(), "scheduled_days": len(scheduled)})
                break
    return out


def _warnings(db: Session, calendar: HolidayCalendar, year: int, *, first_publication: bool,
              employees: Set[int], dates: Set[date], own_rows: Sequence[Holiday], notes: Sequence[dict],
              flat_rows) -> Tuple[List[dict], List[dict]]:
    """(errors, warnings) of 01 §6.3 V-01..V-10 for the state currently in the session (inside the simulation)."""
    svc = _svc()
    cid = calendar.company_id
    errors: List[dict] = []
    warnings: List[dict] = []
    today, window = _bands(db, calendar)
    live = [h for h in own_rows if h.status != "CANCELLED"]
    # V-01: occurrence fields (portions gated until LH-M5b, OPTIONAL is FULL, dates inside the year)
    for h in live:
        if h.portion != "FULL" and (h.holiday_type == "OPTIONAL" or not HALF_DAY_HOLIDAYS_ENABLED):
            errors.append({"code": "INVALID_PORTION", "message": f"{h.name} on {h.date}: half-day holidays are "
                                                                  "not enabled yet." if h.holiday_type != "OPTIONAL"
                           else f"{h.name}: optional holidays are full days.", "dates": [h.date.isoformat()]})
    # V-04: year bounds
    if year < today.year:
        errors.append({"code": "HOLIDAY_IN_PAST", "message": f"{year} has passed.", "dates": []})
    if year > today.year + FUTURE_YEARS:
        errors.append({"code": "FUTURE_LIMIT", "message": f"Years can be published up to {today.year + FUTURE_YEARS}.",
                       "dates": []})
    # V-03: declared working day on a date that holds a holiday in this calendar or one that includes it
    flattener = svc.Flattener(db, cid)
    for cal in sorted(svc.dependents(flattener.includes, calendar.id)):
        flat = flattener.flatten(cal, date(year, 1, 1), date(year, 12, 31))
        holiday_dates = {f.row.date for f in flat.rows}
        for w_date, w_cal in svc.working_days_reaching(db, cid, cal, date(year, 1, 1), date(year, 12, 31)):
            if w_date in holiday_dates:
                errors.append({"code": "WORKING_DAY_CONFLICT", "message": f"{w_date.isoformat()} is both a declared "
                               f"working day and a holiday on {svc._code(db, cid, cal)}.",  # noqa: SLF001
                               "dates": [w_date.isoformat()]})
    # V-02
    covered = _covered_periods(db, cid, employees, dates)
    for p in covered:
        warnings.append({"code": "HOLIDAYS_COVER_ALL_WORKING_DAYS", "confirm": True, "severity": "warning",
                         "message": f"{p['pay_group'] or 'Pay group'} period {p['period_start']} – {p['period_end']} "
                                    "has no working days left.", "details": p})
    # HOLIDAY_DATE_NEEDED (02 §6.2)
    needed = [n for n in notes if n.get("code") == "HOLIDAY_DATE_NEEDED"]
    if needed:
        warnings.append({"code": "HOLIDAY_DATE_NEEDED", "confirm": True, "severity": "warning",
                         "message": "; ".join(n["message"] for n in needed), "details": {"items": needed}})
    # V-10: first publication with dates before the backdate window
    if first_publication:
        old = sorted({h.date for h in live if h.date < today - timedelta(days=window)})
        if old:
            warnings.append({"code": "PAST_DATES_IN_FIRST_PUBLICATION", "confirm": True, "severity": "warning",
                             "message": f"{len(old)} date(s) before {(today - timedelta(days=window)).isoformat()} "
                                        "will be recounted, also in locked or finalized pay periods.",
                             "details": {"dates": [d.isoformat() for d in old],
                                         "periods": _pay_periods(db, cid, employees, set(old))}})
    # V-06: two different half-day holidays on one date
    halves: Dict[date, Set[str]] = defaultdict(set)
    for f in flat_rows:
        if f.row.portion in ("FIRST_HALF", "SECOND_HALF"):
            halves[f.row.date].add(f.row.portion)
    combined = sorted(d for d, p in halves.items() if len(p) == 2)
    if combined:
        warnings.append({"code": "HALF_DAY_HOLIDAYS_COMBINED", "confirm": True, "severity": "warning",
                         "message": "Two half-day holidays make a full day on " + ", ".join(d.isoformat()
                                                                                         for d in combined),
                         "details": {"dates": [d.isoformat() for d in combined]}})
    # V-07: bank-only closures on a calendar employees resolve to
    banks = [h for h in live if h.category == "BANK"]
    if banks and employees:
        warnings.append({"code": "BANK_HOLIDAY_ON_EMPLOYEE_CALENDAR", "confirm": True, "severity": "warning",
                         "message": f"{len(banks)} bank closure(s) on a calendar employees follow.",
                         "details": {"dates": [h.date.isoformat() for h in banks]}})
    # V-08: provisional rows (info)
    provisional = [h for h in live if h.status == "PROVISIONAL"]
    if provisional:
        warnings.append({"code": "PROVISIONAL_DATES", "confirm": False, "severity": "info",
                         "message": f"{len({(h.rule_id, h.name) for h in provisional})} expected date(s): confirm "
                                    "them when announced.",
                         "details": {"dates": [h.date.isoformat() for h in provisional]}})
    # V-09: same name and date here and in an included calendar (info)
    inherited = {(f.row.date, f.row.name.strip().lower()) for f in flat_rows if f.inherited}
    same = sorted({h.date.isoformat() for h in live if (h.date, h.name.strip().lower()) in inherited})
    if same:
        warnings.append({"code": "SAME_AS_INCLUDED", "confirm": False, "severity": "info",
                         "message": "Also on an included calendar: " + ", ".join(same), "details": {"dates": same}})
    # Included calendars whose year is still a draft (LH-HOL-21; info)
    unpublished = [svc._code(db, cid, c) for c in sorted(svc.reachable(flattener.includes, calendar.id))  # noqa: SLF001
                   if c != calendar.id and state_of(db, cid, c, year) == "DRAFT"]
    if unpublished:
        warnings.append({"code": "INCLUDED_YEAR_NOT_PUBLISHED", "confirm": False, "severity": "info",
                         "message": f"{', '.join(unpublished)} {year} is not published; its holidays will appear for "
                                    "this calendar's employees only after it is.", "details": {"calendars": unpublished}})
    return errors, warnings


def _impact(db: Session, calendar: HolidayCalendar, year: int, apply: Callable[[], Any], *,
            first_publication: bool) -> dict:
    """Before/after of the flattened sets of the calendar and its dependents with ``apply`` simulated."""
    svc = _svc()
    cid = calendar.company_id
    flattener = svc.Flattener(db, cid)
    targets = _affected_calendars(db, flattener, calendar)
    before = _flat_state(db, cid, targets, year)

    def run():
        applied = apply()
        db.flush()
        after = _flat_state(db, cid, targets, year)
        changes, dates = _diff_state(before, after)
        employees, by_branch = _people(db, cid, [c for c in changes], year)
        own_rows = year_rows(db, cid, calendar.id, year)
        notes = []
        if get_record(db, cid, calendar.id, year) is not None:
            try:
                notes = [holiday_rollover._issue_json(n) for n in  # noqa: SLF001
                         holiday_rollover.run_generator(db, cid, calendar, year).notes]
            except rec.RecurrenceError:
                notes = []
        flat = svc.Flattener(db, cid).flatten(calendar.id, date(year, 1, 1), date(year, 12, 31))
        errors, warnings = _warnings(db, calendar, year, first_publication=first_publication, employees=employees,
                                     dates=dates, own_rows=own_rows, notes=notes, flat_rows=flat.rows)
        names = {c: svc._code(db, cid, c) for c in changes}  # noqa: SLF001
        diff = []
        for cal, by_day in sorted(changes.items()):
            for day, (old, new) in sorted(by_day.items()):
                old_names = {n for _, n, _ in old}
                new_names = {n for _, n, _ in new}
                for n in sorted(new_names - old_names):
                    diff.append({"calendar_code": names[cal], "date": day.isoformat(), "change": "added", "name": n})
                for n in sorted(old_names - new_names):
                    diff.append({"calendar_code": names[cal], "date": day.isoformat(), "change": "removed", "name": n})
                if old_names == new_names:
                    diff.append({"calendar_code": names[cal], "date": day.isoformat(), "change": "changed",
                                 "name": ", ".join(sorted(new_names))})
        return {
            "applied": applied,
            "content_hash": content_hash(db, cid, calendar.id, year),
            "diff": diff,
            "changed_dates": sorted(d.isoformat() for d in dates),
            "people": {"employees": len(employees), "by_branch": by_branch},
            "leave": _leave_impact(db, cid, employees, dates),
            "payroll": {"locked_periods": _pay_periods(db, cid, employees, dates)},
            "errors": errors, "warnings": warnings,
            "holidays": {"own": sum(1 for h in own_rows if h.status != "CANCELLED"),
                         "expected": sum(1 for h in own_rows if h.status == "PROVISIONAL")},
        }

    return _simulate(db, run)


def _require_acknowledged(impact: dict, acknowledged: Sequence[str]) -> None:
    if impact["errors"]:
        first = impact["errors"][0]
        status = 409 if first["code"] in ("HOLIDAY_IN_PAST", "WORKING_DAY_CONFLICT") else 422
        raise AppError(status, first["message"], first["code"], extra={"details": impact["errors"]})
    missing = [w for w in impact["warnings"] if w.get("confirm") and w["code"] not in set(acknowledged or ())]
    if missing:
        raise AppError(422, missing[0]["message"], missing[0]["code"],
                       extra={"details": {"warnings": missing}})


def preview_year(db: Session, company_id: int, calendar_id: int, year: int,
                 operations: Optional[Sequence[Mapping[str, Any]]] = None, *,
                 principal: Optional[Principal] = None) -> dict:
    """``YearImpact`` (01 §6.3) of publishing the DRAFT year, or of amending a PUBLISHED one with ``operations``."""
    calendar = db.get(HolidayCalendar, calendar_id)
    if calendar is None or calendar.company_id != company_id:
        raise not_found("Holiday calendar")
    record = get_record(db, company_id, calendar.id, year)
    if operations is None:
        if record is None or record.status != "DRAFT":
            raise conflict(f"{year} has no draft to publish.", "YEAR_PUBLISHED" if record is not None else "NO_DRAFT")
        return _impact(db, calendar, year, lambda: _mark_published(db, record, None, None), first_publication=True)
    ops = _check_operations(db, principal, calendar, year, operations)
    return _impact(db, calendar, year, lambda: _apply_operations(db, principal, calendar, year, ops, reason=None),
                   first_publication=False)


# ============================================================================================== publication (01 §6.2)
def _mark_published(db: Session, record: HolidayCalendarYear, published_by: Optional[int],
                    approved_by: Optional[int]) -> dict:
    record.status = "PUBLISHED"
    record.version = 1
    record.published_at = clock.utcnow()
    record.published_by_user_id = published_by
    record.approved_by_user_id = approved_by if approved_by != published_by else None
    record.content_hash = content_hash(db, record.company_id, record.calendar_id, record.year)
    _clear_request(record)
    record.row_version = (record.row_version or 1) + 1
    return {"version": 1}


def _check_publisher(db: Session, principal: Principal, calendar: HolidayCalendar) -> None:
    if not _svc().can_act(db, principal, calendar, "holiday:publish"):
        raise forbidden("Publishing holidays needs the 'Publish and backdate holidays' permission for this calendar.")


def _notify_employees(db: Session, calendar: HolidayCalendar, employees: Set[int], title: str, message: str,
                      link: str) -> int:
    from app.payroll.services.holders import notify_users

    if not employees or not _setting(db, calendar.company_id, "holiday.publish.notify_employees", True):
        return 0
    users = [u for (u,) in db.query(Employee.user_id).filter(Employee.company_id == calendar.company_id,
                                                             Employee.id.in_(sorted(employees)),
                                                             Employee.user_id.isnot(None))]
    return notify_users(db, users, title, message, link)


def _notify_publishers(db: Session, calendar: HolidayCalendar, actor: Principal, year: int, kind: str) -> None:
    from app.payroll.services.holders import notify_holders

    entity = calendar.legal_entity_id
    if entity is None:
        from app.models.org import LegalEntity

        first = db.query(LegalEntity.id).filter(LegalEntity.company_id == calendar.company_id).order_by(
            LegalEntity.id).first()
        entity = first[0] if first else None
    if entity is None:
        return
    try:
        notify_holders(db, calendar.company_id, "holiday:publish", entity,
                       f"Holiday {kind.lower()} waiting for you",
                       f"{calendar.code} {year}: a {kind.lower()} needs a second holiday publisher.",
                       f"/holidays/calendars/{calendar.id}?year={year}", exclude_user_id=actor.user_id)
    except Exception:  # noqa: BLE001 - a notification failure never blocks the request
        pass


def _after_commit(db: Session, principal: Principal, calendar: HolidayCalendar, record: HolidayCalendarYear,
                  impact: dict, *, event: str, change: str, note: Optional[str] = None) -> dict:
    from app.payroll.services import outbox, time_inputs
    from app.services import leave_jobs

    cid = calendar.company_id
    year = record.year
    # source_change_log.change keeps its CREATED / UPDATED / DELETED vocabulary; the year event is in details.
    time_inputs.log_change(db, cid, record_type="holiday_year", record_id=record.id,
                           change="CREATED" if change == "PUBLISHED" else "UPDATED",
                           content={"calendar_id": calendar.id, "year": year, "version": record.version,
                                    "content_hash": record.content_hash},
                           date_from=date(year, 1, 1), date_to=date(year, 12, 31), actor_user_id=principal.user_id,
                           details={"calendar_id": calendar.id, "version": record.version, "event": change})
    dates = [date.fromisoformat(d) for d in impact["changed_dates"]]
    recount = leave_jobs.recount_dates(db, cid, dates,
                                       key=f"recalc:year:{calendar.id}:{year}:v{record.version}",
                                       actor_user_id=principal.user_id)
    outbox.emit(db, event, {"calendar_id": calendar.id, "year": year, "version": record.version})
    employees: Set[int] = set()
    flattener = _svc().Flattener(db, cid)
    for cal in _affected_calendars(db, flattener, calendar):
        employees |= _svc().calendar_users(db, cid, cal, date(year, 1, 1))[1]
    if event == "holiday.year.published":
        count = impact["holidays"]["own"]
        message = f"Your {year} holidays from {calendar.name} are published: {count} holiday(s)."
        if note:
            message += f" {note}"
        notified = _notify_employees(db, calendar, employees, f"{year} holidays published", message,
                                     f"/holidays?year={year}")
    else:
        future = [d for d in dates if d > clock.company_today(db, cid)]
        notified = _notify_employees(db, calendar, employees, f"{year} holidays changed",
                                     f"{calendar.name} {year} changed on " + ", ".join(d.isoformat() for d in future[:5])
                                     + ("…" if len(future) > 5 else ""), f"/holidays?year={year}") if future else 0
    return {"recount": recount, "notified": notified}


def require_publish_step_up(db: Session, principal: Principal) -> None:
    """LH-SEC-02 (07 §5.2, the step-up catalogue): publishing a holiday year — or requesting / completing a
    second-person publication — needs a fresh step-up (<= 300 s) of the current session; 403 STEP_UP_REQUIRED
    otherwise (the UI opens "Confirm it's you" and retries). Dry runs never need it."""
    from app.payroll.services.step_up import ensure_step_up

    ensure_step_up(db, principal)


def publish_year(db: Session, principal: Principal, calendar_id: int, year: int, *, dry_run: bool = False,
                 acknowledged_warnings: Sequence[str] = (), note: Optional[str] = None,
                 content_hash_seen: Optional[str] = None, reason: Optional[str] = None) -> Tuple[int, dict]:
    """POST /{id}/years/{year}/publish (01 §6.2): (status code, body). 202 = second-person request recorded.
    ``note`` goes to employees with the notification, ``reason`` stays in the calendar history."""
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    _check_publisher(db, principal, calendar)
    cid = principal.company_id
    record = get_record(db, cid, calendar.id, year, lock=not dry_run)
    if record is None:
        raise not_found("Holiday year draft")
    if record.status == "PUBLISHED":
        raise conflict(f"{year} is already published; change it through an amendment.", "YEAR_PUBLISHED")
    impact = _impact(db, calendar, year, lambda: _mark_published(db, record, principal.user_id, None),
                     first_publication=True)
    impact.pop("applied", None)
    body = {"dry_run": dry_run, "impact": impact, "year": year_json(db, record, calendar, year),
            "requires_second_person": requires_second_person(db, calendar)}
    if dry_run:
        return 200, body
    current = content_hash(db, cid, calendar.id, year)
    if content_hash_seen and content_hash_seen != current:
        raise conflict("The draft changed since you reviewed it. Review the impact again.", "ROW_VERSION_CONFLICT")
    _require_acknowledged(impact, acknowledged_warnings)
    require_publish_step_up(db, principal)
    approved_by = None
    published_by = principal.user_id
    if body["requires_second_person"]:
        if record.publish_requested_by_user_id is None:
            record.publish_requested_by_user_id = principal.user_id
            record.publish_requested_at = clock.utcnow()
            record.requested_content_hash = current
            record.publish_request_payload = {"kind": "PUBLISH", "note": _reason(note), "reason": _reason(reason),
                                              "acknowledged_warnings": list(acknowledged_warnings or ())}
            record.row_version = (record.row_version or 1) + 1
            db.flush()
            AuditLogService.log(db, principal, AUDIT_MODULE, "YEAR_PUBLISH_REQUESTED",
                                f"{calendar.code} {year}: publication requested", "holiday_calendar", calendar.id,
                                details={"year": year, "content_hash": current})
            _notify_publishers(db, calendar, principal, year, "Publication")
            body["year"] = year_json(db, record, calendar, year)
            body["status"] = "PENDING_SECOND_PERSON"
            return 202, body
        if record.publish_requested_by_user_id == principal.user_id:
            raise AppError(403, "Another holiday publisher has to complete the publication you requested.",
                           "SELF_DECISION_FORBIDDEN", extra={"details": {"cause": "REQUESTER"}})
        if record.requested_content_hash != current:
            # The request is voided (V3-027) and the void is kept: answer 409 without rolling it back.
            _clear_request(record)
            record.row_version = (record.row_version or 1) + 1
            db.flush()
            return 409, {"detail": "The draft changed after publication was requested; request it again.",
                         "code": "ROW_VERSION_CONFLICT", "voided": True}
        published_by = record.publish_requested_by_user_id
        approved_by = principal.user_id
        note = note or (record.publish_request_payload or {}).get("note")
        reason = reason or (record.publish_request_payload or {}).get("reason")
    _mark_published(db, record, published_by, approved_by)
    db.flush()
    effects = _after_commit(db, principal, calendar, record, impact, event="holiday.year.published",
                            change="PUBLISHED", note=_reason(note))
    AuditLogService.log(db, principal, AUDIT_MODULE, "YEAR_PUBLISHED", f"{calendar.code} {year}: published v1",
                        "holiday_calendar", calendar.id,
                        details={"year": year, "version": 1, "content_hash": record.content_hash,
                                 "reason": _reason(reason),
                                 "approved_by_user_id": approved_by, "holidays": impact["holidays"],
                                 "people": impact["people"]["employees"],
                                 "acknowledged_warnings": list(acknowledged_warnings or ())})
    body.update({"status": "PUBLISHED", "year": year_json(db, record, calendar, year), "effects": effects})
    return 200, body


# ============================================================================================== amendments (01 §6.4)
def _band_check(day: date, today: date, window: int, *, kind: str, provisional: bool = False) -> str:
    """V3-042 date bands for one date: ``future`` / ``backdated``; raises the refusal otherwise."""
    if day > today:
        return "future"
    if day >= today - timedelta(days=window):
        if kind == "addition" or provisional:
            return "backdated"
        raise AppError(409, f"{day.isoformat()} has started; a confirmed holiday cannot be changed.", "HOLIDAY_IN_PAST",
                       extra={"details": {"dates": [day.isoformat()], "calendar_local_today": today.isoformat()}})
    if kind == "addition":
        raise AppError(422, f"{day.isoformat()} is before the backdating window "
                            f"({(today - timedelta(days=window)).isoformat()} onward).", "BACKDATE_LIMIT",
                       extra={"details": {"dates": [day.isoformat()]}})
    raise AppError(409, f"{day.isoformat()} is in the past and kept for payroll history.", "HOLIDAY_IN_PAST",
                   extra={"details": {"dates": [day.isoformat()]}})


def _occurrence(db: Session, company_id: int, holiday_id: Any, calendar_id: int, year: int) -> Holiday:
    try:
        hid = int(holiday_id)
    except (TypeError, ValueError):
        raise bad_request("holiday_id must be a number.", "VALIDATION_ERROR") from None
    h = db.query(Holiday).filter(Holiday.company_id == company_id, Holiday.id == hid).first()
    if h is None or h.calendar_id != calendar_id:
        raise not_found("Holiday")
    if h.date.year != year:
        raise AppError(422, f"{h.name} belongs to {h.date.year}, not {year}.", "INVALID_DATE")
    return h


def _occurrence_fields(data: Mapping[str, Any], *, require_name: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "name" in data or require_name:
        name = " ".join(str(data.get("name") or "").split())
        if not name or len(name) > 150:
            raise bad_request("Name must be 1-150 characters.", "ORG_NAME_INVALID")
        out["name"] = name
    if "holiday_type" in data or require_name:
        holiday_type = data.get("holiday_type") or "COMPANY"
        if holiday_type not in TYPES:
            raise AppError(422, "Holiday type must be PUBLIC, COMPANY or OPTIONAL.", "INVALID_HOLIDAY_TYPE")
        out["holiday_type"] = holiday_type
    if "category" in data or require_name:
        category = data.get("category") or "OTHER"
        if category not in CATEGORIES:
            raise AppError(422, "Unknown holiday category.", "INVALID_CATEGORY")
        out["category"] = category
    if "portion" in data or require_name:
        portion = data.get("portion") or "FULL"
        if portion not in PORTIONS:
            raise AppError(422, "Portion must be FULL, FIRST_HALF or SECOND_HALF.", "INVALID_PORTION")
        out["portion"] = portion
    if "description" in data:
        description = (str(data.get("description") or "").strip() or None)
        if description and len(description) > 500:
            raise bad_request("Description may have at most 500 characters.", "VALIDATION_ERROR")
        out["description"] = description
    if "name_i18n" in data:
        value = data.get("name_i18n") or {}
        if not isinstance(value, Mapping):
            raise bad_request("name_i18n must be an object.", "VALIDATION_ERROR")
        out["name_i18n"] = dict(value)
    holiday_type = out.get("holiday_type")
    portion = out.get("portion")
    if portion and portion != "FULL":
        if holiday_type == "OPTIONAL":
            raise AppError(422, "Optional holidays are full days.", "INVALID_PORTION")
        if not HALF_DAY_HOLIDAYS_ENABLED:
            raise AppError(422, "Half-day holidays are not enabled yet.", "INVALID_PORTION")
    return out


def _check_operations(db: Session, principal: Optional[Principal], calendar: HolidayCalendar, year: int,
                      operations: Sequence[Mapping[str, Any]]) -> List[dict]:
    """Validate amendment operations against the date bands; returns normalised ops with their classification."""
    if not operations:
        raise bad_request("Give at least one change.", "VALIDATION_ERROR")
    if len(operations) > 200:
        raise bad_request("At most 200 changes per amendment.", "VALIDATION_ERROR")
    cid = calendar.company_id
    today, window = _bands(db, calendar)
    counts_provisional = _svc().provisional_counts(db, cid)
    out: List[dict] = []
    for raw in operations:
        op = str(raw.get("op") or "").lower()
        if op not in OPERATIONS:
            raise bad_request(f"Unknown change {op!r}.", "VALIDATION_ERROR")
        item: Dict[str, Any] = {"op": op, "bands": [], "p4": False}
        if op == "add":
            fields = _occurrence_fields(raw, require_name=True)
            start = _parse_date(raw.get("date"))
            days = int(raw.get("duration_days") or 1)
            if not 1 <= days <= 31:
                raise bad_request("A holiday lasts 1-31 days.", "VALIDATION_ERROR")
            dates = [start + timedelta(days=i) for i in range(days)]
            if any(d.year != year for d in dates):
                raise AppError(422, f"Every day must be in {year}; add the rest to its own year.", "INVALID_DATE")
            status = raw.get("status") or "CONFIRMED"
            if status not in ("CONFIRMED", "PROVISIONAL"):
                raise AppError(422, "A new holiday is CONFIRMED or PROVISIONAL.", "INVALID_TRANSITION")
            for d in dates:
                item["bands"].append(_band_check(d, today, window, kind="addition"))
                if db.query(Holiday.id).filter(Holiday.company_id == cid, Holiday.calendar_id == calendar.id,
                                               Holiday.date == d, Holiday.status != "CANCELLED",
                                               func.lower(Holiday.name) == fields["name"].lower()).first():
                    raise conflict("This calendar already has a holiday of that name on that date.",
                                   "DUPLICATE_HOLIDAY")
            working_day_clash(db, calendar, dates)
            item.update(fields=fields, dates=[d.isoformat() for d in dates], status=status,
                        legal_ref=raw.get("legal_ref"), issued_on=raw.get("issued_on"))
            if raw.get("rule_id"):
                # A row the rules give for a published year (apply_rules, 02 §4.6): keeps its rule identity.
                rule = db.query(HolidayRule).filter(HolidayRule.company_id == cid,
                                                    HolidayRule.calendar_id == calendar.id,
                                                    HolidayRule.id == int(raw["rule_id"])).first()
                if rule is None or len(dates) != 1:
                    raise not_found("Holiday rule")
                observance = raw.get("observance") or "ORIGINAL"
                item["rule"] = {
                    "rule_id": rule.id, "observance": observance,
                    "observed_from_date": raw.get("observed_from_date") if observance == "OBSERVED" else None,
                    "sequence_index": int(raw.get("sequence_index") or 1),
                    "source_ref": {"rule_version": rule.version, "rule_year": raw.get("rule_year"),
                                   "occurrence_key": raw.get("occurrence_key")},
                }
        elif op in ("add_working_day", "remove_working_day"):
            if op == "add_working_day":
                item.update(_check_working_day(db, calendar, raw, year))
                item["bands"].append(_band_check(date.fromisoformat(item["date"]), today, window, kind="addition"))
            else:
                w = db.query(CalendarWorkingDay).filter(CalendarWorkingDay.company_id == cid,
                                                        CalendarWorkingDay.calendar_id == calendar.id,
                                                        CalendarWorkingDay.id == int(raw.get("working_day_id") or 0)
                                                        ).first()
                if w is None:
                    raise not_found("Working day")
                item["bands"].append(_band_check(w.date, today, window, kind="addition"))
                item["working_day_id"] = w.id
        else:
            h = _occurrence(db, cid, raw.get("holiday_id"), calendar.id, year)
            item["holiday_id"] = h.id
            provisional = h.status == "PROVISIONAL"
            if h.status == "CANCELLED":
                raise AppError(422, f"{h.name} on {h.date} is already cancelled.", "INVALID_TRANSITION")
            if op == "cancel":
                item["bands"].append(_band_check(h.date, today, window, kind="edit", provisional=provisional))
            elif op == "move":
                new_date = _parse_date(raw.get("new_date") or raw.get("date"), "new_date")
                item["bands"].append(_band_check(h.date, today, window, kind="edit", provisional=provisional))
                if new_date <= today:
                    raise AppError(409, "A holiday can only move to a future date; record a past closure instead.",
                                   "HOLIDAY_IN_PAST", extra={"details": {"dates": [new_date.isoformat()]}})
                if new_date.year != year:
                    raise AppError(422, f"Move {h.name} within {year}; another year gets its own holiday.",
                                   "INVALID_DATE")
                if new_date == h.date:
                    raise bad_request("Choose a different date.", "NO_CHANGE")
                working_day_clash(db, calendar, [new_date])
                item["new_date"] = new_date.isoformat()
            elif op == "confirm":
                if not provisional:
                    raise AppError(422, f"{h.name} on {h.date} is already confirmed.", "INVALID_TRANSITION")
                new_date = _parse_date(raw["date"]) if raw.get("date") else h.date
                if new_date == h.date:
                    if counts_provisional:
                        item["p4"] = True  # P-4: day facts unchanged, any age (LH-HOL-32)
                    else:
                        item["bands"].append(_band_check(h.date, today, window, kind="addition"))
                else:
                    item["bands"].append(_band_check(h.date, today, window, kind="edit", provisional=True))
                    if new_date <= today:
                        raise AppError(409, "The announced date must be in the future; record a past closure.",
                                       "HOLIDAY_IN_PAST", extra={"details": {"dates": [new_date.isoformat()]}})
                    if new_date.year != year:
                        raise AppError(422, f"Confirm {h.name} within {year}.", "INVALID_DATE")
                    item["new_date"] = new_date.isoformat()
            elif op == "edit":
                fields = _occurrence_fields(raw, require_name=False)
                if not fields:
                    raise bad_request("Nothing to change.", "NO_CHANGE")
                hashed = {k for k in fields if k in ("name", "holiday_type", "portion")
                          and fields[k] != getattr(h, k)}
                if h.date <= today and (hashed or ("category" in fields and fields["category"] != h.category)):
                    raise AppError(409, f"{h.name} on {h.date} has started; only its description and translations "
                                        "can change.", "HOLIDAY_IN_PAST",
                                   extra={"details": {"dates": [h.date.isoformat()], "fields": sorted(hashed)}})
                item["fields"] = fields
        out.append(item)
    return out


def working_day_clash(db: Session, calendar: HolidayCalendar, dates: Iterable[date]) -> None:
    """LH-HOL-36 (01 §5.5): a holiday may not land on a declared working day of any calendar it shares a flattened
    date with — the calendar itself, calendars that include it, and the calendars those include (a working day
    reaches every including calendar). WORKING_DAY_CONFLICT names the calendar and the working day."""
    svc = _svc()
    cid = calendar.company_id
    wanted = sorted(set(dates))
    if not wanted:
        return
    graph = svc._graph(db, cid)  # noqa: SLF001
    reach: Set[int] = set()
    for dependent in svc.dependents(graph, calendar.id):
        reach |= svc.reachable(graph, dependent)
    clash = (db.query(CalendarWorkingDay)
             .filter(CalendarWorkingDay.company_id == cid, CalendarWorkingDay.calendar_id.in_(sorted(reach)),
                     CalendarWorkingDay.date.in_(wanted))
             .order_by(CalendarWorkingDay.date, CalendarWorkingDay.id).first())
    if clash is not None:
        code = svc._code(db, cid, clash.calendar_id)  # noqa: SLF001
        raise AppError(409, f"{clash.date.isoformat()} is the declared working day '{clash.name}' on {code}; remove "
                            "the working day first.", "WORKING_DAY_CONFLICT",
                       extra={"details": {"calendar_code": code, "date": clash.date.isoformat(),
                                          "working_day_id": clash.id}})


def _check_working_day(db: Session, calendar: HolidayCalendar, raw: Mapping[str, Any], year: int) -> dict:
    """Validate one declared working day (04-holiday/01 §5.5; 11 §4.2.13): one per calendar and date, no holiday
    on the date in the flattened calendar or in a calendar that includes it (WORKING_DAY_CONFLICT)."""
    svc = _svc()
    cid = calendar.company_id
    day = _parse_date(raw.get("date"))
    if day.year != year:
        raise AppError(422, f"The date must be in {year}.", "INVALID_DATE")
    name = " ".join(str(raw.get("name") or "").split())
    if not name or len(name) > 150:
        raise bad_request("Name must be 1-150 characters.", "ORG_NAME_INVALID")
    reason = " ".join(str(raw.get("reason") or "").split())
    if not reason:
        raise bad_request("Give a reason for the working day.", "REASON_REQUIRED")
    if len(reason) > 300:
        raise bad_request("The reason may have at most 300 characters.", "REASON_TOO_LONG")
    portion = raw.get("portion") or "FULL"
    if portion not in PORTIONS:
        raise AppError(422, "Portion must be FULL, FIRST_HALF or SECOND_HALF.", "INVALID_PORTION")
    if portion != "FULL" and not HALF_DAY_HOLIDAYS_ENABLED:
        raise AppError(422, "Half working days are not enabled yet.", "INVALID_PORTION")
    if db.query(CalendarWorkingDay.id).filter(CalendarWorkingDay.company_id == cid,
                                              CalendarWorkingDay.calendar_id == calendar.id,
                                              CalendarWorkingDay.date == day).first():
        raise conflict(f"{day.isoformat()} is already a declared working day.", "DUPLICATE_WORKING_DAY")
    flattener = svc.Flattener(db, cid, drafts={(c, year) for c in svc.dependents(svc._graph(db, cid),  # noqa: SLF001
                                                                                calendar.id)},
                              include_provisional=True)
    for cal in sorted(svc.dependents(flattener.includes, calendar.id)):
        flat = flattener.flatten(cal, day, day)
        if flat.rows:
            raise AppError(409, f"{day.isoformat()} holds {flat.rows[0].row.name} on {svc._code(db, cid, cal)}; "  # noqa: SLF001
                                "a holiday and a working day cannot share a date.", "WORKING_DAY_CONFLICT",
                           extra={"details": {"calendar_code": svc._code(db, cid, cal), "date": day.isoformat()}})  # noqa: SLF001
    return {"date": day.isoformat(), "name": name, "reason": reason, "portion": portion}


def _apply_operations(db: Session, principal: Optional[Principal], calendar: HolidayCalendar, year: int,
                      ops: Sequence[Mapping[str, Any]], *, reason: Optional[str]) -> dict:
    """Write the operations (already checked); returns the touched holiday ids and dates."""
    cid = calendar.company_id
    actor = principal.user_id if principal else None
    touched: List[Tuple[int, str]] = []
    with _amending(db):
        for item in ops:
            op = item["op"]
            if op == "add":
                rows = []
                for i, d in enumerate(item["dates"]):
                    ref: Dict[str, Any] = {}
                    if item.get("legal_ref"):
                        ref["legal_ref"] = str(item["legal_ref"])[:100]
                    if item.get("issued_on"):
                        ref["issued_on"] = _parse_date(item["issued_on"], "issued_on").isoformat()
                    h = Holiday(company_id=cid, calendar_id=calendar.id, date=date.fromisoformat(d), location=None,
                                is_optional=item["fields"]["holiday_type"] == "OPTIONAL", status=item["status"],
                                source="GOVT_NOTICE" if ref.get("legal_ref") else "MANUAL", source_ref=ref,
                                sequence_index=i + 1 if len(item["dates"]) > 1 else 1, **item["fields"])
                    if item.get("rule"):
                        r = item["rule"]
                        h.rule_id, h.source, h.observance = r["rule_id"], "RULE", r["observance"]
                        h.observed_from_date = date.fromisoformat(r["observed_from_date"]) \
                            if r["observed_from_date"] else None
                        h.sequence_index = r["sequence_index"]
                        h.source_ref = {**ref, **r["source_ref"]}
                    db.add(h)
                    rows.append(h)
                db.flush()
                touched += [(h.id, "CREATED") for h in rows]
            elif op == "cancel":
                h = db.get(Holiday, item["holiday_id"])
                h.status = "CANCELLED"
                h.row_version = (h.row_version or 1) + 1
                touched.append((h.id, "UPDATED"))
            elif op in ("move", "confirm"):
                h = db.get(Holiday, item["holiday_id"])
                if item.get("new_date"):
                    new = _move(db, h, date.fromisoformat(item["new_date"]),
                                status="CONFIRMED" if op == "confirm" else h.status, actor=actor)
                    touched += [(h.id, "UPDATED"), (new.id, "CREATED")]
                    # LH-REC-27 (02 §3.5 rule 5): the ORIGINAL's OBSERVED partner belongs to the old date — it is
                    # cancelled with it; the moved row gets an observed partner only through the rules (regeneration).
                    if h.observance == "ORIGINAL" and h.rule_id is not None:
                        key = _svc()._pair_key(h)  # noqa: SLF001
                        for partner in db.query(Holiday).filter(
                                Holiday.company_id == cid, Holiday.calendar_id == h.calendar_id,
                                Holiday.rule_id == h.rule_id, Holiday.observance == "OBSERVED",
                                Holiday.status != "CANCELLED", Holiday.id != new.id):
                            if _svc()._pair_key(partner) == key:  # noqa: SLF001
                                partner.status = "CANCELLED"
                                partner.row_version = (partner.row_version or 1) + 1
                                touched.append((partner.id, "UPDATED"))
                else:
                    h.status = "CONFIRMED"
                    h.confirmed_by_user_id = actor
                    h.confirmed_at = clock.utcnow()
                    h.row_version = (h.row_version or 1) + 1
                    touched.append((h.id, "UPDATED"))
            elif op == "edit":
                from app.services.holiday_service import content

                h = db.get(Holiday, item["holiday_id"])
                before = content(h)
                for k, v in item["fields"].items():
                    setattr(h, k, v)
                h.is_optional = h.holiday_type == "OPTIONAL"
                h.row_version = (h.row_version or 1) + 1
                if content(h) != before:  # description / translations are outside the hash: no change log (HOL-23)
                    touched.append((h.id, "UPDATED"))
            elif op == "add_working_day":
                db.add(CalendarWorkingDay(company_id=cid, calendar_id=calendar.id, date=date.fromisoformat(item["date"]),
                                          name=item["name"], portion=item["portion"], reason=item["reason"],
                                          created_by_user_id=actor, row_version=1))
            elif op == "remove_working_day":
                db.delete(db.get(CalendarWorkingDay, item["working_day_id"]))
        db.flush()
    return {"touched": touched}


def _move(db: Session, h: Holiday, new_date: date, *, status: str, actor: Optional[int]) -> Holiday:
    """Cancel + new occurrence (SPINE §5.6): same rule, ``moved_from``, key suffix ``:m{n}`` (02 §4.4)."""
    ref = dict(h.source_ref or {})
    key = ref.get("occurrence_key")
    if key:
        base = key.split(":m")[0]
        moves = db.query(func.count(Holiday.id)).filter(
            Holiday.company_id == h.company_id, Holiday.calendar_id == h.calendar_id, Holiday.rule_id == h.rule_id,
            Holiday.source_ref["occurrence_key"].astext.like(f"{base}:m%")).scalar() or 0
        ref["occurrence_key"] = f"{base}:m{moves + 1}"
    ref["moved_from"] = h.date.isoformat()
    h.status = "CANCELLED"
    h.row_version = (h.row_version or 1) + 1
    db.flush()
    new = Holiday(company_id=h.company_id, calendar_id=h.calendar_id, name=h.name, date=new_date, location=None,
                  is_optional=h.is_optional, description=h.description, holiday_type=h.holiday_type,
                  rule_id=h.rule_id, portion=h.portion, status=status, observance=h.observance,
                  observed_from_date=h.observed_from_date, source=h.source, source_ref=ref, category=h.category,
                  sequence_index=h.sequence_index, name_i18n=dict(h.name_i18n or {}),
                  confirmed_by_user_id=actor if status == "CONFIRMED" else None,
                  confirmed_at=clock.utcnow() if status == "CONFIRMED" else None)
    db.add(new)
    db.flush()
    return new


def _log_rows(db: Session, principal: Optional[Principal], touched: Sequence[Tuple[int, str]]) -> None:
    from app.payroll.services import time_inputs
    from app.services.holiday_service import content

    for holiday_id, change in touched:
        h = db.get(Holiday, holiday_id)
        if h is None:
            continue
        time_inputs.log_change(db, h.company_id, record_type="holiday", record_id=h.id, change=change,
                               content=content(h), date_from=h.date, actor_user_id=principal.user_id if principal else None,
                               details={"calendar_id": h.calendar_id, "holiday_type": h.holiday_type,
                                        "status": h.status})


def amend_year(db: Session, principal: Principal, calendar_id: int, year: int,
               operations: Optional[Sequence[Mapping[str, Any]]], *, reason: Optional[str] = None,
               dry_run: bool = False, acknowledged_warnings: Sequence[str] = (),
               content_hash_seen: Optional[str] = None, complete_request: bool = False,
               _legacy: bool = False) -> Tuple[int, dict]:
    """POST /{id}/years/{year}/amend (01 §6.4): every change to a PUBLISHED year, one version increment."""
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    cid = principal.company_id
    if state_of(db, cid, calendar.id, year) != "PUBLISHED":
        raise conflict(f"{year} is not published; edit the draft directly and publish it.", "YEAR_NOT_PUBLISHED")
    record = ensure_year(db, cid, calendar, year, lock=not dry_run)
    if not _legacy:
        _check_publisher(db, principal, calendar)
    pending = record.publish_request_payload or {}
    if complete_request:
        if record.publish_requested_by_user_id is None or pending.get("kind") != "AMEND":
            raise conflict("There is no amendment waiting for a second publisher.", "NO_PENDING_REQUEST")
        operations = pending.get("operations")
        reason = pending.get("reason")
        acknowledged_warnings = pending.get("acknowledged_warnings") or []
    reason = _reason(reason)
    ops = _check_operations(db, principal, calendar, year, operations or [])
    only_p4 = all(o["op"] == "confirm" and o["p4"] for o in ops)
    if not reason and not only_p4:
        raise bad_request("Give a reason for changing a published year.", "REASON_REQUIRED")
    impact = _impact(db, calendar, year, lambda: _apply_operations(db, principal, calendar, year, ops, reason=reason),
                     first_publication=False)
    impact.pop("applied", None)
    body = {"dry_run": dry_run, "impact": impact, "year": year_json(db, record, calendar, year),
            "requires_second_person": requires_second_person(db, calendar) and not only_p4 and not _legacy}
    if dry_run:
        return 200, body
    current = content_hash(db, cid, calendar.id, year)
    if content_hash_seen and content_hash_seen != current:
        raise conflict("The year changed since you reviewed it. Review the impact again.", "ROW_VERSION_CONFLICT")
    _require_acknowledged(impact, acknowledged_warnings)
    approved_by = None
    if body["requires_second_person"]:
        if not complete_request:
            if record.publish_requested_by_user_id is not None:
                raise conflict("Another change to this year is already waiting for a second publisher.",
                               "ROW_VERSION_CONFLICT")
            record.publish_requested_by_user_id = principal.user_id
            record.publish_requested_at = clock.utcnow()
            record.requested_content_hash = current
            record.publish_request_payload = {"kind": "AMEND", "operations": [dict(o) for o in (operations or [])],
                                              "reason": reason,
                                              "acknowledged_warnings": list(acknowledged_warnings or ())}
            record.row_version = (record.row_version or 1) + 1
            db.flush()
            AuditLogService.log(db, principal, AUDIT_MODULE, "YEAR_AMEND_REQUESTED",
                                f"{calendar.code} {year}: amendment requested", "holiday_calendar", calendar.id,
                                details={"year": year, "operations": len(ops), "reason": reason})
            _notify_publishers(db, calendar, principal, year, "Amendment")
            body["year"] = year_json(db, record, calendar, year)
            body["status"] = "PENDING_SECOND_PERSON"
            return 202, body
        if record.publish_requested_by_user_id == principal.user_id:
            raise AppError(403, "Another holiday publisher has to complete the change you requested.",
                           "SELF_DECISION_FORBIDDEN", extra={"details": {"cause": "REQUESTER"}})
        if record.requested_content_hash != current:
            _clear_request(record)
            record.row_version = (record.row_version or 1) + 1
            db.flush()
            return 409, {"detail": "The year changed after the amendment was requested; request it again.",
                         "code": "ROW_VERSION_CONFLICT", "voided": True}
        approved_by = principal.user_id
    applied = _apply_operations(db, principal, calendar, year, ops, reason=reason)
    record.version = (record.version or 1) + 1
    record.content_hash = content_hash(db, cid, calendar.id, year)
    if approved_by is not None:
        requester = record.publish_requested_by_user_id
        _clear_request(record)
        record.approved_by_user_id = approved_by if approved_by != record.published_by_user_id else None
        actor_note = {"requested_by_user_id": requester, "approved_by_user_id": approved_by}
    else:
        actor_note = {}
    record.row_version = (record.row_version or 1) + 1
    db.flush()
    _log_rows(db, principal, applied["touched"])
    effects = _after_commit(db, principal, calendar, record, impact, event="holiday.year.amended", change="AMENDED")
    actions = sorted({o["op"] for o in ops})
    backdated = any("backdated" in o["bands"] for o in ops)
    AuditLogService.log(db, principal, AUDIT_MODULE, "BACKDATED_CLOSURE" if backdated and actions == ["add"]
                        else "YEAR_AMENDED", f"{calendar.code} {year}: amended to v{record.version}",
                        "holiday_calendar", calendar.id,
                        details={"year": year, "version": record.version, "reason": reason, "operations": actions,
                                 "changed_dates": impact["changed_dates"], **actor_note})
    for item in ops:
        if item["op"] in ("confirm", "cancel", "move"):
            action = {"confirm": "OCCURRENCE_CONFIRMED", "cancel": "OCCURRENCE_CANCELLED",
                      "move": "OCCURRENCE_MOVED"}[item["op"]]
            AuditLogService.log(db, principal, AUDIT_MODULE, action, f"{calendar.code}: occurrence {item['op']}",
                                "holiday", item["holiday_id"], details={"year": year, "version": record.version})
    created = [hid for hid, change in applied["touched"] if change == "CREATED"]
    # 02 §10.2 (LH-HOL-24, LH-HOL-33): any amended date inside an input-locked period is flagged for change detection
    # and a finalized one gets retro detection, whatever the change (closure, move, cancel); the run never changes.
    from app.services import holiday_closures

    changed = [date.fromisoformat(x) for x in impact["changed_dates"]]
    effects["payroll"] = holiday_closures.flag_payroll(
        db, principal, calendar, changed, created or [o.get("holiday_id") for o in ops if o.get("holiday_id")],
        action="CLOSURE_PAYROLL_FLAGGED" if backdated and actions == ["add"] else "AMENDMENT_PAYROLL_FLAGGED")
    body.update({"status": "AMENDED", "year": year_json(db, record, calendar, year), "effects": effects,
                 "created": created, "id": created[0] if created else None})
    return 200, body


def decline_request(db: Session, principal: Principal, calendar_id: int, year: int, reason: Optional[str]) -> dict:
    """Withdraw (requester) or decline (another publisher) a pending second-person request."""
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    _check_publisher(db, principal, calendar)
    record = get_record(db, principal.company_id, calendar.id, year, lock=True)
    if record is None or record.publish_requested_by_user_id is None:
        raise conflict("Nothing is waiting for a second publisher.", "NO_PENDING_REQUEST")
    requester = record.publish_requested_by_user_id
    _clear_request(record)
    record.row_version = (record.row_version or 1) + 1
    db.flush()
    AuditLogService.log(db, principal, AUDIT_MODULE, "YEAR_REQUEST_DECLINED",
                        f"{calendar.code} {year}: pending request {'withdrawn' if requester == principal.user_id else 'declined'}",
                        "holiday_calendar", calendar.id, details={"year": year, "reason": _reason(reason)})
    return year_json(db, record, calendar, year)


# ============================================================================================== single occurrences
def _row_and_year(db: Session, principal: Principal, holiday_id: int) -> Tuple[Holiday, HolidayCalendar, str]:
    h = db.query(Holiday).filter(Holiday.company_id == principal.company_id, Holiday.id == int(holiday_id)).first()
    if h is None or h.calendar_id is None:
        raise not_found("Holiday")
    calendar = _svc()._get_visible(db, principal, h.calendar_id)  # noqa: SLF001
    return h, calendar, state_of(db, principal.company_id, calendar.id, h.date.year)


def _single(db: Session, principal: Principal, holiday_id: int, op: Dict[str, Any], *, reason: Optional[str],
            dry_run: bool, acknowledged_warnings: Sequence[str], expected_row_version: Optional[int]) -> Tuple[int, dict]:
    h, calendar, status = _row_and_year(db, principal, holiday_id)
    if expected_row_version is not None and expected_row_version != h.row_version:
        raise conflict("This holiday was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    if status == "PUBLISHED":
        return amend_year(db, principal, calendar.id, h.date.year, [{**op, "holiday_id": h.id}], reason=reason,
                          dry_run=dry_run, acknowledged_warnings=acknowledged_warnings)
    # DRAFT year: a direct edit (holiday:manage), never counted until publication
    _svc()._get_managed(db, principal, calendar.id)  # noqa: SLF001
    if dry_run:
        return 200, {"dry_run": True, "impact": None}
    record = ensure_year(db, principal.company_id, calendar, h.date.year, lock=True)
    kind = op["op"]
    if kind == "confirm":
        if h.status != "PROVISIONAL":
            raise AppError(422, f"{h.name} is not an expected date.", "INVALID_TRANSITION")
        if op.get("date") and _parse_date(op["date"]) != h.date:
            h.date = _parse_date(op["date"])
            h.source_ref = {**(h.source_ref or {}), "overridden": True}
        h.status = "CONFIRMED"
        h.confirmed_by_user_id = principal.user_id
        h.confirmed_at = clock.utcnow()
    elif kind == "cancel":
        h.status = "CANCELLED"
    elif kind == "move":
        h.date = _parse_date(op.get("new_date"), "new_date")
        if h.date.year != record.year:
            raise AppError(422, f"Move {h.name} within {record.year}.", "INVALID_DATE")
        h.source_ref = {**(h.source_ref or {}), "overridden": True}
    h.row_version = (h.row_version or 1) + 1
    touch_draft(db, record)
    _svc()._flush(db)  # noqa: SLF001
    return 200, {"status": "DRAFT_UPDATED", "occurrence": serialize_occurrence(h)}


def confirm_occurrence(db: Session, principal: Principal, holiday_id: int, *, date_: Optional[Any] = None,
                       reason: Optional[str] = None, dry_run: bool = False, acknowledged_warnings: Sequence[str] = (),
                       expected_row_version: Optional[int] = None) -> Tuple[int, dict]:
    return _single(db, principal, holiday_id, {"op": "confirm", "date": date_}, reason=reason, dry_run=dry_run,
                   acknowledged_warnings=acknowledged_warnings, expected_row_version=expected_row_version)


def cancel_occurrence(db: Session, principal: Principal, holiday_id: int, *, reason: Optional[str] = None,
                      dry_run: bool = False, acknowledged_warnings: Sequence[str] = (),
                      expected_row_version: Optional[int] = None) -> Tuple[int, dict]:
    return _single(db, principal, holiday_id, {"op": "cancel"}, reason=reason, dry_run=dry_run,
                   acknowledged_warnings=acknowledged_warnings, expected_row_version=expected_row_version)


def move_occurrence(db: Session, principal: Principal, holiday_id: int, *, to_date: Any,
                    reason: Optional[str] = None, dry_run: bool = False, acknowledged_warnings: Sequence[str] = (),
                    expected_row_version: Optional[int] = None) -> Tuple[int, dict]:
    return _single(db, principal, holiday_id, {"op": "move", "new_date": to_date}, reason=reason, dry_run=dry_run,
                   acknowledged_warnings=acknowledged_warnings, expected_row_version=expected_row_version)


def edit_draft_row(db: Session, principal: Principal, holiday_id: int, data: Mapping[str, Any], *,
                   expected_row_version: Optional[int] = None) -> dict:
    """PATCH /occurrences/{hid} (02 §4.5): DRAFT-year edits — fields (marks a generated row ``overridden``), action
    SKIP (generated row -> CANCELLED tombstone; manual row deleted), RESET (back to the rule), RESTORE (undo SKIP),
    or ``expected_date`` on day 1 of an announced holiday (TENANT expected start, IG-LH-17)."""
    h, calendar, status = _row_and_year(db, principal, holiday_id)
    _svc()._get_managed(db, principal, calendar.id)  # noqa: SLF001
    if status == "PUBLISHED":
        raise conflict(f"{h.date.year} is published; use confirm, cancel, move or an amendment.", "YEAR_PUBLISHED")
    if expected_row_version is not None and expected_row_version != h.row_version:
        raise conflict("This holiday was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    record = ensure_year(db, principal.company_id, calendar, h.date.year, lock=True)
    action = (data.get("action") or "").upper()
    year = h.date.year
    if action == "SKIP":
        if h.rule_id is None:
            db.delete(h)
            touch_draft(db, record)
            _svc()._flush(db)  # noqa: SLF001
            return {"status": "DELETED", "id": int(holiday_id)}
        h.status = "CANCELLED"
    elif action == "RESTORE":
        if h.status != "CANCELLED":
            raise AppError(422, "Only a skipped holiday can be restored.", "INVALID_TRANSITION")
        h.status = "PROVISIONAL" if (h.source_ref or {}).get("expected_source") else "CONFIRMED"
    elif action == "RESET":
        if h.rule_id is None:
            raise AppError(422, "Only holidays from a rule can be reset.", "INVALID_TRANSITION")
        ref = dict(h.source_ref or {})
        ref.pop("overridden", None)
        h.source_ref = ref
        if h.status == "CANCELLED":
            h.status = "CONFIRMED"
        db.flush()
        holiday_rollover.generate(db, principal.company_id, calendar.id, year, principal)
        db.refresh(h)
    elif "expected_date" in data:
        if h.rule_id is None or h.status != "PROVISIONAL":
            raise AppError(422, "Only an expected holiday from an announced rule has an expected date.",
                           "INVALID_TRANSITION")
        rule_year = (h.source_ref or {}).get("rule_year") or year
        set_expected_date(db, principal, h.rule_id, int(rule_year), data["expected_date"])
        return {"status": "REGENERATED", "year": year}
    else:
        fields = _occurrence_fields(data, require_name=False)
        if "date" in data:
            new_date = _parse_date(data["date"])
            if new_date.year != year:
                raise AppError(422, f"Keep the date in {year}.", "INVALID_DATE")
            working_day_clash(db, db.get(HolidayCalendar, h.calendar_id), [new_date])
            fields["date"] = new_date
        if not fields:
            raise bad_request("Nothing to change.", "NO_CHANGE")
        for k, v in fields.items():
            setattr(h, k, v)
        h.is_optional = h.holiday_type == "OPTIONAL"
        if h.rule_id is not None:
            h.source_ref = {**(h.source_ref or {}), "overridden": True}
    h.row_version = (h.row_version or 1) + 1
    touch_draft(db, record)
    _svc()._flush(db)  # noqa: SLF001
    return {"status": "DRAFT_UPDATED", "occurrence": serialize_occurrence(h)}


def add_occurrence(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]) -> Tuple[int, dict]:
    """POST /{id}/occurrences: into a DRAFT year (or a year without rows: a new DRAFT) directly; into a PUBLISHED
    year as an amendment (``holiday:publish``, reason; a date in the backdate window is a backdated closure)."""
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    if calendar.status != "ACTIVE":
        raise bad_request(f"Holiday calendar {calendar.code} is inactive.", "ORG_DIMENSION_INACTIVE")
    day = _parse_date(data.get("date"))
    status = state_of(db, principal.company_id, calendar.id, day.year)
    op = {"op": "add", "name": data.get("name"), "date": day.isoformat(),
          "holiday_type": data.get("holiday_type") or "COMPANY", "category": data.get("category") or "OTHER",
          "portion": data.get("portion") or "FULL", "status": data.get("status") or "CONFIRMED",
          "duration_days": data.get("duration_days") or 1, "description": data.get("description"),
          "legal_ref": data.get("legal_ref"), "issued_on": data.get("issued_on")}
    if status == "PUBLISHED":
        return amend_year(db, principal, calendar.id, day.year, [op], reason=data.get("reason"),
                          dry_run=bool(data.get("dry_run")),
                          acknowledged_warnings=data.get("acknowledged_warnings") or ())
    svc._get_managed(db, principal, calendar.id)  # noqa: SLF001
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    if day.year < today.year:
        raise conflict(f"{day.year} has passed.", "HOLIDAY_IN_PAST")
    if day.year > today.year + FUTURE_YEARS:
        raise AppError(422, f"Years can be prepared up to {today.year + FUTURE_YEARS}.", "FUTURE_LIMIT")
    record = ensure_year(db, principal.company_id, calendar, day.year, lock=True)
    ops = _check_operations(db, principal, calendar, day.year, [op]) if record.status == "PUBLISHED" else None
    if ops is not None:  # pragma: no cover - ensure_year made it published between the two reads
        return amend_year(db, principal, calendar.id, day.year, [op], reason=data.get("reason"))
    fields = _occurrence_fields(op, require_name=True)
    days = int(op["duration_days"])
    if not 1 <= days <= 31:
        raise bad_request("A holiday lasts 1-31 days.", "VALIDATION_ERROR")
    if op["status"] not in ("CONFIRMED", "PROVISIONAL"):
        raise AppError(422, "A new holiday is CONFIRMED or PROVISIONAL.", "INVALID_TRANSITION")
    working_day_clash(db, calendar, [day + timedelta(days=i) for i in range(days)])
    created = []
    for i in range(days):
        d = day + timedelta(days=i)
        if d.year != day.year:
            raise AppError(422, f"Every day must be in {day.year}.", "INVALID_DATE")
        if db.query(Holiday.id).filter(Holiday.company_id == principal.company_id, Holiday.calendar_id == calendar.id,
                                       Holiday.date == d, Holiday.status != "CANCELLED",
                                       func.lower(Holiday.name) == fields["name"].lower()).first():
            raise conflict("This calendar already has a holiday of that name on that date.", "DUPLICATE_HOLIDAY")
        h = Holiday(company_id=principal.company_id, calendar_id=calendar.id, date=d, location=None,
                    is_optional=fields["holiday_type"] == "OPTIONAL", status=op["status"], source="MANUAL",
                    source_ref={k: op[k] for k in ("legal_ref", "issued_on") if op.get(k)},
                    sequence_index=i + 1 if days > 1 else 1, **fields)
        db.add(h)
        created.append(h)
    touch_draft(db, record)
    svc._flush(db)  # noqa: SLF001
    AuditLogService.log(db, principal, AUDIT_MODULE, "OCCURRENCE_ADDED",
                        f"{calendar.code}: {fields['name']} on {day.isoformat()} (draft)", "holiday_calendar",
                        calendar.id, details={"year": day.year, "days": days})
    return 201, {"status": "DRAFT_UPDATED", "id": created[0].id,
                 "occurrences": [serialize_occurrence(h) for h in created]}


# ============================================================================================== working days
def declare_working_day(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]) -> Tuple[int, dict]:
    """POST /{id}/working-days (01 §5.5): DRAFT year directly, PUBLISHED year as an amendment."""
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    day = _parse_date(data.get("date"))
    status = state_of(db, principal.company_id, calendar.id, day.year)
    op = {"op": "add_working_day", "date": day.isoformat(), "name": data.get("name"), "reason": data.get("reason"),
          "portion": data.get("portion") or "FULL"}
    if status == "PUBLISHED":
        return amend_year(db, principal, calendar.id, day.year, [op], reason=data.get("reason"),
                          dry_run=bool(data.get("dry_run")),
                          acknowledged_warnings=data.get("acknowledged_warnings") or ())
    svc._get_managed(db, principal, calendar.id)  # noqa: SLF001
    values = _check_working_day(db, calendar, op, day.year)
    record = ensure_year(db, principal.company_id, calendar, day.year, lock=True)
    row = CalendarWorkingDay(company_id=principal.company_id, calendar_id=calendar.id, date=day, name=values["name"],
                             portion=values["portion"], reason=values["reason"], created_by_user_id=principal.user_id,
                             row_version=1)
    db.add(row)
    touch_draft(db, record)
    svc._flush(db)  # noqa: SLF001
    AuditLogService.log(db, principal, AUDIT_MODULE, "WORKING_DAY_DECLARED",
                        f"{calendar.code}: working day {day.isoformat()} (draft)", "holiday_calendar", calendar.id,
                        details={"date": day.isoformat(), "name": values["name"]})
    return 201, _working_day_json(row)


def delete_working_day(db: Session, principal: Principal, calendar_id: int, working_day_id: int, *,
                       reason: Optional[str] = None) -> Tuple[int, dict]:
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    row = db.query(CalendarWorkingDay).filter(CalendarWorkingDay.company_id == principal.company_id,
                                              CalendarWorkingDay.calendar_id == calendar.id,
                                              CalendarWorkingDay.id == int(working_day_id)).first()
    if row is None:
        raise not_found("Working day")
    if state_of(db, principal.company_id, calendar.id, row.date.year) == "PUBLISHED":
        return amend_year(db, principal, calendar.id, row.date.year,
                          [{"op": "remove_working_day", "working_day_id": row.id}], reason=reason)
    svc._get_managed(db, principal, calendar.id)  # noqa: SLF001
    record = ensure_year(db, principal.company_id, calendar, row.date.year, lock=True)
    db.delete(row)
    touch_draft(db, record)
    svc._flush(db)  # noqa: SLF001
    return 200, {"deleted": int(working_day_id)}


# ============================================================================================== history
def history(db: Session, principal: Principal, calendar_id: int) -> dict:
    """Years with their versions and the calendar's audit trail (09-ui-ux/02 §18 History tab)."""
    from app.models.audit_log import AuditLog

    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    cid = principal.company_id
    years = [year_json(db, r, calendar, r.year) for r in db.query(HolidayCalendarYear).filter(
        HolidayCalendarYear.company_id == cid, HolidayCalendarYear.calendar_id == calendar.id
    ).order_by(HolidayCalendarYear.year.desc())]
    rule_ids = [r for (r,) in db.query(HolidayRule.id).filter(HolidayRule.company_id == cid,
                                                              HolidayRule.calendar_id == calendar.id)]
    query = db.query(AuditLog).filter(AuditLog.company_id == cid, (
        ((AuditLog.entity_type == "holiday_calendar") & (AuditLog.entity_id == str(calendar.id)))
        | ((AuditLog.entity_type == "holiday_rule") & (AuditLog.entity_id.in_([str(r) for r in rule_ids] or ["-1"])))))
    events = []
    for a in query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(200):
        details = a.details or {}
        events.append({"id": a.id, "action": a.action, "description": a.description,
                       "actor": a.actor_email or ("system" if a.actor_type != "user" else None),
                       "created_at": a.created_at.isoformat() if a.created_at else None,
                       "year": details.get("year"), "version": details.get("version"),
                       "reason": details.get("reason")})
    return {"calendar_id": calendar.id, "years": years, "events": events}


def apply_rules(db: Session, principal: Principal, calendar_id: int, year: int, *, reason: Optional[str],
                dry_run: bool = False, acknowledged_warnings: Sequence[str] = ()) -> Tuple[int, dict]:
    """"Review and apply to <year>…" (02 §3.8, §4.6): what the rules give a PUBLISHED year that it lacks or has
    differently, as one amendment (reason, date bands, second person); never written without it."""
    svc = _svc()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    if state_of(db, principal.company_id, calendar.id, year) != "PUBLISHED":
        raise conflict(f"{year} is not published; generate the draft instead.", "YEAR_NOT_PUBLISHED")
    ops: List[dict] = []
    for line in holiday_rollover.published_difference(db, principal.company_id, calendar, year):
        if line["change"] == "add":
            ops.append({"op": "add", "name": line["name"], "date": line["date"], "holiday_type": line["holiday_type"],
                        "category": line["category"], "portion": line["portion"], "status": line["status"],
                        "rule_id": line["rule_id"], "rule_year": line["rule_year"],
                        "occurrence_key": line["occurrence_key"], "observance": line["observance"],
                        "observed_from_date": line["observed_from_date"], "sequence_index": line["sequence_index"]})
        elif line["change"] == "update":
            current = db.get(Holiday, line["holiday_id"])
            if current is not None and current.date.isoformat() != line["date"]:
                ops.append({"op": "move", "holiday_id": current.id, "new_date": line["date"]})
            elif current is not None:
                ops.append({"op": "edit", "holiday_id": current.id, "name": line["name"],
                            "holiday_type": line["holiday_type"], "category": line["category"],
                            "portion": line["portion"]})
    if not ops:
        return 200, {"dry_run": dry_run, "operations": [], "status": "NO_CHANGE"}
    status, body = amend_year(db, principal, calendar.id, year, ops, reason=reason, dry_run=dry_run,
                              acknowledged_warnings=acknowledged_warnings)
    body["operations"] = ops
    return status, body
