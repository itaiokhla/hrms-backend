"""Registers, statements, payment advice, certificates and letters (3.6 §2, §11; EC-DOC-10, EC-DOC-21).

Every document here is a frozen view model rendered by ``pdf_render`` (generic layout) and issued through
``documents.issue`` (numbering, private storage, SHA-256, immutability, audit):

* payroll register (PAYROLL_REGISTER): every employee's totals and the component totals per currency.
  A run that is not finalized only gets a **draft** PDF rendered on the fly with a "DRAFT — not issued"
  watermark (never numbered, never stored); a finalized run gets one numbered, immutable register;
* funding request (FUNDING_REQUEST, on approval): net pay, employee statutory deductions and employer
  contributions per currency, and the entity's active payroll funding accounts (masked);
* bank transfer report (BANK_TRANSFER_REPORT, per payment batch): one row per instruction with the masked
  account, amount, currency and reference; a new version when the batch's items changed;
* payment advice (PAYMENT_ADVICE): "Payment instruction" only once the instruction was submitted,
  "Payment confirmation" only once it is settled with verified evidence; never without the matching
  payment state (EC-DOC-10);
* final settlement statement (FINAL_SETTLEMENT_STATEMENT): every settlement line with its calculation
  and the payment due date, for finalized final-settlement runs;
* salary certificate / employment letter on an approved request (``document_requests``: the employee
  asks, another person with ``payroll:payslip:publish`` decides; amounts only when the request includes
  them and the decider may read compensation), increment letter from an approved compensation revision,
  loan and reimbursement statements from the ledgers (unnumbered snapshots).

Employee-facing documents are published on issue (trigger ON_ISSUE) and notified once. Bank numbers are
never decrypted here: only the masked values stored with instructions and accounts are printed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.pay_documents import DocumentRequest, PayDocument
from app.models.pay_runs import PayPeriod, PayrollLine, PayrollRun, PayrollRunEmployee
from app.payroll import audit
from app.payroll import state_machines as sm
from app.payroll.banking import masking
from app.payroll.money import amount_str
from app.payroll.services import access, documents, payslips
from app.services.audit_log_service import AuditLogService

REPORTS_READ = "payroll:reports:read"
PREPARE = sm.PAYMENT_PREPARE
PUBLISH = documents.PUBLISH
COMPENSATION_READ = "payroll:compensation:read"
LOANS_READ = "payroll:loans:read"
REIMBURSEMENTS_READ = "payroll:reimbursements:read"
RESULT_STATES = frozenset(("CALCULATED", "VALIDATION_FAILED", "REVIEW_REQUIRED", "APPROVAL_PENDING", "APPROVED",
                           "REJECTED", "REOPENED_FOR_CORRECTION"))
FUNDING_STATES = frozenset(("APPROVED", *sm.RUN_FINALIZED_STATES))
SUBMITTED_STATES = frozenset(("SUBMITTED", "ACKNOWLEDGED", "PROCESSING", "PARTIALLY_SETTLED", "SETTLED", "RECONCILED"))
SETTLED_STATES = frozenset(("SETTLED", "RECONCILED"))
ADVICE_KINDS = ("INSTRUCTION", "CONFIRMATION")


# --------------------------------------------------------------------------- shared


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def _entity(db: Session, company_id: int, legal_entity_id: int) -> LegalEntity:
    return db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.id == legal_entity_id).one()


def base_content(db: Session, company_id: int, legal_entity_id: int, title: str, *, subtitle: Optional[str] = None,
                 languages: Optional[Sequence[str]] = None) -> dict:
    """Brand, employer and locale of a generic document of the legal entity."""
    from app.models.company import Company
    from app.services import brand_service, domain_service

    entity = _entity(db, company_id, legal_entity_id)
    langs = payslips.languages_of(list(languages) if languages else None, db.get(Company, company_id), entity)
    return {
        "kind": "GENERIC", "schema": 1, "title": title, "subtitle": subtitle,
        "brand": brand_service.document_brand(company_id, entity.id), "employer": payslips._employer(entity),
        "languages": langs[:1], "locale": payslips.number_locale_for(langs, entity),
        "verify_base": domain_service.public_base_url(company_id), "info": [], "paragraphs": [], "tables": [],
        "notes": [], "labels": [],
    }


def _money(value: Any, currency: str) -> str:
    return amount_str(Decimal(value or 0), currency)


def _staff_run(db: Session, principal: Principal, run_id: int, permission: str) -> PayrollRun:
    documents.require_staff(principal, permission)
    return access.get_in_scope(db, principal, permission, PayrollRun, run_id, "Payroll run")


def _period(db: Session, run: PayrollRun) -> PayPeriod:
    return db.query(PayPeriod).filter(PayPeriod.company_id == run.company_id, PayPeriod.id == run.pay_period_id).one()


def _employees(db: Session, company_id: int, ids: Iterable[int]) -> dict[int, Employee]:
    wanted = sorted({i for i in ids if i is not None})
    if not wanted:
        return {}
    return {e.id: e for e in db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(wanted))}


def _next_version(db: Session, company_id: int, doc_type: str, source_key: str) -> int:
    latest = documents.latest_version(db, company_id, doc_type, source_key)
    return 1 if latest is None else latest.version + 1


def _issue(db: Session, principal: Optional[Principal], content: dict, *, doc_type: str, legal_entity_id: int,
           subject_type: str, subject_id: int, source_key: str, title: str, file_stem: str, version: int = 1,
           publish: Optional[str] = None, company_id: Optional[int] = None, **extra: Any) -> PayDocument:
    """Issue a generic document for the caller's tenant (or ``company_id`` for a system job); employee-facing
    ones published on issue are notified."""
    from app.payroll.services import publication

    cid = principal.company_id if principal is not None else company_id
    if cid is None:
        raise ValueError("a system issue needs the company")
    doc = documents.issue(
        db, principal, company_id=cid, doc_type=doc_type, legal_entity_id=legal_entity_id,
        subject_type=subject_type, subject_id=subject_id, source_key=source_key, content=content, title=title,
        file_stem=file_stem, version=version, issue_date=_today(db, cid),
        template_version=f"{doc_type}:1", locale=content.get("locale") or "en",
        languages=content.get("languages") or ["en"], labels=content.get("labels") or [], publish=publish, **extra,
    )
    if doc.status != "GENERATED" and doc.employee_id is not None:
        publication.notify(db, doc)
    return doc


def run_labels(db: Session, run: PayrollRun) -> list[str]:
    period = _period(db, run)
    entity = _entity(db, run.company_id, run.legal_entity_id)
    lines = db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id, PayrollLine.run_id == run.id).all()
    return payslips.statutory_labels(db, run, period, entity, lines)


# --------------------------------------------------------------------------- payroll register


def register_content(db: Session, run: PayrollRun, *, draft: bool) -> dict:
    period = _period(db, run)
    content = base_content(db, run.company_id, run.legal_entity_id, "Payroll register",
                           subtitle=f"{run.label} · {run.number}")
    # A FULL hold (ON_HOLD) keeps the employee out of finalization: a finalized register lists and totals the
    # finalized employees only; a draft lists held employees too but never counts them in the totals.
    statuses = ("CALCULATED",) if sm.is_run_finalized(run.status) else ("CALCULATED", "ON_HOLD")
    rows = (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
                    PayrollRunEmployee.status.in_(statuses))
            .order_by(PayrollRunEmployee.id).all())
    employees = _employees(db, run.company_id, [r.employee_id for r in rows])
    by_currency: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    table_rows = []
    held = 0
    for row in rows:
        employee = employees.get(row.employee_id)
        name = employee.full_name if employee else ""
        if row.status != "CALCULATED":
            held += 1
            name = f"{name} (on hold — not in totals)"
        table_rows.append([employee.employee_code if employee else "", name,
                           row.currency, _money(row.gross, row.currency), _money(row.deductions, row.currency),
                           _money(row.reimbursements, row.currency), _money(row.net, row.currency),
                           _money(row.employer_cost, row.currency)])
        if row.status != "CALCULATED":
            continue
        bucket = by_currency[row.currency]
        for figure in ("gross", "deductions", "reimbursements", "net", "employer_cost"):
            bucket[figure] += Decimal(getattr(row, figure) or 0)
    components: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for code, kind, currency, total in (
        db.query(PayrollLine.component_code, PayrollLine.kind, PayrollLine.currency, func.sum(PayrollLine.amount))
        .join(PayrollRunEmployee, (PayrollRunEmployee.company_id == PayrollLine.company_id)
              & (PayrollRunEmployee.id == PayrollLine.run_employee_id))
        .filter(PayrollLine.company_id == run.company_id, PayrollLine.run_id == run.id,
                PayrollRunEmployee.status == "CALCULATED")
        .group_by(PayrollLine.component_code, PayrollLine.kind, PayrollLine.currency)
    ):
        components[(code, kind, currency)] += Decimal(total)
    content.update({
        "draft": draft,
        "info": [["Run", run.number], ["Run type", run.run_type.replace("_", " ").title()], ["Status", run.status],
                 ["Pay period", f"{period.period_start.isoformat()} – {period.period_end.isoformat()}"],
                 ["Pay date", period.pay_date.isoformat()], ["Employees", str(len(rows))]],
        "compact": True,
        "tables": [
            {"title": "Employees", "columns": ["Code", "Name", "Cur.", "Gross", "Deductions", "Reimb.", "Net",
                                               "Employer cost"],
             "numeric": [False, False, False, True, True, True, True, True], "widths": [1, 2.2, 0.7, 1.3, 1.3, 1.1,
                                                                                         1.3, 1.3],
             "rows": table_rows},
            {"title": "Totals per currency", "columns": ["Currency", "Gross", "Deductions", "Reimbursements", "Net",
                                                         "Employer cost"],
             "numeric": [False, True, True, True, True, True],
             "rows": [[cur, *[_money(t[n], cur) for n in ("gross", "deductions", "reimbursements", "net",
                                                          "employer_cost")]] for cur, t in sorted(by_currency.items())]},
            {"title": "Components", "columns": ["Component", "Kind", "Currency", "Total"],
             "numeric": [False, False, False, True],
             "rows": [[code, kind.replace("_", " ").title(), cur, _money(total, cur)]
                      for (code, kind, cur), total in sorted(components.items())]},
        ],
        "labels": run_labels(db, run),
        "notes": (["Draft: figures may still change until the run is finalized."] if draft else [])
        + ([f"{held} employee(s) on a full hold are listed but not included in the totals."] if held else []),
    })
    return content


def issue_register(db: Session, principal: Principal, run_id: int) -> PayDocument:
    run = _staff_run(db, principal, run_id, REPORTS_READ)
    if not sm.is_run_finalized(run.status):
        raise conflict("The register is issued when the run is finalized; download the draft meanwhile.",
                       "RUN_NOT_FINALIZED")
    key = f"run:{run.id}"
    existing = documents.find_version(db, run.company_id, "PAYROLL_REGISTER", key, 1)
    if existing is not None:
        return existing
    period = _period(db, run)
    return _issue(db, principal, register_content(db, run, draft=False), doc_type="PAYROLL_REGISTER",
                  legal_entity_id=run.legal_entity_id, subject_type="RUN", subject_id=run.id, source_key=key,
                  title=f"Payroll register — {run.label}", file_stem=f"payroll-register-{run.number}",
                  pay_group_id=run.pay_group_id, run_id=run.id, period=(period.period_start, period.period_end),
                  source_refs={"run_id": run.id})


def draft_register_pdf(db: Session, principal: Principal, run_id: int) -> tuple[bytes, str]:
    """A watermarked draft register of a calculated run (never numbered, never stored)."""
    from app.payroll.services import pdf_render

    run = _staff_run(db, principal, run_id, REPORTS_READ)
    if sm.is_run_finalized(run.status):
        raise conflict("The run is finalized; use the issued register.", "RUN_FINALIZED")
    if run.status not in RESULT_STATES:
        raise conflict("The run has no calculation results yet.", "RUN_NOT_CALCULATED")
    content = documents.jsonable({**register_content(db, run, draft=True), "number": None,
                                  "issue_date": _today(db, run.company_id), "doc_type": "PAYROLL_REGISTER"})
    data, _ = pdf_render.render_with_brand(content)
    AuditLogService.log(db, principal, audit.PAYSLIPS, "DRAFT_REGISTER_DOWNLOADED",
                        f"Draft register of run {run.number} downloaded", "payroll_run", run.id,
                        details={"run_id": run.id})
    return data, documents.safe_file_name(f"payroll-register-{run.number}-DRAFT")


# --------------------------------------------------------------------------- funding request


def issue_funding_request(db: Session, principal: Principal, run_id: int) -> PayDocument:
    from app.models.payments import FundingAccount

    run = _staff_run(db, principal, run_id, REPORTS_READ)
    if run.status not in FUNDING_STATES:
        raise conflict("A funding request is issued once the run is approved.", "RUN_NOT_APPROVED")
    key = f"run:{run.id}"
    existing = documents.find_version(db, run.company_id, "FUNDING_REQUEST", key, 1)
    if existing is not None:
        return existing
    figures: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                                   PayrollRunEmployee.run_id == run.id,
                                                   PayrollRunEmployee.status == "CALCULATED"):
        net = Decimal(row.net or 0)
        if net > 0:
            figures[row.currency]["net"] += net
    for kind, statutory, currency, total in (
        db.query(PayrollLine.kind, PayrollLine.statutory, PayrollLine.currency, func.sum(PayrollLine.amount))
        .join(PayrollRunEmployee, (PayrollRunEmployee.company_id == PayrollLine.company_id)
              & (PayrollRunEmployee.id == PayrollLine.run_employee_id))
        .filter(PayrollLine.company_id == run.company_id, PayrollLine.run_id == run.id,
                PayrollRunEmployee.status == "CALCULATED", PayrollLine.kind.in_(("DEDUCTION", "EMPLOYER_CONTRIBUTION")))
        .group_by(PayrollLine.kind, PayrollLine.statutory, PayrollLine.currency)
    ):
        if kind == "DEDUCTION" and statutory:
            figures[currency]["employee_statutory"] += Decimal(total)
        elif kind == "EMPLOYER_CONTRIBUTION":
            figures[currency]["employer"] += Decimal(total)
    content = base_content(db, run.company_id, run.legal_entity_id, "Funding request",
                           subtitle=f"{run.label} · {run.number}")
    rows = []
    for cur, f in sorted(figures.items()):
        total = f["net"] + f["employee_statutory"] + f["employer"]
        rows.append([cur, _money(f["net"], cur), _money(f["employee_statutory"], cur), _money(f["employer"], cur),
                     _money(0, cur), _money(total, cur)])
    accounts = (db.query(FundingAccount)
                .filter(FundingAccount.company_id == run.company_id, FundingAccount.legal_entity_id == run.legal_entity_id,
                        FundingAccount.purpose == "PAYROLL", FundingAccount.status == "ACTIVE")
                .order_by(FundingAccount.id).all())
    period = _period(db, run)
    content.update({
        "info": [["Run", run.number], ["Pay date", period.pay_date.isoformat()], ["Status", run.status]],
        "tables": [
            {"title": "Funds required", "columns": ["Currency", "Net pay", "Employee statutory", "Employer contrib.",
                                                    "Fees", "Total"],
             "numeric": [False, True, True, True, True, True], "rows": rows},
            {"title": "Payroll funding accounts", "columns": ["Account", "Bank", "Currency"],
             "numeric": [False, False, False],
             "rows": [[f"{a.name} {masking.MASK}{a.last4}", a.bank_name or "", a.currency] for a in accounts]},
        ],
        "notes": ["Fees: none recorded in the system.",
                  "Statutory amounts are as calculated; see the labels for their verification status."],
        "labels": run_labels(db, run),
    })
    return _issue(db, principal, content, doc_type="FUNDING_REQUEST", legal_entity_id=run.legal_entity_id,
                  subject_type="RUN", subject_id=run.id, source_key=key, title=f"Funding request — {run.label}",
                  file_stem=f"funding-request-{run.number}", pay_group_id=run.pay_group_id, run_id=run.id,
                  period=(period.period_start, period.period_end), source_refs={"run_id": run.id})


# --------------------------------------------------------------------------- bank transfer report


def issue_bank_transfer_report(db: Session, principal: Principal, batch_id: int) -> PayDocument:
    from app.models.payment_flows import PaymentBatch, PaymentInstruction

    documents.require_staff(principal, PREPARE)
    batch = access.get_in_scope(db, principal, PREPARE, PaymentBatch, batch_id, "Payment batch", pay_group_attr=None)
    instructions = (db.query(PaymentInstruction)
                    .filter(PaymentInstruction.company_id == batch.company_id, PaymentInstruction.batch_id == batch.id)
                    .order_by(PaymentInstruction.id).all())
    employees = _employees(db, batch.company_id, [i.employee_id for i in instructions])
    totals: dict[str, Decimal] = defaultdict(Decimal)
    rows = []
    for item in instructions:
        snapshot = item.beneficiary_snapshot or {}
        employee = employees.get(item.employee_id)
        rows.append([item.reference or "", employee.employee_code if employee else "",
                     snapshot.get("holder_name_masked") or masking.mask_name(employee.full_name if employee else ""),
                     snapshot.get("account_masked") or masking.MASK, item.currency, _money(item.amount, item.currency),
                     item.status])
        totals[item.currency] += Decimal(item.amount)
    content = base_content(db, batch.company_id, batch.legal_entity_id, "Bank transfer report",
                           subtitle=f"Batch {batch.reference}")
    content.update({
        "info": [["Batch", batch.reference], ["Status", batch.status], ["Method", batch.method],
                 ["Value date", batch.value_date.isoformat() if batch.value_date else "—"],
                 ["Items", str(len(rows))]],
        "compact": True,
        "tables": [{"title": "Transfers", "columns": ["Reference", "Employee", "Beneficiary", "Account", "Cur.",
                                                      "Amount", "Status"],
                    "numeric": [False, False, False, False, False, True, False], "widths": [1.5, 1, 1.6, 1.4, 0.6, 1.3, 1.2],
                    "rows": rows},
                   {"title": "Totals", "columns": ["Currency", "Amount"], "numeric": [False, True],
                    "rows": [[cur, _money(total, cur)] for cur, total in sorted(totals.items())]}],
        "notes": ["Account numbers are masked. Full numbers never appear in documents."],
    })
    key = f"batch:{batch.id}"
    latest = documents.latest_version(db, batch.company_id, "BANK_TRANSFER_REPORT", key)
    if latest is not None:
        previous = {k: latest.content.get(k) for k in ("info", "tables")}
        if documents.content_hash(previous) == documents.content_hash(documents.jsonable(
                {k: content[k] for k in ("info", "tables")})):
            return latest
    return _issue(db, principal, content, doc_type="BANK_TRANSFER_REPORT", legal_entity_id=batch.legal_entity_id,
                  subject_type="BATCH", subject_id=batch.id, source_key=key,
                  version=1 if latest is None else latest.version + 1, title=f"Bank transfer report — {batch.reference}",
                  file_stem=f"bank-transfer-{batch.reference}", batch_id=batch.id, run_id=batch.run_id,
                  source_refs={"batch_id": batch.id})


# --------------------------------------------------------------------------- payment advice (EC-DOC-10)


def issue_payment_advice(db: Session, principal: Principal, instruction_id: int, kind: str) -> PayDocument:
    """"Payment instruction" after submission, "Payment confirmation" after verified settlement; never earlier."""
    from app.models.payment_flows import PaymentInstruction, PaymentObligation

    if kind not in ADVICE_KINDS:
        raise bad_request("kind is INSTRUCTION or CONFIRMATION.", "ADVICE_KIND_INVALID")
    documents.require_staff(principal, PREPARE)
    item = access.get_in_scope(db, principal, PREPARE, PaymentInstruction, instruction_id, "Payment instruction",
                               pay_group_attr=None)
    if kind == "INSTRUCTION" and not (item.status in SUBMITTED_STATES and item.submitted_at is not None):
        raise conflict("A payment instruction advice needs a submitted payment.", "PAYMENT_NOT_SUBMITTED")
    if kind == "CONFIRMATION" and not (item.status in SETTLED_STATES
                                       and item.evidence_status == sm.EvidenceStatus.VERIFIED.value):
        raise conflict("A payment confirmation needs a settled payment with verified evidence.",
                       "PAYMENT_NOT_SETTLED")
    key = f"instruction:{item.id}:{kind.lower()}"
    existing = documents.find_version(db, item.company_id, "PAYMENT_ADVICE", key, 1)
    if existing is not None:
        return existing
    obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == item.company_id,
                                                    PaymentObligation.id == item.obligation_id).one()
    employee = db.query(Employee).filter(Employee.company_id == item.company_id, Employee.id == item.employee_id).one()
    snapshot = item.beneficiary_snapshot or {}
    title = "Payment instruction" if kind == "INSTRUCTION" else "Payment confirmation"
    content = base_content(db, item.company_id, item.legal_entity_id, title)
    info = [["Employee", employee.full_name], ["Employee ID", employee.employee_code],
            ["Beneficiary", snapshot.get("holder_name_masked") or masking.mask_name(employee.full_name)],
            ["Account", snapshot.get("account_masked") or masking.MASK],
            ["Amount", f"{item.currency} {_money(item.amount, item.currency)}"],
            ["Method", (item.submission_channel or "").replace("_", " ").title()],
            ["Reference", item.reference or "—"],
            ["Submitted on", item.submitted_at.date().isoformat() if item.submitted_at else "—"]]
    if kind == "CONFIRMATION":
        settled = item.settled_amount if item.settled_amount is not None else item.amount
        info += [["Settled on", item.settled_at.date().isoformat() if item.settled_at else "—"],
                 ["Settled amount", f"{item.currency} {_money(settled, item.currency)}"],
                 ["Evidence reference", f"{masking.MASK}{(item.evidence_ref or '')[-4:]}" if item.evidence_ref else "—"]]
    if item.submission_channel == sm.SubmissionChannel.SANDBOX.value:
        content["labels"] = ["SANDBOX — no real money moved"]
    content["info"] = info
    return _issue(db, principal, content, doc_type="PAYMENT_ADVICE", legal_entity_id=item.legal_entity_id,
                  subject_type="INSTRUCTION", subject_id=item.id, source_key=key, title=title,
                  file_stem=f"payment-{kind.lower()}-{employee.employee_code}", employee_id=employee.id,
                  pay_group_id=obligation.pay_group_id, run_id=item.run_id, batch_id=item.batch_id,
                  instruction_id=item.id, source_refs={"instruction_id": item.id, "kind": kind}, publish="ON_ISSUE")


# --------------------------------------------------------------------------- final settlement statement


def issue_final_settlement(db: Session, principal: Principal, run_id: int) -> list[PayDocument]:
    run = _staff_run(db, principal, run_id, PUBLISH)
    return final_settlement_documents(db, principal, run)


def _settlement_cases(db: Session, run: PayrollRun) -> dict[int, Any]:
    """employee id -> the final settlement case settled by ``run`` (corrections milestone), when one exists."""
    from app.models.corrections import FinalSettlementCase

    return {case.employee_id: case for case in db.query(FinalSettlementCase).filter(
        FinalSettlementCase.company_id == run.company_id, FinalSettlementCase.run_id == run.id)}


SETTLEMENT_KINDS = ("EARNING", "DEDUCTION", "REIMBURSEMENT", "ROUNDING")


def final_settlement_documents(db: Session, principal: Optional[Principal], run: PayrollRun,
                               *, employee_ids: Optional[Iterable[int]] = None) -> list[PayDocument]:
    """The full-and-final statement of every settled employee of a finalized FINAL_SETTLEMENT run (idempotent).

    Called by HR (``issue_final_settlement``) or by the ``payroll.final_settlement.finalized`` event (system,
    ``principal`` None). Last working day and payment due date come from the settlement case when the
    corrections milestone created one; otherwise from the employee record and the run's pay date.
    """
    if run.run_type != "FINAL_SETTLEMENT" or not sm.is_run_finalized(run.status):
        raise conflict("Final settlement statements come from finalized final-settlement runs.",
                       "NOT_A_FINALIZED_SETTLEMENT")
    period = _period(db, run)
    issued = []
    query = (db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                                 PayrollRunEmployee.run_id == run.id,
                                                 PayrollRunEmployee.status == "CALCULATED"))
    if employee_ids is not None:
        query = query.filter(PayrollRunEmployee.employee_id.in_(list(employee_ids)))
    rows = query.order_by(PayrollRunEmployee.id).all()
    employees = _employees(db, run.company_id, [r.employee_id for r in rows])
    cases = _settlement_cases(db, run)
    for row in rows:
        key = f"run_employee:{row.id}"
        existing = documents.find_version(db, run.company_id, "FINAL_SETTLEMENT_STATEMENT", key, 1)
        if existing is not None:
            issued.append(existing)
            continue
        employee = employees[row.employee_id]
        case = cases.get(row.employee_id)
        last_day = case.last_working_day if case is not None else employee.termination_date
        due = (case.due_date if case is not None and case.due_date else None) or period.pay_date
        lines = (db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id,
                                              PayrollLine.run_employee_id == row.id,
                                              PayrollLine.visibility == "EMPLOYEE_VISIBLE")
                 .order_by(PayrollLine.sort_order, PayrollLine.id).all())
        content = base_content(db, run.company_id, run.legal_entity_id, "Final settlement statement")
        # Settlement items in the payee's perspective (paid +, recovered −) so they add up to the net settlement;
        # employer contributions are shown apart, never as part of the settlement (3.6 §3, payslip rule).
        items = [line for line in lines if line.kind in SETTLEMENT_KINDS]
        employer = [line for line in lines if line.kind == "EMPLOYER_CONTRIBUTION"]
        tables = [{"title": "Settlement items", "columns": ["Item", "Type", "Calculation", f"Amount ({row.currency})"],
                   "numeric": [False, False, False, True], "widths": [1.6, 1, 3, 1.3],
                   "rows": [[line.component_code, line.kind.replace("_", " ").title(), line.explanation or "",
                             _money(-Decimal(line.amount) if line.kind == "DEDUCTION" else line.amount,
                                    line.currency)] for line in items],
                   "total_row": ["Net settlement", "", "", _money(row.net, row.currency)]}]
        if employer:
            tables.append({"title": payslips.EMPLOYER_NOTE, "columns": ["Item", f"Amount ({row.currency})"],
                           "numeric": [False, True], "widths": [4.6, 1.3],
                           "rows": [[line.component_code, _money(line.amount, line.currency)] for line in employer]})
        content.update({
            "info": [["Employee", employee.full_name], ["Employee ID", employee.employee_code],
                     ["Last working day", last_day.isoformat() if last_day else "—"],
                     ["Payment due date", due.isoformat()], ["Run", run.number]],
            "tables": tables,
            "labels": run_labels(db, run),
        })
        if case is not None and case.due_note:
            content["notes"] = [f"Payment due: {case.due_note}"]
        issued.append(_issue(db, principal, content, doc_type="FINAL_SETTLEMENT_STATEMENT",
                             legal_entity_id=run.legal_entity_id, subject_type="EMPLOYEE", subject_id=employee.id,
                             source_key=key, title="Final settlement statement",
                             file_stem=f"final-settlement-{employee.employee_code}", employee_id=employee.id,
                             pay_group_id=run.pay_group_id, run_id=run.id, run_employee_id=row.id,
                             period=(period.period_start, period.period_end), publish="ON_ISSUE",
                             company_id=run.company_id,
                             source_refs={"run_id": run.id, "run_employee_id": row.id,
                                          "case_id": case.id if case is not None else None}))
    return issued


# --------------------------------------------------------------------------- employee scope helpers


def _employee_placement(db: Session, company_id: int, employee_id: int, on: date):
    from app.payroll.services import settings as settings_service

    return settings_service.primary_assignment(db, company_id, employee_id, on)


def _staff_employee(db: Session, principal: Principal, permission: str, employee_id: int) -> Employee:
    """An employee the caller reaches beyond SELF with ``permission`` (403 without it, 404 outside scope)."""
    documents.require_staff(principal, permission)
    today = _today(db, principal.company_id)
    access.ensure_employee_in_scope(db, principal, permission, employee_id, today, today)
    return db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.id == employee_id).one()


def _placement_or_404(db: Session, company_id: int, employee_id: int, on: date):
    placement = _employee_placement(db, company_id, employee_id, on)
    if placement is None:
        raise conflict("The employee has no placement on this date.", "NOT_PLACED")
    return placement


# --------------------------------------------------------------------------- increment letter


def issue_increment_letter(db: Session, principal: Principal, compensation_assignment_id: int, *,
                           employee_id: Optional[int] = None) -> PayDocument:
    from app.models.compensation import CompensationAssignment

    row = db.query(CompensationAssignment).filter(CompensationAssignment.company_id == principal.company_id,
                                                  CompensationAssignment.id == compensation_assignment_id).one_or_none()
    if row is None or (employee_id is not None and row.employee_id != employee_id):
        raise not_found("Compensation")
    employee = _staff_employee(db, principal, COMPENSATION_READ, row.employee_id)
    _staff_employee(db, principal, PUBLISH, row.employee_id)
    if row.status not in ("SCHEDULED", "ACTIVE", "SUPERSEDED") or row.previous_assignment_id is None:
        raise conflict("An increment letter needs an approved revision of an earlier compensation.",
                       "NOT_A_REVISION")
    previous = db.query(CompensationAssignment).filter(CompensationAssignment.company_id == row.company_id,
                                                       CompensationAssignment.id == row.previous_assignment_id).one()
    key = f"compensation:{row.id}"
    existing = documents.find_version(db, row.company_id, "INCREMENT_LETTER", key, 1)
    if existing is not None:
        return existing
    placement = _placement_or_404(db, row.company_id, row.employee_id, row.effective_from)
    content = base_content(db, row.company_id, placement.legal_entity_id, "Increment letter")
    old = f"{previous.currency} {_money(previous.pay_basis_amount, previous.currency)}"
    new = f"{row.currency} {_money(row.pay_basis_amount, row.currency)}"
    content.update({
        "info": [["Employee", employee.full_name], ["Employee ID", employee.employee_code],
                 ["Effective from", row.effective_from.isoformat()]],
        "paragraphs": [f"Dear {employee.first_name},",
                       f"We are pleased to confirm that your pay basis is revised from {old} to {new} with effect from "
                       f"{row.effective_from.isoformat()}.",
                       "All other terms of your employment remain unchanged."],
    })
    return _issue(db, principal, content, doc_type="INCREMENT_LETTER", legal_entity_id=placement.legal_entity_id,
                  subject_type="EMPLOYEE", subject_id=employee.id, source_key=key, title="Increment letter",
                  file_stem=f"increment-letter-{employee.employee_code}", employee_id=employee.id,
                  pay_group_id=placement.pay_group_id, source_refs={"compensation_assignment_id": row.id},
                  publish="ON_ISSUE")


# --------------------------------------------------------------------------- loan and reimbursement statements


def _self_or_staff(db: Session, principal: Principal, permission: str, employee_id: int) -> bool:
    """True for the caller's own record (SELF grant), False for staff in scope; 403 / 404 otherwise."""
    if principal.employee_id == employee_id and principal.has(permission):
        return True
    _staff_employee(db, principal, permission, employee_id)
    return False


def issue_loan_statement(db: Session, principal: Principal, loan_id: int) -> PayDocument:
    from app.models.variable_inputs import Loan, LoanTransaction

    loan = db.query(Loan).filter(Loan.company_id == principal.company_id, Loan.id == loan_id).one_or_none()
    if loan is None:
        raise not_found("Loan")
    if principal.employee_id == loan.employee_id and principal.has(LOANS_READ):
        pass
    else:
        documents.require_staff(principal, LOANS_READ)
        if not access.target_scope(principal, LOANS_READ).covers(loan.legal_entity_id, loan.pay_group_id):
            raise not_found("Loan")
    employee = db.query(Employee).filter(Employee.company_id == loan.company_id, Employee.id == loan.employee_id).one()
    txs = (db.query(LoanTransaction).filter(LoanTransaction.company_id == loan.company_id,
                                            LoanTransaction.loan_id == loan.id)
           .order_by(LoanTransaction.effective_on, LoanTransaction.id).all())
    balance = Decimal(0)
    rows = []
    for tx in txs:
        amount = Decimal(tx.amount)
        balance += amount if tx.kind in ("DISBURSEMENT",) else (-amount if tx.kind != "ADJUSTMENT" else amount)
        rows.append([tx.effective_on.isoformat(), tx.kind.title(), _money(amount, tx.currency),
                     _money(balance, tx.currency)])
    content = base_content(db, loan.company_id, loan.legal_entity_id,
                           "Loan statement" if loan.kind == "LOAN" else "Advance statement")
    content.update({
        "info": [["Employee", employee.full_name], ["Employee ID", employee.employee_code],
                 ["Principal", f"{loan.currency} {_money(loan.principal, loan.currency)}"],
                 ["Status", loan.status.replace("_", " ").title()], ["Tenure", f"{loan.tenure} instalments"]],
        "tables": [{"title": "Transactions", "columns": ["Date", "Type", "Amount", "Balance"],
                    "numeric": [False, False, True, True], "rows": rows}],
        "notes": ["Snapshot of the loan ledger on the issue date."],
    })
    key = f"loan:{loan.id}"
    return _issue(db, principal, content, doc_type="LOAN_STATEMENT", legal_entity_id=loan.legal_entity_id,
                  subject_type="EMPLOYEE", subject_id=employee.id, source_key=key,
                  version=_next_version(db, loan.company_id, "LOAN_STATEMENT", key), title=content["title"],
                  file_stem=f"loan-statement-{employee.employee_code}", employee_id=employee.id,
                  pay_group_id=loan.pay_group_id, source_refs={"loan_id": loan.id}, publish="ON_ISSUE")


def issue_reimbursement_statement(db: Session, principal: Principal, employee_id: int, start: date, end: date
                                  ) -> PayDocument:
    from app.models.variable_inputs import ReimbursementClaim

    if end < start or (end - start).days > 366 * 2:
        raise bad_request("Choose a range of at most two years.", "RANGE_INVALID")
    own = _self_or_staff(db, principal, REIMBURSEMENTS_READ, employee_id)
    employee = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.id == employee_id).one()
    placement = _placement_or_404(db, principal.company_id, employee_id, min(end, _today(db, principal.company_id)))
    claims = (db.query(ReimbursementClaim)
              .filter(ReimbursementClaim.company_id == principal.company_id, ReimbursementClaim.employee_id == employee_id,
                      ReimbursementClaim.expense_date >= start, ReimbursementClaim.expense_date <= end)
              .order_by(ReimbursementClaim.expense_date, ReimbursementClaim.id).all())
    totals: dict[str, Decimal] = defaultdict(Decimal)
    rows = []
    for claim in claims:
        approved = claim.approved_amount if claim.approved_amount is not None else None
        rows.append([claim.expense_date.isoformat(), claim.claim_type_name, claim.currency,
                     _money(claim.amount, claim.currency),
                     _money(approved, claim.currency) if approved is not None else "—", claim.status.title()])
        if claim.status == "PAID" and approved is not None:
            totals[claim.currency] += Decimal(approved)
    content = base_content(db, principal.company_id, placement.legal_entity_id, "Reimbursement statement")
    content.update({
        "info": [["Employee", employee.full_name], ["Employee ID", employee.employee_code],
                 ["From", start.isoformat()], ["To", end.isoformat()]],
        "tables": [{"title": "Claims", "columns": ["Date", "Type", "Cur.", "Claimed", "Approved", "Status"],
                    "numeric": [False, False, False, True, True, False], "rows": rows},
                   {"title": "Paid", "columns": ["Currency", "Amount"], "numeric": [False, True],
                    "rows": [[cur, _money(t, cur)] for cur, t in sorted(totals.items())]}],
        "notes": ["Reimbursements are not wages."],
    })
    key = f"reimbursements:{employee_id}:{start.isoformat()}:{end.isoformat()}"
    return _issue(db, principal, content, doc_type="REIMBURSEMENT_STATEMENT",
                  legal_entity_id=placement.legal_entity_id, subject_type="EMPLOYEE", subject_id=employee_id,
                  source_key=key, version=_next_version(db, principal.company_id, "REIMBURSEMENT_STATEMENT", key),
                  title="Reimbursement statement", file_stem=f"reimbursements-{employee.employee_code}",
                  employee_id=employee_id, pay_group_id=placement.pay_group_id,
                  source_refs={"employee_id": employee_id, "from": start.isoformat(), "to": end.isoformat(),
                               "requested_by": "SELF" if own else "STAFF"}, publish="ON_ISSUE")


# --------------------------------------------------------------------------- certificate / letter requests


def request_json(row: DocumentRequest) -> dict:
    return {
        "id": row.id, "employee_id": row.employee_id, "doc_type": row.doc_type, "include_amounts": row.include_amounts,
        "purpose": row.purpose, "status": row.status, "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decision_note": row.decision_note, "document_id": row.document_id, "row_version": row.row_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def create_request(db: Session, principal: Principal, doc_type: str, *, include_amounts: bool,
                   purpose: Optional[str]) -> DocumentRequest:
    if principal.employee_id is None:
        raise not_found("Employee")
    if doc_type not in ("SALARY_CERTIFICATE", "EMPLOYMENT_LETTER"):
        raise bad_request("Unknown document type.", "DOC_TYPE_INVALID")
    pending = db.query(DocumentRequest.id).filter(
        DocumentRequest.company_id == principal.company_id, DocumentRequest.employee_id == principal.employee_id,
        DocumentRequest.doc_type == doc_type, DocumentRequest.status == "PENDING").first()
    if pending is not None:
        raise conflict("A request for this document is already pending.", "REQUEST_PENDING")
    row = DocumentRequest(company_id=principal.company_id, employee_id=principal.employee_id, doc_type=doc_type,
                          include_amounts=bool(include_amounts), purpose=(purpose or "").strip()[:300] or None,
                          status="PENDING", requested_by_user_id=principal.user_id, row_version=1)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, audit.PAYSLIPS, "DOCUMENT_REQUESTED", f"{doc_type.replace('_', ' ').title()} requested",
                        "document_request", row.id, details={"doc_type": doc_type, "include_amounts": include_amounts})
    return row


def list_own_requests(db: Session, principal: Principal) -> list[dict]:
    if principal.employee_id is None:
        return []
    rows = (db.query(DocumentRequest).filter(DocumentRequest.company_id == principal.company_id,
                                             DocumentRequest.employee_id == principal.employee_id)
            .order_by(DocumentRequest.id.desc()).limit(100).all())
    return [request_json(r) for r in rows]


def list_requests(db: Session, principal: Principal, *, status: Optional[str] = None, skip: int = 0,
                  limit: int = 50) -> dict:
    documents.require_staff(principal, PUBLISH)
    today = _today(db, principal.company_id)
    scope = access.payroll_employee_scope(db, principal, PUBLISH, today, today)
    query = db.query(DocumentRequest).filter(DocumentRequest.company_id == principal.company_id,
                                             scope.clause(DocumentRequest.employee_id),
                                             DocumentRequest.employee_id != (principal.employee_id or 0))
    if status:
        query = query.filter(DocumentRequest.status == status)
    total = query.count()
    rows = query.order_by(DocumentRequest.id.desc()).offset(skip).limit(limit).all()
    names = documents.employee_names(db, principal.company_id, [r.employee_id for r in rows])
    return {"total": total, "items": [{**request_json(r), "employee": names.get(r.employee_id)} for r in rows]}


def _latest_pay(db: Session, company_id: int, employee_id: int) -> Optional[tuple[PayrollRunEmployee, PayPeriod]]:
    found = (db.query(PayrollRunEmployee, PayPeriod)
             .join(PayrollRun, (PayrollRun.company_id == PayrollRunEmployee.company_id)
                   & (PayrollRun.id == PayrollRunEmployee.run_id))
             .join(PayPeriod, (PayPeriod.company_id == PayrollRun.company_id) & (PayPeriod.id == PayrollRun.pay_period_id))
             .filter(PayrollRunEmployee.company_id == company_id, PayrollRunEmployee.employee_id == employee_id,
                     PayrollRunEmployee.status == "CALCULATED", PayrollRun.run_type == "REGULAR",
                     PayrollRun.status.in_(sm.RUN_FINALIZED_STATES))
             .order_by(PayPeriod.period_end.desc(), PayrollRunEmployee.id.desc()).first())
    return found


def decide_request(db: Session, principal: Principal, request_id: int, *, approve: bool, note: Optional[str],
                   expected_version: Optional[int] = None) -> DocumentRequest:
    documents.require_staff(principal, PUBLISH)
    row = (db.query(DocumentRequest).filter(DocumentRequest.company_id == principal.company_id,
                                            DocumentRequest.id == request_id).with_for_update().one_or_none())
    if row is None:
        raise not_found("Document request")
    _staff_employee(db, principal, PUBLISH, row.employee_id)
    if row.requested_by_user_id == principal.user_id or row.employee_id == principal.employee_id:
        raise forbidden("Another person decides your own request.")
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("This request was changed by someone else; reload and try again.", "ROW_VERSION_CONFLICT")
    if row.status != "PENDING":
        raise conflict("This request was already decided.", "ALREADY_DECIDED")
    now = clock.utcnow()
    row.decided_by_user_id, row.decided_at = principal.user_id, now
    row.decision_note = (note or "").strip()[:1000] or None
    row.row_version += 1
    if not approve:
        if not row.decision_note:
            raise bad_request("A reason is required.", "REASON_REQUIRED")
        row.status = "REJECTED"
        db.flush()
        AuditLogService.log(db, principal, audit.PAYSLIPS, "DOCUMENT_REQUEST_REJECTED", "Document request rejected",
                            "document_request", row.id, details={"reason": row.decision_note})
        return row
    if row.include_amounts:
        _staff_employee(db, principal, COMPENSATION_READ, row.employee_id)
    employee = db.query(Employee).filter(Employee.company_id == row.company_id, Employee.id == row.employee_id).one()
    today = _today(db, row.company_id)
    placement = _placement_or_404(db, row.company_id, employee.id, today)
    entity = _entity(db, row.company_id, placement.legal_entity_id)
    title = "Salary certificate" if row.doc_type == "SALARY_CERTIFICATE" else "Employment letter"
    content = base_content(db, row.company_id, entity.id, title)
    paragraphs = [f"This is to certify that {employee.full_name} (employee ID {employee.employee_code}) has been "
                  f"employed by {entity.legal_name} since {employee.hire_date.isoformat()}"
                  + (f" as {employee.position}." if employee.position else ".")]
    if row.include_amounts:
        found = _latest_pay(db, row.company_id, employee.id)
        if found is None:
            paragraphs.append("No finalized payroll exists yet, so no pay figures are stated.")
        else:
            pay, period = found
            paragraphs.append(f"Their gross pay for the pay period {period.period_start.isoformat()} to "
                              f"{period.period_end.isoformat()} was {pay.currency} {_money(pay.gross, pay.currency)} and "
                              f"their net pay {pay.currency} {_money(pay.net, pay.currency)}.")
    if row.purpose:
        paragraphs.append(f"This document is issued at the employee's request for: {row.purpose}.")
    content["paragraphs"] = paragraphs
    signature = [entity.signatory_name, entity.signatory_title]
    if any(signature):
        content["notes"] = [" — ".join(v for v in signature if v)]
    doc = _issue(db, principal, content, doc_type=row.doc_type, legal_entity_id=entity.id, subject_type="EMPLOYEE",
                 subject_id=employee.id, source_key=f"request:{row.id}", title=title,
                 file_stem=f"{row.doc_type.lower().replace('_', '-')}-{employee.employee_code}", employee_id=employee.id,
                 pay_group_id=placement.pay_group_id, source_refs={"request_id": row.id,
                                                                   "include_amounts": row.include_amounts},
                 publish="ON_ISSUE")
    row.status, row.document_id = "ISSUED", doc.id
    db.flush()
    AuditLogService.log(db, principal, audit.PAYSLIPS, "DOCUMENT_REQUEST_APPROVED", f"{title} issued", "document_request",
                        row.id, details={"document_id": doc.id, "include_amounts": row.include_amounts})
    return row


__all__ = [
    "ADVICE_KINDS", "base_content", "create_request", "decide_request", "draft_register_pdf",
    "final_settlement_documents", "issue_bank_transfer_report", "issue_final_settlement", "issue_funding_request", "issue_increment_letter",
    "issue_loan_statement", "issue_payment_advice", "issue_register", "issue_reimbursement_statement",
    "list_own_requests", "list_requests", "register_content", "request_json",
]
