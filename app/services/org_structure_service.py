"""Legal entities, branches and effective-dated employee placement (02-multi-level-company).

* A legal entity is the employer: country (ISO 3166-1), base currency (ISO 4217),
  timezone and the identity printed on documents. It is never deleted, only
  deactivated; its country cannot change once it has issued payroll.
* A branch is a work location inside exactly one legal entity.
* An employee's placement is a history of primary assignments. A change starts a new
  row on its effective date and closes the previous one the day before; rows never
  overlap (database trigger). Concurrent assignments are not enabled.
* A placement also carries the payroll dimensions (pay group of its legal entity, grade,
  employment type, designation, cost center, work week, shift, FTE; app.payroll.services.
  org_dimensions); a caller scoped to some entities or branches may move people only into them.
* Termination ends every placement on the last working day: the active row is closed and rows
  that would start later are removed, so nothing stays open after it.
* For backwards compatibility the current primary assignment is copied into the
  legacy columns employees.department_id, employees.shift_id and employees.location
  (= branch name), so existing screens, holiday matching and reports keep working.

Supported here: country/currency metadata for every ISO code. Statutory payroll
rules per country are NOT implemented (no country packs exist yet).
"""

from datetime import date, timedelta
from typing import Dict, List, Optional

import pycountry
import pytz
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.company import Company
from app.models.employee import Department, Employee
from app.models.holiday import Holiday
from app.models.org import ASSIGNMENT_REASONS, Branch, EmployeeAssignment, LegalEntity
from app.models.payroll import Payroll
from app.payroll.services import org_dimensions
from app.services.audit_log_service import AuditLogService

FINALIZED_PAYROLL = ("approved", "processed")
MANUAL_REASONS = tuple(r for r in ASSIGNMENT_REASONS if r not in ("MIGRATION", "HIRE", "TERMINATION", "REHIRE"))


# ---------------------------------------------------------------------------
# ISO catalogues
# ---------------------------------------------------------------------------
def valid_country(code: Optional[str]) -> str:
    code = (code or "").strip().upper()
    if len(code) != 2 or pycountry.countries.get(alpha_2=code) is None:
        raise bad_request("Choose a country (ISO 3166-1 code).", "ORG_COUNTRY_INVALID")
    return code


def valid_currency(code: Optional[str]) -> str:
    code = (code or "").strip().upper()
    if len(code) != 3 or pycountry.currencies.get(alpha_3=code) is None:
        raise bad_request("Choose a currency (ISO 4217 code).", "ORG_CURRENCY_INVALID")
    return code


def valid_subdivision(country: str, code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    code = code.strip().upper()
    sub = pycountry.subdivisions.get(code=code)
    if sub is None or not code.startswith(country + "-"):
        raise bad_request("Choose a state or region of the branch's country (ISO 3166-2).", "ORG_SUBDIVISION_INVALID")
    return code


def valid_timezone(tz: Optional[str]) -> str:
    if not tz or tz not in pytz.all_timezones_set:
        raise bad_request("Choose a valid timezone.", "ORG_TIMEZONE_INVALID")
    return tz


def catalog() -> dict:
    return {
        "countries": sorted(
            ({"code": c.alpha_2, "name": getattr(c, "common_name", None) or c.name} for c in pycountry.countries),
            key=lambda c: c["name"],
        ),
        "currencies": sorted(({"code": c.alpha_3, "name": c.name} for c in pycountry.currencies), key=lambda c: c["code"]),
    }


def subdivisions(country: str) -> List[dict]:
    country = valid_country(country)
    rows = pycountry.subdivisions.get(country_code=country) or []
    return sorted(({"code": s.code, "name": s.name, "type": s.type} for s in rows), key=lambda s: s["name"])


# ---------------------------------------------------------------------------
# Legal entities
# ---------------------------------------------------------------------------
def _address(raw: Optional[dict], country: str) -> dict:
    raw = raw or {}
    allowed = ("line1", "line2", "city", "subdivision_code", "postal_code")
    result = {k: str(raw.get(k) or "").strip()[:150] for k in allowed if raw.get(k)}
    if result.get("subdivision_code"):
        result["subdivision_code"] = valid_subdivision(country, result["subdivision_code"])
    result["country_code"] = country
    return result


def _registration_lines(raw) -> List[dict]:
    lines = []
    for item in (raw or [])[:8]:
        label = " ".join(str(item.get("label") or "").split())[:40]
        value = " ".join(str(item.get("value") or "").split())[:80]
        if label and value:
            lines.append({"label": label, "value": value})
    return lines


def serialize_entity(entity: LegalEntity, counts: Optional[dict] = None) -> dict:
    country = pycountry.countries.get(alpha_2=entity.country_code)
    return {
        "id": entity.id,
        "code": entity.code,
        "legal_name": entity.legal_name,
        "trade_name": entity.trade_name,
        "country_code": entity.country_code,
        "country_name": (getattr(country, "common_name", None) or country.name) if country else entity.country_code,
        "country_confirmed": entity.country_confirmed,
        "registered_address": entity.registered_address or {},
        "registration_lines": entity.registration_lines or [],
        "base_currency": entity.base_currency,
        "fiscal_year_start": entity.fiscal_year_start,
        "timezone": entity.timezone,
        "default_locale": entity.default_locale,
        "signatory_name": entity.signatory_name,
        "signatory_title": entity.signatory_title,
        "is_default": entity.is_default,
        "status": entity.status,
        "employee_count": (counts or {}).get(entity.id, 0),
        # Payroll calculation rules per country are not implemented (configuration only).
        "capability": "CONFIGURATION_ONLY",
    }


def _entity(db: Session, company_id: int, entity_id: int) -> LegalEntity:
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.id == entity_id).first()
    if entity is None:
        raise not_found("Legal entity")
    return entity


def _current_counts(db: Session, company_id: int, column) -> Dict[int, int]:
    today = date.today()
    rows = (
        db.query(column, func.count(EmployeeAssignment.employee_id))
        .join(Employee, (Employee.id == EmployeeAssignment.employee_id) & Employee.is_active.is_(True))
        .filter(EmployeeAssignment.company_id == company_id, _current_filter(today))
        .group_by(column)
        .all()
    )
    return {k: v for k, v in rows}


def list_entities(db: Session, company_id: int) -> List[dict]:
    counts = _current_counts(db, company_id, EmployeeAssignment.legal_entity_id)
    rows = db.query(LegalEntity).filter(LegalEntity.company_id == company_id).order_by(
        LegalEntity.is_default.desc(), LegalEntity.code
    )
    return [serialize_entity(e, counts) for e in rows]


def has_finalized_payroll(db: Session, company_id: int, entity_id: int) -> bool:
    return db.query(Payroll.id).filter(
        Payroll.company_id == company_id, Payroll.legal_entity_id == entity_id, Payroll.status.in_(FINALIZED_PAYROLL)
    ).first() is not None


def save_entity(db: Session, principal: Principal, data: dict, entity_id: Optional[int] = None) -> LegalEntity:
    cid = principal.company_id
    entity = _entity(db, cid, entity_id) if entity_id else None
    code = " ".join(str(data.get("code") or (entity.code if entity else "")).split()).upper()[:20]
    legal_name = " ".join(str(data.get("legal_name") or (entity.legal_name if entity else "")).split())
    if not code or not legal_name or len(legal_name) < 2:
        raise bad_request("Code and legal name are required.", "ORG_ENTITY_INVALID")
    country = valid_country(data.get("country_code") or (entity.country_code if entity else None))
    currency = valid_currency(data.get("base_currency") or (entity.base_currency if entity else None))
    tz = valid_timezone(data.get("timezone") or (entity.timezone if entity else None))
    if entity is not None:
        finalized = has_finalized_payroll(db, cid, entity.id)
        if country != entity.country_code and finalized:
            # EC-ORG-04: law follows the country; create a new entity instead.
            raise conflict("The country cannot change after payroll was approved for this entity. Create a new legal "
                           "entity instead.", "ORG_COUNTRY_LOCKED")
        if currency != entity.base_currency and finalized:
            raise conflict("The base currency cannot change after payroll was approved for this entity "
                           "(redenomination is not supported).", "ORG_CURRENCY_LOCKED")
    else:
        entity = LegalEntity(company_id=cid, status="ACTIVE", is_default=False)
        db.add(entity)
    clash = db.query(LegalEntity.id).filter(LegalEntity.company_id == cid, LegalEntity.code == code)
    if entity.id:
        clash = clash.filter(LegalEntity.id != entity.id)
    if clash.first():
        raise conflict("Another legal entity already uses this code.", "ORG_CODE_TAKEN")
    entity.code = code
    entity.legal_name = legal_name[:200]
    entity.trade_name = (" ".join(str(data.get("trade_name") or "").split()) or None) if "trade_name" in data else entity.trade_name
    entity.country_code = country
    entity.country_confirmed = True if ("country_code" in data or data.get("confirm_country")) else bool(entity.country_confirmed)
    entity.base_currency = currency
    entity.timezone = tz
    if "registered_address" in data:
        entity.registered_address = _address(data.get("registered_address"), country)
    if "registration_lines" in data:
        entity.registration_lines = _registration_lines(data.get("registration_lines"))
    if "fiscal_year_start" in data and data["fiscal_year_start"]:
        value = str(data["fiscal_year_start"])
        try:
            month, day = (int(x) for x in value.split("-"))
            date(2024, month, day)
        except (ValueError, TypeError):
            raise bad_request("Fiscal year start must look like 04-01.", "ORG_ENTITY_INVALID")
        entity.fiscal_year_start = f"{month:02d}-{day:02d}"
    for key, limit in (("default_locale", 35), ("signatory_name", 150), ("signatory_title", 150)):
        if key in data:
            setattr(entity, key, (" ".join(str(data[key] or "").split()) or None) and str(data[key]).strip()[:limit])
    db.flush()
    AuditLogService.log(db, principal, "Organization", "ENTITY_SAVED", f"Saved legal entity {entity.code} ({entity.legal_name})",
                        "legal_entity", entity.id)
    return entity


def set_entity_status(db: Session, principal: Principal, entity_id: int, active: bool) -> LegalEntity:
    entity = _entity(db, principal.company_id, entity_id)
    if not active:
        if entity.is_default:
            raise bad_request("The default legal entity cannot be deactivated.", "ORG_ENTITY_DEFAULT")
        in_use = db.query(EmployeeAssignment.id).filter(
            EmployeeAssignment.company_id == principal.company_id, EmployeeAssignment.legal_entity_id == entity.id,
            _current_filter(date.today()),
        ).first()
        if in_use:
            raise conflict("Move the people placed in this legal entity first.", "ORG_ENTITY_IN_USE")
    entity.status = "ACTIVE" if active else "INACTIVE"
    AuditLogService.log(db, principal, "Organization", "ENTITY_STATUS", f"{entity.code}: {entity.status}", "legal_entity", entity.id)
    return entity


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------
def serialize_branch(branch: Branch, entity: Optional[LegalEntity], counts: Optional[dict] = None) -> dict:
    return {
        "id": branch.id,
        "legal_entity_id": branch.legal_entity_id,
        "legal_entity_code": entity.code if entity else None,
        "code": branch.code,
        "name": branch.name,
        "address": branch.address or {},
        "country_code": branch.country_code,
        "subdivision_code": branch.subdivision_code,
        "timezone": branch.timezone,
        "status": branch.status,
        "holiday_calendar_id": branch.holiday_calendar_id,
        "work_week_pattern_id": branch.work_week_pattern_id,
        "employee_count": (counts or {}).get(branch.id, 0),
    }


def list_branches(db: Session, company_id: int, legal_entity_id: Optional[int] = None) -> List[dict]:
    entities = {e.id: e for e in db.query(LegalEntity).filter(LegalEntity.company_id == company_id)}
    counts = _current_counts(db, company_id, EmployeeAssignment.branch_id)
    query = db.query(Branch).filter(Branch.company_id == company_id)
    if legal_entity_id:
        query = query.filter(Branch.legal_entity_id == legal_entity_id)
    return [serialize_branch(b, entities.get(b.legal_entity_id), counts) for b in query.order_by(Branch.name)]


def _branch(db: Session, company_id: int, branch_id: int) -> Branch:
    branch = db.query(Branch).filter(Branch.company_id == company_id, Branch.id == branch_id).first()
    if branch is None:
        raise not_found("Branch")
    return branch


def save_branch(db: Session, principal: Principal, data: dict, branch_id: Optional[int] = None) -> Branch:
    cid = principal.company_id
    branch = _branch(db, cid, branch_id) if branch_id else None
    entity_id = data.get("legal_entity_id") or (branch.legal_entity_id if branch else None)
    if not entity_id:
        raise bad_request("Choose the legal entity this branch belongs to.", "ORG_BRANCH_INVALID")
    entity = _entity(db, cid, int(entity_id))
    if entity.status != "ACTIVE":
        raise bad_request("The legal entity is inactive.", "ORG_ENTITY_INACTIVE")
    if branch is not None and entity.id != branch.legal_entity_id:
        in_use = db.query(EmployeeAssignment.id).filter(
            EmployeeAssignment.company_id == cid, EmployeeAssignment.branch_id == branch.id
        ).first()
        if in_use:
            # 02 §10: moving a branch creates a new branch record instead of rewriting history.
            raise conflict("A branch with placement history cannot move to another legal entity. Create a new branch.",
                           "ORG_BRANCH_ENTITY_LOCKED")
    name = " ".join(str(data.get("name") or (branch.name if branch else "")).split())[:100]
    code = " ".join(str(data.get("code") or (branch.code if branch else "")).split()).upper()[:20]
    if len(name) < 2 or not code:
        raise bad_request("Branch name and code are required.", "ORG_BRANCH_INVALID")
    country = entity.country_code
    subdivision = valid_subdivision(country, data.get("subdivision_code")) if "subdivision_code" in data else (
        branch.subdivision_code if branch else None)
    tz = valid_timezone(data.get("timezone") or (branch.timezone if branch else entity.timezone))
    created = branch is None
    if created:
        branch = Branch(company_id=cid, status="ACTIVE")
        db.add(branch)
    old_name = branch.name
    branch.legal_entity_id = entity.id
    branch.name, branch.code, branch.country_code = name, code, country
    branch.subdivision_code, branch.timezone = subdivision, tz
    if "address" in data:
        branch.address = _address(data.get("address"), country)
    if created:
        with db.no_autoflush:  # a name clash must surface in the flush below, as a 409
            org_dimensions.apply_branch_defaults(db, branch)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("Another branch already uses this name or code.", "ORG_BRANCH_TAKEN")
    if old_name and old_name != branch.name:
        # Keep the legacy location columns (regional holidays match them by text) in step with the name (D-7).
        db.query(Employee).filter(Employee.company_id == cid, Employee.location == old_name).update(
            {Employee.location: branch.name}, synchronize_session=False
        )
        db.query(Holiday).filter(Holiday.company_id == cid, Holiday.location == old_name).update(
            {Holiday.location: branch.name}, synchronize_session=False
        )
    AuditLogService.log(db, principal, "Organization", "BRANCH_SAVED", f"Saved branch {branch.code} ({branch.name})",
                        "branch", branch.id)
    return branch


def set_branch_status(db: Session, principal: Principal, branch_id: int, active: bool) -> Branch:
    branch = _branch(db, principal.company_id, branch_id)
    if not active and db.query(EmployeeAssignment.id).filter(
        EmployeeAssignment.company_id == principal.company_id, EmployeeAssignment.branch_id == branch.id,
        _current_filter(date.today()),
    ).first():
        raise conflict("Move the people placed in this branch first.", "ORG_BRANCH_IN_USE")
    branch.status = "ACTIVE" if active else "INACTIVE"
    AuditLogService.log(db, principal, "Organization", "BRANCH_STATUS", f"{branch.code}: {branch.status}", "branch", branch.id)
    return branch


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------
def _current_filter(on: date):
    return and_(
        EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.effective_from <= on,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on),
    )


def current_assignment(db: Session, employee_id: int, on: Optional[date] = None) -> Optional[EmployeeAssignment]:
    return (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.employee_id == employee_id, _current_filter(on or date.today()))
        .first()
    )


def current_assignment_select(company_id: int, column: str, ids, on: Optional[date] = None):
    """Employee ids whose primary placement on `on` is in `ids` (for LEGAL_ENTITY / BRANCH scopes)."""
    target = getattr(EmployeeAssignment, column)
    return select(EmployeeAssignment.employee_id).where(
        EmployeeAssignment.company_id == company_id, target.in_(sorted(ids)), _current_filter(on or date.today())
    )


def default_placement(db: Session, company_id: int, location: Optional[str] = None):
    """(entity, branch) for a new employee: the branch named like `location`, else the default entity's first branch."""
    if location:
        branch = db.query(Branch).filter(
            Branch.company_id == company_id, Branch.name == location.strip(), Branch.status == "ACTIVE"
        ).first()
        if branch is not None:
            return branch.legal_entity_id, branch
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.is_default.is_(True)).first()
    if entity is None:
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id).order_by(LegalEntity.id).first()
    if entity is None:
        return None, None
    branch = db.query(Branch).filter(
        Branch.company_id == company_id, Branch.legal_entity_id == entity.id, Branch.status == "ACTIVE"
    ).order_by(Branch.id).first()
    return entity.id, branch


def branch_for_location(db: Session, company_id: int, name: str) -> Branch:
    """The branch a free-text work location means: the branch of that name, else a new one in the
    default legal entity (as the 0005 backfill does), so location and placement never disagree."""
    name = name.strip()[:100]
    branch = db.query(Branch).filter(Branch.company_id == company_id, Branch.name == name).first()
    if branch is not None:
        return branch
    entity_id, _ = default_placement(db, company_id)
    if entity_id is None:
        raise bad_request("Create a legal entity first.", "ORG_NO_ENTITY")
    entity = db.get(LegalEntity, entity_id)
    taken = {code for (code,) in db.query(Branch.code).filter(Branch.company_id == company_id, Branch.legal_entity_id == entity.id)}
    code = next(f"B{n:02d}" for n in range(1, 1000) if f"B{n:02d}" not in taken)
    branch = Branch(company_id=company_id, legal_entity_id=entity.id, code=code, name=name,
                    country_code=entity.country_code, timezone=entity.timezone, status="ACTIVE")
    org_dimensions.apply_branch_defaults(db, branch)
    db.add(branch)
    db.flush()
    return branch


def _guess_country(currency: Optional[str], timezone: Optional[str]) -> str:
    by_currency = {"INR": "IN", "AED": "AE", "USD": "US", "GBP": "GB", "SGD": "SG", "AUD": "AU", "CAD": "CA",
                   "SAR": "SA", "QAR": "QA", "JPY": "JP", "EUR": None}
    by_timezone = {"Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Asia/Dubai": "AE", "Europe/London": "GB",
                   "Asia/Singapore": "SG", "America/New_York": "US", "America/Chicago": "US",
                   "America/Los_Angeles": "US", "America/Denver": "US"}
    return by_currency.get(currency or "") or by_timezone.get(timezone or "") or "IN"


def provision_structure(db: Session, company: Company, country_code: Optional[str] = None) -> tuple:
    """Default legal entity + 'Main' branch for a new company (country unconfirmed unless given)."""
    country = country_code or _guess_country(company.currency, company.timezone)
    entity = LegalEntity(
        company_id=company.id, code="LE01", legal_name=company.name[:200], country_code=country,
        country_confirmed=bool(country_code), base_currency=company.currency or "INR",
        timezone=company.timezone or "UTC", is_default=True, status="ACTIVE",
    )
    db.add(entity)
    db.flush()
    branch = Branch(company_id=company.id, legal_entity_id=entity.id, code="B01", name="Main", country_code=country,
                    timezone=company.timezone or "UTC", status="ACTIVE")
    org_dimensions.apply_branch_defaults(db, branch)  # at signup org_starter links it once the rows exist
    db.add(branch)
    db.flush()
    return entity, branch


def record_hire(db: Session, employee: Employee, user_id: Optional[int], legal_entity_id: Optional[int] = None,
                branch_id: Optional[int] = None, dimensions: Optional[dict] = None) -> Optional[EmployeeAssignment]:
    cid = employee.company_id
    if branch_id:
        branch = _branch(db, cid, branch_id)
        if legal_entity_id and branch.legal_entity_id != legal_entity_id:
            raise bad_request("The branch does not belong to that legal entity.", "ORG_BRANCH_MISMATCH")
        entity_id = branch.legal_entity_id
    elif (employee.location or "").strip() and not legal_entity_id:
        branch = branch_for_location(db, cid, employee.location)
        entity_id = branch.legal_entity_id
    else:
        entity_id, branch = default_placement(db, cid, employee.location)
        if legal_entity_id and legal_entity_id != entity_id:
            entity = _entity(db, cid, legal_entity_id)
            branch = db.query(Branch).filter(Branch.company_id == cid, Branch.legal_entity_id == entity.id,
                                             Branch.status == "ACTIVE").order_by(Branch.id).first()
            entity_id = entity.id
    if branch is None:
        raise bad_request("Create a branch for this legal entity first.", "ORG_NO_BRANCH")
    _assert_active_placement(db, cid, entity_id, branch)
    dims = org_dimensions.placement_dimensions(db, cid, entity_id=entity_id, branch=branch, data=dimensions or {},
                                               base=None, employee=employee)
    assignment = EmployeeAssignment(
        company_id=cid, employee_id=employee.id, legal_entity_id=entity_id, branch_id=branch.id,
        department_id=employee.department_id, is_primary=True, reason="HIRE", effective_from=employee.hire_date,
        created_by_user_id=user_id, **dims,
    )
    db.add(assignment)
    db.flush()
    _sync_legacy(db, employee, assignment)
    return assignment


def _assert_active_placement(db: Session, company_id: int, entity_id: int, branch: Branch) -> None:
    entity = _entity(db, company_id, entity_id)
    if entity.status != "ACTIVE" or branch.status != "ACTIVE":
        raise bad_request("Choose an active legal entity and branch.", "ORG_INACTIVE_TARGET")
    if branch.legal_entity_id != entity.id:
        raise bad_request("The branch does not belong to that legal entity.", "ORG_BRANCH_MISMATCH")


def _assert_destination_allowed(principal: Principal, entity_id: int, branch_id: int,
                                current: Optional[EmployeeAssignment]) -> None:
    """D-8: a caller with entity- or branch-scoped employee:update moves people only into their own targets.

    Company-wide holders move anyone anywhere; staying in the same entity and branch is not a move.
    A destination outside the caller's reach is reported like an unknown branch (404).
    """
    code = "employee:update"
    if principal.is_company_admin or principal.has_company_scope(code):
        return
    if current is not None and (current.legal_entity_id, current.branch_id) == (entity_id, branch_id):
        return
    if entity_id in principal.scope_targets(code, "LEGAL_ENTITY") or branch_id in principal.scope_targets(code, "BRANCH"):
        return
    raise not_found("Branch")


def _sync_legacy(db: Session, employee: Employee, assignment: EmployeeAssignment) -> None:
    """Mirror the placement that is in effect today into the legacy employee columns."""
    today = date.today()
    if assignment.effective_from > today or (assignment.effective_to and assignment.effective_to < today):
        return
    branch = db.get(Branch, assignment.branch_id)
    employee.department_id = assignment.department_id
    employee.location = branch.name if branch else employee.location
    if assignment.shift_id:
        employee.shift_id = assignment.shift_id
    # Flush now: callers such as org_service.assign_manager refresh the employee from the
    # database, which would silently discard these values (regional holidays then miss them).
    db.flush()


def _placement_key(assignment: EmployeeAssignment) -> tuple:
    return (assignment.legal_entity_id, assignment.branch_id, assignment.department_id,
            *(getattr(assignment, f) for f in org_dimensions.PLACEMENT_FIELDS))


def _placement_view(assignment: Optional[EmployeeAssignment]) -> Optional[dict]:
    if assignment is None:
        return None
    keys = ("legal_entity_id", "branch_id", "department_id") + org_dimensions.PLACEMENT_FIELDS
    return {k: (str(getattr(assignment, k)) if k == "fte" else getattr(assignment, k)) for k in keys}


def change_placement(db: Session, principal: Principal, employee: Employee, data: dict) -> EmployeeAssignment:
    """Transfer/promotion/correction from an effective date (02 §13 rules 3, 5, 6)."""
    cid = principal.company_id
    reason = str(data.get("reason") or "TRANSFER").upper()
    if reason not in MANUAL_REASONS:
        raise bad_request(f"Reason must be one of {', '.join(MANUAL_REASONS)}.", "ORG_REASON_INVALID")
    if not employee.is_active:
        raise bad_request("End or rehire the employee instead of changing a closed placement.", "ORG_EMPLOYEE_INACTIVE")
    try:
        effective = date.fromisoformat(str(data.get("effective_from")))
    except ValueError:
        raise bad_request("Choose the date the change takes effect.", "ORG_DATE_INVALID")
    if data.get("is_primary") is False:
        raise bad_request("Concurrent assignments are not enabled for this company.", "ORG_CONCURRENT_DISABLED")
    branch = _branch(db, cid, int(data.get("branch_id") or 0)) if data.get("branch_id") else None
    if branch is None:
        raise bad_request("Choose a branch.", "ORG_BRANCH_REQUIRED")
    entity_id = int(data.get("legal_entity_id") or branch.legal_entity_id)
    _assert_active_placement(db, cid, entity_id, branch)
    department_id = data.get("department_id", employee.department_id)
    if department_id:
        dept = db.query(Department).filter(Department.company_id == cid, Department.id == int(department_id)).first()
        if dept is None:
            raise not_found("Department")
        department_id = dept.id

    current = (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.employee_id == employee.id, EmployeeAssignment.is_primary.is_(True))
        .order_by(EmployeeAssignment.effective_from.desc())
        .with_for_update()
        .first()
    )
    _assert_destination_allowed(principal, entity_id, branch.id, current)
    if current is not None and effective <= current.effective_from:
        raise conflict(
            f"The change must start after the current placement began ({current.effective_from.isoformat()}).",
            "ORG_DATE_OVERLAP",
        )
    if effective < employee.hire_date:
        raise bad_request("The change cannot start before the hire date.", "ORG_DATE_INVALID")
    dims = org_dimensions.placement_dimensions(db, cid, entity_id=entity_id, branch=branch, data=data, base=current,
                                               employee=employee)
    # A finalized pay period is never changed retroactively (retro pay is not implemented). Entity,
    # branch and pay group decide pay context; a department-only change does not touch payroll.
    moves_pay_context = current is None or (current.legal_entity_id, current.branch_id, current.pay_group_id) != (
        entity_id, branch.id, dims["pay_group_id"])
    finalized = db.query(func.max(Payroll.salary_month)).filter(
        Payroll.company_id == cid, Payroll.employee_id == employee.id, Payroll.status.in_(FINALIZED_PAYROLL)
    ).scalar() if moves_pay_context else None
    if finalized is not None:
        last_day = (finalized.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if effective <= last_day:
            raise conflict(
                f"Payroll up to {last_day.isoformat()} is already approved; choose a date after it.",
                "ORG_PERIOD_FINALIZED",
            )
    assignment = EmployeeAssignment(
        company_id=cid, employee_id=employee.id, legal_entity_id=entity_id, branch_id=branch.id,
        department_id=department_id, is_primary=True, reason=reason,
        note=(" ".join(str(data.get("note") or "").split())[:500] or None),
        effective_from=effective, effective_to=None, created_by_user_id=principal.user_id, **dims,
    )
    if current is not None and _placement_key(current) == _placement_key(assignment):
        raise bad_request("The new placement is the same as the current one.", "ORG_NO_CHANGE")
    if current is not None:
        current.effective_to = effective - timedelta(days=1)
        db.flush()
    db.add(assignment)
    db.flush()
    _sync_legacy(db, employee, assignment)
    if current is not None and current.legal_entity_id != entity_id:
        note = " Moving between legal entities ends payroll in the old entity from this date."
    else:
        note = ""
    AuditLogService.log(
        db, principal, "Organization", "PLACEMENT",
        f"{employee.employee_code}: {reason} to branch {branch.code} from {effective.isoformat()}.{note}"[:500],
        "employee", employee.id,
        details={"reason": reason, "effective_from": effective.isoformat(), "assignment_id": assignment.id,
                 "previous_assignment_id": current.id if current is not None else None,
                 "before": _placement_view(current), "after": _placement_view(assignment)},
    )
    return assignment


def close_on_termination(db: Session, employee: Employee, end: date) -> dict:
    """End every placement on the last working day `end` (D-4): close the row active then and remove
    rows that would only start after it, so nothing stays open after the termination."""
    rows = (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.company_id == employee.company_id, EmployeeAssignment.employee_id == employee.id)
        .order_by(EmployeeAssignment.effective_from)
        .with_for_update()
        .all()
    )
    closed, removed = [], []
    for row in rows:
        if row.effective_from > end:
            removed.append(row.id)
            db.delete(row)  # a placement that never started
        elif row.effective_to is None or row.effective_to > end:
            closed.append(row.id)
            row.effective_to = end
    db.flush()
    return {"closed_assignment_ids": closed, "removed_assignment_ids": removed}


def employee_local_today(db: Session, employee: Employee) -> date:
    """Today in the timezone of the legal entity the employee is placed in (D-11)."""
    entity = entity_for_employee_on(db, employee.company_id, employee.id, date.today())
    zone = entity.timezone if entity is not None else None
    if zone is None:
        company = db.get(Company, employee.company_id)
        zone = company.timezone if company is not None else None
    try:
        tz = pytz.timezone(zone or "UTC")
    except pytz.UnknownTimeZoneError:
        tz = pytz.utc
    return utcnow().astimezone(tz).date()


def record_rehire(db: Session, employee: Employee, user_id: Optional[int], start: date,
                  dimensions: Optional[dict] = None) -> Optional[EmployeeAssignment]:
    """Rehire keeps the same employee row and code and opens a new placement (02 §13 rule 7)."""
    last = (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.employee_id == employee.id, EmployeeAssignment.is_primary.is_(True))
        .order_by(EmployeeAssignment.effective_from.desc())
        .first()
    )
    if last is not None and (last.effective_to is None or last.effective_to >= start):
        if last.effective_to is None:
            return last
        start = last.effective_to + timedelta(days=1)
    base = None
    if last is not None:
        branch = db.get(Branch, last.branch_id)
        entity_id = last.legal_entity_id
        if branch is None or branch.status != "ACTIVE":
            entity_id, branch = default_placement(db, employee.company_id, None)
        else:
            base = last
    else:
        entity_id, branch = default_placement(db, employee.company_id, employee.location)
    if branch is None:
        return None
    dims = org_dimensions.placement_dimensions(db, employee.company_id, entity_id=entity_id, branch=branch,
                                               data=dimensions or {}, base=base, employee=employee)
    assignment = EmployeeAssignment(
        company_id=employee.company_id, employee_id=employee.id, legal_entity_id=entity_id, branch_id=branch.id,
        department_id=employee.department_id, is_primary=True, reason="REHIRE", effective_from=start,
        created_by_user_id=user_id, **dims,
    )
    db.add(assignment)
    db.flush()
    _sync_legacy(db, employee, assignment)
    return assignment


def _correct_in_place(db: Session, principal: Principal, employee: Employee, current: EmployeeAssignment,
                      **changes) -> None:
    """Correct a placement that starts today in place (no history to keep yet), with an audit (D-13)."""
    before = _placement_view(current)
    for key, value in changes.items():
        setattr(current, key, value)
    db.flush()
    _sync_legacy(db, employee, current)
    after = _placement_view(current)
    if before != after:
        AuditLogService.log(
            db, principal, "Organization", "PLACEMENT_CORRECTED",
            f"{employee.employee_code}: placement from {current.effective_from.isoformat()} corrected", "employee",
            employee.id, details={"assignment_id": current.id, "before": before, "after": after},
        )


def record_location_change(db: Session, principal: Principal, employee: Employee, location: str) -> None:
    """A work-location edit on the employee form becomes a dated transfer to that branch."""
    branch = branch_for_location(db, employee.company_id, location)
    current = current_assignment(db, employee.id)
    if current is None or current.branch_id == branch.id or not employee.is_active:
        return
    today = date.today()
    if current.effective_from >= today:
        # Same-day correction: the destination is authorized like a transfer (D-8) and the entity-bound
        # dimensions (pay group, cost center) and the branch work week follow the new branch.
        _assert_destination_allowed(principal, branch.legal_entity_id, branch.id, current)
        dims = org_dimensions.placement_dimensions(db, employee.company_id, entity_id=branch.legal_entity_id,
                                                   branch=branch, data={}, base=current, employee=employee)
        _correct_in_place(db, principal, employee, current, branch_id=branch.id,
                          legal_entity_id=branch.legal_entity_id, **dims)
        return
    change_placement(db, principal, employee, {
        "branch_id": branch.id, "legal_entity_id": branch.legal_entity_id, "department_id": current.department_id,
        "effective_from": today.isoformat(), "reason": "TRANSFER", "note": "Work location changed on the employee form",
    })


def record_department_change(db: Session, principal: Principal, employee: Employee, department_id: Optional[int]) -> None:
    """A department edit on the employee form becomes a dated placement change instead of an overwrite."""
    current = current_assignment(db, employee.id)
    if current is None or current.department_id == department_id or not employee.is_active:
        return
    today = date.today()
    if current.effective_from >= today:
        _correct_in_place(db, principal, employee, current, department_id=department_id)
        return
    change_placement(db, principal, employee, {
        "branch_id": current.branch_id, "legal_entity_id": current.legal_entity_id, "department_id": department_id,
        "effective_from": today.isoformat(), "reason": "REORG", "note": "Department changed on the employee form",
    })


def history(db: Session, company_id: int, employee_id: int) -> List[dict]:
    rows = (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id)
        .order_by(EmployeeAssignment.effective_from.desc())
        .all()
    )
    entities = {e.id: e for e in db.query(LegalEntity).filter(LegalEntity.company_id == company_id)}
    branches = {b.id: b for b in db.query(Branch).filter(Branch.company_id == company_id)}
    departments = {d.id: d.name for d in db.query(Department).filter(Department.company_id == company_id)}
    codes = org_dimensions.dimension_codes_map(db, company_id)
    today = date.today()
    return [
        {
            "id": a.id,
            "legal_entity_id": a.legal_entity_id,
            "legal_entity": entities[a.legal_entity_id].legal_name if a.legal_entity_id in entities else None,
            "legal_entity_code": entities[a.legal_entity_id].code if a.legal_entity_id in entities else None,
            "currency": entities[a.legal_entity_id].base_currency if a.legal_entity_id in entities else None,
            "branch_id": a.branch_id,
            "branch": branches[a.branch_id].name if a.branch_id in branches else None,
            "department_id": a.department_id,
            "department": departments.get(a.department_id),
            "reason": a.reason,
            "note": a.note,
            "effective_from": a.effective_from.isoformat(),
            "effective_to": a.effective_to.isoformat() if a.effective_to else None,
            "is_current": a.effective_from <= today and (a.effective_to is None or a.effective_to >= today),
            "is_future": a.effective_from > today,
            **{key: getattr(a, key) for key in org_dimensions.PLACEMENT_FIELDS if key != "fte"},
            "fte": str(a.fte) if a.fte is not None else None,
            **{f"{kind}_code": codes[kind].get(getattr(a, column)) for kind, column in org_dimensions.CODE_COLUMNS},
        }
        for a in rows
    ]


def entity_for_employee_on(db: Session, company_id: int, employee_id: int, on: date) -> Optional[LegalEntity]:
    assignment = (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                _current_filter(on))
        .first()
    )
    if assignment is None:
        assignment = (
            db.query(EmployeeAssignment)
            .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                    EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on)
            .order_by(EmployeeAssignment.effective_from.desc())
            .first()
        )
    if assignment is None:
        return db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.is_default.is_(True)).first()
    return db.get(LegalEntity, assignment.legal_entity_id)


def placement_names(db: Session, company_id: int, employee_id: int, on: date) -> tuple:
    """(department name, branch name) of the employee's placement at the end of a period."""
    from calendar import monthrange

    end = on.replace(day=monthrange(on.year, on.month)[1])
    assignment = (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= end)
        .order_by(EmployeeAssignment.effective_from.desc())
        .first()
    )
    if assignment is None:
        return None, None
    department = db.get(Department, assignment.department_id) if assignment.department_id else None
    branch = db.get(Branch, assignment.branch_id)
    return (department.name if department else None), (branch.name if branch else None)


def sync_all_current(db: Session, company_id: int) -> int:
    """Apply placements that became effective since they were recorded (scheduled job)."""
    changed = 0
    today = date.today()
    for assignment in db.query(EmployeeAssignment).filter(EmployeeAssignment.company_id == company_id, _current_filter(today)):
        employee = db.get(Employee, assignment.employee_id)
        branch = db.get(Branch, assignment.branch_id)
        if employee is None or branch is None:
            continue
        if employee.department_id != assignment.department_id or employee.location != branch.name:
            employee.department_id = assignment.department_id
            employee.location = branch.name
            changed += 1
    db.flush()
    return changed


def repair_missing_structure(db: Session, company: Company) -> Dict[str, int]:
    """Give rows written without the organisation structure what the 0005 backfill gives them: a
    default entity and branch for a company with none, and a primary placement (at the branch named
    like the employee's location) for each employee with none. Such rows come from raw SQL or from
    the previous release running after a code rollback."""
    created = {"entities": 0, "branches": 0, "placements": 0}
    if db.query(LegalEntity.id).filter(LegalEntity.company_id == company.id).first() is None:
        provision_structure(db, company)
        created["entities"] += 1
        created["branches"] += 1
    placed = select(EmployeeAssignment.employee_id).where(EmployeeAssignment.company_id == company.id)
    missing = db.query(Employee).filter(Employee.company_id == company.id, Employee.id.not_in(placed)).order_by(Employee.id).all()
    if not missing:
        return created
    _, default_branch = default_placement(db, company.id)
    branches_before = db.query(func.count(Branch.id)).filter(Branch.company_id == company.id).scalar()
    for employee in missing:
        location = (employee.location or "").strip()[:100]
        if location:
            branch = branch_for_location(db, company.id, location)
        else:
            default_branch = default_branch or branch_for_location(db, company.id, "Main")
            branch = default_branch
        ended = employee.termination_date if employee.termination_date and employee.termination_date >= employee.hire_date else None
        dims = org_dimensions.placement_dimensions(db, company.id, entity_id=branch.legal_entity_id, branch=branch,
                                                   data={}, base=None, employee=employee)
        db.add(EmployeeAssignment(
            company_id=company.id, employee_id=employee.id, legal_entity_id=branch.legal_entity_id, branch_id=branch.id,
            department_id=employee.department_id, is_primary=True, reason="MIGRATION",
            effective_from=employee.hire_date, effective_to=ended, **dims,
        ))
        created["placements"] += 1
    db.flush()
    created["branches"] += db.query(func.count(Branch.id)).filter(Branch.company_id == company.id).scalar() - branches_before
    return created


def tree(db: Session, company_id: int) -> dict:
    company = db.get(Company, company_id)
    entities = list_entities(db, company_id)
    branches = list_branches(db, company_id)
    for entity in entities:
        entity["branches"] = [b for b in branches if b["legal_entity_id"] == entity["id"]]
    departments = (
        db.query(Department).filter(Department.company_id == company_id).order_by(Department.name).all()
    )
    return {
        "company": {"id": company.id, "name": company.name, "currency": company.currency},
        "legal_entities": entities,
        "departments": [
            {"id": d.id, "name": d.name, "parent_department_id": d.parent_department_id, "is_active": d.is_active}
            for d in departments
        ],
        "unconfirmed_entities": [e["code"] for e in entities if not e["country_confirmed"]],
    }


def validate_department_parent(db: Session, company_id: int, department_id: Optional[int], parent_id: Optional[int]) -> Optional[int]:
    """Reject unknown parents and cycles (EC-ORG-10); the database trigger enforces the same rule."""
    if parent_id is None:
        return None
    parent = db.query(Department).filter(Department.company_id == company_id, Department.id == parent_id).first()
    if parent is None:
        raise not_found("Parent department")
    if department_id is None:
        return parent.id
    seen = set()
    current = parent
    while current is not None:
        if current.id == department_id:
            raise bad_request("A department cannot be placed under itself or one of its teams.", "ORG_DEPARTMENT_CYCLE")
        if current.id in seen or len(seen) > 1000:
            break
        seen.add(current.id)
        current = db.get(Department, current.parent_department_id) if current.parent_department_id else None
    return parent.id
