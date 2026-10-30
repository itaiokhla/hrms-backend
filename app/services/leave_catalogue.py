"""Leave rule catalogue and HR balance tools (03-leave/01 §8.1 rules 15-16; 03-leave/02 §11-§12; SPINE §4.2).

* Blackouts and coverage rules: ``leave:manage_policy``; tenant-wide rows need COMPANY, rows for one legal entity /
  branch need that scope (V3-075).
* Reason codes: ``leave:manage_policy`` at COMPANY.
* Approval route: the company's leave chain with per-step conditions (leave types, units, legal entities, sensitivity).
* Adjustments (``leave:balance_adjust``, step-up above ``leave.adjustments.step_up_units``) and opening-balance imports
  (``leave:import``: CSV dry run → commit) post through the one ledger writer — V2_ACTIVE tenants only.
"""

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core import authz, clock, concurrency
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import ApprovalChainStep, LeaveType
from app.models.leave_v2 import LeaveBlackout, LeaveCoverageRule, LeaveImport, LeaveReasonCode
from app.services import leave_access, leave_ledger, leave_plans
from app.services.audit_log_service import AuditLogService

MANAGE = "leave:manage_policy"


def _require_scope(principal: Principal, legal_entity_id: Optional[int] = None, branch_ids=None) -> None:
    if principal.has(MANAGE, "COMPANY"):
        return
    if legal_entity_id is not None and legal_entity_id in principal.scope_targets(MANAGE, "LEGAL_ENTITY"):
        return
    if branch_ids and set(branch_ids) <= principal.scope_targets(MANAGE, "BRANCH"):
        return
    raise forbidden("This rule covers people outside your legal entities or branches.")


# --------------------------------------------------------------------------- blackouts

def blackout_json(row: LeaveBlackout) -> dict:
    return {"id": row.id, "name": row.name, "from_date": row.from_date.isoformat(), "to_date": row.to_date.isoformat(),
            "legal_entity_id": row.legal_entity_id, "branch_ids": row.branch_ids, "department_ids": row.department_ids,
            "leave_type_codes": row.leave_type_codes or [], "mode": row.mode, "reason": row.reason, "status": row.status,
            "row_version": row.row_version}


def list_blackouts(db: Session, principal: Principal, include_archived: bool = False) -> List[dict]:
    if not (principal.has(MANAGE) or principal.has("leave:read", "COMPANY")):
        raise forbidden()
    query = db.query(LeaveBlackout).filter(LeaveBlackout.company_id == principal.company_id)
    if not include_archived:
        query = query.filter(LeaveBlackout.status == "ACTIVE")
    return [blackout_json(r) for r in query.order_by(LeaveBlackout.from_date)]


def save_blackout(db: Session, principal: Principal, data, blackout_id: Optional[int] = None) -> dict:
    _require_scope(principal, data.legal_entity_id, data.branch_ids)
    if data.to_date < data.from_date:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    codes = set(data.leave_type_codes or [])
    known = {c for (c,) in db.query(LeaveType.code).filter(LeaveType.company_id == principal.company_id)}
    if codes - known:
        raise bad_request(f"Unknown leave types: {', '.join(sorted(codes - known))}.", "UNKNOWN_REFERENCE")
    if blackout_id:
        row = db.query(LeaveBlackout).filter(LeaveBlackout.company_id == principal.company_id,
                                             LeaveBlackout.id == blackout_id).with_for_update().first()
        if row is None:
            raise not_found("Blackout")
        _require_scope(principal, row.legal_entity_id, row.branch_ids)
        row.row_version += 1
    else:
        row = LeaveBlackout(company_id=principal.company_id, created_by_user_id=principal.user_id, row_version=1)
        db.add(row)
    row.name, row.from_date, row.to_date = data.name.strip(), data.from_date, data.to_date
    row.legal_entity_id, row.branch_ids, row.department_ids = data.legal_entity_id, data.branch_ids, data.department_ids
    row.leave_type_codes, row.mode, row.reason = sorted(codes), data.mode, (data.reason or "").strip() or None
    row.status = data.status
    db.flush()
    AuditLogService.log(db, principal, "Leave", "BLACKOUT_SAVED", f"Saved blackout {row.name}", "leave_blackout", row.id)
    return blackout_json(row)


# --------------------------------------------------------------------------- coverage

def coverage_json(row: LeaveCoverageRule) -> dict:
    return {"id": row.id, "name": row.name, "department_id": row.department_id, "branch_id": row.branch_id,
            "team_id": row.team_id, "max_absent_count": row.max_absent_count,
            "max_absent_pct": None if row.max_absent_pct is None else float(row.max_absent_pct),
            "count_pending": row.count_pending, "mode": row.mode, "status": row.status, "row_version": row.row_version}


def list_coverage(db: Session, principal: Principal) -> List[dict]:
    if not (principal.has(MANAGE) or principal.has("leave:read", "COMPANY")):
        raise forbidden()
    return [coverage_json(r) for r in db.query(LeaveCoverageRule).filter(
        LeaveCoverageRule.company_id == principal.company_id).order_by(LeaveCoverageRule.name)]


def save_coverage(db: Session, principal: Principal, data, rule_id: Optional[int] = None) -> dict:
    _require_scope(principal, None, [data.branch_id] if data.branch_id else None)
    groups = [g for g in (data.department_id, data.branch_id, data.team_id) if g is not None]
    if len(groups) != 1:
        raise bad_request("Choose one department, branch or team.", "RULE_INVALID")
    if (data.max_absent_count is None) == (data.max_absent_pct is None):
        raise bad_request("Set either a number of people or a percentage.", "RULE_INVALID")
    if rule_id:
        row = db.query(LeaveCoverageRule).filter(LeaveCoverageRule.company_id == principal.company_id,
                                                 LeaveCoverageRule.id == rule_id).with_for_update().first()
        if row is None:
            raise not_found("Coverage rule")
        row.row_version += 1
    else:
        row = LeaveCoverageRule(company_id=principal.company_id, created_by_user_id=principal.user_id, row_version=1)
        db.add(row)
    for key in ("name", "department_id", "branch_id", "team_id", "max_absent_count", "max_absent_pct",
                "count_pending", "mode", "status"):
        setattr(row, key, getattr(data, key))
    db.flush()
    AuditLogService.log(db, principal, "Leave", "COVERAGE_SAVED", f"Saved coverage rule {row.name}",
                        "leave_coverage_rule", row.id)
    return coverage_json(row)


# --------------------------------------------------------------------------- reason codes

def list_reason_codes(db: Session, principal: Principal, applies_to: Optional[str] = None) -> List[dict]:
    query = db.query(LeaveReasonCode).filter(LeaveReasonCode.company_id == principal.company_id,
                                             LeaveReasonCode.is_active.is_(True))
    if applies_to:
        query = query.filter(LeaveReasonCode.applies_to == applies_to)
    return [{"id": r.id, "code": r.code, "label": r.label, "applies_to": r.applies_to,
             "counts_as_refused": r.counts_as_refused} for r in query.order_by(LeaveReasonCode.applies_to,
                                                                                LeaveReasonCode.label)]


def save_reason_code(db: Session, principal: Principal, data) -> dict:
    if not principal.has(MANAGE, "COMPANY"):
        raise forbidden()
    row = db.query(LeaveReasonCode).filter(LeaveReasonCode.company_id == principal.company_id,
                                           LeaveReasonCode.applies_to == data.applies_to,
                                           LeaveReasonCode.code == data.code).first()
    if row is None:
        row = LeaveReasonCode(company_id=principal.company_id, code=data.code, applies_to=data.applies_to)
        db.add(row)
    row.label, row.counts_as_refused, row.is_active = data.label, data.counts_as_refused, data.is_active
    db.flush()
    return {"id": row.id, "code": row.code, "label": row.label, "applies_to": row.applies_to,
            "counts_as_refused": row.counts_as_refused, "is_active": row.is_active}


# --------------------------------------------------------------------------- approval route with conditions

def get_route(db: Session, company_id: int) -> List[dict]:
    from app.services.leave_service import APPROVER_LABELS
    from app.models.rbac import Role

    roles = {r.id: r.name for r in db.query(Role).filter(Role.company_id == company_id)}
    steps = (db.query(ApprovalChainStep).filter(ApprovalChainStep.company_id == company_id,
                                                ApprovalChainStep.module == "leave",
                                                ApprovalChainStep.route_id.is_(None))
             .order_by(ApprovalChainStep.step_order).all())
    return [{"step_order": s.step_order, "approver_type": s.approver_type,
             "approver_label": APPROVER_LABELS.get(s.approver_type, s.approver_type), "role_id": s.role_id,
             "role_name": roles.get(s.role_id), "min_days": s.min_days, "condition": s.condition or {},
             "sla_hours": s.sla_hours} for s in steps]


def save_route(db: Session, principal: Principal, steps) -> List[dict]:
    from app.models.rbac import Role

    if not principal.has(MANAGE, "COMPANY"):
        raise forbidden("The approval route is company-wide.")
    if len(steps) > 10:
        raise bad_request("An approval route can have at most 10 steps.")
    known = {c for (c,) in db.query(LeaveType.code).filter(LeaveType.company_id == principal.company_id)}
    for step in steps:
        if step.approver_type == "role":
            if step.role_id is None or db.query(Role.id).filter(Role.company_id == principal.company_id,
                                                                Role.id == step.role_id).first() is None:
                raise bad_request("Choose a role for role-based steps.", "UNKNOWN_REFERENCE")
        elif step.role_id is not None:
            raise bad_request("Only role-based steps take a role.")
        codes = set((step.condition or {}).get("leave_type_codes") or [])
        if codes - known:
            raise bad_request(f"Unknown leave types: {', '.join(sorted(codes - known))}.", "UNKNOWN_REFERENCE")
    db.query(ApprovalChainStep).filter(ApprovalChainStep.company_id == principal.company_id,
                                       ApprovalChainStep.module == "leave",
                                       ApprovalChainStep.route_id.is_(None)).delete(synchronize_session=False)
    db.flush()
    for index, step in enumerate(steps, start=1):
        condition = {k: v for k, v in (step.condition or {}).items() if v not in (None, [], "")}
        db.add(ApprovalChainStep(company_id=principal.company_id, module="leave", step_order=index,
                                 approver_type=step.approver_type, role_id=step.role_id, min_days=step.min_days,
                                 condition=condition or None, sla_hours=step.sla_hours))
    db.flush()
    AuditLogService.log(db, principal, "Leave", "CHAIN_UPDATED",
                        "Approval route: " + " -> ".join(s.approver_type for s in steps) if steps else
                        "Approval route cleared")
    return get_route(db, principal.company_id)


# --------------------------------------------------------------------------- adjustments and imports

def adjust(db: Session, principal: Principal, *, employee_id: int, leave_type: str, delta: Decimal, effective_date: date,
           reason: str, reason_code: Optional[str] = None, expires_on: Optional[date] = None) -> dict:
    leave_access.require_v2(db, principal.company_id)
    if not principal.has("leave:balance_adjust"):
        raise forbidden("Adjusting balances needs the balance adjustment permission.")
    employee = authz.ensure_employee_access(db, principal, "leave:balance_adjust", employee_id)
    if employee.id == principal.employee_id:
        raise forbidden("You cannot adjust your own leave balance.")
    if not (reason or "").strip():
        raise bad_request("Give a reason for the adjustment.", "REASON_REQUIRED")
    delta = Decimal(delta)
    if delta == 0:
        raise bad_request("The adjustment cannot be zero.", "RULE_INVALID")
    threshold = Decimal(str(leave_access.setting(db, principal.company_id, "leave.adjustments.step_up_units",
                                                 default=Decimal("10"))))
    if abs(delta) > threshold:
        from app.payroll.services.step_up import ensure_step_up

        ensure_step_up(db, principal)
    lt = db.query(LeaveType).filter(LeaveType.company_id == principal.company_id, LeaveType.code == leave_type).first()
    if lt is None:
        raise bad_request("Unknown leave type.", "INVALID_LEAVE_TYPE")
    if lt.is_unlimited:
        raise bad_request("Unlimited leave has no balance to adjust.", "RULE_INVALID")
    if lt.kind == "EVENT":  # EVENT units live on the event, never in the ledger (V3-053(7))
        raise bad_request("Event leave has no yearly balance; its units come from the recorded event.", "RULE_INVALID")
    concurrency.lock_employees(db, [employee.id])
    rule = leave_plans.rules_for(db, principal.company_id, employee.id, lt.code, effective_date)
    scope = ((rule or {}).get("balance_scope") or {}).get("kind") or ("LOTS" if lt.kind == "COMP_OFF" else "CYCLE")
    cycle = None if scope != "CYCLE" else leave_plans.cycle_of(db, principal.company_id, employee, rule, effective_date)
    row = leave_ledger.post(db, principal.company_id, employee_id=employee.id, type_code=lt.code, delta=delta,
                            reason="ADJUST", key=f"adjust:{uuid.uuid4().hex}", effective_date=effective_date,
                            cycle=cycle, source="ADJUSTMENT", actor_user_id=principal.user_id,
                            note=f"{reason_code + ': ' if reason_code else ''}{reason.strip()}"[:500],
                            expires_on=expires_on if delta > 0 else None)
    AuditLogService.log(db, principal, "Leave", "BALANCE_ADJUSTED",
                        f"Adjusted {lt.code} of {employee.full_name} by {delta}", "leave_balance_transaction", row.id,
                        details={"employee_id": employee.id, "delta": str(delta), "reason_code": reason_code})
    return leave_ledger.explain_row(db, principal.company_id, row.id)


IMPORT_COLUMNS = ("employee_code", "leave_type", "units", "effective_date", "expires_on", "note")


def import_dry_run(db: Session, principal: Principal, *, kind: str, file_name: str, content: bytes) -> LeaveImport:
    leave_access.require_v2(db, principal.company_id)
    if not principal.has("leave:import"):
        raise forbidden("Imports need the leave import permission.")
    if kind not in ("OPENING_BALANCES", "BALANCE_ADJUSTMENTS"):
        raise bad_request("Unknown import kind.", "UNKNOWN_REFERENCE")
    if len(content) > 2 * 1024 * 1024:
        raise AppError(413, "The file is larger than 2 MB.", "IMPORT_TOO_LARGE")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise bad_request("Upload a UTF-8 CSV file.", "INVALID_FILE")
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in IMPORT_COLUMNS[:4] if c not in (reader.fieldnames or [])]
    if missing:
        raise bad_request(f"Missing columns: {', '.join(missing)}.", "INVALID_FILE")
    cid = principal.company_id
    codes = {e.employee_code: e for e in db.query(Employee).filter(Employee.company_id == cid)}
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == cid)}
    rows, errors = [], []
    seen: Dict[tuple, int] = {}
    for line, raw in enumerate(reader, start=2):
        if line > 5001:
            raise AppError(413, "At most 5000 rows per import.", "IMPORT_TOO_LARGE")
        problems, problem_codes = [], []

        def bad(code: str, message: str) -> None:
            problems.append(message)
            problem_codes.append(code)

        employee = codes.get((raw.get("employee_code") or "").strip())
        if employee is None:
            bad("UNKNOWN_EMPLOYEE", "unknown employee")
        elif not authz.can_access_employee(db, principal, "leave:import", employee.id):
            bad("UNKNOWN_EMPLOYEE", "employee outside your scope")
        lt = types.get((raw.get("leave_type") or "").strip())
        if lt is None or lt.is_unlimited or lt.kind == "EVENT":
            bad("INVALID_LEAVE_TYPE", "unknown, unlimited or event leave type")
        try:
            units = Decimal((raw.get("units") or "").strip())
            if not units.is_finite() or units == 0 or abs(units) > 1000 or units != units.quantize(Decimal("0.01")):
                bad("INVALID_UNITS", "units must be a non-zero number with at most 2 decimals")
                units = None
        except (InvalidOperation, ValueError):
            units = None
            bad("INVALID_UNITS", "units is not a number")
        try:
            effective = date.fromisoformat((raw.get("effective_date") or "").strip())
        except ValueError:
            effective = None
            bad("INVALID_DATE", "effective_date must be YYYY-MM-DD")
        expires = None
        if (raw.get("expires_on") or "").strip():
            try:
                expires = date.fromisoformat(raw["expires_on"].strip())
            except ValueError:
                bad("INVALID_DATE", "expires_on must be YYYY-MM-DD")
        if kind == "OPENING_BALANCES" and units is not None and units < 0:
            bad("INVALID_UNITS", "opening balances must be positive")
        if not problems and kind == "OPENING_BALANCES":
            # LH-IMP-02: one opening balance per (employee, type, leave year); corrections go through ADJUST.
            rule = leave_plans.rules_for(db, cid, employee.id, lt.code, effective)
            scope = ((rule or {}).get("balance_scope") or {}).get("kind") or ("LOTS" if lt.kind == "COMP_OFF"
                                                                               else "CYCLE")
            if scope == "CYCLE":
                cycle = leave_plans.cycle_of(db, cid, employee, rule, effective)
                key = (employee.id, lt.code, cycle.start)
                if key in seen:
                    bad("DUPLICATE_ROW", f"the same employee and leave year are on line {seen[key]}")
                elif any(leave_ledger._in_cycle(r, cycle) for r in leave_ledger._rows(db, cid, employee.id, lt.code)
                         if r.reason == "OPENING"):
                    bad("OPENING_ALREADY_POSTED", "an opening balance is already posted for this leave year; "
                                                  "use an adjustment")
                seen.setdefault(key, line)
        if problems:
            errors.append({"line": line, "errors": problems, "codes": problem_codes})
            continue
        rows.append({"line": line, "employee_id": employee.id, "employee_code": employee.employee_code,
                     "leave_type": lt.code, "units": str(units), "effective_date": effective.isoformat(),
                     "expires_on": expires.isoformat() if expires else None,
                     "note": (raw.get("note") or "").strip()[:200] or None})
    row = LeaveImport(company_id=cid, kind=kind, status="DRY_RUN", file_name=file_name[:200],
                      report={"valid": len(rows), "errors": errors[:500], "error_count": len(errors)}, rows=rows,
                      created_by_user_id=principal.user_id, expires_at=utcnow() + timedelta(days=7))
    db.add(row)
    db.flush()
    return row


def import_commit(db: Session, principal: Principal, import_id: int) -> LeaveImport:
    leave_access.require_v2(db, principal.company_id)
    if not principal.has("leave:import"):
        raise forbidden()
    row = db.query(LeaveImport).filter(LeaveImport.company_id == principal.company_id,
                                       LeaveImport.id == import_id).with_for_update().first()
    if row is None:
        raise not_found("Import")
    if row.status != "DRY_RUN" or row.expires_at < utcnow():
        raise AppError(409, "This import cannot be committed (already committed or expired).", "INVALID_TRANSITION")
    if row.report.get("error_count"):
        raise AppError(409, "Fix the rows with errors and upload again.", "IMPORT_HAS_ERRORS")
    from app.payroll.services.step_up import ensure_step_up

    ensure_step_up(db, principal)
    concurrency.lock_employees(db, [r["employee_id"] for r in row.rows])
    posted = 0
    for item in row.rows:
        employee = db.get(Employee, item["employee_id"])
        lt = db.query(LeaveType).filter(LeaveType.company_id == row.company_id, LeaveType.code == item["leave_type"]).one()
        on = date.fromisoformat(item["effective_date"])
        rule = leave_plans.rules_for(db, row.company_id, employee.id, lt.code, on)
        scope = ((rule or {}).get("balance_scope") or {}).get("kind") or ("LOTS" if lt.kind == "COMP_OFF" else "CYCLE")
        cycle = None if scope != "CYCLE" else leave_plans.cycle_of(db, row.company_id, employee, rule, on)
        if leave_ledger.post(db, row.company_id, employee_id=employee.id, type_code=lt.code,
                             delta=Decimal(item["units"]),
                             reason="OPENING" if row.kind == "OPENING_BALANCES" else "ADJUST",
                             key=f"opening:{row.id}:{item['line']}", effective_date=on, cycle=cycle, source="IMPORT",
                             actor_user_id=principal.user_id, note=item.get("note") or f"Import #{row.id}",
                             expires_on=date.fromisoformat(item["expires_on"]) if item.get("expires_on") else None):
            posted += 1
    row.status, row.committed_by_user_id, row.committed_at = "COMMITTED", principal.user_id, utcnow()
    row.report = dict(row.report) | {"posted": posted}
    db.flush()
    AuditLogService.log(db, principal, "Leave", "IMPORT_COMMITTED", f"Committed leave import #{row.id}", "leave_import",
                        row.id, details={"posted": posted, "kind": row.kind})
    return row


def import_json(row: LeaveImport) -> dict:
    return {"id": row.id, "kind": row.kind, "status": row.status, "file_name": row.file_name, "report": row.report,
            "rows": row.rows[:200], "created_at": row.created_at.isoformat() if row.created_at else None,
            "committed_at": row.committed_at.isoformat() if row.committed_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None}
