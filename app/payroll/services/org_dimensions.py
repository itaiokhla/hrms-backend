"""Organisation dimensions for payroll: pay groups, grades, cost centers, employment types,
designations, work-week patterns and holiday calendars (02-multi-level-company §10-§13, §20; 3.4 §3, §8).

* Every dimension has a tenant-unique code, a name and a status. It is never deleted; it can be
  deactivated only while no current or future placement (or branch, entity, child calendar) uses it.
* A pay group belongs to one legal entity and fixes frequency, a payroll currency (EC-MON-03) and
  calendar rules validated with app.payroll.calendar. Its entity, currency and frequency are locked
  once a placement uses it (a calendar change is a separate, planned operation).
* Placements take their dimensions through `placement_dimensions`; branches and holidays created by
  older code get their calendar and work week through `apply_branch_defaults` / `calendar_for_holiday`,
  so no new row is left outside a calendar.

Services never commit; every change is audited under the payroll settings module.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import company_today
from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.employee import Employee, Shift
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import (
    PAY_FREQUENCIES,
    WORKER_CLASSES,
    CostCenter,
    Designation,
    EmploymentType,
    Grade,
    HolidayCalendar,
    PayGroup,
    WorkWeekPattern,
)
from app.payroll import audit as payroll_audit
from app.payroll import calendar as pay_calendar
from app.payroll import countries, currencies, workweek
from app.payroll.countries import CountryError
from app.payroll.money import amount_str, to_decimal
from app.payroll.money_errors import CurrencyError, MoneyError
from app.services.audit_log_service import AuditLogService

AUDIT_MODULE = payroll_audit.PAYROLL_SETTINGS
COMPANY_CALENDAR = "COMPANY"
UNMATCHED_CALENDAR = "UNMATCHED"
DEFAULT_WORK_WEEK = "MON_FRI"
DEFAULT_EMPLOYMENT_TYPE = "PERMANENT"
DEFAULT_PAY_GROUP = "MONTHLY"
MONTHLY_RULES = {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}
# CalendarRules fields a pay group may set; banking calendar and tax year come from the entity's country.
CALENDAR_RULE_KEYS = (
    "monthly_start_day", "semi_monthly_split_day", "week_start_day", "biweekly_anchor_date", "pay_day_rule",
    "pay_day_value", "pay_day_shift", "attendance_cycle_mode", "attendance_cycle_start_day",
    "input_cutoff_days_before_pay_day", "approval_days_before_pay_day", "payment_lead_business_days",
)
MAX_AMOUNT_DIGITS = 15  # salary_ranges_valid(): at most 15 integer digits
PLACEMENT_FIELDS = (
    "pay_group_id", "grade_id", "employment_type_id", "designation_id", "cost_center_id", "work_week_pattern_id",
    "shift_id", "fte", "holiday_calendar_id", "work_timezone",
)
READ_CODES = ("org:read", "org:manage_dimensions")
PAY_GROUP_READ_CODES = READ_CODES + ("payroll:configuration:read",)

KINDS: Dict[str, Tuple[type, str, str]] = {  # kind -> (model, label, audit entity type)
    "pay_group": (PayGroup, "Pay group", "pay_group"),
    "grade": (Grade, "Grade", "grade"),
    "cost_center": (CostCenter, "Cost center", "cost_center"),
    "employment_type": (EmploymentType, "Employment type", "employment_type"),
    "designation": (Designation, "Designation", "designation"),
    "work_week": (WorkWeekPattern, "Work week", "work_week_pattern"),
    "holiday_calendar": (HolidayCalendar, "Holiday calendar", "holiday_calendar"),
}
CODE_LIMITS = {"holiday_calendar": (50, 150)}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _get(db: Session, company_id: int, kind: str, object_id: Optional[int]):
    model, label, _ = KINDS[kind]
    row = None
    if object_id is not None:
        row = db.query(model).filter(model.company_id == company_id, model.id == int(object_id)).first()
    if row is None:
        raise not_found(label)
    return row


def _active(db: Session, company_id: int, kind: str, object_id: int):
    row = _get(db, company_id, kind, object_id)
    if row.status != "ACTIVE":
        raise bad_request(f"{KINDS[kind][1]} {row.code} is inactive.", "ORG_DIMENSION_INACTIVE")
    return row


def _code(value: Any, limit: int = 30) -> str:
    code = "".join(str(value or "").split()).upper()
    if not code or len(code) > limit or not all(c.isalnum() or c in "-_." for c in code):
        raise bad_request(f"Code must be 1-{limit} letters, digits, '-', '_' or '.'.", "ORG_CODE_INVALID")
    return code


def _name(value: Any, limit: int = 100) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > limit:
        raise bad_request(f"Name must be 1-{limit} characters.", "ORG_NAME_INVALID")
    return name


def _status(value: Any) -> str:
    if value not in ("ACTIVE", "INACTIVE"):
        raise bad_request("Status must be ACTIVE or INACTIVE.", "ORG_STATUS_INVALID")
    return value


def _flush(db: Session, label: str) -> None:
    """Flush; a database rule the checks above missed (a concurrent insert) becomes a 409/400."""
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if getattr(exc.orig, "pgcode", None) == "23505":
            raise conflict(f"Another {label.lower()} already uses this code.", "ORG_CODE_TAKEN")
        raise bad_request(f"{label}: the change breaks an organisation rule.", "ORG_CONSTRAINT")


def _assert_code_free(db: Session, company_id: int, kind: str, code: str, own_id: Optional[int]) -> None:
    model, label, _ = KINDS[kind]
    clash = db.query(model.id).filter(model.company_id == company_id, model.code == code)
    if own_id:
        clash = clash.filter(model.id != own_id)
    if clash.first():
        raise conflict(f"Another {label.lower()} already uses this code.", "ORG_CODE_TAKEN")


def _snapshot(row, fields: Iterable[str]) -> dict:
    out = {}
    for field in fields:
        value = getattr(row, field)
        out[field] = value.isoformat() if isinstance(value, date) else str(value) if isinstance(value, Decimal) else value
    return out


def _audit(db: Session, principal: Principal, kind: str, row, created: bool, before: Optional[dict], after: dict) -> None:
    _, label, entity_type = KINDS[kind]
    action = f"{entity_type.upper()}_{'CREATED' if created else 'UPDATED'}"
    changed = sorted(k for k in after if before is None or before.get(k) != after.get(k))
    AuditLogService.log(
        db, principal, AUDIT_MODULE, action,
        f"{'Created' if created else 'Updated'} {label.lower()} {row.code} ({row.name}): {', '.join(changed) or 'no changes'}",
        entity_type, row.id,
        details={"before": None if before is None else {k: before.get(k) for k in changed},
                 "after": {k: after.get(k) for k in changed}},
    )


def _apply_common(db: Session, principal: Principal, kind: str, row, data: Mapping[str, Any]) -> None:
    code_limit, name_limit = CODE_LIMITS.get(kind, (30, 100))
    if "code" in data or row.id is None:
        row.code = _code(data.get("code"), code_limit)
        _assert_code_free(db, principal.company_id, kind, row.code, row.id)
    if "name" in data or row.id is None:
        row.name = _name(data.get("name"), name_limit)
    if "status" in data and data["status"] is not None:
        status = _status(data["status"])
        if status == "INACTIVE" and row.status != "INACTIVE" and row.id is not None:
            where = usage(db, principal.company_id, kind, row.id)
            if where:
                raise conflict(f"{KINDS[kind][1]} {row.code} is still used by {where}.", "ORG_DIMENSION_IN_USE")
        row.status = status


def _current_or_future(company_id: int, today: date):
    return (
        EmployeeAssignment.company_id == company_id,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= today),
    )


def usage(db: Session, company_id: int, kind: str, object_id: int) -> Optional[str]:
    """What keeps a dimension in use (a current or future placement, a branch, an entity, a child calendar), or None."""
    today = company_today(db, company_id)
    column = {
        "pay_group": EmployeeAssignment.pay_group_id, "grade": EmployeeAssignment.grade_id,
        "cost_center": EmployeeAssignment.cost_center_id, "employment_type": EmployeeAssignment.employment_type_id,
        "designation": EmployeeAssignment.designation_id, "work_week": EmployeeAssignment.work_week_pattern_id,
    }.get(kind)
    if column is not None and db.query(EmployeeAssignment.id).filter(
        *_current_or_future(company_id, today), column == object_id
    ).first():
        return "current or future placements"
    if kind == "work_week":
        from app.services import holiday_work_weeks

        if db.query(Branch.id).filter(Branch.company_id == company_id, Branch.status == "ACTIVE",
                                      Branch.work_week_pattern_id == object_id).first():
            return "a branch"
        # Effective-dated links, criteria rows and entity defaults (LH 04-holiday/03 §5.8 step 6).
        where = holiday_work_weeks.usage_reason(db, company_id, object_id)
        if where:
            return where
        if db.query(LegalEntity.id).filter(LegalEntity.company_id == company_id, LegalEntity.status == "ACTIVE",
                                           LegalEntity.default_work_week_pattern_id == object_id).first():
            return "a legal entity"
    if kind == "holiday_calendar":
        from app.services import holiday_calendar_service

        if db.query(Branch.id).filter(Branch.company_id == company_id, Branch.status == "ACTIVE",
                                      Branch.holiday_calendar_id == object_id).first():
            return "a branch"
        # Effective-dated links, criteria rows, overrides and includes (LH 04-holiday/01 §3.9).
        return holiday_calendar_service.usage_reason(db, company_id, object_id)
    return None


def _is_referenced(db: Session, company_id: int, column, object_id: int) -> bool:
    return db.query(EmployeeAssignment.id).filter(EmployeeAssignment.company_id == company_id,
                                                  column == object_id).first() is not None


# ---------------------------------------------------------------------------
# Read reach (org:read may be entity- or branch-scoped; org:manage_dimensions is company-wide)
# ---------------------------------------------------------------------------
def entity_reach(db: Session, principal: Principal, codes: Iterable[str] = PAY_GROUP_READ_CODES) -> Tuple[Optional[Set[int]], Set[int]]:
    """(legal entity ids, extra pay group ids) the principal may read; None = every entity."""
    codes = tuple(codes)
    if principal.is_company_admin or any(principal.has_company_scope(c) for c in codes):
        return None, set()
    entities: Set[int] = set()
    for code in codes:
        entities |= set(principal.scope_targets(code, "LEGAL_ENTITY"))
    branch_ids = set()
    for code in codes:
        branch_ids |= set(principal.scope_targets(code, "BRANCH"))
    if branch_ids:
        entities |= {e for (e,) in db.query(Branch.legal_entity_id).filter(
            Branch.company_id == principal.company_id, Branch.id.in_(sorted(branch_ids)))}
    pay_groups: Set[int] = set()
    for code in codes:
        pay_groups |= set(principal.scope_targets(code, "PAY_GROUP"))
    return entities, pay_groups


def _pay_group_visible(db: Session, principal: Principal, group: PayGroup) -> bool:
    entities, groups = entity_reach(db, principal)
    return entities is None or group.legal_entity_id in entities or group.id in groups


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
def _base(row) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name, "status": row.status}


def serialize(kind: str, row) -> dict:
    data = _base(row)
    if kind == "pay_group":
        data.update(legal_entity_id=row.legal_entity_id, currency=row.currency, pay_frequency=row.pay_frequency,
                    calendar_rules=row.calendar_rules or {}, description=row.description)
    elif kind == "grade":
        data.update(rank=row.rank, salary_ranges=row.salary_ranges or [])
    elif kind == "cost_center":
        data.update(legal_entity_id=row.legal_entity_id)
    elif kind == "employment_type":
        data.update(worker_class=row.worker_class)
    elif kind == "designation":
        data.update(grade_id=row.grade_id)
    elif kind == "work_week":
        data.update(weekly_off_days=row.weekly_off_days or [], nth_weekday_offs=row.nth_weekday_offs or [])
    elif kind == "holiday_calendar":
        data.update(parent_calendar_id=row.parent_calendar_id, country_code=row.country_code,
                    subdivision_code=row.subdivision_code, legal_entity_id=row.legal_entity_id)
    return data


def list_rows(db: Session, principal: Principal, kind: str, *, status: Optional[str] = None,
              legal_entity_id: Optional[int] = None) -> List[dict]:
    model = KINDS[kind][0]
    query = db.query(model).filter(model.company_id == principal.company_id)
    if status:
        query = query.filter(model.status == _status(status))
    if kind in ("pay_group", "cost_center"):
        if legal_entity_id:
            query = query.filter(model.legal_entity_id == legal_entity_id)
        entities, groups = entity_reach(db, principal, PAY_GROUP_READ_CODES if kind == "pay_group" else READ_CODES)
        if entities is not None:
            allowed = model.legal_entity_id.in_(sorted(entities))
            if kind == "pay_group" and groups:
                allowed = or_(allowed, model.id.in_(sorted(groups)))
            elif kind == "cost_center":
                allowed = or_(allowed, model.legal_entity_id.is_(None))
            query = query.filter(allowed)
    return [serialize(kind, row) for row in query.order_by(model.code)]


def get_row(db: Session, principal: Principal, kind: str, object_id: int) -> dict:
    row = _get(db, principal.company_id, kind, object_id)
    if kind == "pay_group" and not _pay_group_visible(db, principal, row):
        raise not_found(KINDS[kind][1])
    if kind == "cost_center" and row.legal_entity_id is not None:
        entities, _ = entity_reach(db, principal, READ_CODES)
        if entities is not None and row.legal_entity_id not in entities:
            raise not_found(KINDS[kind][1])
    return serialize(kind, row)


# ---------------------------------------------------------------------------
# Pay groups
# ---------------------------------------------------------------------------
def _entity(db: Session, company_id: int, entity_id: Any) -> LegalEntity:
    entity = None
    if entity_id is not None:
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.id == int(entity_id)).first()
    if entity is None:
        raise not_found("Legal entity")
    return entity


def payroll_currency(code: Any) -> str:
    try:
        return currencies.require_payroll_currency(code).code
    except CurrencyError as exc:
        raise bad_request(exc.message, exc.code)


def _country_facts(country_code: str) -> Tuple[Set[int], str]:
    try:
        return countries.default_weekend(country_code), countries.tax_year_start(country_code)
    except CountryError:
        return {5, 6}, "01-01"


def validate_calendar_rules(frequency: Any, rules: Any, country_code: str) -> dict:
    """Normalized calendar rules for a pay group; raises 400 with the calendar's error code."""
    if frequency not in PAY_FREQUENCIES:
        raise bad_request(f"Pay frequency must be one of {', '.join(PAY_FREQUENCIES)}.", "INVALID_FREQUENCY")
    if not isinstance(rules, Mapping):
        raise bad_request("Calendar rules must be an object.", "INVALID_RULES")
    unknown = sorted(set(rules) - set(CALENDAR_RULE_KEYS))
    if unknown:
        raise bad_request(f"Unknown calendar rules: {', '.join(map(str, unknown))}.", "INVALID_RULES")
    fields = dict(rules)
    anchor = fields.get("biweekly_anchor_date")
    try:
        if isinstance(anchor, str):
            try:
                fields["biweekly_anchor_date"] = date.fromisoformat(anchor)
            except ValueError:
                raise pay_calendar.CalendarError("NOT_A_LOCAL_DATE", f"biweekly_anchor_date {anchor!r} is not an ISO date")
        weekend, tax_year_start = _country_facts(country_code)
        parsed = pay_calendar.CalendarRules(banking_calendar=pay_calendar.BankingCalendar(weekend=weekend),
                                            tax_year_start=tax_year_start, **fields)
        pay_calendar.validate_rules(frequency, parsed)
        start = date.today().replace(day=1)
        pay_calendar.generate_periods(frequency, parsed, start, start + timedelta(days=400))
    except pay_calendar.CalendarError as exc:
        raise bad_request(f"Calendar rules: {exc.message}.", exc.code)
    except TypeError as exc:
        raise bad_request(f"Calendar rules: {exc}.", "INVALID_RULES")
    normalized = {}
    for key in rules:
        value = getattr(parsed, key)
        normalized[key] = value.isoformat() if isinstance(value, date) else str(value) if hasattr(value, "value") else value
    return normalized


def save_pay_group(db: Session, principal: Principal, data: Mapping[str, Any], group_id: Optional[int] = None) -> PayGroup:
    cid = principal.company_id
    group = _get(db, cid, "pay_group", group_id) if group_id else PayGroup(company_id=cid, status="ACTIVE")
    fields = ("legal_entity_id", "code", "name", "currency", "pay_frequency", "calendar_rules", "description", "status")
    before = None if group.id is None else _snapshot(group, fields)
    used = group.id is not None and _is_referenced(db, cid, EmployeeAssignment.pay_group_id, group.id)
    entity = _entity(db, cid, data.get("legal_entity_id", group.legal_entity_id))
    if group.id is None and entity.status != "ACTIVE":
        raise bad_request("The legal entity is inactive.", "ORG_ENTITY_INACTIVE")
    currency = payroll_currency(data.get("currency") or group.currency or entity.base_currency)
    frequency = data.get("pay_frequency", group.pay_frequency)
    if used and (entity.id, currency, frequency) != (group.legal_entity_id, group.currency, group.pay_frequency):
        raise conflict("The legal entity, currency and frequency of a pay group in use cannot change. "
                       "Create a new pay group and move people to it.", "ORG_PAY_GROUP_LOCKED")
    rules = data.get("calendar_rules", group.calendar_rules if group.id else None)
    rules = validate_calendar_rules(frequency, {} if rules is None else rules, entity.country_code)
    _apply_common(db, principal, "pay_group", group, data)
    group.legal_entity_id, group.currency, group.pay_frequency, group.calendar_rules = entity.id, currency, frequency, rules
    if "description" in data:
        group.description = (" ".join(str(data["description"] or "").split()) or None)
    created = group.id is None
    if created:
        db.add(group)
    _flush(db, "Pay group")
    _audit(db, principal, "pay_group", group, created, before, _snapshot(group, fields))
    return group


# ---------------------------------------------------------------------------
# Grades, cost centers, employment types, designations
# ---------------------------------------------------------------------------
def _salary_ranges(raw: Any) -> List[dict]:
    if raw is None:
        return []
    bands, seen = [], set()
    for band in raw:
        try:
            currency = currencies.require_payroll_currency(band["currency"]).code
            if currency in seen:
                raise bad_request(f"Grade has two salary ranges in {currency}.", "GRADE_RANGE_DUPLICATE")
            seen.add(currency)
            values = {k: to_decimal(band[k]) for k in ("min", "mid", "max") if band.get(k) is not None}
            out = {"currency": currency}
            for key, value in values.items():
                if value < 0 or len(str(int(value))) > MAX_AMOUNT_DIGITS:
                    raise bad_request("Salary range amounts must be zero or positive and at most 15 digits.",
                                      "GRADE_RANGE_INVALID")
                out[key] = amount_str(value, currency)
        except (CurrencyError, MoneyError) as exc:
            raise bad_request(exc.message, exc.code)
        if "min" not in values or "max" not in values:
            raise bad_request("A salary range needs min and max.", "GRADE_RANGE_INVALID")
        if not values["min"] <= values.get("mid", values["min"]) <= values["max"]:
            raise bad_request("A salary range needs min <= mid <= max.", "GRADE_RANGE_INVALID")
        bands.append(out)
    return sorted(bands, key=lambda b: b["currency"])


def _save_simple(db: Session, principal: Principal, kind: str, data: Mapping[str, Any], object_id: Optional[int],
                 fields: Tuple[str, ...], apply) -> Any:
    cid = principal.company_id
    model = KINDS[kind][0]
    row = _get(db, cid, kind, object_id) if object_id else model(company_id=cid, status="ACTIVE")
    all_fields = ("code", "name", "status") + fields
    before = None if row.id is None else _snapshot(row, all_fields)
    apply(row)
    _apply_common(db, principal, kind, row, data)
    created = row.id is None
    if created:
        db.add(row)
    _flush(db, KINDS[kind][1])
    _audit(db, principal, kind, row, created, before, _snapshot(row, all_fields))
    return row


def save_grade(db: Session, principal: Principal, data: Mapping[str, Any], grade_id: Optional[int] = None) -> Grade:
    def apply(row: Grade) -> None:
        if "rank" in data:
            row.rank = data["rank"]
        if "salary_ranges" in data or row.id is None:
            row.salary_ranges = _salary_ranges(data.get("salary_ranges"))

    return _save_simple(db, principal, "grade", data, grade_id, ("rank", "salary_ranges"), apply)


def save_cost_center(db: Session, principal: Principal, data: Mapping[str, Any], center_id: Optional[int] = None) -> CostCenter:
    cid = principal.company_id

    def apply(row: CostCenter) -> None:
        if "legal_entity_id" in data:
            new = _entity(db, cid, data["legal_entity_id"]).id if data["legal_entity_id"] is not None else None
            if row.id is not None and new != row.legal_entity_id and new is not None:
                today = company_today(db, cid)
                if db.query(EmployeeAssignment.id).filter(
                    *_current_or_future(cid, today), EmployeeAssignment.cost_center_id == row.id,
                    EmployeeAssignment.legal_entity_id != new,
                ).first():
                    raise conflict("People in other legal entities still use this cost center.", "ORG_DIMENSION_IN_USE")
            row.legal_entity_id = new

    return _save_simple(db, principal, "cost_center", data, center_id, ("legal_entity_id",), apply)


def save_employment_type(db: Session, principal: Principal, data: Mapping[str, Any], type_id: Optional[int] = None) -> EmploymentType:
    def apply(row: EmploymentType) -> None:
        if "worker_class" in data or row.id is None:
            if data.get("worker_class") not in WORKER_CLASSES:
                raise bad_request(f"Worker class must be one of {', '.join(WORKER_CLASSES)}.", "ORG_WORKER_CLASS_INVALID")
            row.worker_class = data["worker_class"]

    return _save_simple(db, principal, "employment_type", data, type_id, ("worker_class",), apply)


def save_designation(db: Session, principal: Principal, data: Mapping[str, Any], designation_id: Optional[int] = None) -> Designation:
    def apply(row: Designation) -> None:
        if "grade_id" in data:
            row.grade_id = _active(db, principal.company_id, "grade", data["grade_id"]).id if data["grade_id"] else None

    return _save_simple(db, principal, "designation", data, designation_id, ("grade_id",), apply)


# ---------------------------------------------------------------------------
# Work weeks and holiday calendars
# ---------------------------------------------------------------------------
def save_work_week(db: Session, principal: Principal, data: Mapping[str, Any], pattern_id: Optional[int] = None) -> WorkWeekPattern:
    """Legacy alias of /api/v1/work-weeks (LH 04-holiday/03 §5.8 step 5): a create gets version 1 for every date;
    rule fields on an existing pattern post a new version effective the pattern-local today (no history rewrite)."""
    from app.services import holiday_work_weeks

    pending: Dict[str, Any] = {}

    def apply(row: WorkWeekPattern) -> None:
        if row.id is None or "weekly_off_days" in data or "nth_weekday_offs" in data:
            if row.id is not None and row.kind != workweek.WEEKLY:
                raise bad_request("Edit cycle work weeks under Holidays > Work weeks.", "KIND_MISMATCH")
            raw = {
                "weekly_off_days": data.get("weekly_off_days", row.weekly_off_days if row.id else []),
                "nth_weekday_offs": data.get("nth_weekday_offs", row.nth_weekday_offs if row.id else []),
            }
            try:
                pattern = workweek.WorkWeekPattern.from_json(raw).to_json()
            except workweek.WorkWeekError as exc:
                raise bad_request(f"Work week: {exc.message}.", exc.code)
            if row.id is None:
                row.weekly_off_days, row.nth_weekday_offs = pattern["weekly_off_days"], pattern["nth_weekday_offs"]
            else:
                pending["rules"] = pattern

    row = _save_simple(db, principal, "work_week", data, pattern_id, ("weekly_off_days", "nth_weekday_offs"), apply)
    if pattern_id is None:
        holiday_work_weeks.ensure_first_version(db, row, actor_user_id=principal.user_id)
    elif pending:
        holiday_work_weeks.new_version_for_legacy(db, principal, row, pending["rules"])
    return row


def save_holiday_calendar(db: Session, principal: Principal, data: Mapping[str, Any], calendar_id: Optional[int] = None) -> HolidayCalendar:
    """Legacy alias of /api/v1/holiday-calendars: ``parent_calendar_id`` is mirrored as the priority-0 include
    (LH 04-holiday/01 §3.3 rule 4), so flattening sees the same tree."""
    from app.services import holiday_calendar_service

    cid = principal.company_id
    old_parent = None
    if calendar_id is not None:
        old_parent = db.query(HolidayCalendar.parent_calendar_id).filter(
            HolidayCalendar.company_id == cid, HolidayCalendar.id == int(calendar_id)).scalar()

    def apply(row: HolidayCalendar) -> None:
        if "parent_calendar_id" in data:
            parent_id = data["parent_calendar_id"]
            if parent_id is not None:
                parent = _get(db, cid, "holiday_calendar", parent_id)
                if row.id is not None and (parent.id == row.id or holiday_calendar_service.include_path(
                        holiday_calendar_service._graph(db, cid), parent.id, row.id) is not None):
                    raise bad_request("A holiday calendar cannot inherit from itself or one of its descendants.",
                                      "ORG_CALENDAR_CYCLE")
                parent_id = parent.id
            row.parent_calendar_id = parent_id
        country = row.country_code
        if "country_code" in data:
            country = None
            if data["country_code"]:
                info = countries.find(str(data["country_code"]).upper())
                if info is None:
                    raise bad_request("Choose a country (ISO 3166-1 code).", "ORG_COUNTRY_INVALID")
                country = info.alpha2
            row.country_code = country
        if "subdivision_code" in data or "country_code" in data:
            sub = data.get("subdivision_code", row.subdivision_code)
            if sub:
                sub = str(sub).upper()
                try:
                    countries.subdivision(sub)
                except CountryError:
                    raise bad_request("Choose a state or region (ISO 3166-2).", "ORG_SUBDIVISION_INVALID")
                if country and not sub.startswith(country + "-"):
                    raise bad_request("The region must belong to the calendar's country.", "ORG_SUBDIVISION_INVALID")
            row.subdivision_code = sub or None

    row = _save_simple(db, principal, "holiday_calendar", data, calendar_id,
                       ("parent_calendar_id", "country_code", "subdivision_code"), apply)
    if row.parent_calendar_id != old_parent:
        _mirror_parent(db, row, old_parent)
    return row


def _mirror_parent(db: Session, row: HolidayCalendar, old_parent: Optional[int]) -> None:
    """Create, re-point or delete the priority-0 include that mirrors ``parent_calendar_id``."""
    from app.models.holiday import HolidayCalendarInclude

    mirror = db.query(HolidayCalendarInclude).filter(HolidayCalendarInclude.company_id == row.company_id,
                                                     HolidayCalendarInclude.calendar_id == row.id,
                                                     HolidayCalendarInclude.priority == 0).first()
    if row.parent_calendar_id is None:
        if mirror is not None:
            db.delete(mirror)
    elif mirror is not None:
        mirror.included_calendar_id = row.parent_calendar_id
    else:
        db.add(HolidayCalendarInclude(company_id=row.company_id, calendar_id=row.id,
                                      included_calendar_id=row.parent_calendar_id, priority=0))
    _flush(db, "Holiday calendar")


def link_branch(db: Session, principal: Principal, branch_id: int, data: Mapping[str, Any]) -> Branch:
    """Point a branch at a holiday calendar and/or a work week (3.4 §3, §8).

    Legacy alias of PUT /api/v1/branches/{id}/calendar-link (LH 04-holiday/01 §4.3): an effective-dated link from
    T_link (the branch's local today, or later when the patterns' users are ahead), never backdated.
    """
    from app.services import holiday_assignments

    cid = principal.company_id
    branch = db.query(Branch).filter(Branch.company_id == cid, Branch.id == branch_id).first()
    if branch is None:
        raise not_found("Branch")
    for key in ("holiday_calendar_id", "work_week_pattern_id"):
        if key in data and data[key] is None:
            raise bad_request("A branch always needs a holiday calendar and a work week.", "ORG_BRANCH_LINK_REQUIRED")
    if "holiday_calendar_id" in data:
        _active(db, cid, "holiday_calendar", data["holiday_calendar_id"])
    if "work_week_pattern_id" in data:
        _active(db, cid, "work_week", data["work_week_pattern_id"])
    if any(k in data for k in ("holiday_calendar_id", "work_week_pattern_id")):
        holiday_assignments.set_branch_link(db, principal, branch.id, {
            k: data[k] for k in ("holiday_calendar_id", "work_week_pattern_id") if k in data}, legacy=True)
        db.refresh(branch)
    return branch


def serialize_branch_link(branch: Branch) -> dict:
    return {"id": branch.id, "code": branch.code, "name": branch.name, "legal_entity_id": branch.legal_entity_id,
            "holiday_calendar_id": branch.holiday_calendar_id, "work_week_pattern_id": branch.work_week_pattern_id}


# ---------------------------------------------------------------------------
# Defaults for rows created by existing code (branches, holidays, placements)
# ---------------------------------------------------------------------------
def _by_code(db: Session, model, company_id: int, code: str, active_only: bool = True):
    query = db.query(model).filter(model.company_id == company_id, model.code == code)
    if active_only:
        query = query.filter(model.status == "ACTIVE")
    return query.first()


def default_pay_group(db: Session, company_id: int, entity_id: int) -> Optional[PayGroup]:
    """The entity's first ACTIVE pay group (the starter / migration group unless an administrator made another)."""
    return db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.legal_entity_id == entity_id,
                                     PayGroup.status == "ACTIVE").order_by(PayGroup.id).first()


def default_work_week_id(db: Session, branch: Branch) -> Optional[int]:
    if branch.work_week_pattern_id:
        return branch.work_week_pattern_id
    entity = db.get(LegalEntity, branch.legal_entity_id)
    if entity is not None and entity.default_work_week_pattern_id:
        return entity.default_work_week_pattern_id
    pattern = _by_code(db, WorkWeekPattern, branch.company_id, DEFAULT_WORK_WEEK)
    return pattern.id if pattern else None


def apply_branch_defaults(db: Session, branch: Branch) -> None:
    """A new branch gets its entity's work week and the tenant's company holiday calendar."""
    if branch.work_week_pattern_id is None:
        branch.work_week_pattern_id = default_work_week_id(db, branch)
    if branch.holiday_calendar_id is None:
        calendar = _by_code(db, HolidayCalendar, branch.company_id, COMPANY_CALENDAR)
        branch.holiday_calendar_id = calendar.id if calendar else None


def _new_calendar_code(db: Session, company_id: int, base: str) -> str:
    taken = {c for (c,) in db.query(HolidayCalendar.code).filter(HolidayCalendar.company_id == company_id,
                                                                 HolidayCalendar.code.like(f"{base}%"))}
    code, n = base[:50], 1
    while code in taken:
        n += 1
        code = f"{base[:45]}-{n}"
    return code


def branch_own_calendar(db: Session, branch: Branch, principal: Optional[Principal] = None) -> int:
    """A calendar only this branch uses, for its location-specific holidays.

    A branch sharing a calendar (the tenant-wide COMPANY calendar, or one other branches or calendars
    use) gets a new calendar that inherits from the shared one, so its holidays never reach other branches.
    """
    cid = branch.company_id
    current = db.get(HolidayCalendar, branch.holiday_calendar_id) if branch.holiday_calendar_id else None
    if current is not None and current.code != COMPANY_CALENDAR and current.status == "ACTIVE":
        shared = db.query(Branch.id).filter(Branch.company_id == cid, Branch.holiday_calendar_id == current.id,
                                            Branch.id != branch.id).first()
        children = db.query(HolidayCalendar.id).filter(HolidayCalendar.company_id == cid,
                                                       HolidayCalendar.parent_calendar_id == current.id).first()
        if not shared and not children:
            return current.id
    parent = current or _by_code(db, HolidayCalendar, cid, COMPANY_CALENDAR)
    subdivision = branch.subdivision_code if branch.subdivision_code and branch.country_code and \
        branch.subdivision_code.startswith(branch.country_code + "-") else None
    calendar = HolidayCalendar(
        company_id=cid, code=_new_calendar_code(db, cid, f"BR-{branch.code}"), name=f"{branch.name} holidays"[:150],
        parent_calendar_id=parent.id if parent else None, country_code=branch.country_code, subdivision_code=subdivision,
        status="ACTIVE",
    )
    db.add(calendar)
    db.flush()
    if calendar.parent_calendar_id is not None:
        _mirror_parent(db, calendar, None)
    old_calendar_id = branch.holiday_calendar_id
    branch.holiday_calendar_id = calendar.id
    # The branch's effective-dated links follow too (LH 04-holiday/01 §3.8): the new calendar includes the old one and
    # holds only the regional holidays being created, so every past date keeps its holidays (zero diff with the legacy
    # column-only behaviour, which applied the branch calendar to every date).
    from app.models.pay_org import BranchCalendarLink

    for link in db.query(BranchCalendarLink).filter(BranchCalendarLink.company_id == cid,
                                                    BranchCalendarLink.branch_id == branch.id):
        if link.holiday_calendar_id == old_calendar_id or link.holiday_calendar_id is None:
            link.holiday_calendar_id = calendar.id
            link.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, AUDIT_MODULE, "HOLIDAY_CALENDAR_CREATED",
                        f"Created holiday calendar {calendar.code} for the regional holidays of branch {branch.code}",
                        "holiday_calendar", calendar.id, company_id=cid,
                        actor_type="user" if principal is not None else "system",
                        details={"branch_id": branch.id, "parent_calendar_id": calendar.parent_calendar_id})
    return calendar.id


def calendar_for_holiday(db: Session, company_id: int, location: Optional[str],
                         principal: Optional[Principal] = None) -> Optional[int]:
    """The calendar a holiday created through the legacy holiday screen belongs to (as migration 0008 maps them)."""
    if not location:
        calendar = _by_code(db, HolidayCalendar, company_id, COMPANY_CALENDAR, active_only=False)
        return calendar.id if calendar else None
    branch = db.query(Branch).filter(Branch.company_id == company_id, Branch.name == location).first()
    if branch is not None:
        return branch_own_calendar(db, branch, principal)
    calendar = _by_code(db, HolidayCalendar, company_id, UNMATCHED_CALENDAR, active_only=False)
    if calendar is None:
        calendar = HolidayCalendar(company_id=company_id, code=UNMATCHED_CALENDAR,
                                   name="Holidays without a matching branch", status="ACTIVE")
        db.add(calendar)
        db.flush()
    return calendar.id


def default_holiday_type(is_optional: bool) -> str:
    return "OPTIONAL" if is_optional else "COMPANY"


# ---------------------------------------------------------------------------
# Placement dimensions
# ---------------------------------------------------------------------------
def _fte(value: Any) -> Decimal:
    try:
        fte = Decimal(str(value))
    except Exception:
        raise bad_request("FTE must be a decimal between 0 and 1.", "ORG_FTE_INVALID")
    if not fte.is_finite() or fte <= 0 or fte > 1 or fte.as_tuple().exponent < -4:
        raise bad_request("FTE must be greater than 0, at most 1, with up to 4 decimals.", "ORG_FTE_INVALID")
    return fte


def placement_dimensions(db: Session, company_id: int, *, entity_id: int, branch: Branch, data: Mapping[str, Any],
                         base: Optional[EmployeeAssignment], employee: Employee) -> Dict[str, Any]:
    """Validated dimension columns for a new placement row.

    A key present in `data` wins (null clears an optional dimension, or picks the default of a required
    one); otherwise the placement being continued (`base`) supplies it while still valid, else the
    default: the entity's first active pay group, PERMANENT, the branch's work week, the employee's shift.
    """
    def given(key: str) -> bool:
        return key in data and data[key] is not None

    def keep(key: str):
        return getattr(base, key) if base is not None and key not in data else None

    out: Dict[str, Any] = {}
    if given("pay_group_id"):
        group = _active(db, company_id, "pay_group", data["pay_group_id"])
        if group.legal_entity_id != entity_id:
            raise bad_request(f"Pay group {group.code} belongs to another legal entity.", "ORG_PAY_GROUP_MISMATCH")
        out["pay_group_id"] = group.id
    elif base is not None and "pay_group_id" not in data and base.legal_entity_id == entity_id and base.pay_group_id:
        out["pay_group_id"] = base.pay_group_id
    else:
        group = default_pay_group(db, company_id, entity_id)
        out["pay_group_id"] = group.id if group else None

    if given("employment_type_id"):
        out["employment_type_id"] = _active(db, company_id, "employment_type", data["employment_type_id"]).id
    else:
        kept = keep("employment_type_id")
        default = _by_code(db, EmploymentType, company_id, DEFAULT_EMPLOYMENT_TYPE) if kept is None else None
        out["employment_type_id"] = kept or (default.id if default else None)

    for key, kind in (("grade_id", "grade"), ("designation_id", "designation")):
        out[key] = _active(db, company_id, kind, data[key]).id if given(key) else keep(key)
    if given("designation_id") and not given("grade_id") and "grade_id" not in data:
        designation = db.get(Designation, out["designation_id"])
        out["grade_id"] = designation.grade_id or out["grade_id"]

    if given("cost_center_id"):
        center = _active(db, company_id, "cost_center", data["cost_center_id"])
        if center.legal_entity_id not in (None, entity_id):
            raise bad_request(f"Cost center {center.code} belongs to another legal entity.", "ORG_COST_CENTER_MISMATCH")
        out["cost_center_id"] = center.id
    else:
        kept = keep("cost_center_id")
        center = db.get(CostCenter, kept) if kept else None
        out["cost_center_id"] = kept if center is not None and center.legal_entity_id in (None, entity_id) else None

    # LH V3-014: only an explicit employee-level pattern is stored (it moves with the placement while the branch
    # stays); otherwise NULL, so the branch link, criteria rows and entity default resolve it per date.
    if given("work_week_pattern_id"):
        week = _active(db, company_id, "work_week", data["work_week_pattern_id"])
        if week.legal_entity_id is not None and week.legal_entity_id != entity_id:
            raise AppError(422, f"Work week {week.code} belongs to another legal entity.", "PATTERN_ENTITY_MISMATCH")
        out["work_week_pattern_id"] = week.id
    elif base is not None and "work_week_pattern_id" not in data and base.branch_id == branch.id and base.work_week_pattern_id:
        out["work_week_pattern_id"] = base.work_week_pattern_id
    else:
        out["work_week_pattern_id"] = None

    if given("shift_id"):
        shift = db.query(Shift).filter(Shift.company_id == company_id, Shift.id == int(data["shift_id"])).first()
        if shift is None:
            raise not_found("Shift")
        if not shift.is_active:
            raise bad_request(f"Shift {shift.name} is inactive.", "ORG_DIMENSION_INACTIVE")
        out["shift_id"] = shift.id
    elif data.get("clear_shift"):
        out["shift_id"] = None  # the employee form cleared the placement shift (BUG-AS-34)
    elif base is not None and "shift_id" not in data:
        # The placement is the source of truth (BUG-AS-34): employees.shift_id is only a display mirror of the
        # resolved shift (possibly a rule's), so it is used only when the placement has none (legacy rows).
        out["shift_id"] = base.shift_id or employee.shift_id
    else:
        out["shift_id"] = employee.shift_id

    # Employee-level holiday calendar (LH 04-holiday/01 §4.2): kept on a continuation only while the branch stays.
    if given("holiday_calendar_id"):
        calendar = _active(db, company_id, "holiday_calendar", data["holiday_calendar_id"])
        if calendar.legal_entity_id is not None and calendar.legal_entity_id != entity_id:
            raise AppError(422, f"Holiday calendar {calendar.code} belongs to another legal entity.",
                           "CALENDAR_ENTITY_MISMATCH")
        out["holiday_calendar_id"] = calendar.id
    elif base is not None and "holiday_calendar_id" not in data and base.branch_id == branch.id:
        out["holiday_calendar_id"] = base.holiday_calendar_id
    else:
        out["holiday_calendar_id"] = None
    if given("work_timezone"):
        import pytz

        if str(data["work_timezone"]) not in pytz.all_timezones_set:
            raise bad_request("Choose a time zone from the IANA list (for example Asia/Kolkata).", "INVALID_TIMEZONE")
        out["work_timezone"] = str(data["work_timezone"])
    elif base is not None and "work_timezone" not in data:
        out["work_timezone"] = base.work_timezone
    else:
        out["work_timezone"] = None

    if given("fte"):
        out["fte"] = _fte(data["fte"])
    elif base is not None and "fte" not in data and base.fte is not None:
        out["fte"] = base.fte
    else:
        out["fte"] = Decimal("1")
    return out


CODE_COLUMNS = (
    ("pay_group", "pay_group_id"), ("grade", "grade_id"), ("employment_type", "employment_type_id"),
    ("designation", "designation_id"), ("cost_center", "cost_center_id"), ("work_week", "work_week_pattern_id"),
)


def dimension_codes_map(db: Session, company_id: int) -> Dict[str, Dict[int, str]]:
    """{kind: {id: code}} of the tenant's dimensions, for placement history views."""
    return {kind: dict(db.query(KINDS[kind][0].id, KINDS[kind][0].code).filter(KINDS[kind][0].company_id == company_id))
            for kind, _ in CODE_COLUMNS}


def ensure_starter_rows(db: Session, company_id: int) -> Dict[str, Any]:
    """Get-or-create the tenant-wide starter rows (MON_FRI, COMPANY, PERMANENT); returns them by kind."""
    week = _by_code(db, WorkWeekPattern, company_id, DEFAULT_WORK_WEEK, active_only=False)
    if week is None:
        week = WorkWeekPattern(company_id=company_id, code=DEFAULT_WORK_WEEK, name="Monday to Friday",
                               weekly_off_days=[5, 6], nth_weekday_offs=[], status="ACTIVE")
        db.add(week)
        db.flush()
        from app.services import holiday_work_weeks

        holiday_work_weeks.ensure_first_version(db, week)
    calendar = _by_code(db, HolidayCalendar, company_id, COMPANY_CALENDAR, active_only=False)
    if calendar is None:
        calendar = HolidayCalendar(company_id=company_id, code=COMPANY_CALENDAR, name="Company holidays", status="ACTIVE")
        db.add(calendar)
    kind = _by_code(db, EmploymentType, company_id, DEFAULT_EMPLOYMENT_TYPE, active_only=False)
    if kind is None:
        kind = EmploymentType(company_id=company_id, code=DEFAULT_EMPLOYMENT_TYPE, name="Permanent",
                              worker_class="EMPLOYEE", status="ACTIVE")
        db.add(kind)
    db.flush()
    return {"work_week": week, "holiday_calendar": calendar, "employment_type": kind}
