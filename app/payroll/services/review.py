"""Reviewing a calculated pay run: summary, employees, "Why this amount?", variance, stale inputs and replay
(3.5 §5.5, §8.3-§8.4, §10; 3.8 §5.3-§5.4; 3.9 §4 Runs, §5.4).

Every read needs ``payroll:run:read`` in the run's scope (403 without the permission, 404 outside the scope
or another tenant's run). Employee-level reads, counts and aggregates are additionally limited to the
employees inside the caller's employee scope during the run's period (``runs.employee_scope_clause``), so
totals and counts never reveal out-of-scope employees (EC-SEC-05). HR_ONLY lines are shown only to holders
of ``payroll:run:read`` at company scope or at the run's legal entity. Source links of lines and of the day
ledger are reduced to their type when the viewer lacks the permission of the source record (EC-UX-08).

* ``summary``: totals per currency, population reconciliation (eligible = included + excluded, EC-RUN-11),
  statutory liabilities, the frozen run settings, pack versions with their capability label ("FIXTURE —
  not law", "Compliance not verified"), the FX snapshot and the calculation progress.
* ``list_employees`` (paged, search, filters flags / errors / holds / joiners / leavers / variance) and
  ``employee_detail`` (lines with explanation and trace, day-ledger calendar data, findings).
* ``variance``: component × employee change against the previous finalized period frozen in the snapshots;
  ``acknowledge_variance`` (``payroll:run:review``) acknowledges an employee's variance flags, with a comment
  for a net-pay change (3.5 §5.6). ``variance_flags_acknowledged(db, run)`` is the submit guard
  VARIANCE_FLAGS_ACKNOWLEDGED.
* ``stale``: why a run is stale and which employees are affected; the action is a recalculation of them.
* ``replay`` (``payroll:run:read`` and ``payroll:audit:read`` or ``payroll:run:finalize``): recalculates from
  the stored snapshots with the engine and compares ``calc_hash``; it never writes (EC-STA-04).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.models.compensation import PayComponentVersion
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.pay_runs import (
    PayrollException,
    PayrollInputSnapshot,
    PayrollLine,
    PayrollRun,
    PayrollRunEmployee,
)
from app.payroll import engine, packs
from app.payroll import state_machines as sm
from app.payroll.money import CALC_CONTEXT, amount_str
from app.payroll.packs.base import FIXTURE_LABEL, NOT_VERIFIED_LABEL
from app.payroll.services import access, attestations, calculation, runs
from app.services.audit_log_service import AuditLogService

READ = runs.READ
REVIEW = runs.REVIEW
FINALIZE = runs.FINALIZE
CALCULATE = runs.CALCULATE
AUDIT_READ = "payroll:audit:read"
VARIANCE_ACKNOWLEDGED = "VARIANCE_ACKNOWLEDGED"
ACKNOWLEDGE_STATES = (sm.RunStatus.REVIEW_REQUIRED.value, sm.RunStatus.VALIDATION_FAILED.value,
                      sm.RunStatus.REJECTED.value)
EMPLOYEE_FILTERS = ("flags", "errors", "holds", "joiners", "leavers", "variance", "calculated", "excluded")
REPLAY_LIMIT = 500
# Frozen settings shown on the run summary (the values the calculation used, from the snapshots).
SUMMARY_SETTINGS = (
    "payroll.lop.proration_basis", "payroll.attendance.enabled", "payroll.leave.enabled", "payroll.overtime.enabled",
    "payroll.wage_inputs.enabled", "payroll.reimbursements.enabled", "payroll.loans.enabled",
    "payroll.rounding.line_mode", "payroll.rounding.net_pay_mode", "payroll.deductions.min_net_pay_percent",
    "payroll.approval.variance_review_threshold_percent",
)
# Source record type -> the permission that may follow its link (EC-UX-08). Types not listed need
# payroll:inputs:read (fail closed); None = no record behind it (a policy, a pack rule).
SOURCE_PERMISSIONS: Dict[str, Optional[str]] = {
    "compensation_assignment": "payroll:compensation:read",
    "day_ledger": "attendance:read", "attendance": "attendance:read", "on_duty": "attendance:read",
    "attendance_day": "attendance:read",  # JC-AS-13: evaluation lineage of v2 tenants
    "late_mark": "attendance:read", "shift": "attendance:read",
    "leave": "leave:read",
    "holiday": "holiday:read", "work_week": "holiday:read",
    "assignment": "employee:read", "suspension": "employee:read",
    "wage_input": "payroll:inputs:read", "overtime_entry": "payroll:inputs:read", "task": "payroll:inputs:read",
    "task_carry_forward": "payroll:inputs:read", "task_reversal": "payroll:inputs:read",
    "arrears": "payroll:inputs:read", "manual_lop": "payroll:inputs:read", "court_order": "payroll:inputs:read",
    "claim": "payroll:reimbursements:read", "loan": "payroll:loans:read", "advance": "payroll:loans:read",
    "policy": None, "pack_rule": None, "minimum_wage": None,
}
DEFAULT_SOURCE_PERMISSION = "payroll:inputs:read"
SOURCE_LINKS = {
    "compensation_assignment": "/payroll/compensation?assignment_id={id}", "wage_input": "/payroll/inputs?id={id}",
    "overtime_entry": "/payroll/inputs/overtime?id={id}", "task": "/tasks/{id}", "claim": "/payroll/claims/{id}",
    "loan": "/payroll/loans/{id}", "advance": "/payroll/loans/{id}", "leave": "/leave/requests/{id}",
    "attendance": "/attendance?session_id={id}", "arrears": "/payroll/inputs/arrears?id={id}",
}
# Fields of a source that never identify a record (kept when its link is withheld).
NEUTRAL_SOURCE_FIELDS = ("type", "segment", "lop_days", "minutes", "units", "version", "component", "portion")
ZERO = Decimal(0)


def _now():
    return clock.utcnow()


def _money(value: Optional[Decimal], currency: str) -> Optional[dict]:
    return None if value is None else {"amount": amount_str(Decimal(value), currency), "currency": currency}


def _name(employee: Optional[Employee]) -> Optional[str]:
    return f"{employee.first_name} {employee.last_name}" if employee is not None else None


def _visible_rows(db: Session, principal: Principal, run: PayrollRun, permission: str = READ):
    return db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        runs.employee_scope_clause(db, principal, permission, run, PayrollRunEmployee.employee_id))


def sees_hr_only(principal: Principal, run: PayrollRun) -> bool:
    """HR_ONLY lines: payroll:run:read at company scope, or at the run's legal entity."""
    scopes = principal.scopes(READ)
    if Scope.COMPANY.value in scopes:
        return True
    return Scope.LEGAL_ENTITY.value in scopes and run.legal_entity_id in principal.scope_targets(
        READ, Scope.LEGAL_ENTITY.value)


# ---------------------------------------------------------------------------
# Source links (EC-UX-08)
# ---------------------------------------------------------------------------
def visible_source(principal: Principal, source: Mapping[str, Any]) -> dict:
    """The source with its identifiers and link when the viewer may open the record, else only neutral facts."""
    kind = str(source.get("type") or "")
    permission = SOURCE_PERMISSIONS.get(kind, DEFAULT_SOURCE_PERMISSION)
    if permission is None or principal.has(permission):
        shown = dict(source)
        link = SOURCE_LINKS.get(kind)
        if link and source.get("id") is not None:
            shown["link"] = link.format(id=source["id"])
        return shown
    return {**{k: source[k] for k in NEUTRAL_SOURCE_FIELDS if k in source}, "restricted": True}


def sanitize_sources(principal: Principal, value: Any) -> Any:
    """Apply ``visible_source`` to every source reference inside a ledger or trace structure."""
    if isinstance(value, Mapping):
        if "type" in value and ("id" in value or "content_hash" in value) and isinstance(value.get("type"), str) \
                and value["type"] in SOURCE_PERMISSIONS:
            return visible_source(principal, value)
        return {k: sanitize_sources(principal, v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_sources(principal, v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def _scoped_totals(db: Session, principal: Principal, run: PayrollRun) -> Dict[str, dict]:
    figures = [func.sum(getattr(PayrollRunEmployee, name)) for name in calculation.FIGURES]
    found = (_visible_rows(db, principal, run)
             # The employees in the run totals (``calculation.in_totals``): a FULL hold is never paid in this run.
             .filter(PayrollRunEmployee.status == "CALCULATED", PayrollRunEmployee.calc_hash.isnot(None))
             .with_entities(PayrollRunEmployee.currency, func.count(PayrollRunEmployee.id), *figures)
             .group_by(PayrollRunEmployee.currency).order_by(PayrollRunEmployee.currency).all())
    return {currency: {**{name: amount_str(Decimal(value or 0), currency)
                          for name, value in zip(calculation.FIGURES, values)}, "employees": count}
            for currency, count, *values in found}


def _scoped_population(db: Session, principal: Principal, run: PayrollRun) -> dict:
    rows = _visible_rows(db, principal, run).with_entities(
        PayrollRunEmployee.status, PayrollRunEmployee.exclusion_reason, PayrollRunEmployee.hold_type,
        PayrollRunEmployee.calc_hash, PayrollRunEmployee.error).all()
    reasons = Counter(r.exclusion_reason for r in rows if r.status == "EXCLUDED")
    included = sum(1 for r in rows if r.status != "EXCLUDED")
    return {
        "eligible": len(rows), "included": included, "excluded": sum(reasons.values()),
        "on_hold": sum(1 for r in rows if r.hold_type is not None),
        "excluded_by_reason": dict(sorted(reasons.items())),
        "calculated": sum(1 for r in rows if r.status != "EXCLUDED" and r.calc_hash is not None and r.error is None),
        "errors": sum(1 for r in rows if r.status == "ERROR" or (r.status == "ON_HOLD" and r.error)),
        "pending": sum(1 for r in rows if r.status == "PENDING"),
    }


def _liabilities(db: Session, principal: Principal, run: PayrollRun) -> List[dict]:
    sums: Dict[Tuple[str, str, str], Dict[str, Decimal]] = {}
    flags: Dict[Tuple[str, str, str], dict] = {}
    rows = (_visible_rows(db, principal, run)
            .filter(PayrollRunEmployee.status != "EXCLUDED", PayrollRunEmployee.calc_hash.isnot(None))
            .with_entities(PayrollRunEmployee.stage_outputs).all())
    with localcontext(CALC_CONTEXT):
        for (outputs,) in rows:
            for item in ((outputs or {}).get("statutory") or {}).get("liabilities") or []:
                key = (str(item.get("scheme")), str(item.get("payee")), str(item.get("currency")))
                bucket = sums.setdefault(key, {"EMPLOYEE": ZERO, "EMPLOYER": ZERO})
                portion = "EMPLOYER" if item.get("portion") == "EMPLOYER" else "EMPLOYEE"
                bucket[portion] += Decimal(str(item.get("amount") or "0"))
                flags.setdefault(key, {"due_date": item.get("due_date"),
                                       "fixture": bool(((outputs or {}).get("pack") or {}).get("is_fixture"))})
    return [{"scheme": scheme, "payee_authority": payee, "currency": currency,
             "employee_amount": amount_str(v["EMPLOYEE"], currency), "employer_amount": amount_str(v["EMPLOYER"], currency),
             "due_date": flags[(scheme, payee, currency)]["due_date"],
             "fixture": flags[(scheme, payee, currency)]["fixture"],
             **({"label": FIXTURE_LABEL} if flags[(scheme, payee, currency)]["fixture"] else {})}
            for (scheme, payee, currency), v in sorted(sums.items())]


def _frozen_settings(db: Session, principal: Principal, run: PayrollRun) -> dict:
    snapshot_ids = _visible_rows(db, principal, run).filter(
        PayrollRunEmployee.snapshot_id.isnot(None)).with_entities(PayrollRunEmployee.snapshot_id)
    values: Dict[str, set] = {key: set() for key in SUMMARY_SETTINGS}
    for (settings_value,) in (db.query(PayrollInputSnapshot.content["settings"])
                              .filter(PayrollInputSnapshot.company_id == run.company_id,
                                      PayrollInputSnapshot.id.in_(snapshot_ids.scalar_subquery()))
                              .distinct().all()):
        for key in SUMMARY_SETTINGS:
            values[key].add(json.dumps((settings_value or {}).get(key), sort_keys=True))
    result = {}
    for key, seen in values.items():
        if not seen:
            continue
        result[key] = json.loads(next(iter(seen))) if len(seen) == 1 else "VARIES"
    return result


def _pack_view(db: Session, run: PayrollRun) -> dict:
    period = runs.period_of(db, run)
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == run.company_id,
                                          LegalEntity.id == run.legal_entity_id).one()
    status = runs.statutory_status(db, run, period, entity)
    env = run.environment or packs.PRODUCTION
    capability = packs.country_capability(entity.country_code, period.period_end, environment=env)
    versions = []
    labels = set()
    for item in run.pack_version_ids or []:
        entry = dict(item)
        if entry.get("is_fixture"):
            entry["label"] = FIXTURE_LABEL
            labels.add(FIXTURE_LABEL)
        versions.append(entry)
    if not status["verified"]:
        labels.add(NOT_VERIFIED_LABEL)
    attestation = attestations.active_attestation(db, entity.id, (period.period_start, period.period_end))
    return {"country": entity.country_code, "capability": capability.value, "verified": status["verified"],
            "versions": versions, "evaluated": status["packs"], "labels": sorted(labels),
            "attestation_id": attestation.id if attestation is not None else None,
            "country_confirmed": bool(entity.country_confirmed)}


def _fx_view(db: Session, run: PayrollRun) -> Optional[dict]:
    if run.fx_snapshot_id is None:
        return None
    from app.payroll.services import fx as fx_service

    rates = fx_service.snapshot_rates(db, run.fx_snapshot_id)
    return {"snapshot_id": run.fx_snapshot_id,
            "rates": [{"base": r.base, "quote": r.quote, "rate": format(r.rate, "f"), "rate_type": r.rate_type,
                       "source": r.source, "as_of": r.as_of.isoformat() if r.as_of else None} for r in rates]}


def _exception_counts(db: Session, principal: Principal, run: PayrollRun) -> dict:
    scope = runs.employee_scope_clause(db, principal, READ, run, PayrollException.employee_id)
    rows = (db.query(PayrollException.severity, PayrollException.code, func.count(PayrollException.id))
            .filter(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
                    PayrollException.status == "OPEN", or_(PayrollException.employee_id.is_(None), scope))
            .group_by(PayrollException.severity, PayrollException.code).all())
    by_severity: Counter = Counter()
    by_code: Counter = Counter()
    for severity, code, count in rows:
        by_severity[severity] += count
        by_code[code] += count
    return {"open": sum(by_severity.values()), "by_severity": dict(sorted(by_severity.items())),
            "by_code": dict(sorted(by_code.items())),
            "variance_pending": sum(by_code.get(code, 0) for code in calculation.VARIANCE_CODES)}


def summary(db: Session, principal: Principal, run_id: int) -> dict:
    """The run's review summary within the caller's scope (3.8 §5.3 Summary tab)."""
    run = runs.get_run_for(db, principal, run_id, READ)
    population = _scoped_population(db, principal, run)
    return {
        "run": runs.run_json(db, principal, run, detail=False),
        "totals": _scoped_totals(db, principal, run),
        "population": population,
        "reconciles": population["eligible"] == population["included"] + population["excluded"],
        "liabilities": _liabilities(db, principal, run),
        "settings": _frozen_settings(db, principal, run),
        "pack": _pack_view(db, run),
        "fx": _fx_view(db, run),
        "exceptions": _exception_counts(db, principal, run),
        "calculation": {
            "engine_version": run.engine_version, "calculated_at": run.calculated_at.isoformat()
            if run.calculated_at else None, "calculated_by_user_id": run.calculated_by_user_id,
            "progress": calculation.progress(db, run), "stale": run.stale,
            "stale_reasons": list(run.stale_reasons or []),
        },
    }


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
def _open_variance():
    return exists().where(and_(PayrollException.company_id == PayrollRunEmployee.company_id,
                               PayrollException.run_employee_id == PayrollRunEmployee.id,
                               PayrollException.status == "OPEN",
                               PayrollException.code.in_(calculation.VARIANCE_CODES)))


def _filter_clause(name: str):
    if name == "flags":
        return func.jsonb_array_length(PayrollRunEmployee.flags) > 0
    if name == "errors":
        return or_(PayrollRunEmployee.status == "ERROR", PayrollRunEmployee.error.isnot(None))
    if name == "holds":
        return PayrollRunEmployee.hold_type.isnot(None)
    if name == "joiners":
        return PayrollRunEmployee.flags.contains(["NEW_JOINER"])
    if name == "leavers":
        return PayrollRunEmployee.flags.contains(["LEAVER"])
    if name == "variance":
        return _open_variance()
    if name == "calculated":
        return PayrollRunEmployee.status == "CALCULATED"
    if name == "excluded":
        return PayrollRunEmployee.status == "EXCLUDED"
    raise bad_request(f"Unknown filter {name!r}.", "INVALID_FILTER")


def _change(previous: Optional[str], current: Optional[Decimal]) -> Optional[str]:
    if previous is None or current is None:
        return None
    change = calculation.change_percent(Decimal(str(previous)), Decimal(current))
    return None if change is None else format(change, "f")


def list_employees(db: Session, principal: Principal, run_id: int, *, search: Optional[str] = None,
                   filter_name: Optional[str] = None, status: Optional[str] = None, skip: int = 0,
                   limit: int = 50) -> dict:
    """The run's employees in the caller's scope with their totals; counts honour the scope (EC-SEC-05)."""
    run = runs.get_run_for(db, principal, run_id, READ)
    base = _visible_rows(db, principal, run).join(
        Employee, and_(Employee.company_id == PayrollRunEmployee.company_id, Employee.id == PayrollRunEmployee.employee_id))
    text = (search or "").strip()
    if text:
        pattern = f"%{text.replace('%', '').replace('_', '')}%"
        base = base.filter(or_(Employee.first_name.ilike(pattern), Employee.last_name.ilike(pattern),
                               Employee.employee_code.ilike(pattern),
                               (Employee.first_name + " " + Employee.last_name).ilike(pattern)))
    counts = {name: base.filter(_filter_clause(name)).count() for name in EMPLOYEE_FILTERS}
    query = base
    if filter_name:
        query = query.filter(_filter_clause(filter_name))
    if status:
        query = query.filter(PayrollRunEmployee.status == status)
    total = query.count()
    page = query.order_by(Employee.first_name, Employee.last_name, PayrollRunEmployee.id).offset(skip).limit(limit).all()
    ids = [r.id for r in page]
    employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == run.company_id,
                                                            Employee.id.in_([r.employee_id for r in page] or [0]))}
    open_counts: Dict[int, Counter] = {}
    for row_id, severity, code, count in (
            db.query(PayrollException.run_employee_id, PayrollException.severity, PayrollException.code,
                     func.count(PayrollException.id))
            .filter(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
                    PayrollException.run_employee_id.in_(ids or [0]), PayrollException.status == "OPEN")
            .group_by(PayrollException.run_employee_id, PayrollException.severity, PayrollException.code)):
        bucket = open_counts.setdefault(row_id, Counter())
        bucket["open"] += count
        bucket["blocking"] += count if severity == "BLOCKING" else 0
        bucket["variance"] += count if code in calculation.VARIANCE_CODES else 0
    items = []
    for row in page:
        employee = employees.get(row.employee_id)
        previous = (row.stage_outputs or {}).get("previous") or {}
        found = open_counts.get(row.id, Counter())
        items.append({
            "id": row.id, "employee_id": row.employee_id, "employee_code": employee.employee_code if employee else None,
            "employee_name": _name(employee), "status": row.status, "exclusion_reason": row.exclusion_reason,
            "hold_type": row.hold_type, "currency": row.currency,
            **{name: _money(getattr(row, name), row.currency) for name in calculation.FIGURES},
            "previous_net": None if previous.get("net") is None else {"amount": previous["net"],
                                                                       "currency": previous.get("currency") or row.currency},
            "net_change_percent": _change(previous.get("net"), row.net),
            "flags": list(row.flags or []), "error": row.error,
            "exceptions": {"open": found["open"], "blocking": found["blocking"]},
            "variance_pending": found["variance"] > 0, "calculated": row.calc_hash is not None,
            "row_version": row.row_version,
        })
    return {"run_id": run.id, "total": total, "counts": counts, "items": items}


def _component_names(db: Session, company_id: int, lines: Sequence[PayrollLine]) -> Dict[Tuple[str, int], str]:
    """Display names: the line's component version, else the component's latest version."""
    ids = sorted({l.component_id for l in lines if l.component_id})
    names: Dict[Tuple[str, int], str] = {}
    for component_id, version_id, version, name in (
            db.query(PayComponentVersion.component_id, PayComponentVersion.id, PayComponentVersion.version,
                     PayComponentVersion.name)
            .filter(PayComponentVersion.company_id == company_id, PayComponentVersion.component_id.in_(ids or [0]))
            .order_by(PayComponentVersion.component_id, PayComponentVersion.version)):
        names[("version", version_id)] = name
        names[("component", component_id)] = name   # the highest version wins (ordered by version)
    return names


def _line_json(principal: Principal, line: PayrollLine, names: Mapping[Tuple[str, int], str]) -> dict:
    return {
        "id": line.id, "component": line.component_code, "component_id": line.component_id,
        "component_version_id": line.component_version_id, "name": names.get(("version", line.component_version_id)) or names.get(("component", line.component_id))
        or line.component_code,
        "kind": line.kind, "category": line.category, "stage": line.stage,
        "amount": _money(line.amount, line.currency),
        "units": None if line.units is None else format(Decimal(line.units).normalize(), "f"),
        "rate": None if line.rate is None else format(Decimal(line.rate).normalize(), "f"),
        "period_ref": line.period_ref, "segment": line.segment, "tags": list(line.tags or []),
        "visibility": line.visibility, "explanation": line.explanation,
        "trace": sanitize_sources(principal, dict(line.trace or {})),
        "sources": [visible_source(principal, s) for s in line.sources or []],
        "sort_order": line.sort_order, "statutory": line.statutory, "fixture": line.fixture,
        **({"label": FIXTURE_LABEL} if line.fixture else {}),
    }


def employee_detail(db: Session, principal: Principal, run_id: int, run_employee_id: int) -> dict:
    """One employee of the run: lines with "Why this amount?", day-ledger calendar data and findings (3.8 §5.4)."""
    run = runs.get_run_for(db, principal, run_id, READ)
    row = runs.get_run_employee_for(db, principal, run, run_employee_id, READ)
    employee = db.query(Employee).filter(Employee.company_id == run.company_id, Employee.id == row.employee_id).one()
    hr = sees_hr_only(principal, run)
    lines = (db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id,
                                          PayrollLine.run_employee_id == row.id)
             .order_by(PayrollLine.stage, PayrollLine.sort_order, PayrollLine.id).all())
    shown = [line for line in lines if hr or line.visibility != "HR_ONLY"]
    names = _component_names(db, run.company_id, shown)
    findings = (db.query(PayrollException).filter(PayrollException.company_id == run.company_id,
                                                  PayrollException.run_id == run.id,
                                                  PayrollException.run_employee_id == row.id)
                .order_by(PayrollException.status, PayrollException.id).all())
    outputs = dict(row.stage_outputs or {})
    snapshot = db.query(PayrollInputSnapshot.id, PayrollInputSnapshot.version, PayrollInputSnapshot.content_hash).filter(
        PayrollInputSnapshot.company_id == run.company_id, PayrollInputSnapshot.id == row.snapshot_id).first() \
        if row.snapshot_id else None
    previous = outputs.get("previous") or {}
    if not hr:
        hidden = set(outputs.get("hr_only") or [])
        previous = {**previous, "components": {k: v for k, v in (previous.get("components") or {}).items()
                                               if k not in hidden}}
    data = {
        "run": {"id": run.id, "number": run.number, "status": run.status, "currency": run.currency},
        "id": row.id, "employee": {"id": employee.id, "code": employee.employee_code, "name": _name(employee),
                                   "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
                                   "termination_date": employee.termination_date.isoformat()
                                   if employee.termination_date else None},
        "employee_assignment_id": row.employee_assignment_id, "status": row.status,
        "exclusion_reason": row.exclusion_reason, "exclusion_note": row.exclusion_note, "hold_type": row.hold_type,
        "hold": ({k: v for k, v in (row.hold or {}).items() if k != "status_before"} if row.hold else None),
        "error": row.error, "currency": row.currency,
        "totals": {name: _money(getattr(row, name), row.currency) for name in calculation.FIGURES},
        "previous": previous, "net_change_percent": _change(previous.get("net"), row.net),
        "lines": [_line_json(principal, line, names) for line in shown],
        "hidden_lines": len(lines) - len(shown),
        "ledger": sanitize_sources(principal, outputs.get("ledger")) if outputs.get("ledger") else None,
        "flags": list(row.flags or []),
        "exceptions": [runs.restrict_hr_only(runs.exception_json(f), () if hr else outputs.get("hr_only") or ())
                       for f in findings],
        "snapshot": None if snapshot is None else {"id": snapshot.id, "version": snapshot.version,
                                                   "content_hash": snapshot.content_hash},
        "calc_hash": row.calc_hash, "content_hash": row.content_hash,
        "engine_version": outputs.get("engine_version"),
        "calculated_at": row.calculated_at.isoformat() if row.calculated_at else None,
        "row_version": row.row_version,
    }
    if principal.has("payroll:bank_details:read_masked"):
        data["payments"] = [{k: v for k, v in p.items() if k != "account_id"} | {"account_ref": p.get("account_id")}
                            for p in outputs.get("payments") or []]
    return data


# ---------------------------------------------------------------------------
# Variance (3.5 §5.5, §10)
# ---------------------------------------------------------------------------
def _variance_findings(db: Session, run: PayrollRun, ids: Sequence[int]) -> Dict[int, List[PayrollException]]:
    found: Dict[int, List[PayrollException]] = {}
    for item in (db.query(PayrollException)
                 .filter(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
                         PayrollException.run_employee_id.in_(list(ids) or [0]),
                         PayrollException.code.in_(calculation.VARIANCE_CODES),
                         PayrollException.status.in_(("OPEN", "WAIVED")))
                 .order_by(PayrollException.id)):
        found.setdefault(item.run_employee_id, []).append(item)
    return found


def _entry(previous: Optional[Decimal], current: Decimal, currency: str, threshold: Optional[Decimal]) -> dict:
    change = calculation.change_percent(previous, current)
    with localcontext(CALC_CONTEXT):
        delta = None if previous is None else current - previous
    return {"previous": None if previous is None else amount_str(previous, currency),
            "current": amount_str(current, currency), "change": None if delta is None else amount_str(delta, currency),
            "change_percent": None if change is None else format(change, "f"),
            "flagged": bool(change is not None and threshold is not None and change > threshold and delta != 0)}


def variance(db: Session, principal: Principal, run_id: int, *, only_flagged: bool = False, sort: str = "percent",
             skip: int = 0, limit: int = 50) -> dict:
    """Component × employee change against the previous finalized period (frozen in each snapshot)."""
    run = runs.get_run_for(db, principal, run_id, READ)
    hr = sees_hr_only(principal, run)
    rows = (_visible_rows(db, principal, run)
            .filter(PayrollRunEmployee.status != "EXCLUDED", PayrollRunEmployee.calc_hash.isnot(None))
            .order_by(PayrollRunEmployee.id).all())
    ids = [r.id for r in rows]
    current: Dict[int, Dict[str, Decimal]] = {}
    hidden_lines: Dict[int, set] = {}
    for row_id, code, kind, visibility, total in (
            db.query(PayrollLine.run_employee_id, PayrollLine.component_code, PayrollLine.kind, PayrollLine.visibility,
                     func.sum(PayrollLine.amount))
            .filter(PayrollLine.company_id == run.company_id, PayrollLine.run_employee_id.in_(ids or [0]),
                    PayrollLine.kind.in_(calculation.VARIANCE_KINDS))
            .group_by(PayrollLine.run_employee_id, PayrollLine.component_code, PayrollLine.kind,
                      PayrollLine.visibility)):
        if visibility == "HR_ONLY":
            hidden_lines.setdefault(row_id, set()).add(code)
        bucket = current.setdefault(row_id, {})
        bucket[code] = bucket.get(code, ZERO) + Decimal(total)
    findings = _variance_findings(db, run, ids)
    employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == run.company_id,
                                                            Employee.id.in_([r.employee_id for r in rows] or [0]))}
    items = []
    for row in rows:
        outputs = row.stage_outputs or {}
        previous = outputs.get("previous") or {}
        threshold = outputs.get("variance_threshold_percent")
        threshold = None if threshold is None else Decimal(str(threshold))
        hidden = set() if hr else set(outputs.get("hr_only") or []) | hidden_lines.get(row.id, set())
        before = {k: Decimal(str(v)) for k, v in (previous.get("components") or {}).items()}
        now = current.get(row.id, {})
        components = []
        for code in sorted((set(before) | set(now)) - hidden):
            entry = _entry(before.get(code, ZERO) if previous.get("period_ref") else None, now.get(code, ZERO),
                           row.currency, threshold)
            components.append({"component": code, **entry})
        net_prev = Decimal(str(previous["net"])) if previous.get("net") is not None else None
        net = _entry(net_prev, Decimal(row.net or 0), row.currency, threshold)
        own = findings.get(row.id, [])
        open_codes = sorted({f.code for f in own if f.status == "OPEN"})
        acknowledged = [f for f in own if f.status == "WAIVED"]
        flagged = bool(net["flagged"] or any(c["flagged"] for c in components) or own)
        employee = employees.get(row.employee_id)
        items.append({
            "run_employee_id": row.id, "employee_id": row.employee_id,
            "employee_code": employee.employee_code if employee else None, "employee_name": _name(employee),
            "currency": row.currency, "previous_period": previous.get("period_ref"),
            "threshold_percent": None if threshold is None else format(threshold, "f"),
            "net": net, "components": components, "flagged": flagged,
            "acknowledged": flagged and not open_codes and bool(own),
            "pending": open_codes, "comment_required": any(c in calculation.COMMENT_REQUIRED_CODES for c in open_codes),
            "acknowledgments": [{"code": f.code, "by_user_id": f.resolved_by_user_id,
                                 "at": f.resolved_at.isoformat() if f.resolved_at else None,
                                 "comment": f.resolution_note} for f in acknowledged],
        })
    if only_flagged:
        items = [i for i in items if i["flagged"]]

    def magnitude(item: dict) -> Decimal:
        key = "change_percent" if sort == "percent" else "change"
        value = item["net"].get(key)
        return abs(Decimal(value)) if value is not None else Decimal(-1)

    if sort in ("percent", "absolute"):
        items.sort(key=lambda i: (-magnitude(i), i["run_employee_id"]))
    total = len(items)
    flagged_count = sum(1 for i in items if i["flagged"])
    pending = sum(1 for i in items if i["pending"])
    return {"run_id": run.id, "total": total, "flagged": flagged_count, "pending": pending,
            "items": items[skip:skip + limit]}


def unacknowledged_variance(db: Session, run: PayrollRun) -> int:
    """OPEN variance findings of employees the run will pay (not excluded, not on a full hold); the submit
    guard VARIANCE_FLAGS_ACKNOWLEDGED counts them (``run_approvals`` calls ``unacknowledged_count``)."""
    return (db.query(func.count(PayrollException.id))
            .join(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollException.company_id,
                                           PayrollRunEmployee.id == PayrollException.run_employee_id))
            .filter(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
                    PayrollException.status == "OPEN", PayrollException.code.in_(calculation.VARIANCE_CODES),
                    PayrollRunEmployee.status.notin_(("EXCLUDED", "ON_HOLD")))
            .scalar())


unacknowledged_count = unacknowledged_variance


def variance_flags_acknowledged(db: Session, run: PayrollRun) -> bool:
    """Guard VARIANCE_FLAGS_ACKNOWLEDGED of SUBMIT_FOR_APPROVAL (3.5 §5.6)."""
    return unacknowledged_variance(db, run) == 0


def acknowledge_variance(db: Session, principal: Principal, run_id: int, run_employee_id: int, *,
                         comment: Optional[str] = None, expected_version: Optional[int] = None) -> dict:
    """Acknowledge an employee's variance flags; a net-pay change needs a comment (3.5 §5.6)."""
    run = runs.get_run_for(db, principal, run_id, REVIEW, lock=True)
    runs.ensure_editable(run)
    if run.status not in ACKNOWLEDGE_STATES:
        raise conflict(f"Variance is acknowledged during review; the run is {run.status}.", "RUN_BUSY")
    row = runs.get_run_employee_for(db, principal, run, run_employee_id, REVIEW, lock=True)
    if principal.employee_id is not None and principal.employee_id == row.employee_id:
        raise forbidden("You cannot acknowledge a change in your own pay.")
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The employee line was changed by someone else.", "ROW_VERSION_CONFLICT")
    pending = (db.query(PayrollException)
               .filter(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
                       PayrollException.run_employee_id == row.id, PayrollException.status == "OPEN",
                       PayrollException.code.in_(calculation.VARIANCE_CODES))
               .order_by(PayrollException.id).with_for_update().populate_existing().all())
    if not pending:
        raise conflict("This employee has no variance flag to acknowledge.", "NOTHING_TO_ACKNOWLEDGE")
    note = (comment or "").strip() or None
    if note is None and any(p.code in calculation.COMMENT_REQUIRED_CODES for p in pending):
        raise bad_request("A net-pay change above the review threshold needs a comment.", "COMMENT_REQUIRED")
    now = _now()
    for item in pending:
        item.status, item.resolved_at = "WAIVED", now
        item.resolved_by_user_id, item.resolution_note = principal.user_id, (note or "Acknowledged.")[:1000]
        item.row_version += 1
    db.flush()
    codes = sorted({p.code for p in pending})
    AuditLogService.log(db, principal, runs.AUDIT_MODULE, VARIANCE_ACKNOWLEDGED,
                        f"Acknowledged {', '.join(codes)} for employee #{row.employee_id} in run {run.number}",
                        "payroll_run_employee", row.id, company_id=run.company_id, actor_type="user",
                        details={"run_id": run.id, "employee_id": row.employee_id, "codes": codes,
                                 "exception_ids": [p.id for p in pending], "comment": note})
    return {"run_employee_id": row.id, "acknowledged": [p.id for p in pending], "codes": codes,
            "remaining": unacknowledged_variance(db, run), "row_version": row.row_version}


# ---------------------------------------------------------------------------
# Stale inputs (EC-RUN-09, EC-UX-06)
# ---------------------------------------------------------------------------
def _reason_employee_ids(reasons: Iterable[Mapping[str, Any]]) -> Tuple[set, bool]:
    ids: set = set()
    broad = False
    for reason in reasons:
        if reason.get("employee_id") is not None:
            ids.add(reason["employee_id"])
        elif reason.get("employee_ids"):
            ids.update(reason["employee_ids"])
        else:
            broad = True
    return ids, broad


def _changed_sources(db: Session, run: PayrollRun) -> Dict[int, List[dict]]:
    """Per employee, the sources that changed since lock (``change_detection.detect_changes``: employees
    named by the source change log are rebuilt without writing, and a changed country pack reaches all)."""
    from app.payroll.services import change_detection

    return {int(k): list(v) for k, v in dict(change_detection.detect_changes(db, run) or {}).items()}


def stale(db: Session, principal: Principal, run_id: int) -> dict:
    """Why the run is stale and which employees (in the caller's scope) changed since their inputs were locked."""
    run = runs.get_run_for(db, principal, run_id, READ)
    reasons = list(run.stale_reasons or [])
    ids, broad = _reason_employee_ids(reasons)
    changes = _changed_sources(db, run) if run.status in runs.LOCKED_STATES else {}
    ids |= set(changes)
    rows = _visible_rows(db, principal, run).filter(PayrollRunEmployee.status != "EXCLUDED")
    if not broad:
        rows = rows.filter(PayrollRunEmployee.employee_id.in_(sorted(ids) or [0]))
    rows = rows.order_by(PayrollRunEmployee.id).all()
    employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == run.company_id,
                                                            Employee.id.in_([r.employee_id for r in rows] or [0]))}
    can_recalculate = (run.status in calculation.RECALCULATION_STATES and principal.has(CALCULATE)
                       and access.target_scope(principal, CALCULATE).covers(run.legal_entity_id, run.pay_group_id))
    if not runs.sees_whole_run(db, principal, run):
        # Stale reasons name employees (a salary change, changed sources): only those in scope (EC-SEC-05).
        visible = {e for (e,) in _visible_rows(db, principal, run).with_entities(PayrollRunEmployee.employee_id)}
        reasons = runs.visible_stale_reasons(reasons, visible)
    return {
        "run_id": run.id, "stale": run.stale, "reasons": reasons, "all_employees": broad,
        "employees": [{"run_employee_id": r.id, "employee_id": r.employee_id,
                       "employee_code": employees[r.employee_id].employee_code if r.employee_id in employees else None,
                       "employee_name": _name(employees.get(r.employee_id)),
                       "changes": changes.get(r.employee_id, [])} for r in rows],
        "actions": [
            {"action": "RECALCULATE_SELECTED", "available": can_recalculate,
             "employee_ids": sorted({r.employee_id for r in rows}),
             "endpoint": f"/api/v1/payroll/runs/{run.id}/calculate"},
            {"action": "HANDLE_AS_RETRO", "available": False,
             "reason": "Changes after lock are recalculated before approval; once the run is finalized, later "
                       "changes become retro proposals automatically."},
        ],
    }


# ---------------------------------------------------------------------------
# Replay (EC-STA-04)
# ---------------------------------------------------------------------------
def replay(db: Session, principal: Principal, run_id: int, *, employee_ids: Optional[Sequence[int]] = None,
           skip: int = 0, limit: int = 200) -> dict:
    """Recalculate from the stored snapshots and compare ``calc_hash``; reads only, never writes."""
    if not principal.has(READ):
        raise forbidden()
    if not (principal.has(AUDIT_READ) or principal.has(FINALIZE)):
        raise forbidden("Replaying a run needs payroll:audit:read or payroll:run:finalize.")
    run = runs.get_run_for(db, principal, run_id, READ)
    limit = max(1, min(limit, REPLAY_LIMIT))
    query = _visible_rows(db, principal, run).filter(PayrollRunEmployee.snapshot_id.isnot(None),
                                                     PayrollRunEmployee.calc_hash.isnot(None))
    if employee_ids:
        wanted = sorted(set(int(e) for e in employee_ids))
        query = query.filter(PayrollRunEmployee.employee_id.in_(wanted))
        if {e for (e,) in query.with_entities(PayrollRunEmployee.employee_id)} != set(wanted):
            raise not_found("Run employee")
    total = query.count()
    rows = query.order_by(PayrollRunEmployee.id).offset(skip).limit(limit).all()
    snapshots = {s.id: s for s in db.query(PayrollInputSnapshot).filter(
        PayrollInputSnapshot.company_id == run.company_id,
        PayrollInputSnapshot.id.in_([r.snapshot_id for r in rows] or [0]))}
    items = []
    for row in rows:
        snap = snapshots.get(row.snapshot_id)
        stored_engine = (row.stage_outputs or {}).get("engine_version") or run.engine_version
        item = {"run_employee_id": row.id, "employee_id": row.employee_id, "snapshot_id": row.snapshot_id,
                "snapshot_version": snap.version if snap else None, "stored_calc_hash": row.calc_hash,
                "engine_version": stored_engine}
        if stored_engine != engine.ENGINE_VERSION:
            items.append({**item, "matches": False, "replayed_calc_hash": None, "problem": "ENGINE_VERSION_UNAVAILABLE"})
            continue
        if snap is None:
            items.append({**item, "matches": False, "replayed_calc_hash": None, "problem": "SNAPSHOT_MISSING"})
            continue
        try:
            content_ok = snap is not None and engine.content_hash(snap.content) == snap.content_hash == row.content_hash
            matches, result = engine.replay(snap.content, row.calc_hash)
        except engine.SnapshotError as exc:
            items.append({**item, "matches": False, "replayed_calc_hash": None, "problem": exc.code})
            continue
        entry = {**item, "matches": bool(matches and content_ok), "content_hash_ok": content_ok,
                 "replayed_calc_hash": result.calc_hash}
        if not entry["matches"]:
            entry["stored_net"] = _money(row.net, row.currency)
            entry["replayed_net"] = _money(result.totals.net, row.currency)
        items.append(entry)
    matched = sum(1 for i in items if i["matches"])
    return {"run_id": run.id, "engine_version": {"stored": run.engine_version, "current": engine.ENGINE_VERSION},
            "total": total, "checked": len(items), "matched": matched, "mismatched": len(items) - matched,
            "items": items}


__all__ = [
    "SOURCE_PERMISSIONS", "acknowledge_variance", "employee_detail", "list_employees", "replay", "sanitize_sources",
    "sees_hr_only", "stale", "summary", "unacknowledged_count", "unacknowledged_variance", "variance", "variance_flags_acknowledged",
    "visible_source",
]
