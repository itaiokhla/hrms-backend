"""Payroll-cost invoices (owner request 2026-10-29: "invoices — both").

One invoice per finalized payroll run and cost group, for internal recharge or client billing:

* **Groups** — DEPARTMENT, BRANCH, TEAM (primary team membership), LEGAL_ENTITY, PAY_GROUP or COMPANY; the run's
  employees are grouped by their placement on the run's period end. ``preview`` lists every group with its totals.
* **Lines** — one per group member: gross pay and employer contributions (``employer_cost - gross``), from the run's
  employee rows; a service fee (percent of gross + employer) and taxes entered explicitly (never invented).
* **Lifecycle** — DRAFT (``payroll:payment:prepare`` over the run's entity) -> ISSUED (``payroll:payment:approve``
  by another person: maker-checker, also a DB CHECK; allocates a gapless ``CINV-YYYY-NNNN`` number under an advisory
  lock) -> VOID (with a reason; the number is never reused). The PDF renders from the stored invoice.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.org import EmployeeAssignment
from app.models.pay_runs import PayrollRun, PayrollRunEmployee
from app.models.team_payroll import COST_GROUPS, CostInvoice
from app.payroll.money import amount_str, quantize_amount
from app.payroll.services import access, documents
from app.services.audit_log_service import AuditLogService

PREPARE, APPROVE = "payroll:payment:prepare", "payroll:payment:approve"
AUDIT = "Payroll cost invoices"
FINAL_STATUSES = ("FINALIZED", "PAID", "PARTIALLY_PAID", "CLOSED", "PAYMENTS_IN_PROGRESS")


def _run(db: Session, principal: Principal, run_id: int, permission: str) -> PayrollRun:
    documents.require_staff(principal, permission)
    run = access.get_in_scope(db, principal, permission, PayrollRun, run_id, "Payroll run")
    if run.finalized_at is None:
        raise conflict("Cost invoices are made from finalized runs.", "RUN_NOT_FINALIZED")
    return run


def _period_end(db: Session, run: PayrollRun) -> date:
    from app.models.pay_runs import PayPeriod

    period = db.get(PayPeriod, run.pay_period_id)
    return period.period_end if period is not None else clock.company_today(db, run.company_id)


def _groups(db: Session, run: PayrollRun, group_by: str) -> Dict[Optional[int], dict]:
    """{group_id: {label, rows[]}} of the run's employee rows."""
    from app.models.org import Branch, LegalEntity
    from app.models.teams import Team, TeamMembership

    if group_by not in COST_GROUPS:
        raise bad_request(f"Group by one of {', '.join(COST_GROUPS)}.", "INVALID_FIELDS")
    rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                               PayrollRunEmployee.run_id == run.id).all()
    on = _period_end(db, run)
    ids = [r.employee_id for r in rows] or [0]
    place = {a.employee_id: a for a in db.query(EmployeeAssignment).filter(
        EmployeeAssignment.company_id == run.company_id, EmployeeAssignment.employee_id.in_(ids),
        EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))}
    people = {e.id: e for e in db.query(Employee).filter(Employee.company_id == run.company_id, Employee.id.in_(ids))}
    team_of: Dict[int, int] = {}
    if group_by == "TEAM":
        for m in db.query(TeamMembership).filter(TeamMembership.company_id == run.company_id,
                                                 TeamMembership.employee_id.in_(ids),
                                                 TeamMembership.effective_from <= on,
                                                 or_(TeamMembership.effective_to.is_(None),
                                                     TeamMembership.effective_to >= on)).order_by(TeamMembership.id):
            team_of.setdefault(m.employee_id, m.team_id)
    out: Dict[Optional[int], dict] = {}
    for row in rows:
        a = place.get(row.employee_id)
        key = {"DEPARTMENT": getattr(a, "department_id", None), "BRANCH": getattr(a, "branch_id", None),
               "LEGAL_ENTITY": getattr(a, "legal_entity_id", None) or run.legal_entity_id,
               "PAY_GROUP": getattr(a, "pay_group_id", None) or run.pay_group_id,
               "TEAM": team_of.get(row.employee_id), "COMPANY": None}[group_by]
        out.setdefault(key, {"rows": [], "label": None})["rows"].append((row, people.get(row.employee_id)))
    labels: Dict[Optional[int], str] = {}
    keys = [k for k in out if k is not None]
    if group_by == "BRANCH":
        labels = dict(db.query(Branch.id, Branch.name).filter(Branch.company_id == run.company_id, Branch.id.in_(keys or [0])))
    elif group_by == "LEGAL_ENTITY":
        labels = dict(db.query(LegalEntity.id, LegalEntity.legal_name).filter(
            LegalEntity.company_id == run.company_id, LegalEntity.id.in_(keys or [0])))
    elif group_by == "TEAM":
        labels = dict(db.query(Team.id, Team.name).filter(Team.company_id == run.company_id, Team.id.in_(keys or [0])))
    elif group_by == "DEPARTMENT":
        from app.models.employee import Department

        labels = dict(db.query(Department.id, Department.name).filter(
            Department.company_id == run.company_id, Department.id.in_(keys or [0])))
    elif group_by == "PAY_GROUP":
        from app.models.pay_org import PayGroup

        labels = dict(db.query(PayGroup.id, PayGroup.name).filter(PayGroup.company_id == run.company_id,
                                                                 PayGroup.id.in_(keys or [0])))
    for key, group in out.items():
        group["label"] = labels.get(key) or ("Whole company" if group_by == "COMPANY" else "Unassigned")
    return out


def _totals(rows, currency: str) -> tuple:
    gross = sum((Decimal(r.gross or 0) for r, _ in rows), Decimal(0))
    employer = sum((max(Decimal(r.employer_cost or 0) - Decimal(r.gross or 0), Decimal(0)) for r, _ in rows), Decimal(0))
    return quantize_amount(gross, currency), quantize_amount(employer, currency)


def preview(db: Session, principal: Principal, run_id: int, group_by: str) -> dict:
    run = _run(db, principal, run_id, PREPARE)
    groups = _groups(db, run, group_by)
    items = []
    for key, group in groups.items():
        gross, employer = _totals(group["rows"], run.currency)
        items.append({"group_id": key, "label": group["label"], "employees": len(group["rows"]),
                      "gross": amount_str(gross, run.currency), "employer": amount_str(employer, run.currency),
                      "cost": amount_str(gross + employer, run.currency)})
    items.sort(key=lambda i: i["label"])
    return {"run_id": run.id, "currency": run.currency, "group_by": group_by, "groups": items}


def _serialize(row: CostInvoice) -> dict:
    money = lambda v: amount_str(Decimal(v or 0), row.currency)  # noqa: E731
    return {"id": row.id, "run_id": row.run_id, "group_by": row.group_by, "group_id": row.group_id,
            "group_label": row.group_label, "number": row.number, "status": row.status, "currency": row.currency,
            "bill_to": row.bill_to, "lines": row.lines, "taxes": row.taxes, "employee_count": row.employee_count,
            "gross_total": money(row.gross_total), "employer_total": money(row.employer_total),
            "fee_percent": format(Decimal(row.fee_percent).normalize(), "f"), "fee_total": money(row.fee_total),
            "subtotal": money(row.subtotal), "tax_total": money(row.tax_total), "total": money(row.total),
            "issue_date": row.issue_date.isoformat() if row.issue_date else None,
            "due_date": row.due_date.isoformat() if row.due_date else None, "notes": row.notes,
            "created_by_user_id": row.created_by_user_id, "issued_by_user_id": row.issued_by_user_id,
            "void_reason": row.void_reason, "row_version": row.row_version}


def create(db: Session, principal: Principal, body: Mapping[str, Any]) -> CostInvoice:
    run = _run(db, principal, int(body.get("run_id") or 0), PREPARE)
    group_by = str(body.get("group_by") or "COMPANY")
    groups = _groups(db, run, group_by)
    key = body.get("group_id")
    group = groups.get(int(key) if key not in (None, "") else None)
    if group is None:
        raise not_found("Cost group")
    try:
        fee_percent = Decimal(str(body.get("fee_percent") or "0"))
    except Exception:  # noqa: BLE001
        raise bad_request("The fee is a percentage.", "INVALID_FIELDS")
    if fee_percent < 0 or fee_percent > 100:
        raise bad_request("The fee is between 0 and 100 percent.", "INVALID_FIELDS")
    currency = run.currency
    lines = []
    for row, person in sorted(group["rows"], key=lambda p: (p[1].full_name if p[1] else "")):
        gross, employer = _totals([(row, person)], currency)
        lines.append({"employee_id": row.employee_id, "employee_code": person.employee_code if person else None,
                      "name": person.full_name if person else f"#{row.employee_id}",
                      "gross": amount_str(gross, currency), "employer": amount_str(employer, currency),
                      "cost": amount_str(gross + employer, currency)})
    gross, employer = _totals(group["rows"], currency)
    fee = quantize_amount((gross + employer) * fee_percent / Decimal(100), currency)
    subtotal = gross + employer + fee
    taxes, tax_total = [], Decimal(0)
    for item in body.get("taxes") or []:
        label = str(item.get("label") or "").strip()[:60]
        try:
            rate = Decimal(str(item.get("rate_percent")))
        except Exception:  # noqa: BLE001
            raise bad_request("Tax rates are percentages.", "TAX_INVALID")
        if not label or rate < 0 or rate > 100:
            raise bad_request("Every tax line needs a label and a rate between 0 and 100.", "TAX_INVALID")
        amount = quantize_amount(subtotal * rate / Decimal(100), currency)
        tax_total += amount
        taxes.append({"label": label, "rate_percent": format(rate.normalize(), "f"), "amount": amount_str(amount, currency)})
    bill_to = {k: (str(v)[:200] if v else None) for k, v in (body.get("bill_to") or {}).items()
               if k in ("name", "address", "tax_registration", "reference")}
    if not bill_to.get("name"):
        bill_to["name"] = group["label"]
    row = CostInvoice(company_id=principal.company_id, run_id=run.id, group_by=group_by, group_id=key or None,
                      group_label=group["label"], status="DRAFT", currency=currency, bill_to=bill_to, lines=lines,
                      taxes=taxes, employee_count=len(lines), gross_total=gross, employer_total=employer,
                      fee_percent=fee_percent, fee_total=fee, subtotal=subtotal, tax_total=tax_total,
                      total=subtotal + tax_total, due_date=body.get("due_date") or None,
                      notes=(body.get("notes") or "")[:2000] or None, created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, AUDIT, "COST_INVOICE_CREATED",
                        f"Draft payroll-cost invoice for {group['label']} (run {run.id})", "cost_invoice", row.id,
                        details={"run_id": run.id, "group_by": group_by, "total": str(row.total)})
    return row


def _load(db: Session, principal: Principal, invoice_id: int, *, lock: bool = False) -> CostInvoice:
    if not (documents.has_staff_scope(principal, PREPARE) or documents.has_staff_scope(principal, APPROVE)):
        raise forbidden()
    query = db.query(CostInvoice).filter(CostInvoice.company_id == principal.company_id, CostInvoice.id == invoice_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Cost invoice")
    run = db.get(PayrollRun, row.run_id)
    code = PREPARE if principal.has(PREPARE) else APPROVE
    if run is None or not access.target_scope(principal, code).covers(run.legal_entity_id):
        raise not_found("Cost invoice")
    return row


def get(db: Session, principal: Principal, invoice_id: int) -> dict:
    return _serialize(_load(db, principal, invoice_id))


def list_invoices(db: Session, principal: Principal, *, run_id: Optional[int] = None, status: Optional[str] = None
                  ) -> List[dict]:
    if not (documents.has_staff_scope(principal, PREPARE) or documents.has_staff_scope(principal, APPROVE)):
        raise forbidden()
    query = (db.query(CostInvoice).join(PayrollRun, and_(PayrollRun.company_id == CostInvoice.company_id,
                                                         PayrollRun.id == CostInvoice.run_id))
             .filter(CostInvoice.company_id == principal.company_id))
    code = PREPARE if principal.has(PREPARE) else APPROVE
    query = query.filter(access.run_scope_clause(principal, code, PayrollRun.legal_entity_id))
    if run_id:
        query = query.filter(CostInvoice.run_id == run_id)
    if status:
        query = query.filter(CostInvoice.status == status)
    return [_serialize(r) for r in query.order_by(CostInvoice.id.desc()).limit(200)]


def issue(db: Session, principal: Principal, invoice_id: int, row_version: Optional[int]) -> CostInvoice:
    """Maker-checker: issued by an approver who did not prepare it; gapless number under an advisory lock."""
    row = _load(db, principal, invoice_id, lock=True)
    documents.require_staff(principal, APPROVE)
    run = db.get(PayrollRun, row.run_id)
    if not access.target_scope(principal, APPROVE).covers(run.legal_entity_id):
        raise not_found("Cost invoice")
    if row.status != "DRAFT":
        raise AppError(409, "Only a draft can be issued.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})
    if row_version is not None and row.row_version != row_version:
        raise conflict("This invoice changed meanwhile; reload.", "ROW_VERSION_CONFLICT")
    if row.created_by_user_id == principal.user_id:
        raise forbidden("Someone other than the preparer issues the invoice.")
    today = clock.company_today(db, row.company_id)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('cost_invoice_number'), :c)"), {"c": row.company_id})
    prefix = f"CINV-{today.year}-"
    last = db.query(func.max(CostInvoice.number)).filter(CostInvoice.company_id == row.company_id,
                                                         CostInvoice.number.like(f"{prefix}%")).scalar()
    seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    row.number = f"{prefix}{seq:04d}"
    row.status, row.issue_date = "ISSUED", today
    row.issued_by_user_id, row.issued_at = principal.user_id, clock.utcnow()
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, AUDIT, "COST_INVOICE_ISSUED", f"Payroll-cost invoice {row.number} issued",
                        "cost_invoice", row.id, details={"number": row.number, "total": str(row.total)})
    return row


def void(db: Session, principal: Principal, invoice_id: int, reason: str) -> CostInvoice:
    row = _load(db, principal, invoice_id, lock=True)
    documents.require_staff(principal, APPROVE)
    if row.status == "VOID":
        raise AppError(409, "Already void.", "INVALID_TRANSITION", extra={"details": {"current_status": row.status}})
    if len((reason or "").strip()) < 3:
        raise bad_request("Say why the invoice is void.", "REASON_REQUIRED")
    row.status, row.void_reason = "VOID", reason.strip()[:500]
    if row.number is None:
        row.number = f"VOID-DRAFT-{row.id}"
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, AUDIT, "COST_INVOICE_VOIDED", f"Payroll-cost invoice {row.number} void",
                        "cost_invoice", row.id, details={"reason": row.void_reason})
    return row


def pdf(db: Session, principal: Principal, invoice_id: int) -> tuple:
    from app.payroll.services import pdf_render, registers

    row = _load(db, principal, invoice_id)
    run = db.get(PayrollRun, row.run_id)
    content = registers.base_content(db, row.company_id, run.legal_entity_id,
                                     "Payroll cost invoice" + (f" {row.number}" if row.number else " (draft)"))
    loc = content.get("locale") or "en"
    fmt = lambda v: pdf_render.format_amount(v, row.currency, loc)  # noqa: E731
    bill = row.bill_to or {}
    content["info"] = [["Bill to", bill.get("name") or row.group_label], ["Address", bill.get("address") or "—"],
                       ["Tax registration", bill.get("tax_registration") or "—"],
                       ["Reference", bill.get("reference") or "—"], ["Payroll run", str(row.run_id)],
                       ["Cost group", f"{row.group_by.replace('_', ' ').title()}: {row.group_label}"],
                       ["Issue date", row.issue_date.isoformat() if row.issue_date else "Draft"],
                       ["Due date", row.due_date.isoformat() if row.due_date else "—"]]
    content["tables"] = [
        {"title": "Employees", "columns": ["Code", "Name", f"Gross ({row.currency})", f"Employer ({row.currency})",
                                           f"Cost ({row.currency})"],
         "numeric": [False, False, True, True, True], "widths": [0.9, 2.4, 1.2, 1.2, 1.2],
         "rows": [[l.get("employee_code") or "", l["name"], fmt(l["gross"]), fmt(l["employer"]), fmt(l["cost"])]
                  for l in row.lines],
         "total_row": ["", "Total", fmt(row.gross_total), fmt(row.employer_total),
                       fmt(Decimal(row.gross_total) + Decimal(row.employer_total))]},
        {"title": "Summary", "columns": ["", f"Amount ({row.currency})"], "numeric": [False, True],
         "rows": [["Payroll cost", fmt(Decimal(row.gross_total) + Decimal(row.employer_total))],
                  [f"Service fee ({format(Decimal(row.fee_percent).normalize(), 'f')} %)", fmt(row.fee_total)],
                  *[[f"{t['label']} ({t['rate_percent']} %)", fmt(t["amount"])] for t in row.taxes],
                  ["Total", fmt(row.total)]]}]
    content["notes"] = ["Taxes are printed as entered."] + ([f"VOID: {row.void_reason}"] if row.status == "VOID" else [])
    data, _ = pdf_render.render_with_brand(content)
    return data, f"payroll-cost-{row.number or 'draft-' + str(row.id)}.pdf"
