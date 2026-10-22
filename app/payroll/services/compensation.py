"""Compensation assignments: assign, revise, approve, activate, list, bulk import (3.3 §7-§8.1, 3.8 §5.6).

* propose() validates a new salary for an employee placement: a published (ACTIVE) structure version the
  placement is eligible for, the pay basis amount in the pay group currency (another contract currency
  needs an FX rule), overrides only on overridable lines and within their bounds, the balancing
  component >= 0 (otherwise rejected with the minimum pay basis, EC-CMP-04) and the grade range
  (warning, or blocked when payroll.compensation.enforce_grade_range is ON, EC-CMP-23). It is saved as
  DRAFT and, by default, submitted for approval through the approvals engine (module "compensation",
  chain payroll.approval.compensation_chain_id): the maker cannot approve and nobody approves their own
  salary.
* Approval activates it: the assignment in force before it is closed the day before its start, one
  starting on or after it is SUPERSEDED (a later SCHEDULED one stays and bounds it); a future start is
  SCHEDULED until its date (EC-CMP-11), a start inside a pay period is flagged split_period (EC-CMP-16).
  ACTIVE/SCHEDULED windows of one placement never overlap (trigger; 409, EC-CMP-10). A start reaching
  into finalized periods calls arrears_proposals() and marks affected runs stale (EC-CMP-12/13): both
  are no-op hooks until pay runs exist. employees.base_salary follows the current BASIC.
* Lists: with / without salary (the "Without salary" KPI, EC-CMP-14), scheduled, pending, draft; every
  list and count applies the caller's payroll:compensation:read scope (SELF for own records); managers
  see nothing unless granted (contract §25). HR_ONLY lines are hidden from self-service.
* Bulk CSV: a dry run validates every row (cells starting with = + - @ tab or CR are text, never
  formulas, and are echoed escaped) and stores the valid rows; commit queues a background job that
  proposes each row for approval and records a result report.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal, build_principal
from app.core.security import utcnow
from app.models.company import Company
from app.models.compensation import (
    ASSIGNMENT_REASONS,
    IN_FORCE_STATUSES,
    CompensationAssignment,
    CompensationAssignmentLine,
    CompensationImport,
    PayComponent,
    SalaryTemplate,
    SalaryTemplateVersion,
)
from app.models.employee import Employee
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import Designation, EmploymentType, Grade, PayGroup
from app.models.user import User
from app.payroll import audit as payroll_audit
from app.payroll import currencies
from app.payroll import formula as fm
from app.payroll.money import amount_str, decimal_str, is_storable, quantize_amount
from app.payroll.money_errors import MoneyError
from app.payroll.services import access, approvals, jobs
from app.payroll.services import components as comp
from app.payroll.services import structures
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

READ = "payroll:compensation:read"
EDIT = "payroll:compensation:edit"
APPROVE = "payroll:compensation:approve"
MODULE = "compensation"
SUBJECT = "compensation_assignment"
IMPORT_JOB = "compensation.import_commit"
ACTIVATE_JOB = "compensation.activate_scheduled"
GRADE_SETTING = "payroll.compensation.enforce_grade_range"
FILTERS = ("with", "without", "scheduled", "pending", "draft")
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MAX_IMPORT_ROWS = 5000
CSV_COLUMNS = ("employee_code", "structure_code", "pay_basis_amount", "effective_from")
CSV_OPTIONAL = ("currency", "reason", "note")
OVERRIDE_PREFIX = "override:"
ONE_DAY = dt.timedelta(days=1)
FX_SOURCES = ("PERIOD_SNAPSHOT", "FIXED_RATE")

COMPENSATION_DRAFTED = "COMPENSATION_DRAFTED"
COMPENSATION_SUBMITTED = "COMPENSATION_SUBMITTED"
COMPENSATION_APPROVED = "COMPENSATION_APPROVED"
COMPENSATION_REJECTED = "COMPENSATION_REJECTED"
COMPENSATION_WITHDRAWN = "COMPENSATION_WITHDRAWN"
COMPENSATION_ACTIVATED = "COMPENSATION_ACTIVATED"
IMPORT_VALIDATED = "IMPORT_VALIDATED"
IMPORT_COMMITTED = "IMPORT_COMMITTED"


# --------------------------------------------------------------------------- hooks (pay runs)
def finalized_periods(db: Session, company_id: int, employee_id: int, from_date: dt.date) -> list[dict[str, Any]]:
    """Finalized pay periods of the employee from `from_date` on ({run_id, start, end}); none until runs exist."""
    return []


def arrears_proposals(db: Session, principal: Principal | None, assignment: CompensationAssignment,
                      periods: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Arrears proposals for a backdated assignment reaching finalized periods (3.3 §8.3, EC-CMP-12).

    Each finalized period is recalculated from its frozen snapshot with the new compensation only and the
    differences are proposed for approval (app.payroll.services.retro); finalized runs never change.
    """
    from app.payroll.services import retro

    return retro.on_compensation_change(db, principal, assignment, periods)


def mark_runs_stale(db: Session, company_id: int, employee_id: int, from_date: dt.date) -> int:
    """Runs whose locked inputs an approved change affects become stale (EC-CMP-13); no-op until runs exist."""
    return 0


# --------------------------------------------------------------------------- helpers
def _today(db: Session, company_id: int) -> dt.date:
    return comp.today(db, company_id)


def _scope_clause(db: Session, principal: Principal, permission: str, start: dt.date, end: dt.date, column):
    return access.payroll_employee_scope(db, principal, permission, start, end).clause(column)


def _in_scope(db: Session, principal: Principal, permission: str, employee_id: int, start: dt.date,
              end: dt.date) -> bool:
    if not principal.has(permission):
        return False
    return db.query(Employee.id).filter(
        Employee.company_id == principal.company_id, Employee.id == employee_id,
        _scope_clause(db, principal, permission, start, end, Employee.id),
    ).first() is not None


def ensure_read(db: Session, principal: Principal, employee_id: int, start: dt.date, end: dt.date) -> None:
    access.ensure_employee_in_scope(db, principal, READ, employee_id, start, end, what="Employee")


def can_edit_employee(db: Session, principal: Principal, employee_id: int, on: dt.date) -> bool:
    return _in_scope(db, principal, EDIT, employee_id, on, on)


def _ensure_edit(db: Session, principal: Principal, employee_id: int, on: dt.date) -> None:
    access.ensure_employee_in_scope(db, principal, EDIT, employee_id, on, on, what="Employee")


def placement_on(db: Session, company_id: int, employee_id: int, on: dt.date) -> EmployeeAssignment | None:
    return (db.query(EmployeeAssignment)
            .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                    EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
                    or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))
            .first())


def _covers(on: dt.date):
    return and_(CompensationAssignment.effective_from <= on,
                or_(CompensationAssignment.effective_to.is_(None), CompensationAssignment.effective_to >= on))


def in_force_row(db: Session, company_id: int, employee_id: int, on: dt.date) -> CompensationAssignment | None:
    return (db.query(CompensationAssignment)
            .filter(CompensationAssignment.company_id == company_id, CompensationAssignment.employee_id == employee_id,
                    CompensationAssignment.status.in_(IN_FORCE_STATUSES), _covers(on))
            .order_by(CompensationAssignment.effective_from.desc())
            .first())


def in_force(db: Session, principal: Principal, employee_id: int, on: dt.date) -> CompensationAssignment | None:
    """The assignment in force on `on` for an employee the caller may read (404 otherwise)."""
    ensure_read(db, principal, employee_id, on, on)
    return in_force_row(db, principal.company_id, employee_id, on)


def _load(db: Session, company_id: int, assignment_id: int, *, lock: bool = False) -> CompensationAssignment:
    query = db.query(CompensationAssignment).filter(CompensationAssignment.company_id == company_id,
                                                    CompensationAssignment.id == assignment_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Compensation assignment")
    return row


def _visible(db: Session, principal: Principal, row: CompensationAssignment, permission: str = READ) -> bool:
    end = row.effective_to or max(row.effective_from, _today(db, principal.company_id))
    return _in_scope(db, principal, permission, row.employee_id, row.effective_from, max(end, row.effective_from))


def _get_visible(db: Session, principal: Principal, assignment_id: int, permission: str = READ, *,
                 lock: bool = False) -> CompensationAssignment:
    if not principal.has(permission):
        raise forbidden()
    row = _load(db, principal.company_id, assignment_id, lock=lock)
    if not _visible(db, principal, row, permission):
        raise not_found("Compensation assignment")
    return row


def _template(db: Session, version: SalaryTemplateVersion) -> SalaryTemplate:
    return db.query(SalaryTemplate).filter(SalaryTemplate.company_id == version.company_id,
                                           SalaryTemplate.id == version.template_id).one()


def _template_version(db: Session, company_id: int, version_id: int) -> SalaryTemplateVersion:
    version = db.query(SalaryTemplateVersion).filter(SalaryTemplateVersion.company_id == company_id,
                                                     SalaryTemplateVersion.id == version_id).first()
    if version is None:
        raise not_found("Salary structure version")
    return version


def pay_currency(db: Session, placement: EmployeeAssignment) -> str:
    if placement.pay_group_id is not None:
        group = db.get(PayGroup, placement.pay_group_id)
        if group is not None:
            return group.currency
    entity = db.get(LegalEntity, placement.legal_entity_id)
    return entity.base_currency


def overrides_of(db: Session, row: CompensationAssignment) -> dict[str, dict[str, str]]:
    lines = (db.query(CompensationAssignmentLine, PayComponent.code)
             .join(PayComponent, and_(PayComponent.id == CompensationAssignmentLine.component_id,
                                      PayComponent.company_id == CompensationAssignmentLine.company_id))
             .filter(CompensationAssignmentLine.company_id == row.company_id,
                     CompensationAssignmentLine.compensation_assignment_id == row.id)
             .all())
    result: dict[str, dict[str, str]] = {}
    for line, code in lines:
        if line.amount is not None:
            result[code] = {"amount": comp.plain(line.amount)}
        elif line.percent is not None:
            result[code] = {"percent": comp.plain(line.percent)}
        else:
            result[code] = {"formula": line.formula}
    return result


def employee_facts(db: Session, company_id: int, employee_id: int, on: dt.date) -> dict[str, Any]:
    """Formula `emp.*` variables of an employee on a date (the facts a snapshot carries)."""
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    placement = placement_on(db, company_id, employee_id, on)
    facts: dict[str, Any] = {"emp.hire_date": employee.hire_date.isoformat(),
                             "emp.exit_date": employee.termination_date.isoformat() if employee.termination_date else None}
    months = (on.year - employee.hire_date.year) * 12 + on.month - employee.hire_date.month - (
        1 if on.day < employee.hire_date.day else 0)
    facts["emp.months_of_service"] = str(max(months, 0))
    facts["emp.years_of_service"] = str(max(months, 0) // 12)
    if placement is not None:
        entity = db.get(LegalEntity, placement.legal_entity_id)
        branch = db.get(Branch, placement.branch_id)
        facts["emp.country"] = entity.country_code
        facts["emp.subdivision"] = branch.subdivision_code if branch else None
        facts["emp.fte"] = decimal_str(Decimal(placement.fte or 1))
        for key, model, column in (("emp.grade", Grade, placement.grade_id),
                                   ("emp.designation", Designation, placement.designation_id),
                                   ("emp.employment_type", EmploymentType, placement.employment_type_id)):
            if column is not None:
                found = db.get(model, column)
                facts[key] = found.code if found else None
        if placement.employment_type_id is not None:
            found = db.get(EmploymentType, placement.employment_type_id)
            facts["emp.worker_class"] = found.worker_class if found else None
    return facts


def _engine_employee(db: Session, company_id: int, employee_id: int, on: dt.date) -> dict[str, Any]:
    facts = employee_facts(db, company_id, employee_id, on)
    return {"employee_id": employee_id, "hire_date": facts["emp.hire_date"],
            "country": facts.get("emp.country") or "ZZ", "grade": facts.get("emp.grade"),
            "designation": facts.get("emp.designation"),
            "employment_type": facts.get("emp.employment_type") or "PERMANENT",
            "worker_class": facts.get("emp.worker_class") or "EMPLOYEE", "subdivision": facts.get("emp.subdivision"),
            "fte": facts.get("emp.fte") or "1"}


def evaluate_assignment(db: Session, row: CompensationAssignment, *, on: dt.date | None = None,
                        statutory: bool = False) -> structures.Evaluation:
    version = _template_version(db, row.company_id, row.template_version_id)
    template = _template(db, version)
    on = on or max(row.effective_from, _today(db, row.company_id))
    employee = _engine_employee(db, row.company_id, row.employee_id, on)
    return structures.evaluate(
        db, row.company_id, structure_type=template.type, currency=row.currency,
        lines=structures._resolved_lines(db, version), pay_basis_amount=Decimal(row.pay_basis_amount),
        overrides=overrides_of(db, row), on=on, employee=employee, country=employee["country"], statutory=statutory,
        ids={"id": row.id, "structure_id": template.id, "structure_version_id": version.id},
    )


def _money(amount: Decimal | None, currency: str) -> dict[str, str] | None:
    if amount is None:
        return None
    return {"amount": amount_str(quantize_amount(Decimal(amount), currency), currency), "currency": currency}


def _change_pct(before: Decimal | None, after: Decimal | None) -> str | None:
    if before is None or after is None or before == 0:
        return None
    return decimal_str(((after - before) * 100 / before).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- grade range
def annual_equivalent(structure_type: str, amount: Decimal) -> Decimal | None:
    """The annual amount grade ranges are compared with (ranges are annual; hourly/daily/net are not comparable)."""
    if structure_type == "CTC_BASED":
        return amount
    if structure_type == "GROSS_BASED":
        return amount * 12
    return None


def grade_range_check(db: Session, company_id: int, placement: EmployeeAssignment | None, structure_type: str,
                      amount: Decimal, currency: str) -> dict[str, Any]:
    """Position of the salary in the grade's range: {status: IN_RANGE|BELOW|ABOVE|NO_RANGE|NOT_COMPARABLE, ...}."""
    result: dict[str, Any] = {"status": "NO_RANGE", "grade": None, "range": None, "position_pct": None}
    if placement is None or placement.grade_id is None:
        return result
    grade = db.get(Grade, placement.grade_id)
    result["grade"] = {"id": grade.id, "code": grade.code, "name": grade.name}
    band = next((r for r in grade.salary_ranges or [] if r.get("currency") == currency), None)
    if band is None:
        return result
    result["range"] = {"min": band.get("min"), "max": band.get("max"), "currency": currency}
    annual = annual_equivalent(structure_type, amount)
    if annual is None:
        result["status"] = "NOT_COMPARABLE"
        return result
    low = Decimal(band["min"]) if band.get("min") is not None else None
    high = Decimal(band["max"]) if band.get("max") is not None else None
    if low is not None and annual < low:
        result["status"] = "BELOW"
    elif high is not None and annual > high:
        result["status"] = "ABOVE"
    else:
        result["status"] = "IN_RANGE"
    if low is not None and high is not None and high > low:
        result["position_pct"] = decimal_str(((annual - low) * 100 / (high - low)).quantize(Decimal("0.1")))
    return result


def _enforce_grade_range(db: Session, company_id: int, employee_id: int, on: dt.date) -> bool:
    """The rule in force when the change is decided (today), or on its start when that is later."""
    from app.payroll.services import settings as settings_engine

    on = max(on, _today(db, company_id))
    try:
        return settings_engine.resolve(db, company_id, GRADE_SETTING, employee_id=employee_id, on=on).value is True
    except settings_engine.SettingConfigError:
        return True  # an invalid stored value fails closed


# --------------------------------------------------------------------------- validation
def _eligible(db: Session, version: SalaryTemplateVersion, placement: EmployeeAssignment) -> list[str]:
    reasons = []
    rules = version.eligibility or {}
    for key, value, label in (("legal_entity_ids", placement.legal_entity_id, "legal entity"),
                              ("pay_group_ids", placement.pay_group_id, "pay group"),
                              ("grade_ids", placement.grade_id, "grade"),
                              ("employment_type_ids", placement.employment_type_id, "employment type")):
        allowed = rules.get(key) or []
        if allowed and value not in allowed:
            reasons.append(f"the employee's {label} is not eligible for this structure")
    return reasons


def _normalize_overrides(db: Session, company_id: int, template: SalaryTemplate, lines: Sequence[Mapping[str, Any]],
                         raw: Mapping[str, Mapping[str, Any]] | None, currency: str, on: dt.date
                         ) -> dict[str, dict[str, Any]]:
    by_code = {line["component"]: line for line in lines}
    result: dict[str, dict[str, Any]] = {}
    for code, override in (raw or {}).items():
        line = by_code.get(code)
        if line is None:
            raise comp.invalid(f"{code} is not a line of this structure.", "UNKNOWN_OVERRIDE", "overrides")
        if not line["overridable"]:
            raise comp.invalid(f"{code} cannot be overridden in this structure.", "NOT_OVERRIDABLE", "overrides")
        if not isinstance(override, Mapping) or len(override) != 1:
            raise comp.invalid(f"The override of {code} is one of amount, percent or formula.", "INVALID_OVERRIDE",
                               "overrides")
        (key, value), = override.items()
        method = line["method"]
        expected = {"FIXED": "amount", "RATE_X_UNITS": "amount", "PERCENT_OF": "percent", "FORMULA": "formula"}[method]
        if key != expected:
            raise comp.invalid(f"{code} ({method}) is overridden by {expected}.", "INVALID_OVERRIDE", "overrides")
        if key == "formula":
            entries = comp.catalogue(db, company_id, on)
            env = comp.type_env(entries, currency, tables=comp.table_sigs(db, company_id, on), component=code,
                                stage=entries[code].stage if code in entries else 4, structure=list(by_code))
            comp.check_formula(value, env, field_name=f"overrides.{code}")
            result[code] = {"formula": fm.render(fm.parse(value))}
            continue
        number = comp.parse_decimal(value, f"overrides.{code}")
        if key == "amount" and method == "FIXED":
            try:
                amount_str(number, currency)
            except MoneyError as exc:
                raise comp.invalid(f"{code}: {exc.message}", exc.code, "overrides")
        low = None if line["override_min"] is None else Decimal(str(line["override_min"]))
        high = None if line["override_max"] is None else Decimal(str(line["override_max"]))
        if (low is not None and number < low) or (high is not None and number > high):
            raise comp.invalid(f"{code} must stay between {line['override_min'] or '-'} and "
                               f"{line['override_max'] or '-'}.", "OVERRIDE_OUT_OF_BOUNDS", "overrides")
        result[code] = {key: number}
    for line in lines:
        if line["method"] == "FIXED" and line["value"] is None and line["component"] not in result:
            raise comp.invalid(f"{line['component']} needs an amount: the structure leaves it to the assignment.",
                               "MISSING_AMOUNT", "overrides")
    return result


def _fx_rule(raw: Mapping[str, Any] | None, contract: str, pay: str) -> dict[str, Any] | None:
    if contract == pay:
        if raw:
            raise comp.invalid("An FX rule is only for a contract currency other than the pay currency.",
                               "FX_RULE_NOT_NEEDED", "fx_rule")
        return None
    if not raw:
        raise comp.invalid(f"The pay currency is {pay}: a salary in {contract} needs an FX rule (no implicit FX, "
                           "EC-CMP-07).", "FX_RULE_REQUIRED", "fx_rule")
    source = raw.get("source")
    if source not in FX_SOURCES or set(raw) - {"source", "rate", "note"}:
        raise comp.invalid("fx_rule is {source: PERIOD_SNAPSHOT | FIXED_RATE, rate?, note?}.", "INVALID_FX_RULE",
                           "fx_rule")
    rule: dict[str, Any] = {"source": source, "from": contract, "to": pay}
    if source == "FIXED_RATE":
        rate = comp.parse_decimal(raw.get("rate"), "fx_rule.rate")
        if rate <= 0:
            raise comp.invalid("fx_rule.rate must be above zero.", "INVALID_FX_RULE", "fx_rule")
        rule["rate"] = decimal_str(rate)
    if raw.get("note"):
        rule["note"] = str(raw["note"])[:200]
    return rule


def _split_period(db: Session, placement: EmployeeAssignment, start: dt.date) -> bool:
    """Whether `start` falls inside a monthly pay period of the pay group (EC-CMP-16)."""
    group = db.get(PayGroup, placement.pay_group_id) if placement.pay_group_id else None
    if group is not None and group.pay_frequency != "MONTHLY":
        return True  # other frequencies: the run's calendar decides; record it for the split check
    start_day = int(((group.calendar_rules or {}) if group else {}).get("monthly_start_day") or 1)
    return start.day != start_day


def _too_low(db: Session, company_id: int, template: SalaryTemplate, version: SalaryTemplateVersion,
             lines: Sequence[Mapping[str, Any]], amount: Decimal, currency: str, overrides: Mapping[str, Any],
             on: dt.date) -> AppError:
    """EC-CMP-04: the assignment is rejected with the minimum pay basis that keeps the balancing component >= 0."""
    high = Decimal((version.test_range or {}).get("max") or 0)
    high = max(high, amount * 2, Decimal(1))
    minimum = None
    for _ in range(12):
        minimum = structures.minimum_pay_basis(db, company_id, structure_type=template.type, currency=currency,
                                               lines=lines, low=amount, high=high, on=on, overrides=overrides)
        if minimum is not None:
            break
        high *= 4
    detail: dict[str, Any] = {"message": "The balancing component would be negative for this amount."}
    if minimum is not None:
        detail["minimum_pay_basis"] = {"amount": amount_str(minimum, currency), "currency": currency}
        detail["message"] = (f"The balancing component would be negative: the minimum for this structure is "
                             f"{amount_str(minimum, currency)} {currency}.")
    return AppError(400, detail, "BALANCING_NEGATIVE")


def _validate(db: Session, principal: Principal, employee_id: int, *, template_version_id: int,
              pay_basis_amount: Decimal, currency: str | None, effective_from: dt.date, reason: str,
              overrides: Mapping[str, Mapping[str, Any]] | None, fx_rule: Mapping[str, Any] | None,
              effective_to: dt.date | None = None, exclude_id: int | None = None) -> dict[str, Any]:
    cid = principal.company_id
    if reason not in ASSIGNMENT_REASONS or reason == "MIGRATED":
        raise comp.invalid(f"reason is one of {', '.join(r for r in ASSIGNMENT_REASONS if r != 'MIGRATED')}.",
                           "INVALID_REASON", "reason")
    if effective_to is not None and effective_to < effective_from:
        raise comp.invalid("effective_to is before effective_from.", "INVALID_WINDOW", "effective_to")
    employee = db.query(Employee).filter(Employee.company_id == cid, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    placement = placement_on(db, cid, employee_id, effective_from)
    if placement is None:
        raise bad_request("The employee has no placement on that date.", "NO_PLACEMENT")
    version = _template_version(db, cid, template_version_id)
    template = _template(db, version)
    if version.status != "ACTIVE" or template.status != "ACTIVE":
        raise conflict("Only the published version of an active structure can be assigned.", "STRUCTURE_NOT_ACTIVE")
    reasons = _eligible(db, version, placement)
    if reasons:
        raise AppError(400, {"message": "This structure is not available for the employee: " + "; ".join(reasons) + ".",
                             "reasons": reasons}, "NOT_ELIGIBLE")
    pay = pay_currency(db, placement)
    try:
        contract = currencies.require_payroll_currency(currency or template.currency or pay).code
    except MoneyError as exc:
        raise comp.invalid(exc.message, exc.code, "currency")
    if template.currency and template.currency != contract:
        raise bad_request(f"This structure pays in {template.currency}.", "CURRENCY_MISMATCH")
    rule = _fx_rule(fx_rule, contract, pay)
    try:
        amount_str(pay_basis_amount, contract)
    except MoneyError as exc:
        raise comp.invalid(exc.message, exc.code, "pay_basis_amount")
    if pay_basis_amount < 0 or not is_storable(pay_basis_amount):
        raise comp.invalid("The pay basis amount must be zero or more and storable.", "INVALID_AMOUNT",
                           "pay_basis_amount")
    lines = structures._resolved_lines(db, version)
    normalized = _normalize_overrides(db, cid, template, lines, overrides, contract, effective_from)
    employee_json = _engine_employee(db, cid, employee_id, effective_from)
    evaluation = structures.evaluate(db, cid, structure_type=template.type, currency=contract, lines=lines,
                                     pay_basis_amount=pay_basis_amount, overrides=normalized, on=effective_from,
                                     employee=employee_json, country=employee_json["country"])
    balancing = structures._balancing_code(lines)
    if balancing and not structures._balancing_ok(evaluation, balancing):
        raise _too_low(db, cid, template, version, lines, pay_basis_amount, contract, normalized, effective_from)
    if evaluation.blocking:
        issues = [i.to_json() for i in evaluation.blocking]
        raise AppError(400, {"message": issues[0]["message"], "issues": issues}, "STRUCTURE_ERROR")
    grade = grade_range_check(db, cid, placement, template.type, pay_basis_amount, contract)
    warnings = []
    if grade["status"] in ("BELOW", "ABOVE"):
        message = f"The salary is {grade['status'].lower()} the range of grade {grade['grade']['code']}."
        if _enforce_grade_range(db, cid, employee_id, effective_from):
            raise AppError(400, {"message": message + " Grade ranges are enforced.", "grade_range": grade},
                           "OUT_OF_GRADE_RANGE")
        warnings.append({"code": "OUT_OF_GRADE_RANGE", "message": message, "grade_range": grade})
    pending = db.query(CompensationAssignment.id).filter(
        CompensationAssignment.company_id == cid, CompensationAssignment.assignment_id == placement.id,
        CompensationAssignment.status == "PENDING_APPROVAL",
        CompensationAssignment.id != (exclude_id or 0)).first()
    if pending is not None:
        raise conflict("A salary change for this employee is already waiting for approval.", "COMPENSATION_PENDING")
    return {"employee": employee, "placement": placement, "version": version, "template": template,
            "currency": contract, "pay_currency": pay, "fx_rule": rule, "overrides": normalized, "lines": lines,
            "evaluation": evaluation, "grade_range": grade, "warnings": warnings}


# --------------------------------------------------------------------------- serialization
def _structure_ref(db: Session, version_id: int, cache: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    if cache is not None and version_id in cache:
        return cache[version_id]
    version = db.get(SalaryTemplateVersion, version_id)
    template = _template(db, version)
    ref = {"id": template.id, "code": template.code, "name": template.name, "type": template.type,
           "version": version.version, "version_id": version.id, "version_status": version.status}
    if cache is not None:
        cache[version_id] = ref
    return ref


def _status(row: CompensationAssignment, today: dt.date) -> str:
    return "ACTIVE" if row.status == "SCHEDULED" and row.effective_from <= today else row.status


def assignment_json(db: Session, row: CompensationAssignment, *, today: dt.date,
                    cache: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": row.id, "employee_id": row.employee_id, "assignment_id": row.assignment_id,
        "structure": _structure_ref(db, row.template_version_id, cache),
        "pay_basis_amount": _money(row.pay_basis_amount, row.currency), "currency": row.currency,
        "fx_rule": row.fx_rule, "effective_from": row.effective_from, "effective_to": row.effective_to,
        "reason": row.reason, "status": _status(row, today), "note": row.note, "split_period": row.split_period,
        "previous_assignment_id": row.previous_assignment_id, "revision_cycle_id": row.revision_cycle_id,
        "legacy_salary_structure_id": row.legacy_salary_structure_id, "row_version": row.row_version,
        "created_by_user_id": row.created_by_user_id, "approved_by_user_id": row.approved_by_user_id,
        "approved_at": row.approved_at, "created_at": row.created_at,
    }


def _hidden_codes(db: Session, company_id: int, on: dt.date) -> set[str]:
    return {code for code, entry in comp.catalogue(db, company_id, on, include_archived=True).items()
            if entry.version.visibility == "HR_ONLY"}


def _breakdown(db: Session, row: CompensationAssignment, *, self_only: bool, on: dt.date) -> dict[str, Any]:
    version = _template_version(db, row.company_id, row.template_version_id)
    lines = structures._resolved_lines(db, version)
    evaluation = evaluate_assignment(db, row, on=on)
    result = structures.breakdown(db, evaluation, lines)
    if self_only:
        hidden = _hidden_codes(db, row.company_id, on)
        result["lines"] = [line for line in result["lines"] if line["component"] not in hidden]
        result["issues"] = []
    return result


# --------------------------------------------------------------------------- changes
def _audit(db: Session, principal: Principal | None, action: str, description: str, row: CompensationAssignment,
           **details: Any) -> None:
    AuditLogService.log(db, principal, payroll_audit.COMPENSATION, action, description, "compensation_assignment",
                        row.id, company_id=row.company_id, actor_type="user" if principal else "system",
                        details={"employee_id": row.employee_id, **details})


def propose(db: Session, principal: Principal, employee_id: int, *, template_version_id: int,
            pay_basis_amount: Decimal, effective_from: dt.date, reason: str, currency: str | None = None,
            overrides: Mapping[str, Mapping[str, Any]] | None = None, fx_rule: Mapping[str, Any] | None = None,
            note: str | None = None, attachments: Sequence[Mapping[str, Any]] | None = None,
            effective_to: dt.date | None = None, submit: bool = True, revision_cycle_id: int | None = None
            ) -> dict[str, Any]:
    """Assign or revise an employee's salary: saved as DRAFT and (by default) submitted for approval."""
    if not principal.has(EDIT):
        raise forbidden()
    _ensure_edit(db, principal, employee_id, effective_from)
    checked = _validate(db, principal, employee_id, template_version_id=template_version_id,
                        pay_basis_amount=pay_basis_amount, currency=currency, effective_from=effective_from,
                        reason=reason, overrides=overrides, fx_rule=fx_rule, effective_to=effective_to)
    placement = checked["placement"]
    row = CompensationAssignment(
        company_id=principal.company_id, employee_id=employee_id, assignment_id=placement.id,
        template_version_id=template_version_id, pay_basis_amount=pay_basis_amount, currency=checked["currency"],
        fx_rule=checked["fx_rule"], effective_from=effective_from, effective_to=effective_to, reason=reason,
        status="DRAFT", note=(note or None) and note[:1000], attachments=_attachments(attachments),
        split_period=_split_period(db, placement, effective_from), revision_cycle_id=revision_cycle_id,
        row_version=1, created_by_user_id=principal.user_id,
    )
    db.add(row)
    db.flush()
    components = {c.code: c.id for c in db.query(PayComponent).filter(PayComponent.company_id == principal.company_id,
                                                                     PayComponent.code.in_(list(checked["overrides"]) or [""]))}
    for code, override in checked["overrides"].items():
        (key, value), = override.items()
        db.add(CompensationAssignmentLine(company_id=principal.company_id, compensation_assignment_id=row.id,
                                          component_id=components[code], **{key: value}))
    db.flush()
    previous = in_force_row(db, principal.company_id, employee_id, effective_from - ONE_DAY)
    _audit(db, principal, COMPENSATION_DRAFTED, f"Proposed a {reason.lower()} salary for employee #{employee_id} from "
           f"{effective_from.isoformat()}", row, reason=reason, structure=_structure_ref(db, template_version_id),
           pay_basis_amount=_money(pay_basis_amount, row.currency), effective_from=effective_from.isoformat(),
           previous=None if previous is None else {"id": previous.id,
                                                   "pay_basis_amount": _money(previous.pay_basis_amount,
                                                                              previous.currency)},
           grade_range=checked["grade_range"]["status"], warnings=[w["code"] for w in checked["warnings"]])
    if submit:
        submit_assignment(db, principal, row.id)
    result = assignment_json(db, row, today=_today(db, principal.company_id))
    result["warnings"] = checked["warnings"]
    result["grade_range"] = checked["grade_range"]
    return result


def _attachments(raw: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    result = []
    for item in raw or []:
        if not isinstance(item, Mapping) or set(item) - {"document_id", "name"} or "document_id" not in item:
            raise comp.invalid("attachments are [{document_id, name}] of privately stored documents.",
                               "INVALID_ATTACHMENT", "attachments")
        result.append({"document_id": str(item["document_id"])[:100], "name": str(item.get("name") or "")[:200]})
    return result[:20]


def submit_assignment(db: Session, principal: Principal, assignment_id: int) -> dict[str, Any]:
    row = _get_visible(db, principal, assignment_id, EDIT, lock=True)
    if row.status != "DRAFT":
        raise conflict(f"This salary change is {row.status.lower().replace('_', ' ')}.", "NOT_DRAFT")
    placement = db.get(EmployeeAssignment, row.assignment_id)
    pending = db.query(CompensationAssignment.id).filter(
        CompensationAssignment.company_id == row.company_id, CompensationAssignment.assignment_id == row.assignment_id,
        CompensationAssignment.status == "PENDING_APPROVAL").first()
    if pending is not None:
        raise conflict("A salary change for this employee is already waiting for approval.", "COMPENSATION_PENDING")
    row.status = "PENDING_APPROVAL"
    row.submitted_by_user_id = principal.user_id
    row.row_version += 1
    db.flush()
    makers = sorted({u for u in (row.created_by_user_id, principal.user_id) if u is not None})
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT, subject_id=row.id, subject_employee_id=row.employee_id,
        amount=row.pay_basis_amount, currency=row.currency,
        context={"legal_entity_id": placement.legal_entity_id, "pay_group_id": placement.pay_group_id,
                 "maker_user_ids": makers, "effective_from": row.effective_from.isoformat(), "reason": row.reason},
    )
    _audit(db, principal, COMPENSATION_SUBMITTED, f"Submitted the salary change #{row.id} for approval", row,
           approval_request_id=request.id, pending_reason=(request.context or {}).get("pending_reason"))
    return {**assignment_json(db, row, today=_today(db, row.company_id)), "approval_request_id": request.id}


def decide(db: Session, principal: Principal, assignment_id: int, decision: str, remarks: str | None = None
           ) -> dict[str, Any]:
    """Approve or reject the pending request of a salary change (the approvals engine decides who may)."""
    if not principal.has(APPROVE):
        raise forbidden()
    row = _get_visible(db, principal, assignment_id, APPROVE)
    request = approvals.pending_request(db, MODULE, SUBJECT, row.id)
    if request is None:
        raise conflict("This salary change is not waiting for approval.", "NOT_PENDING")
    approvals.decide(db, principal, request.id, decision, remarks)
    db.refresh(row)
    return {**assignment_json(db, row, today=_today(db, row.company_id)), "approval_request_id": request.id}


@approvals.on_decided(MODULE, SUBJECT)
def _on_decided(db: Session, principal: Principal | None, request) -> None:
    row = _load(db, request.company_id, request.subject_id, lock=True)
    if row.status != "PENDING_APPROVAL":
        return
    if request.status == approvals.APPROVED:
        activate(db, principal, row)
    elif request.status == approvals.REJECTED:
        row.status = "REJECTED"
        row.row_version += 1
        db.flush()
        _audit(db, principal, COMPENSATION_REJECTED, f"Rejected the salary change #{row.id}", row,
               approval_request_id=request.id)
    else:
        row.status = "DRAFT"
        row.row_version += 1
        db.flush()
        _audit(db, principal, COMPENSATION_WITHDRAWN, f"Withdrew the salary change #{row.id} from approval", row,
               approval_request_id=request.id)


def _flush(db: Session) -> None:
    """Flush, turning the overlap trigger's refusal into a 409 (EC-CMP-10)."""
    try:
        with db.begin_nested():
            db.flush()
    except DBAPIError as exc:
        if getattr(exc.orig, "pgcode", None) == "23P01":
            raise conflict("Compensation assignments of one placement cannot overlap.", "COMPENSATION_OVERLAP")
        raise


def activate(db: Session, approver: Principal | None, row: CompensationAssignment, *,
             approved_by_user_id: int | None = None) -> CompensationAssignment:
    """Put an approved assignment in force (see the module docstring for the window rules)."""
    cid = row.company_id
    today = _today(db, cid)
    db.query(Employee.id).filter(Employee.company_id == cid, Employee.id == row.employee_id).with_for_update().first()
    others = (db.query(CompensationAssignment)
              .filter(CompensationAssignment.company_id == cid, CompensationAssignment.employee_id == row.employee_id,
                      CompensationAssignment.status.in_(IN_FORCE_STATUSES), CompensationAssignment.id != row.id)
              .order_by(CompensationAssignment.effective_from)
              .with_for_update()
              .all())
    start, end = row.effective_from, row.effective_to
    closed: list[dict[str, Any]] = []
    previous_id = None
    for other in others:
        if not (other.effective_from <= (end or dt.date.max) and start <= (other.effective_to or dt.date.max)):
            continue
        if other.effective_from < start:
            closed.append({"id": other.id, "effective_to": {"before": other.effective_to, "after": start - ONE_DAY}})
            other.effective_to = start - ONE_DAY
            other.row_version += 1
            previous_id = other.id
        elif other.status == "SCHEDULED" and other.effective_from > start and other.effective_from > today:
            end = other.effective_from - ONE_DAY  # a later scheduled change stays and bounds this one
        else:
            closed.append({"id": other.id, "status": {"before": other.status, "after": "SUPERSEDED"}})
            other.status = "SUPERSEDED"
            other.row_version += 1
    _flush(db)
    row.effective_to = end
    row.previous_assignment_id = row.previous_assignment_id or previous_id
    row.status = "SCHEDULED" if start > today else "ACTIVE"
    row.approved_by_user_id = approved_by_user_id or (approver.user_id if approver else None)
    row.approved_at = utcnow()
    row.row_version += 1
    _flush(db)
    periods = finalized_periods(db, cid, row.employee_id, start)
    proposals = arrears_proposals(db, approver, row, periods) if periods else []
    stale = mark_runs_stale(db, cid, row.employee_id, start)
    if row.status == "ACTIVE" and (row.effective_to is None or row.effective_to >= today):
        sync_base_salary(db, row)
    _audit(db, approver, COMPENSATION_APPROVED, f"Approved the salary change #{row.id} ({row.status.lower()} from "
           f"{start.isoformat()})", row, status=row.status, effective_from=start.isoformat(),
           effective_to=row.effective_to.isoformat() if row.effective_to else None, closed=closed,
           split_period=row.split_period, backdated=start < today, finalized_periods=len(periods),
           arrears_proposals=len(proposals), stale_runs=stale)
    return row


def sync_base_salary(db: Session, row: CompensationAssignment) -> None:
    """employees.base_salary mirrors the current assignment's BASIC (backward compatibility, 3.3 §10)."""
    try:
        evaluation = evaluate_assignment(db, row, on=max(row.effective_from, _today(db, row.company_id)))
    except AppError:
        return
    basic = evaluation.rates.get("BASIC")
    employee = db.query(Employee).filter(Employee.company_id == row.company_id, Employee.id == row.employee_id).first()
    if basic is None or employee is None:
        return
    value = basic.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if value >= Decimal("1e10"):
        logger.warning("Employee %s: BASIC %s does not fit employees.base_salary; not synced.", employee.id, value)
        return
    employee.base_salary = value
    db.flush()


def activate_due(db: Session, company_id: int) -> int:
    """SCHEDULED assignments whose start has come become ACTIVE (and sync base_salary)."""
    today = _today(db, company_id)
    rows = (db.query(CompensationAssignment)
            .filter(CompensationAssignment.company_id == company_id, CompensationAssignment.status == "SCHEDULED",
                    CompensationAssignment.effective_from <= today)
            .with_for_update().all())
    for row in rows:
        row.status = "ACTIVE"
        row.row_version += 1
        db.flush()
        if row.effective_to is None or row.effective_to >= today:
            sync_base_salary(db, row)
        _audit(db, None, COMPENSATION_ACTIVATED, f"Salary change #{row.id} came into force", row,
               effective_from=row.effective_from.isoformat())
    return len(rows)


@jobs.register(ACTIVATE_JOB)
def _activate_job(ctx: jobs.JobContext) -> dict:
    return {"activated_count": activate_due(ctx.db, ctx.company_id)}


# --------------------------------------------------------------------------- queries
def _employee_ref(employee: Employee) -> dict[str, Any]:
    return {"id": employee.id, "employee_code": employee.employee_code,
            "name": f"{employee.first_name} {employee.last_name}".strip()}


def _placements_today(db: Session, company_id: int, ids: Sequence[int], today: dt.date) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    rows = (db.query(EmployeeAssignment, LegalEntity, PayGroup)
            .join(LegalEntity, and_(LegalEntity.id == EmployeeAssignment.legal_entity_id,
                                    LegalEntity.company_id == EmployeeAssignment.company_id))
            .outerjoin(PayGroup, and_(PayGroup.id == EmployeeAssignment.pay_group_id,
                                      PayGroup.company_id == EmployeeAssignment.company_id))
            .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(list(ids)),
                    EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= today,
                    or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= today))
            .all())
    return {a.employee_id: {"assignment_id": a.id,
                            "legal_entity": {"id": e.id, "code": e.code, "name": e.legal_name},
                            "pay_group": None if g is None else {"id": g.id, "code": g.code, "name": g.name,
                                                                 "currency": g.currency}}
            for a, e, g in rows}


def _eligible_employees(db: Session, principal: Principal, today: dt.date):
    """Active employees placed today, inside the caller's read scope."""
    cid = principal.company_id
    placed = (db.query(EmployeeAssignment.employee_id)
              .filter(EmployeeAssignment.company_id == cid, EmployeeAssignment.is_primary.is_(True),
                      EmployeeAssignment.effective_from <= today,
                      or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= today)))
    return (db.query(Employee)
            .filter(Employee.company_id == cid, Employee.is_active.is_(True), Employee.hire_date <= today,
                    or_(Employee.termination_date.is_(None), Employee.termination_date >= today),
                    Employee.id.in_(placed), _scope_clause(db, principal, READ, today, today, Employee.id)))


def _filter_query(db: Session, principal: Principal, name: str, today: dt.date):
    cid = principal.company_id
    scoped = _scope_clause(db, principal, READ, today, today, CompensationAssignment.employee_id)
    base = db.query(CompensationAssignment).filter(CompensationAssignment.company_id == cid, scoped)
    if name == "with":
        return base.filter(CompensationAssignment.status.in_(IN_FORCE_STATUSES), _covers(today))
    if name == "scheduled":
        return base.filter(CompensationAssignment.status == "SCHEDULED", CompensationAssignment.effective_from > today)
    if name == "pending":
        return base.filter(CompensationAssignment.status == "PENDING_APPROVAL")
    return base.filter(CompensationAssignment.status == "DRAFT")


def _without_query(db: Session, principal: Principal, today: dt.date):
    paid = (db.query(CompensationAssignment.employee_id)
            .filter(CompensationAssignment.company_id == principal.company_id,
                    CompensationAssignment.status.in_(IN_FORCE_STATUSES), _covers(today)))
    return _eligible_employees(db, principal, today).filter(~Employee.id.in_(paid))


def counts(db: Session, principal: Principal, today: dt.date | None = None) -> dict[str, int]:
    today = today or _today(db, principal.company_id)
    result = {name: _filter_query(db, principal, name, today).count() for name in ("with", "scheduled", "pending",
                                                                                     "draft")}
    result["without"] = _without_query(db, principal, today).count()
    return result


def list_compensation(db: Session, principal: Principal, *, filter_: str = "with", legal_entity_id: int | None = None,
                      search: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    if not principal.has(READ):
        raise forbidden()
    if filter_ not in FILTERS:
        raise comp.invalid(f"filter is one of {', '.join(FILTERS)}.", "INVALID_FILTER", "filter")
    cid = principal.company_id
    today = _today(db, cid)
    totals = counts(db, principal, today)
    if filter_ == "without":
        query = _without_query(db, principal, today)
        if search:
            like = f"%{search.strip().lower()}%"
            query = query.filter(or_(func.lower(Employee.first_name + " " + Employee.last_name).like(like),
                                     func.lower(Employee.employee_code).like(like)))
        employees = query.order_by(Employee.first_name, Employee.id).offset(offset).limit(limit).all()
        places = _placements_today(db, cid, [e.id for e in employees], today)
        items = [{"employee": _employee_ref(e), **places.get(e.id, {}), "compensation": None,
                  "reason": "NO_SALARY", "message": "No active salary: excluded from payroll with this reason, never "
                  "paid zero (EC-CMP-14)."} for e in employees]
        if legal_entity_id is not None:
            items = [i for i in items if (i.get("legal_entity") or {}).get("id") == legal_entity_id]
        return {"filter": filter_, "counts": totals, "total": totals["without"], "items": items}
    query = _filter_query(db, principal, filter_, today).join(
        Employee, and_(Employee.id == CompensationAssignment.employee_id, Employee.company_id == cid))
    if legal_entity_id is not None:
        query = query.join(EmployeeAssignment, and_(EmployeeAssignment.id == CompensationAssignment.assignment_id,
                                                    EmployeeAssignment.company_id == cid)
                           ).filter(EmployeeAssignment.legal_entity_id == legal_entity_id)
    if search:
        like = f"%{search.strip().lower()}%"
        query = query.filter(or_(func.lower(Employee.first_name + " " + Employee.last_name).like(like),
                                 func.lower(Employee.employee_code).like(like)))
    total = query.count()
    rows = query.order_by(Employee.first_name, CompensationAssignment.effective_from.desc()).offset(offset).limit(limit).all()
    employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == cid,
                                                            Employee.id.in_([r.employee_id for r in rows] or [0]))}
    places = _placements_today(db, cid, list(employees), today)
    cache: dict[int, dict[str, Any]] = {}
    items = [{"employee": _employee_ref(employees[r.employee_id]), **places.get(r.employee_id, {}),
              "compensation": assignment_json(db, r, today=today, cache=cache)} for r in rows]
    return {"filter": filter_, "counts": totals, "total": total, "items": items}


def without_salary(db: Session, principal: Principal, on: dt.date | None = None) -> list[int]:
    """Ids of eligible employees in scope without an assignment in force on `on` (pre-check, KPI)."""
    return [e.id for e in _without_query(db, principal, on or _today(db, principal.company_id)).all()]


def employee_compensation(db: Session, principal: Principal, employee_id: int) -> dict[str, Any]:
    """Current package with its breakdown, scheduled and pending changes and the history with diffs."""
    cid = principal.company_id
    today = _today(db, cid)
    employee = db.query(Employee).filter(Employee.company_id == cid, Employee.id == employee_id).first()
    if employee is None or not principal.has(READ):
        raise forbidden() if employee is not None else not_found("Employee")
    if not _in_scope(db, principal, READ, employee_id, min(employee.hire_date, today), today):
        raise not_found("Employee")
    self_only = principal.employee_id == employee_id and not _in_scope_beyond_self(db, principal, employee_id, today)
    rows = (db.query(CompensationAssignment)
            .filter(CompensationAssignment.company_id == cid, CompensationAssignment.employee_id == employee_id)
            .order_by(CompensationAssignment.effective_from.desc(), CompensationAssignment.id.desc())
            .all())
    if self_only:
        rows = [r for r in rows if r.status in ("ACTIVE", "SCHEDULED", "SUPERSEDED")]
    cache: dict[int, dict[str, Any]] = {}
    history = [assignment_json(db, r, today=today, cache=cache) for r in rows]
    in_force_rows = sorted((r for r in rows if r.status in IN_FORCE_STATUSES), key=lambda r: r.effective_from)
    for item in history:
        row = next(r for r in rows if r.id == item["id"])
        earlier = [r for r in in_force_rows if r.effective_from < row.effective_from and r.id != row.id]
        before = earlier[-1] if earlier else None
        item["change"] = None if before is None else {
            "from_assignment_id": before.id, "pay_basis_before": _money(before.pay_basis_amount, before.currency),
            "change_pct": _change_pct(Decimal(before.pay_basis_amount), Decimal(row.pay_basis_amount))
            if before.currency == row.currency else None}
    current = in_force_row(db, cid, employee_id, today)
    result: dict[str, Any] = {
        "employee": _employee_ref(employee), **_placements_today(db, cid, [employee_id], today).get(employee_id, {}),
        "current": None, "breakdown": None, "scheduled": [h for h in history if h["status"] == "SCHEDULED"],
        "pending": [h for h in history if h["status"] in ("PENDING_APPROVAL", "DRAFT")], "history": history,
        "without_salary": current is None, "self_service": self_only,
    }
    if current is not None:
        result["current"] = assignment_json(db, current, today=today, cache=cache)
        result["breakdown"] = _breakdown(db, current, self_only=self_only, on=today)
        placement = placement_on(db, cid, employee_id, today)
        template = db.get(SalaryTemplate, cache[current.template_version_id]["id"])
        if not self_only:
            result["grade_range"] = grade_range_check(db, cid, placement, template.type,
                                                      Decimal(current.pay_basis_amount), current.currency)
    return result


def _in_scope_beyond_self(db: Session, principal: Principal, employee_id: int, today: dt.date) -> bool:
    """Whether a non-SELF grant also reaches the employee (HR viewing their own record sees everything)."""
    others = frozenset(s for s in principal.scopes(READ) if s != Scope.SELF.value)
    if not others:
        return False
    widened = replace(principal, grants={**principal.grants, READ: others})
    return _in_scope(db, widened, READ, employee_id, today, today)


def compare(db: Session, principal: Principal, row: CompensationAssignment | None, *, template_version_id: int,
            pay_basis_amount: Decimal, on: dt.date, overrides: Mapping[str, Any] | None = None,
            employee_id: int | None = None) -> dict[str, Any]:
    """Before / after of a change: every component monthly and annual with the change %, and the grade position."""
    cid = principal.company_id
    employee_id = row.employee_id if row is not None else employee_id
    version = _template_version(db, cid, template_version_id)
    template = _template(db, version)
    lines = structures._resolved_lines(db, version)
    placement = placement_on(db, cid, employee_id, on)
    currency = row.currency if row is not None else (template.currency or (pay_currency(db, placement) if placement
                                                                           else comp.company_currency(db, cid)))
    overrides = dict(overrides if overrides is not None else (overrides_of(db, row) if row is not None else {}))
    employee = _engine_employee(db, cid, employee_id, on)
    after_eval = structures.evaluate(db, cid, structure_type=template.type, currency=currency, lines=lines,
                                     pay_basis_amount=Decimal(pay_basis_amount), overrides={
                                         k: v for k, v in overrides.items() if k in {ln["component"] for ln in lines}},
                                     on=on, employee=employee, country=employee["country"])
    after = structures.breakdown(db, after_eval, lines)
    before = None
    if row is not None:
        before = _breakdown(db, row, self_only=False, on=on)
    codes = list(dict.fromkeys([*(ln["component"] for ln in (before or {}).get("lines", [])),
                                *(ln["component"] for ln in after["lines"])]))

    def amount(data: Mapping[str, Any] | None, code: str) -> Decimal | None:
        line = next((ln for ln in (data or {}).get("lines", []) if ln["component"] == code), None)
        return Decimal(line["monthly"]["amount"]) if line and line["monthly"] else None

    diff = []
    for code in codes:
        old, new = amount(before, code), amount(after, code)
        diff.append({"component": code, "before_monthly": _money(old, currency), "after_monthly": _money(new, currency),
                     "before_annual": _money(old * 12 if old is not None else None, currency),
                     "after_annual": _money(new * 12 if new is not None else None, currency),
                     "change_pct": _change_pct(old, new)})
    return {
        "assignment_id": row.id if row is not None else None, "employee_id": employee_id,
        "structure_before": _structure_ref(db, row.template_version_id) if row is not None else None,
        "structure_after": _structure_ref(db, template_version_id),
        "pay_basis": {"before": _money(row.pay_basis_amount, row.currency) if row is not None else None,
                      "after": _money(Decimal(pay_basis_amount), currency),
                      "change_pct": _change_pct(Decimal(row.pay_basis_amount), Decimal(pay_basis_amount))
                      if row is not None else None},
        "lines": diff, "before": before, "after": after,
        "grade_range": grade_range_check(db, cid, placement, template.type, Decimal(pay_basis_amount), currency),
    }


def preview_change(db: Session, principal: Principal, employee_id: int, *, pay_basis_amount: Decimal,
                   effective_from: dt.date, template_version_id: int | None = None,
                   overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The revision modal's side-by-side before / after (3.3 §8.1)."""
    if not principal.has(EDIT):
        raise forbidden()
    _ensure_edit(db, principal, employee_id, effective_from)
    current = in_force_row(db, principal.company_id, employee_id, effective_from) or in_force_row(
        db, principal.company_id, employee_id, _today(db, principal.company_id))
    if template_version_id is None:
        if current is None:
            raise comp.invalid("Choose a structure: the employee has no salary yet.", "STRUCTURE_REQUIRED",
                               "template_version_id")
        template_version_id = current.template_version_id
    return compare(db, principal, current, template_version_id=template_version_id, pay_basis_amount=pay_basis_amount,
                   on=effective_from, overrides=overrides, employee_id=employee_id)


def get_assignment(db: Session, principal: Principal, assignment_id: int) -> dict[str, Any]:
    row = _get_visible(db, principal, assignment_id)
    today = _today(db, principal.company_id)
    data = assignment_json(db, row, today=today)
    data["overrides"] = overrides_of(db, row)
    request = approvals.latest_request(db, MODULE, SUBJECT, row.id)
    data["approval_request_id"] = request.id if request is not None else None
    return data


# --------------------------------------------------------------------------- segments (snapshot builder)
def segments(db: Session, company_id: int, employee_id: int, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """The compensation segments of [start, end] in date order for the run snapshot (3.5 §8.1): each
    {start, end, assignment_id, compensation} where compensation is the engine dict, or None for days
    without a salary (NO_SALARY, EC-CMP-14). A revision mid-period gives two segments (EC-CMP-16)."""
    rows = (db.query(CompensationAssignment)
            .filter(CompensationAssignment.company_id == company_id, CompensationAssignment.employee_id == employee_id,
                    CompensationAssignment.status.in_(IN_FORCE_STATUSES), CompensationAssignment.effective_from <= end,
                    or_(CompensationAssignment.effective_to.is_(None), CompensationAssignment.effective_to >= start))
            .order_by(CompensationAssignment.effective_from).all())
    result: list[dict[str, Any]] = []
    cursor = start
    for row in rows:
        seg_start = max(start, row.effective_from)
        seg_end = min(end, row.effective_to or end)
        if seg_start > cursor:
            result.append({"start": cursor, "end": seg_start - ONE_DAY, "assignment_id": None, "compensation": None})
        version = _template_version(db, company_id, row.template_version_id)
        template = _template(db, version)
        compensation = structures.compensation_dict(
            template.type, row.currency, structures._resolved_lines(db, version), Decimal(row.pay_basis_amount),
            overrides_of(db, row), ids={"id": row.id, "version": row.row_version, "structure_id": template.id,
                                        "structure_version_id": version.id})
        result.append({"start": seg_start, "end": seg_end, "assignment_id": row.assignment_id,
                       "compensation_assignment_id": row.id, "compensation": compensation})
        cursor = seg_end + ONE_DAY
    if cursor <= end:
        result.append({"start": cursor, "end": end, "assignment_id": None, "compensation": None})
    return result


# --------------------------------------------------------------------------- bulk import
def _injection(value: str) -> bool:
    return bool(value) and value.startswith(FORMULA_PREFIXES)


def safe_cell(value: Any) -> str:
    """A cell as echoed back to a spreadsheet: formula-like text is prefixed with an apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if _injection(text) else text


def _read_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise bad_request("The file is empty.", "IMPORT_EMPTY")
    missing = [c for c in CSV_COLUMNS if c not in header]
    if missing:
        raise bad_request(f"Missing columns: {', '.join(missing)}.", "IMPORT_COLUMNS")
    unknown = [h for h in header if h not in CSV_COLUMNS + CSV_OPTIONAL and not h.startswith(OVERRIDE_PREFIX)]
    if unknown:
        raise bad_request(f"Unknown columns: {', '.join(safe_cell(u) for u in unknown)}.", "IMPORT_COLUMNS")
    rows = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        if len(rows) >= MAX_IMPORT_ROWS:
            raise bad_request(f"At most {MAX_IMPORT_ROWS} rows per file.", "IMPORT_TOO_LARGE")
        rows.append({name: (values[i].strip() if i < len(values) else "") for i, name in enumerate(header)})
    return header, rows


def import_dry_run(db: Session, principal: Principal, text: str, file_name: str | None = None) -> dict[str, Any]:
    """Validate a bulk-assignment CSV row by row and keep the valid rows for commit."""
    if not principal.has(EDIT):
        raise forbidden()
    cid = principal.company_id
    _, records = _read_csv(text)
    structures_by_code = {t.code: t for t in db.query(SalaryTemplate).filter(SalaryTemplate.company_id == cid)}
    results, valid = [], []
    seen: set[int] = set()
    for number, record in enumerate(records, start=2):
        errors: list[str] = []
        warnings: list[str] = []
        echo = {k: safe_cell(v) for k, v in record.items()}
        for name, value in record.items():
            if _injection(value):
                errors.append(f"{name}: the cell starts with {value[0]!r} and is treated as text, never as a formula.")
        employee = db.query(Employee).filter(Employee.company_id == cid,
                                             Employee.employee_code == record["employee_code"]).first()
        start = None
        try:
            start = dt.date.fromisoformat(record["effective_from"])
        except ValueError:
            errors.append("effective_from: use YYYY-MM-DD.")
        if employee is None or (start is not None and not can_edit_employee(db, principal, employee.id, start)):
            errors.append("employee_code: unknown employee.")
        elif employee.id in seen:
            errors.append("employee_code: the employee appears twice in the file.")
        template = structures_by_code.get(record["structure_code"])
        version = None
        if template is not None:
            version = next((v for v in db.query(SalaryTemplateVersion).filter(
                SalaryTemplateVersion.company_id == cid, SalaryTemplateVersion.template_id == template.id,
                SalaryTemplateVersion.status == "ACTIVE")), None)
        if version is None:
            errors.append("structure_code: unknown or unpublished structure.")
        amount = None
        try:
            amount = comp.parse_decimal(record["pay_basis_amount"], "pay_basis_amount")
        except AppError:
            errors.append("pay_basis_amount: a plain decimal number such as 1200000.00.")
        overrides = {}
        for name, value in record.items():
            if name.startswith(OVERRIDE_PREFIX) and value:
                overrides[name[len(OVERRIDE_PREFIX):]] = {"amount": value}
        reason = record.get("reason") or "REVISION"
        if not errors:
            try:
                for key, override in overrides.items():
                    line = next((ln for ln in structures._resolved_lines(db, version) if ln["component"] == key), None)
                    if line is not None and line["method"] == "PERCENT_OF":
                        overrides[key] = {"percent": override["amount"]}
                checked = _validate(db, principal, employee.id, template_version_id=version.id,
                                    pay_basis_amount=amount, currency=record.get("currency") or None,
                                    effective_from=start, reason=reason, overrides=overrides, fx_rule=None)
                warnings.extend(w["message"] for w in checked["warnings"])
            except AppError as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
                errors.append(f"{exc.code}: {detail.get('message')}")
        ok = not errors
        results.append({"row": number, "employee_code": echo.get("employee_code"), "ok": ok, "errors": errors,
                        "warnings": warnings, "values": echo})
        if ok:
            seen.add(employee.id)
            valid.append({"row": number, "employee_id": employee.id, "template_version_id": version.id,
                          "pay_basis_amount": decimal_str(amount), "currency": record.get("currency") or None,
                          "effective_from": start.isoformat(), "reason": reason, "note": record.get("note") or None,
                          "overrides": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in overrides.items()}})
    summary = {"rows": len(results), "valid": len(valid), "invalid": len(results) - len(valid)}
    row = CompensationImport(company_id=cid, status="VALIDATED", file_name=(file_name or None) and file_name[:255],
                             rows=valid, summary=summary, created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, payroll_audit.COMPENSATION, IMPORT_VALIDATED,
                        f"Validated a compensation import ({summary['valid']} of {summary['rows']} rows valid)",
                        "compensation_import", row.id, details=summary)
    return {"import_id": row.id, "summary": summary, "rows": results}


def import_commit(db: Session, principal: Principal, import_id: int) -> dict[str, Any]:
    if not principal.has(EDIT):
        raise forbidden()
    row = (db.query(CompensationImport)
           .filter(CompensationImport.company_id == principal.company_id, CompensationImport.id == import_id)
           .with_for_update().first())
    if row is None or row.created_by_user_id != principal.user_id:
        raise not_found("Compensation import")
    if row.status != "VALIDATED":
        raise conflict(f"This import is already {row.status.lower()}.", "IMPORT_NOT_VALIDATED")
    if not row.rows:
        raise bad_request("No valid rows to commit.", "IMPORT_EMPTY")
    job = jobs.enqueue(db, company_id=principal.company_id, type=IMPORT_JOB, payload={"import_id": row.id},
                       idempotency_key=f"compensation-import-{row.id}", created_by_user_id=principal.user_id,
                       read_permission=EDIT)
    row.status = "QUEUED"
    row.job_id = job.id
    db.flush()
    AuditLogService.log(db, principal, payroll_audit.COMPENSATION, "IMPORT_COMMIT_REQUESTED",
                        f"Queued the commit of compensation import #{row.id}", "compensation_import", row.id,
                        details={"job_id": job.id, "rows": len(row.rows)})
    return {"import_id": row.id, "status": row.status, "job": jobs.serialize(job)}


def get_import(db: Session, principal: Principal, import_id: int) -> dict[str, Any]:
    if not principal.has(EDIT):
        raise forbidden()
    row = db.query(CompensationImport).filter(CompensationImport.company_id == principal.company_id,
                                              CompensationImport.id == import_id).first()
    if row is None or (row.created_by_user_id != principal.user_id and not principal.has_company_scope(EDIT)):
        raise not_found("Compensation import")
    return {"import_id": row.id, "status": row.status, "file_name": row.file_name, "summary": row.summary,
            "result": row.result, "job_id": row.job_id, "created_at": row.created_at}


def _principal_for(db: Session, company_id: int, user_id: int) -> Principal | None:
    user = db.query(User).filter(User.company_id == company_id, User.id == user_id).first()
    if user is None or user.status != "active":
        return None
    return build_principal(db, user, None, db.get(Company, company_id))


@jobs.register(IMPORT_JOB)
def _run_import(ctx: jobs.JobContext) -> dict:
    db = ctx.db
    row = (db.query(CompensationImport)
           .filter(CompensationImport.company_id == ctx.company_id, CompensationImport.id == ctx.payload["import_id"])
           .with_for_update().first())
    if row is None:
        raise jobs.PermanentJobError("The import no longer exists")
    if row.status == "COMMITTED":
        return dict(row.result or {})
    principal = _principal_for(db, ctx.company_id, row.created_by_user_id)
    outcomes = []
    if principal is None or not principal.has(EDIT):
        outcomes = [{"row": item["row"], "ok": False, "error": "The importing user can no longer edit compensation."}
                    for item in row.rows]
    else:
        for index, item in enumerate(row.rows, start=1):
            try:
                with db.begin_nested():
                    created = propose(
                        db, principal, item["employee_id"], template_version_id=item["template_version_id"],
                        pay_basis_amount=Decimal(item["pay_basis_amount"]), currency=item["currency"],
                        effective_from=dt.date.fromisoformat(item["effective_from"]), reason=item["reason"],
                        overrides=item["overrides"], note=item["note"], submit=True)
                outcomes.append({"row": item["row"], "ok": True, "compensation_id": created["id"]})
            except AppError as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
                outcomes.append({"row": item["row"], "ok": False, "error": f"{exc.code}: {detail.get('message')}"})
            if index % 50 == 0:
                ctx.progress(index, len(row.rows))
    created_count = sum(1 for o in outcomes if o["ok"])
    row.result = {"rows": outcomes, "created_count": created_count, "failed_count": len(outcomes) - created_count}
    row.status = "COMMITTED" if created_count or not outcomes else "FAILED"
    db.flush()
    AuditLogService.log(db, principal, payroll_audit.COMPENSATION, IMPORT_COMMITTED,
                        f"Committed compensation import #{row.id}: {created_count} proposed", "compensation_import",
                        row.id, company_id=ctx.company_id, details={k: v for k, v in row.result.items() if k != "rows"})
    return {"import_id": row.id, "created_count": created_count, "failed_count": len(outcomes) - created_count}
