"""Contractor invoices, self-billed invoices and credit notes (3.6 §10; INVOICE machine; EC-DOC-07/08/09).

* Who: only payees whose worker class (employment type of the primary assignment) is CONTRACTOR on the
  invoice date. A salaried employee never gets an invoice, and an invoice is never made from a payslip
  (EC-DOC-07, contract §23.1).
* Source: approved, unpaid tasks of which the payee is an assignee, at the task's rate and the payee's
  split of the accepted quantity. A task is billed by at most one live invoice (partial unique index
  ``uq_invoice_sources_live``); a credit note releases its sources.
* Modes: INVOICE (the contractor's own invoice, recorded with their number; the company approves it) and
  SELF_BILLED (issued by the company on the contractor's behalf; requires an active self-billing agreement
  covering the issue date).
* Lifecycle (``state_machines.INVOICE``) through the single ``transition`` function: row lock,
  ``row_version`` (409), permission in the legal entity's scope (403 / 404), guards, audit. ISSUE allocates
  the internal number from the issuer's sequence with ``SELECT ... FOR UPDATE`` in the issuing transaction,
  so concurrent issues get consecutive, unique numbers (EC-DOC-08), and renders the immutable document.
  APPROVE needs a second person (maker-checker; also a database CHECK). PAID only from payment evidence
  (system action ``record_payment``). CREDIT issues a gapless credit note for the full amount; the original
  stays, marked CREDITED, and its number is never reused (EC-DOC-09).
* Taxes: never computed from invented rules. Tax lines are entered explicitly (label, rate) and printed as
  entered; a reverse-charge line prints without an amount.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.pay_documents import Invoice, InvoiceSource, PayDocument, SelfBillingAgreement
from app.models.pay_org import EmploymentType
from app.payroll import audit
from app.payroll import currencies
from app.payroll import state_machines as sm
from app.payroll.money import amount_str, quantize_amount, to_decimal
from app.payroll.services import access, documents
from app.services.audit_log_service import AuditLogService

PREPARE = sm.PAYMENT_PREPARE
APPROVE = sm.PAYMENT_APPROVE
VIEW = (PREPARE, APPROVE)
MAX_TASKS = 500
DOC_TYPE = {("INVOICE", "INVOICE"): "INVOICE", ("INVOICE", "SELF_BILLED"): "SELF_BILLED_INVOICE",
            ("CREDIT_NOTE", "INVOICE"): "CREDIT_NOTE", ("CREDIT_NOTE", "SELF_BILLED"): "CREDIT_NOTE"}
TITLES = {"INVOICE": "Invoice", "SELF_BILLED_INVOICE": "Self-billed invoice", "CREDIT_NOTE": "Credit note"}


# --------------------------------------------------------------------------- helpers


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def worker_class(db: Session, company_id: int, employee_id: int, on: date) -> Optional[str]:
    """The worker class of the employee's primary assignment on ``on`` (None when unplaced or untyped)."""
    from app.payroll.services import settings as settings_service

    placement = settings_service.primary_assignment(db, company_id, employee_id, on)
    if placement is None:
        return None
    if placement.employment_type_id is None:
        return "EMPLOYEE"
    row = db.query(EmploymentType).filter(EmploymentType.company_id == company_id,
                                          EmploymentType.id == placement.employment_type_id).first()
    return row.worker_class if row is not None else None


def _placement(db: Session, company_id: int, employee_id: int, on: date):
    from app.payroll.services import settings as settings_service

    return settings_service.primary_assignment(db, company_id, employee_id, on)


def _require_contractor(db: Session, company_id: int, employee_id: int, on: date) -> None:
    if worker_class(db, company_id, employee_id, on) != "CONTRACTOR":
        raise conflict("Invoices are only for contractors; employees receive payslips.", "NOT_A_CONTRACTOR")


def active_agreement(db: Session, company_id: int, employee_id: int, legal_entity_id: int, on: date
                     ) -> Optional[SelfBillingAgreement]:
    return (db.query(SelfBillingAgreement)
            .filter(SelfBillingAgreement.company_id == company_id, SelfBillingAgreement.employee_id == employee_id,
                    SelfBillingAgreement.legal_entity_id == legal_entity_id, SelfBillingAgreement.status == "ACTIVE",
                    SelfBillingAgreement.effective_from <= on,
                    or_(SelfBillingAgreement.effective_to.is_(None), SelfBillingAgreement.effective_to >= on))
            .order_by(SelfBillingAgreement.id.desc()).first())


def _covers(principal: Principal, permission: str, legal_entity_id: int) -> bool:
    return access.target_scope(principal, permission).covers(legal_entity_id)


def _require_in_scope(principal: Principal, permission: str, legal_entity_id: int, what: str = "Invoice") -> None:
    if not principal.has(permission) or not documents.has_staff_scope(principal, permission):
        raise forbidden()
    if not _covers(principal, permission, legal_entity_id):
        raise not_found(what)


def _view_clause(principal: Principal):
    clauses = [access.run_scope_clause(principal, p, Invoice.legal_entity_id) for p in VIEW if principal.has(p)]
    return or_(*clauses) if clauses else None


def money_json(value: Any, currency: str) -> dict:
    return {"amount": amount_str(Decimal(value), currency), "currency": currency}


def invoice_json(row: Invoice, *, for_self: bool = False) -> dict:
    data = {
        "id": row.id, "kind": row.kind, "mode": row.mode, "status": row.status, "number": row.number,
        "external_number": row.external_number, "legal_entity_id": row.legal_entity_id,
        "payee_employee_id": row.payee_employee_id, "credit_of_invoice_id": row.credit_of_invoice_id,
        "currency": row.currency, "seller": dict(row.seller or {}), "buyer": dict(row.buyer or {}),
        "lines": list(row.lines or []), "taxes": list(row.taxes or []),
        "subtotal": money_json(row.subtotal, row.currency), "tax_total": money_json(row.tax_total, row.currency),
        "total": money_json(row.total, row.currency), "issue_date": row.issue_date.isoformat() if row.issue_date else None,
        "due_date": row.due_date.isoformat() if row.due_date else None, "payment_terms": row.payment_terms,
        "document_id": row.document_id, "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "credited_at": row.credited_at.isoformat() if row.credited_at else None, "row_version": row.row_version,
    }
    if not for_self:
        data.update({"notes": row.notes, "status_reason": row.status_reason,
                     "created_by_user_id": row.created_by_user_id, "issued_by_user_id": row.issued_by_user_id,
                     "approved_by_user_id": row.approved_by_user_id,
                     "issued_at": row.issued_at.isoformat() if row.issued_at else None,
                     "approved_at": row.approved_at.isoformat() if row.approved_at else None})
    return data


def _audit(db: Session, principal: Optional[Principal], action: str, row: Invoice, description: str, **details: Any
           ) -> None:
    AuditLogService.log(db, principal, audit.PAYMENTS, action, description, "invoice", row.id, company_id=row.company_id,
                        actor_type="user" if principal else "system",
                        details={"kind": row.kind, "mode": row.mode, "number": row.number, **details})


# --------------------------------------------------------------------------- drafts


def _decimal(value: Any, name: str) -> Decimal:
    try:
        return to_decimal(value)
    except Exception:  # noqa: BLE001
        raise bad_request(f"{name} must be a decimal string.", "DECIMAL_INVALID")


def _task_lines(db: Session, company_id: int, payee_id: int, task_ids: Sequence[int]) -> tuple[list[dict], str, Decimal]:
    from app.models.work_items import Task, TaskAssignee

    if not task_ids:
        raise bad_request("Choose at least one approved task.", "TASKS_REQUIRED")
    if len(set(task_ids)) != len(task_ids) or len(task_ids) > MAX_TASKS:
        raise bad_request(f"Choose up to {MAX_TASKS} distinct tasks.", "TASKS_INVALID")
    tasks = {t.id: t for t in db.query(Task).filter(Task.company_id == company_id, Task.id.in_(list(task_ids)))}
    shares = {a.task_id: Decimal(a.split_percent) for a in db.query(TaskAssignee).filter(
        TaskAssignee.company_id == company_id, TaskAssignee.task_id.in_(list(task_ids)),
        TaskAssignee.employee_id == payee_id)}
    live = {s for (s,) in db.query(InvoiceSource.source_id).filter(
        InvoiceSource.company_id == company_id, InvoiceSource.source_type == "TASK",
        InvoiceSource.source_id.in_(list(task_ids)), InvoiceSource.active.is_(True))}
    lines, currency, subtotal = [], None, Decimal(0)
    for task_id in task_ids:
        task = tasks.get(task_id)
        if task is None or task_id not in shares:
            raise not_found("Task")
        if not sm.task_is_payable(task.status) or task.paid_in_run_id is not None:
            raise conflict(f"Task {task_id} is not an approved, unpaid task.", "TASK_NOT_BILLABLE")
        if task_id in live:
            raise conflict(f"Task {task_id} is already on a live invoice.", "TASK_ALREADY_INVOICED")
        if task.rate is None or not task.rate_currency:
            raise conflict(f"Task {task_id} has no rate.", "TASK_RATE_MISSING")
        if currency is None:
            currency = task.rate_currency
        elif currency != task.rate_currency:
            raise conflict("All tasks of an invoice use one currency.", "CURRENCY_MISMATCH")
        quantity = Decimal(task.accepted_quantity if task.accepted_quantity is not None else task.quantity)
        share = shares[task_id] / Decimal(100)
        billed = quantity * share
        amount = quantize_amount(billed * Decimal(task.rate), currency)
        subtotal += amount
        lines.append({"task_id": task.id, "description": task.title, "quantity": format(billed.normalize(), "f"),
                      "unit": task.unit, "rate": amount_str(quantize_amount(task.rate, currency), currency)
                      if Decimal(task.rate) == quantize_amount(task.rate, currency) else format(Decimal(task.rate), "f"),
                      "amount": amount_str(amount, currency)})
    return lines, currency, subtotal


def _tax_lines(raw: Sequence[Mapping[str, Any]], subtotal: Decimal, currency: str) -> tuple[list[dict], Decimal]:
    taxes, total = [], Decimal(0)
    for item in raw or []:
        label = str(item.get("label") or "").strip()[:60]
        if not label:
            raise bad_request("Every tax line needs a label.", "TAX_INVALID")
        rate = _decimal(item.get("rate_percent"), "rate_percent")
        if rate < 0 or rate > 100:
            raise bad_request("A tax rate is between 0 and 100 percent.", "TAX_INVALID")
        reverse = bool(item.get("reverse_charge"))
        amount = Decimal(0) if reverse else quantize_amount(subtotal * rate / Decimal(100), currency)
        total += amount
        taxes.append({"label": label, "rate_percent": format(rate.normalize(), "f"), "reverse_charge": reverse,
                      "amount": amount_str(amount, currency), "source": "ENTERED"})
    return taxes, total


TEAM_SUBMIT = "team_pay:submit"
MAX_TIMESHEETS = 26


def _timesheet_lines(db: Session, company_id: int, payee_id: int, timesheet_ids: Sequence[int], hourly_rate: Any,
                     currency: Optional[str]) -> tuple[list[dict], str, Decimal]:
    """Lines from APPROVED weekly timesheets × an entered hourly rate (owner decision 2026-10-29): the hours are the
    net worked minutes of the evaluated days of each approved week; a week is billed by at most one live invoice."""
    from app.models.attendance import AttendanceDay
    from app.models.attendance_requests import AttendanceTimesheetApproval

    if not timesheet_ids:
        raise bad_request("Choose at least one approved timesheet.", "TIMESHEETS_REQUIRED")
    if len(set(timesheet_ids)) != len(timesheet_ids) or len(timesheet_ids) > MAX_TIMESHEETS:
        raise bad_request(f"Choose up to {MAX_TIMESHEETS} distinct timesheets.", "TIMESHEETS_INVALID")
    rate = _decimal(hourly_rate, "hourly_rate")
    if rate <= 0:
        raise bad_request("The hourly rate is above zero.", "RATE_INVALID")
    if not currency:
        raise bad_request("Give the currency of the rate.", "CURRENCY_REQUIRED")
    sheets = {t.id: t for t in db.query(AttendanceTimesheetApproval).filter(
        AttendanceTimesheetApproval.company_id == company_id, AttendanceTimesheetApproval.id.in_(list(timesheet_ids)))}
    live = {s for (s,) in db.query(InvoiceSource.source_id).filter(
        InvoiceSource.company_id == company_id, InvoiceSource.source_type == "TIMESHEET",
        InvoiceSource.source_id.in_(list(timesheet_ids)), InvoiceSource.active.is_(True))}
    lines, subtotal = [], Decimal(0)
    for sheet_id in timesheet_ids:
        sheet = sheets.get(sheet_id)
        if sheet is None or sheet.employee_id != payee_id:
            raise not_found("Timesheet")
        if sheet.status != "APPROVED":
            raise conflict(f"Timesheet {sheet_id} is not approved.", "TIMESHEET_NOT_APPROVED")
        if sheet_id in live:
            raise conflict(f"Timesheet {sheet_id} is already on a live invoice.", "TIMESHEET_ALREADY_INVOICED")
        minutes = sum(int(m or 0) for (m,) in db.query(AttendanceDay.net_minutes).filter(
            AttendanceDay.company_id == company_id, AttendanceDay.employee_id == payee_id,
            AttendanceDay.shift_date >= sheet.period_start, AttendanceDay.shift_date <= sheet.period_end))
        hours = (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"))
        if hours <= 0:
            raise conflict(f"Timesheet {sheet_id} has no worked hours.", "TIMESHEET_EMPTY")
        amount = quantize_amount(hours * rate, currency)
        subtotal += amount
        lines.append({"timesheet_id": sheet.id,
                      "description": f"Hours worked {sheet.period_start.isoformat()} – {sheet.period_end.isoformat()}",
                      "quantity": format(hours.normalize(), "f"), "unit": "HOUR",
                      "rate": format(rate.normalize(), "f"), "amount": amount_str(amount, currency)})
    return lines, currency, subtotal


def _may_prepare(db: Session, principal: Principal, payee_id: int, legal_entity_id: int) -> bool:
    """Payroll staff (payment prepare over the entity) or the payee's manager / team lead (team_pay:submit)."""
    from app.core import authz

    if documents.has_staff_scope(principal, PREPARE) and _covers(principal, PREPARE, legal_entity_id):
        return True
    return principal.has(TEAM_SUBMIT) and principal.employee_id != payee_id and authz.can_access_employee(
        db, principal, TEAM_SUBMIT, payee_id)


def create_invoice(db: Session, principal: Principal, data: Mapping[str, Any]) -> Invoice:
    """A DRAFT invoice for a contractor from approved tasks or approved timesheets (EC-DOC-07). Payroll staff or the
    contractor's manager prepare it; issuing and approving stay with payroll / finance (maker-checker)."""
    mode = data.get("mode")
    if mode not in ("INVOICE", "SELF_BILLED"):
        raise bad_request("mode is INVOICE or SELF_BILLED.", "MODE_INVALID")
    payee_id = int(data["payee_employee_id"])
    today = _today(db, principal.company_id)
    if not (principal.has(TEAM_SUBMIT) or documents.has_staff_scope(principal, PREPARE)):
        raise forbidden()
    payee = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.id == payee_id).one_or_none()
    placement = _placement(db, principal.company_id, payee_id, today) if payee is not None else None
    if payee is None or placement is None or not _may_prepare(db, principal, payee_id, placement.legal_entity_id):
        raise not_found("Payee")
    _require_contractor(db, principal.company_id, payee_id, today)
    if mode == "INVOICE" and not (data.get("external_number") or "").strip():
        raise bad_request("A contractor invoice records the contractor's own invoice number.", "EXTERNAL_NUMBER_REQUIRED")
    source_type = "TIMESHEET" if data.get("timesheet_ids") else "TASK"
    if source_type == "TIMESHEET":
        lines, currency, subtotal = _timesheet_lines(db, principal.company_id, payee_id,
                                                     list(data.get("timesheet_ids") or []), data.get("hourly_rate"),
                                                     (data.get("currency") or "").upper() or None)
    else:
        lines, currency, subtotal = _task_lines(db, principal.company_id, payee_id, list(data.get("task_ids") or []))
    currencies.require_payroll_currency(currency)
    taxes, tax_total = _tax_lines(data.get("taxes") or [], subtotal, currency)
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id,
                                          LegalEntity.id == placement.legal_entity_id).one()
    seller_in = dict(data.get("seller") or {})
    seller = {"legal_name": payee.full_name, "address": (seller_in.get("address") or None),
              "tax_registration": (seller_in.get("tax_registration") or None)}
    buyer = {"legal_name": entity.legal_name, "code": entity.code,
             "registration_lines": list(entity.registration_lines or [])}
    row = Invoice(
        company_id=principal.company_id, kind="INVOICE", mode=mode, legal_entity_id=entity.id,
        payee_employee_id=payee_id, status="DRAFT", external_number=(data.get("external_number") or None),
        currency=currency, seller=seller, buyer=buyer, lines=lines, taxes=taxes, subtotal=subtotal,
        tax_total=tax_total, total=subtotal + tax_total, due_date=data.get("due_date"),
        payment_terms=(data.get("payment_terms") or None), notes=(data.get("notes") or None),
        created_by_user_id=principal.user_id, row_version=1,
    )
    db.add(row)
    db.flush()
    key = "timesheet_id" if source_type == "TIMESHEET" else "task_id"
    for line in lines:
        db.add(InvoiceSource(company_id=row.company_id, invoice_id=row.id, source_type=source_type,
                             source_id=line[key], active=True))
    db.flush()
    _audit(db, principal, "INVOICE_CREATED", row, f"Draft {mode.lower()} invoice for {payee.full_name}",
           source=source_type, sources=[line[key] for line in lines])
    return row


def list_for_team(db: Session, principal: Principal) -> list[dict]:
    """Contractor invoices of people in the caller's team_pay:read scope (managers follow their drafts through
    issue and approval) — never other people's."""
    from app.core import authz

    if not principal.has("team_pay:read") and not principal.has(TEAM_SUBMIT):
        raise forbidden()
    code = "team_pay:read" if principal.has("team_pay:read") else TEAM_SUBMIT
    scope = authz.employee_scope(db, principal, code)
    query = db.query(Invoice).filter(Invoice.company_id == principal.company_id)
    if not principal.has_company_scope(code):
        query = query.filter(or_(Invoice.created_by_user_id == principal.user_id,
                                 scope.clause(Invoice.payee_employee_id)))
    rows = query.order_by(Invoice.id.desc()).limit(200).all()
    names = {e.id: e.full_name for e in db.query(Employee).filter(
        Employee.company_id == principal.company_id, Employee.id.in_([r.payee_employee_id for r in rows] or [0]))}
    return [{**invoice_json(r), "payee_name": names.get(r.payee_employee_id)} for r in rows]


def approved_timesheets(db: Session, principal: Principal, payee_id: int) -> list[dict]:
    """The payee's APPROVED weeks not yet on a live invoice (picker of the timesheet source)."""
    from app.core import authz
    from app.models.attendance_requests import AttendanceTimesheetApproval

    if not (principal.has(TEAM_SUBMIT) and authz.can_access_employee(db, principal, TEAM_SUBMIT, payee_id)) and not (
            documents.has_staff_scope(principal, PREPARE)):
        raise not_found("Payee")
    live = {s for (s,) in db.query(InvoiceSource.source_id).filter(
        InvoiceSource.company_id == principal.company_id, InvoiceSource.source_type == "TIMESHEET",
        InvoiceSource.active.is_(True))}
    rows = (db.query(AttendanceTimesheetApproval)
            .filter(AttendanceTimesheetApproval.company_id == principal.company_id,
                    AttendanceTimesheetApproval.employee_id == payee_id,
                    AttendanceTimesheetApproval.status == "APPROVED")
            .order_by(AttendanceTimesheetApproval.period_start.desc()).limit(52).all())
    return [{"id": r.id, "period_start": r.period_start.isoformat(), "period_end": r.period_end.isoformat()}
            for r in rows if r.id not in live]


# --------------------------------------------------------------------------- the transition function


def load(db: Session, company_id: int, invoice_id: int, *, lock: bool = False) -> Invoice:
    query = db.query(Invoice).filter(Invoice.company_id == company_id, Invoice.id == invoice_id)
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.one_or_none()
    if row is None:
        raise not_found("Invoice")
    return row


def get_for_staff(db: Session, principal: Principal, invoice_id: int) -> Invoice:
    clause = _view_clause(principal)
    if clause is None or not any(documents.has_staff_scope(principal, p) for p in VIEW):
        raise forbidden()
    row = db.query(Invoice).filter(Invoice.company_id == principal.company_id, Invoice.id == invoice_id,
                                   clause).one_or_none()
    if row is None:
        raise not_found("Invoice")
    return row


def get_own(db: Session, principal: Principal, invoice_id: int) -> Invoice:
    row = db.query(Invoice).filter(Invoice.company_id == principal.company_id, Invoice.id == invoice_id,
                                   Invoice.payee_employee_id == (principal.employee_id or 0),
                                   Invoice.status != "DRAFT").one_or_none()
    if row is None:
        raise not_found("Invoice")
    return row


def _document_content(db: Session, row: Invoice, doc_type: str, *, credit_of: Optional[Invoice] = None,
                      agreement: Optional[SelfBillingAgreement] = None) -> dict:
    from app.payroll.services import pdf_render, registers

    content = registers.base_content(db, row.company_id, row.legal_entity_id, TITLES[doc_type])
    loc = content.get("locale") or "en"

    def fmt(value: Any) -> str:  # grouped for reading; the invoice row keeps the exact decimal strings
        return pdf_render.format_amount(value, row.currency, loc)

    seller, buyer = row.seller or {}, row.buyer or {}
    info = [["Seller", seller.get("legal_name")], ["Seller address", seller.get("address") or "—"],
            ["Seller tax registration", seller.get("tax_registration") or "—"], ["Buyer", buyer.get("legal_name")],
            ["Issue date", row.issue_date.isoformat() if row.issue_date else "—"],
            ["Due date", row.due_date.isoformat() if row.due_date else "—"]]
    if row.external_number:
        info.append(["Contractor's invoice no.", row.external_number])
    if credit_of is not None:
        info.append(["Credits invoice", credit_of.number])
    currency = row.currency
    tables = [{"title": "Lines", "columns": ["Description", "Quantity", "Unit", "Rate", f"Amount ({currency})"],
               "numeric": [False, True, False, True, True], "widths": [3, 1, 0.9, 1.1, 1.3],
               "rows": [[ln["description"], ln["quantity"], ln["unit"],
                         fmt(ln["rate"]) if Decimal(ln["rate"]) == quantize_amount(Decimal(ln["rate"]), currency)
                         else ln["rate"], fmt(ln["amount"])]
                        for ln in row.lines],
               "total_row": ["Subtotal", "", "", "", fmt(row.subtotal)]}]
    if row.taxes:
        tables.append({"title": "Taxes (as entered)", "columns": ["Tax", "Rate %", f"Amount ({currency})"],
                       "numeric": [False, True, True],
                       "rows": [[t["label"] + (" — reverse charge" if t.get("reverse_charge") else ""), t["rate_percent"],
                                 fmt(t["amount"])] for t in row.taxes]})
    tables.append({"title": "Total", "columns": ["", f"Amount ({currency})"], "numeric": [False, True],
                   "rows": [["Total" if doc_type != "CREDIT_NOTE" else "Total credited",
                             fmt(row.total)]]})
    notes = ["Contractor payment: no payslip and no employee statutory deductions."]
    if agreement is not None:
        notes.append(f"Self-billed invoice issued by the buyer under self-billing agreement {agreement.reference}.")
    if row.payment_terms:
        notes.append(f"Payment terms: {row.payment_terms}")
    content.update({"info": info, "tables": tables, "notes": notes})
    return content


def _issue_document(db: Session, principal: Principal, row: Invoice, doc_type: str, **kwargs: Any) -> PayDocument:
    from app.payroll.services import publication

    payee = db.query(Employee).filter(Employee.company_id == row.company_id, Employee.id == row.payee_employee_id).one()
    doc = documents.issue(
        db, principal, company_id=row.company_id, doc_type=doc_type, legal_entity_id=row.legal_entity_id,
        subject_type="INVOICE", subject_id=row.id, source_key=f"invoice:{row.id}",
        content=_document_content(db, row, doc_type, **kwargs), title=f"{TITLES[doc_type]} {row.number}",
        file_stem=f"{doc_type.lower().replace('_', '-')}-{payee.employee_code}", issue_date=row.issue_date,
        employee_id=row.payee_employee_id, number=row.number, allocate=False, template_version=f"{doc_type}:1",
        source_refs={"invoice_id": row.id}, publish="ON_ISSUE",
    )
    publication.notify(db, doc)
    return doc


def transition(db: Session, principal: Optional[Principal], invoice: Invoice | int, action: str, *,
               reason: Optional[str] = None, expected_version: Optional[int] = None,
               company_id: Optional[int] = None, evidence_ref: Optional[str] = None,
               **guards: bool) -> Invoice:
    """The single status change of an invoice (INVOICE machine), with its effects."""
    cid = company_id or (principal.company_id if principal is not None else None) or (
        invoice.company_id if isinstance(invoice, Invoice) else None)
    if cid is None:
        raise ValueError("a system transition needs the company")
    row = load(db, cid, invoice.id if isinstance(invoice, Invoice) else int(invoice), lock=True)
    try:
        spec = sm.INVOICE.spec(row.status, action)
    except sm.InvalidTransition as exc:
        raise AppError(409, f"This invoice cannot {action.lower()} from {row.status}.", exc.code)
    if spec.permission is None:
        if principal is not None:
            raise forbidden("This step is recorded from payment evidence only.")
    else:
        if principal is None:
            raise ValueError(f"{action} is a user action")
        _require_in_scope(principal, spec.permission, row.legal_entity_id)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("This invoice was changed by someone else; reload and try again.", "ROW_VERSION_CONFLICT")
    text = (reason or "").strip()
    if spec.reason_required and not text:
        raise bad_request("A reason is required.", "REASON_REQUIRED")
    now = clock.utcnow()
    before = row.status
    if action == "ISSUE":
        today = _today(db, cid)
        _require_contractor(db, cid, row.payee_employee_id, today)  # guard CONTRACTOR_PAYEE (EC-DOC-07)
        agreement = None
        if row.mode == "SELF_BILLED":  # guard SELF_BILLING_AGREEMENT
            agreement = active_agreement(db, cid, row.payee_employee_id, row.legal_entity_id, today)
            if agreement is None:
                raise conflict("A self-billed invoice needs an active self-billing agreement.", "NO_SELF_BILLING_AGREEMENT")
        _recheck_sources(db, row)
        doc_type = DOC_TYPE[(row.kind, row.mode)]
        # Effect ALLOCATE_GAPLESS_NUMBER: the sequence row is locked FOR UPDATE in this transaction (EC-DOC-08).
        row.number = documents.allocate_number(db, cid, row.legal_entity_id, doc_type, today)
        row.issue_date, row.issued_at, row.issued_by_user_id = today, now, principal.user_id
        row.status = spec.target
        row.row_version += 1
        db.flush()
        row.document_id = _issue_document(db, principal, row, doc_type, agreement=agreement).id
    elif action == "APPROVE":
        if row.issued_by_user_id == principal.user_id or row.created_by_user_id == principal.user_id:
            raise forbidden("Another person approves an invoice you prepared (maker-checker).")
        row.approved_by_user_id, row.approved_at = principal.user_id, now
        row.status = spec.target
        row.row_version += 1
    elif action == "RECORD_PAYMENT":
        if not guards.get("SETTLEMENT_EVIDENCE") or not evidence_ref:
            raise conflict("Payment is recorded from settlement evidence only.", "EVIDENCE_REQUIRED")
        row.paid_at, row.status_reason = now, f"Settled: {evidence_ref}"[:1000]
        row.status = spec.target
        row.row_version += 1
    elif action == "CREDIT":
        row.status, row.credited_at, row.status_reason = spec.target, now, text
        row.row_version += 1
        db.flush()
        _issue_credit_note(db, principal, row, text)
    else:  # pragma: no cover - every INVOICE action is handled above
        raise conflict("Unsupported invoice action.", "UNKNOWN_ACTION")
    db.flush()
    _audit(db, principal, f"INVOICE_{action}" if action != "RECORD_PAYMENT" else "INVOICE_PAID", row,
           f"Invoice {row.number or row.id}: {before} → {row.status}", reason=text or None, evidence_ref=evidence_ref)
    return row


def _recheck_sources(db: Session, row: Invoice) -> None:
    from app.models.work_items import Task

    from app.models.attendance_requests import AttendanceTimesheetApproval

    sources = db.query(InvoiceSource).filter(InvoiceSource.company_id == row.company_id,
                                             InvoiceSource.invoice_id == row.id, InvoiceSource.active.is_(True)).all()
    ids = [s.source_id for s in sources if s.source_type == "TASK"]
    for task in db.query(Task).filter(Task.company_id == row.company_id, Task.id.in_(ids or [0])):
        if not sm.task_is_payable(task.status) or task.paid_in_run_id is not None:
            raise conflict(f"Task {task.id} is no longer an approved, unpaid task.", "TASK_NOT_BILLABLE")
    sheets = [s.source_id for s in sources if s.source_type == "TIMESHEET"]
    for sheet in db.query(AttendanceTimesheetApproval).filter(AttendanceTimesheetApproval.company_id == row.company_id,
                                                              AttendanceTimesheetApproval.id.in_(sheets or [0])):
        if sheet.status != "APPROVED":  # reopened after drafting: the hours may have changed
            raise conflict(f"Timesheet {sheet.id} is no longer approved.", "TIMESHEET_NOT_APPROVED")


def _issue_credit_note(db: Session, principal: Principal, original: Invoice, reason: str) -> Invoice:
    """Effect ISSUE_CREDIT_NOTE: a gapless credit note for the full amount; the original's tasks are released."""
    today = _today(db, original.company_id)
    note = Invoice(
        company_id=original.company_id, kind="CREDIT_NOTE", mode=original.mode, legal_entity_id=original.legal_entity_id,
        payee_employee_id=original.payee_employee_id, status="ISSUED", credit_of_invoice_id=original.id,
        currency=original.currency, seller=dict(original.seller or {}), buyer=dict(original.buyer or {}),
        lines=list(original.lines or []), taxes=list(original.taxes or []), subtotal=original.subtotal,
        tax_total=original.tax_total, total=original.total, issue_date=today, issued_at=clock.utcnow(),
        issued_by_user_id=principal.user_id, created_by_user_id=principal.user_id,
        notes=f"Credits invoice {original.number}: {reason}"[:2000], row_version=1,
        number=documents.allocate_number(db, original.company_id, original.legal_entity_id, "CREDIT_NOTE", today),
    )
    db.add(note)
    db.flush()
    db.query(InvoiceSource).filter(InvoiceSource.company_id == original.company_id,
                                   InvoiceSource.invoice_id == original.id).update({"active": False},
                                                                                   synchronize_session=False)
    note.document_id = _issue_document(db, principal, note, "CREDIT_NOTE", credit_of=original).id
    db.flush()
    _audit(db, principal, "CREDIT_NOTE_ISSUED", note, f"Credit note {note.number} for invoice {original.number}",
           credit_of=original.id, reason=reason)
    return note


def record_payment(db: Session, company_id: int, invoice_id: int, evidence_ref: str) -> Invoice:
    """System hook for the payment services: an APPROVED invoice becomes PAID on settlement evidence."""
    return transition(db, None, invoice_id, "RECORD_PAYMENT", company_id=company_id, evidence_ref=evidence_ref,
                      SETTLEMENT_EVIDENCE=True)


# --------------------------------------------------------------------------- lists


def list_invoices(db: Session, principal: Principal, *, status: Optional[str] = None, kind: Optional[str] = None,
                  mode: Optional[str] = None, payee_employee_id: Optional[int] = None,
                  legal_entity_id: Optional[int] = None, skip: int = 0, limit: int = 50) -> dict:
    clause = _view_clause(principal)
    if clause is None or not any(documents.has_staff_scope(principal, p) for p in VIEW):
        raise forbidden()
    query = db.query(Invoice).filter(Invoice.company_id == principal.company_id, clause)
    for column, value in ((Invoice.status, status), (Invoice.kind, kind), (Invoice.mode, mode),
                          (Invoice.payee_employee_id, payee_employee_id), (Invoice.legal_entity_id, legal_entity_id)):
        if value is not None:
            query = query.filter(column == value)
    total = query.count()
    rows = query.order_by(Invoice.id.desc()).offset(skip).limit(limit).all()
    names = documents.employee_names(db, principal.company_id, [r.payee_employee_id for r in rows])
    return {"total": total, "items": [{**invoice_json(r), "payee": names.get(r.payee_employee_id)} for r in rows]}


def list_own(db: Session, principal: Principal) -> list[dict]:
    if principal.employee_id is None:
        return []
    rows = (db.query(Invoice).filter(Invoice.company_id == principal.company_id,
                                     Invoice.payee_employee_id == principal.employee_id, Invoice.status != "DRAFT")
            .order_by(Invoice.id.desc()).limit(200).all())
    return [invoice_json(r, for_self=True) for r in rows]


# --------------------------------------------------------------------------- self-billing agreements


def agreement_json(row: SelfBillingAgreement) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "legal_entity_id": row.legal_entity_id,
            "reference": row.reference, "effective_from": row.effective_from.isoformat(),
            "effective_to": row.effective_to.isoformat() if row.effective_to else None, "status": row.status}


def create_agreement(db: Session, principal: Principal, *, employee_id: int, legal_entity_id: int, reference: str,
                     effective_from: date, effective_to: Optional[date]) -> SelfBillingAgreement:
    _require_in_scope(principal, PREPARE, legal_entity_id, "Legal entity")
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id,
                                          LegalEntity.id == legal_entity_id).one_or_none()
    employee = db.query(Employee).filter(Employee.company_id == principal.company_id,
                                         Employee.id == employee_id).one_or_none()
    if entity is None:
        raise not_found("Legal entity")
    if employee is None:
        raise not_found("Payee")
    # The payee must be a contractor now (or when a future agreement starts); an agreement may be dated back to
    # the start of the engagement.
    _require_contractor(db, principal.company_id, employee_id, max(effective_from, _today(db, principal.company_id)))
    if effective_to is not None and effective_to < effective_from:
        raise bad_request("The agreement ends before it starts.", "DATES_INVALID")
    text = (reference or "").strip()
    if not text:
        raise bad_request("An agreement reference is required.", "REFERENCE_REQUIRED")
    row = SelfBillingAgreement(company_id=principal.company_id, employee_id=employee_id, legal_entity_id=legal_entity_id,
                               reference=text[:100], effective_from=effective_from, effective_to=effective_to,
                               status="ACTIVE", created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, audit.PAYMENTS, "SELF_BILLING_AGREEMENT_ADDED",
                        f"Self-billing agreement {row.reference}", "self_billing_agreement", row.id,
                        details=agreement_json(row))
    return row


def revoke_agreement(db: Session, principal: Principal, agreement_id: int, reason: str) -> SelfBillingAgreement:
    row = (db.query(SelfBillingAgreement).filter(SelfBillingAgreement.company_id == principal.company_id,
                                                 SelfBillingAgreement.id == agreement_id).with_for_update().one_or_none())
    if row is None:
        raise not_found("Self-billing agreement")
    _require_in_scope(principal, PREPARE, row.legal_entity_id, "Self-billing agreement")
    if row.status != "ACTIVE":
        raise conflict("The agreement is already revoked.", "ALREADY_REVOKED")
    text = (reason or "").strip()
    if not text:
        raise bad_request("A reason is required.", "REASON_REQUIRED")
    row.status = "REVOKED"
    db.flush()
    AuditLogService.log(db, principal, audit.PAYMENTS, "SELF_BILLING_AGREEMENT_REVOKED",
                        f"Self-billing agreement {row.reference} revoked", "self_billing_agreement", row.id,
                        details={"reason": text})
    return row


def list_agreements(db: Session, principal: Principal, *, employee_id: Optional[int] = None) -> list[dict]:
    documents.require_staff(principal, PREPARE)
    query = db.query(SelfBillingAgreement).filter(
        SelfBillingAgreement.company_id == principal.company_id,
        access.run_scope_clause(principal, PREPARE, SelfBillingAgreement.legal_entity_id))
    if employee_id is not None:
        query = query.filter(SelfBillingAgreement.employee_id == employee_id)
    return [agreement_json(r) for r in query.order_by(SelfBillingAgreement.id.desc()).limit(500)]


__all__ = [
    "active_agreement", "agreement_json", "create_agreement", "create_invoice", "get_for_staff", "get_own",
    "invoice_json", "list_agreements", "list_invoices", "list_own", "load", "record_payment", "revoke_agreement",
    "transition", "worker_class",
]
