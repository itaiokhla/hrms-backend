"""Payroll documents API: public verification, registers, statements, certificates and the download center.

* ``GET /api/v1/documents/verify/{public_id}`` (public, rate limited): issuer, document type, issue date,
  number and the masked name only, never amounts (EC-DOC-18). On a tenant host only that tenant's
  documents verify.
* HR: ``GET /api/v1/payroll/documents`` (non-payslip documents in scope), ``GET …/{id}``, ``…/{id}/download``;
  issue the payroll register (draft PDF before finalization), funding request, bank transfer report,
  payment advice (only with the matching payment state, EC-DOC-10), final settlement statements,
  increment letters, loan and reimbursement statements; decide certificate / letter requests.
* Employees: ``GET /api/v1/me/payroll/documents`` (own published documents), request a salary certificate
  or employment letter, generate their own loan / reimbursement statements.
* Download center: ``GET/POST /api/v1/payroll/exports``, ``GET …/{id}``, ``POST …/{id}/download`` (the
  requester only; signed link); ``GET /api/v1/payroll/downloads/mine`` merges these with the caller's report
  exports (one paging model, signed links for both).
"""

from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.errors import not_found
from app.core.hosts import PLATFORM, TENANT, request_host
from app.core.principal import Principal, get_principal
from app.core.rate_limit import client_ip, limiter
from app.database.db import get_db
from app.models.pay_documents import DOC_TYPES, DOCUMENT_STATUSES, EXPORT_KINDS, REQUEST_STATUSES, PayDocument
from app.payroll.api_types import StrictModel
from app.payroll.services import documents, downloads, idempotency, registers

public = APIRouter(prefix="/api/v1/documents", tags=["Document verification"])
router = APIRouter(prefix="/api/v1", tags=["Payroll documents"])

DocType = Literal[DOC_TYPES]  # type: ignore[valid-type]
DocumentStatus = Literal[DOCUMENT_STATUSES]  # type: ignore[valid-type]
ExportKind = Literal[EXPORT_KINDS]  # type: ignore[valid-type]
RequestStatus = Literal[REQUEST_STATUSES]  # type: ignore[valid-type]


def _reason(value: str) -> str:
    if not value.strip():
        raise ValueError("Give a reason.")
    return value.strip()


class ExportIn(StrictModel):
    kind: ExportKind
    run_id: int = Field(ge=1)


class AdviceIn(StrictModel):
    kind: Literal["INSTRUCTION", "CONFIRMATION"]


class IncrementIn(StrictModel):
    compensation_assignment_id: int = Field(ge=1)


class RangeIn(StrictModel):
    start: date
    end: date


class RequestIn(StrictModel):
    doc_type: Literal["SALARY_CERTIFICATE", "EMPLOYMENT_LETTER"]
    include_amounts: bool = False
    purpose: Optional[str] = Field(None, max_length=300)


class DecisionIn(StrictModel):
    note: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class RejectIn(StrictModel):
    note: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _reason(value)


def _doc(doc: PayDocument, *, as_staff: bool = True) -> dict:
    return documents.document_json(doc, as_staff=as_staff)


def _docs(docs: List[PayDocument]) -> dict:
    return {"items": [_doc(d) for d in docs]}


# ---------------------------------------------------------------------------- public verification (EC-DOC-18)


@public.get("/verify/{public_id}")
def verify(public_id: str, request: Request):
    limiter.check(f"verify:ip:{client_ip(request)}", 30, 60)
    context = request_host(request)
    if context.kind not in (TENANT, PLATFORM):  # an unknown host verifies nothing
        raise not_found("Document")
    host_company = context.company_id if context.kind == TENANT else None
    return documents.verify_public(public_id, host_company_id=host_company, ip=client_ip(request))


# ---------------------------------------------------------------------------- HR documents


@router.get("/payroll/documents")
def list_documents(
    doc_type: Optional[DocType] = Query(None),
    run_id: Optional[int] = Query(None, ge=1),
    employee_id: Optional[int] = Query(None, ge=1),
    status: Optional[DocumentStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Documents of every type the caller reaches (each type through its own permission and scope)."""
    from sqlalchemy import and_, or_

    clauses = []
    for kind, permission in documents.TYPE_PERMISSION.items():
        if documents.has_staff_scope(principal, permission):
            clauses.append(and_(PayDocument.doc_type == kind, documents.staff_clause(principal, permission)))
    if not clauses:
        documents.require_staff(principal, documents.READ)
    query = db.query(PayDocument).filter(PayDocument.company_id == principal.company_id, or_(*clauses))
    for column, value in ((PayDocument.doc_type, doc_type), (PayDocument.run_id, run_id),
                          (PayDocument.employee_id, employee_id), (PayDocument.status, status)):
        if value is not None:
            query = query.filter(column == value)
    total, rows = documents.count_and_page(query.order_by(PayDocument.id.desc()), skip, limit)
    names = documents.employee_names(db, principal.company_id, [r.employee_id for r in rows])
    return {"total": total, "items": [{**_doc(r), "employee": names.get(r.employee_id)} for r in rows]}


@router.get("/payroll/documents/{document_id}")
def get_document(document_id: int, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    doc, as_staff = documents.get_visible(db, principal, document_id)
    doc = downloads.record_view(db, principal, doc, as_staff=as_staff, ip=client_ip(request))
    body = {**_doc(doc, as_staff=as_staff), "content": doc.content}
    db.commit()
    return body


@router.get("/payroll/documents/{document_id}/download")
def document_download_link(document_id: int, principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)):
    limiter.check(f"payslip-link:{principal.company_id}:{principal.user_id}", 120, 60)
    doc, _ = documents.get_visible(db, principal, document_id)
    return downloads.issue_download(db, principal, doc)


@router.post("/payroll/runs/{run_id}/documents/register", status_code=201)
def issue_register(run_id: int, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        return 201, _doc(registers.issue_register(db, principal, run_id))

    return idempotency.execute(db, principal, request, handler, body={"run_id": run_id})


@router.get("/payroll/runs/{run_id}/documents/register/draft")
def draft_register(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """A draft register (watermarked, never numbered or stored) of a run that is not finalized yet."""
    data, name = registers.draft_register_pdf(db, principal, run_id)
    db.commit()
    return Response(content=data, media_type="application/pdf",
                    headers={**documents.FILE_HEADERS, "Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/payroll/runs/{run_id}/documents/funding-request", status_code=201)
def issue_funding_request(run_id: int, request: Request, principal: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    def handler():
        return 201, _doc(registers.issue_funding_request(db, principal, run_id))

    return idempotency.execute(db, principal, request, handler, body={"run_id": run_id})


@router.post("/payroll/runs/{run_id}/documents/final-settlement", status_code=201)
def issue_final_settlement(run_id: int, request: Request, principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)):
    def handler():
        return 201, _docs(registers.issue_final_settlement(db, principal, run_id))

    return idempotency.execute(db, principal, request, handler, body={"run_id": run_id})


@router.post("/payroll/payment-batches/{batch_id}/documents/bank-transfer-report", status_code=201)
def issue_bank_transfer_report(batch_id: int, request: Request, principal: Principal = Depends(get_principal),
                               db: Session = Depends(get_db)):
    def handler():
        return 201, _doc(registers.issue_bank_transfer_report(db, principal, batch_id))

    return idempotency.execute(db, principal, request, handler, body={"batch_id": batch_id})


@router.post("/payroll/payment-instructions/{instruction_id}/documents/payment-advice", status_code=201)
def issue_payment_advice(instruction_id: int, data: AdviceIn, request: Request,
                         principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Payment instruction / confirmation advice; never without the matching payment state (EC-DOC-10)."""

    def handler():
        return 201, _doc(registers.issue_payment_advice(db, principal, instruction_id, data.kind))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/employees/{employee_id}/documents/increment-letter", status_code=201)
def issue_increment_letter(employee_id: int, data: IncrementIn, request: Request,
                           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        doc = registers.issue_increment_letter(db, principal, data.compensation_assignment_id,
                                               employee_id=employee_id)
        return 201, _doc(doc)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/loans/{loan_id}/documents/statement", status_code=201)
def issue_loan_statement(loan_id: int, request: Request, principal: Principal = Depends(get_principal),
                         db: Session = Depends(get_db)):
    def handler():
        return 201, _doc(registers.issue_loan_statement(db, principal, loan_id))

    return idempotency.execute(db, principal, request, handler, body={"loan_id": loan_id})


@router.post("/payroll/employees/{employee_id}/documents/reimbursement-statement", status_code=201)
def issue_reimbursement_statement(employee_id: int, data: RangeIn, request: Request,
                                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        return 201, _doc(registers.issue_reimbursement_statement(db, principal, employee_id, data.start, data.end))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/document-requests")
def list_document_requests(status: Optional[RequestStatus] = Query(None), skip: int = Query(0, ge=0),
                           limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)):
    return registers.list_requests(db, principal, status=status, skip=skip, limit=limit)


@router.post("/payroll/document-requests/{request_id}/approve")
def approve_document_request(request_id: int, data: DecisionIn, request: Request,
                             principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = registers.decide_request(db, principal, request_id, approve=True, note=data.note,
                                       expected_version=data.row_version)
        return 200, registers.request_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/document-requests/{request_id}/reject")
def reject_document_request(request_id: int, data: RejectIn, request: Request,
                            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = registers.decide_request(db, principal, request_id, approve=False, note=data.note,
                                       expected_version=data.row_version)
        return 200, registers.request_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# ---------------------------------------------------------------------------- self-service


@router.get("/me/payroll/documents")
def my_documents(doc_type: Optional[DocType] = Query(None), skip: int = Query(0, ge=0),
                 limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    query = db.query(PayDocument).filter(PayDocument.company_id == principal.company_id,
                                         documents.own_clause(principal))
    if doc_type is not None:
        query = query.filter(PayDocument.doc_type == doc_type)
    total, rows = documents.count_and_page(query.order_by(PayDocument.id.desc()), skip, limit)
    return {"total": total, "items": [_doc(r, as_staff=False) for r in rows]}


@router.get("/me/payroll/documents/{document_id}")
def my_document(document_id: int, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    doc = documents.get_own(db, principal, document_id)
    doc = downloads.record_view(db, principal, doc, as_staff=False, ip=client_ip(request))
    body = {**_doc(doc, as_staff=False), "content": doc.content}
    db.commit()
    return body


@router.get("/me/payroll/documents/{document_id}/download")
def my_document_download_link(document_id: int, principal: Principal = Depends(get_principal),
                              db: Session = Depends(get_db)):
    limiter.check(f"payslip-link:{principal.company_id}:{principal.user_id}", 120, 60)
    doc = documents.get_own(db, principal, document_id)
    return downloads.issue_download(db, principal, doc)


@router.get("/me/payroll/document-requests")
def my_document_requests(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": registers.list_own_requests(db, principal)}


@router.post("/me/payroll/document-requests", status_code=201)
def request_document(data: RequestIn, request: Request, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    def handler():
        row = registers.create_request(db, principal, data.doc_type, include_amounts=data.include_amounts,
                                       purpose=data.purpose)
        return 201, registers.request_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/me/payroll/loans/{loan_id}/statement", status_code=201)
def my_loan_statement(loan_id: int, request: Request, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    def handler():
        doc = registers.issue_loan_statement(db, principal, loan_id)
        return 201, _doc(doc, as_staff=False)

    return idempotency.execute(db, principal, request, handler, body={"loan_id": loan_id})


@router.post("/me/payroll/reimbursement-statement", status_code=201)
def my_reimbursement_statement(data: RangeIn, request: Request, principal: Principal = Depends(get_principal),
                               db: Session = Depends(get_db)):
    def handler():
        if principal.employee_id is None:
            from app.core.errors import not_found

            raise not_found("Employee")
        doc = registers.issue_reimbursement_statement(db, principal, principal.employee_id, data.start, data.end)
        return 201, _doc(doc, as_staff=False)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# ---------------------------------------------------------------------------- download center


@router.get("/payroll/downloads/mine")
def my_downloads(source: Optional[Literal["EXPORT", "REPORT_EXPORT"]] = Query(None),
                 skip: int = Query(0, ge=0, le=downloads.CENTER_MAX_WINDOW),
                 limit: int = Query(50, ge=1, le=200),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The caller's own payslip / register exports and report exports in one list (newest first), each READY
    file with a signed five-minute link. Registered before payslips' ``/payroll/downloads/{token}``."""
    result = downloads.download_center(db, principal, source=source, skip=skip, limit=limit)
    db.commit()  # expired exports are marked EXPIRED
    return result


@router.get("/payroll/exports")
def list_exports(skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = downloads.list_exports(db, principal, skip=skip, limit=limit)
    db.commit()
    return result


@router.post("/payroll/exports", status_code=202)
def create_export(data: ExportIn, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    def handler():
        job = downloads.request_export(db, principal, data.kind, {"run_id": data.run_id})
        return 202, downloads.export_json(job)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/exports/{export_id}")
def get_export(export_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return downloads.export_json(downloads.own_export(db, principal, export_id))


@router.post("/payroll/exports/{export_id}/download")
def export_download_link(export_id: int, principal: Principal = Depends(get_principal),
                         db: Session = Depends(get_db)):
    job = downloads.own_export(db, principal, export_id)
    downloads.recheck_export_access(db, principal, job)  # 403 once revoked, 404 once out of scope
    if job.status != "READY":
        from app.core.errors import conflict

        raise conflict("This export is not ready.", "EXPORT_NOT_READY")
    return downloads.download_link(principal, "export", job.id)
