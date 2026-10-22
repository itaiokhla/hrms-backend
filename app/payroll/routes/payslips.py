"""Payslips v2 API (3.9 §4 Payslips & documents; 3.6 §5-§8).

HR (``payroll:payslip:read`` beyond SELF, scoped by legal entity / pay group):
``GET /api/v1/payroll/payslips`` (filters), ``GET /api/v1/payroll/payslips/{id}``, ``GET …/{id}/download``
(signed five-minute link), ``POST …/{id}/publish`` (manual, with a reason; ``payroll:payslip:publish``),
``POST …/{id}/resend``, ``POST /api/v1/payroll/payslips/publish`` (bulk manual), ``POST
/api/v1/payroll/payslips/zip`` (download-center export), ``POST /api/v1/payroll/runs/{id}/publish``,
generation progress / start / retry of failed payslips.

Employees: ``GET /api/v1/me/payroll/payslips`` and ``…/{id}`` (own **published** payslips only, EC-DOC-02),
``…/{id}/download``; the payslip PDF password (set or reset after step-up, never shown, EC-DOC-15).

``GET /api/v1/payroll/downloads/{token}`` serves a signed link: same user, same session, within five
minutes, access re-checked (EC-DOC-06). Unknown or out-of-scope ids are 404 (EC-DOC-01).
"""

from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import Field, field_validator
from sqlalchemy import and_
from sqlalchemy.orm import Session, aliased

from app.core.principal import Principal, get_principal
from app.core.rate_limit import client_ip, limiter
from app.database.db import get_db
from app.models.pay_documents import DOCUMENT_STATUSES, PayDocument
from app.payroll.api_types import StrictModel
from app.payroll.services import documents, downloads, idempotency, publication, step_up

router = APIRouter(prefix="/api/v1", tags=["Payslips"])

DocumentStatus = Literal[DOCUMENT_STATUSES]  # type: ignore[valid-type]
PAYSLIP = documents.PAYSLIP


def _reason(value: str) -> str:
    if not value.strip():
        raise ValueError("Give a reason.")
    return value.strip()


class ManualPublishIn(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _reason(value)


class BulkPublishIn(StrictModel):
    document_ids: List[int] = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _reason(value)


class RunPublishIn(StrictModel):
    reason: Optional[str] = Field(None, max_length=1000)


class ZipIn(StrictModel):
    run_id: int = Field(ge=1)


class PasswordIn(StrictModel):
    password: str = Field(min_length=8, max_length=40)


def _summary(doc: PayDocument) -> dict:
    content = doc.content or {}
    totals = content.get("totals") or {}
    return {"period_label": (content.get("period") or {}).get("label"), "currency": content.get("currency"),
            "net": {"amount": totals.get("net"), "currency": content.get("currency")} if totals.get("net") else None,
            "payment": (content.get("payment") or {}).get("text")}


def _payslip_out(doc: PayDocument, *, as_staff: bool, names: dict) -> dict:
    return {**documents.document_json(doc, as_staff=as_staff), **_summary(doc), "employee": names.get(doc.employee_id)}


# ---------------------------------------------------------------------------- HR


@router.get("/payroll/payslips")
def list_payslips(
    run_id: Optional[int] = Query(None, ge=1),
    employee_id: Optional[int] = Query(None, ge=1),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    pay_group_id: Optional[int] = Query(None, ge=1),
    status: Optional[DocumentStatus] = Query(None),
    period_from: Optional[date] = Query(None),
    period_to: Optional[date] = Query(None),
    latest_only: bool = Query(True),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    documents.require_staff(principal, documents.READ)
    query = db.query(PayDocument).filter(PayDocument.company_id == principal.company_id,
                                         PayDocument.doc_type == PAYSLIP,
                                         documents.staff_clause(principal, documents.READ))
    for column, value in ((PayDocument.run_id, run_id), (PayDocument.employee_id, employee_id),
                          (PayDocument.legal_entity_id, legal_entity_id), (PayDocument.pay_group_id, pay_group_id),
                          (PayDocument.status, status)):
        if value is not None:
            query = query.filter(column == value)
    if period_from is not None:
        query = query.filter(PayDocument.period_end >= period_from)
    if period_to is not None:
        query = query.filter(PayDocument.period_start <= period_to)
    if latest_only:  # superseded versions stay reachable by id and with latest_only=false
        later = aliased(PayDocument)
        query = query.filter(~db.query(later.id).filter(
            later.company_id == PayDocument.company_id, later.doc_type == PayDocument.doc_type,
            later.source_key == PayDocument.source_key, later.version > PayDocument.version).exists())
    total, rows = documents.count_and_page(query.order_by(PayDocument.period_end.desc(), PayDocument.id.desc()),
                                           skip, limit)
    names = documents.employee_names(db, principal.company_id, [r.employee_id for r in rows])
    return {"total": total, "items": [_payslip_out(r, as_staff=True, names=names) for r in rows]}


@router.post("/payroll/payslips/publish")
def publish_bulk(data: BulkPublishIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, publication.publish_bulk(db, principal, data.document_ids, data.reason)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/payslips/zip", status_code=202)
def payslips_zip(data: ZipIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    """A bulk ZIP of a run's payslips through the download center (EC-DOC-20)."""

    def handler():
        job = downloads.request_export(db, principal, "PAYSLIPS_ZIP", {"run_id": data.run_id})
        return 202, downloads.export_json(job)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/payslips/{document_id}")
def get_payslip(document_id: int, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    doc, as_staff = documents.get_visible(db, principal, document_id, doc_types=(PAYSLIP,))
    doc = downloads.record_view(db, principal, doc, as_staff=as_staff, ip=client_ip(request))
    names = documents.employee_names(db, principal.company_id, [doc.employee_id])
    body = {**_payslip_out(doc, as_staff=as_staff, names=names), "content": doc.content}
    db.commit()
    return body


@router.get("/payroll/payslips/{document_id}/download")
def payslip_download_link(document_id: int, principal: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    limiter.check(f"payslip-link:{principal.company_id}:{principal.user_id}", 120, 60)
    doc, _ = documents.get_visible(db, principal, document_id, doc_types=(PAYSLIP,))
    return downloads.issue_download(db, principal, doc)


@router.post("/payroll/payslips/{document_id}/publish")
def publish_payslip(document_id: int, data: ManualPublishIn, request: Request,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Manual publication with a reason (EC-DOC-11)."""

    def handler():
        doc = publication.publish_manually(db, principal, document_id, data.reason, expected_version=data.row_version)
        return 200, documents.document_json(doc)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/payslips/{document_id}/resend")
def resend_payslip(document_id: int, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        return 200, publication.resend(db, principal, document_id)

    return idempotency.execute(db, principal, request, handler, body={"document_id": document_id})


@router.post("/payroll/runs/{run_id}/publish")
def publish_run(run_id: int, data: RunPublishIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    """Apply the publish trigger now; with a reason, publish the remaining payslips manually."""

    def handler():
        return 200, publication.publish_run(db, principal, run_id, reason=data.reason)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/runs/{run_id}/payslips/generation")
def generation_status(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return publication.generation_status(db, principal, run_id)


@router.post("/payroll/runs/{run_id}/payslips/generate", status_code=202)
def start_generation(run_id: int, request: Request, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    def handler():
        return 202, publication.start_generation(db, principal, run_id)

    return idempotency.execute(db, principal, request, handler, body={"run_id": run_id})


@router.post("/payroll/runs/{run_id}/payslips/retry-failed", status_code=202)
def retry_failed(run_id: int, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 202, publication.retry_failed(db, principal, run_id)

    return idempotency.execute(db, principal, request, handler, body={"run_id": run_id})


# ---------------------------------------------------------------------------- signed downloads


@router.get("/payroll/downloads/{token}")
def download(token: str, request: Request, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    """Serve a signed link: this user, this session, within five minutes (EC-DOC-06)."""
    limiter.check(f"download:{principal.company_id}:{principal.user_id}", 120, 60)
    served = downloads.redeem(db, principal, token, client_ip(request))
    db.commit()
    headers = {**served.headers, "Content-Disposition": f'attachment; filename="{served.file_name}"'}
    if served.path is not None:
        return FileResponse(served.path, media_type=served.mime, headers=headers)
    return Response(content=served.data, media_type=served.mime, headers=headers)


# ---------------------------------------------------------------------------- self-service


@router.get("/me/payroll/payslips")
def my_payslips(
    year: Optional[int] = Query(None, ge=1900, le=2200),
    skip: int = Query(0, ge=0),
    limit: int = Query(36, ge=1, le=120),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """The caller's own published payslips only (EC-DOC-02)."""
    query = db.query(PayDocument).filter(PayDocument.company_id == principal.company_id,
                                         PayDocument.doc_type == PAYSLIP, documents.own_clause(principal))
    if year is not None:
        query = query.filter(and_(PayDocument.period_end >= date(year, 1, 1), PayDocument.period_start <= date(year, 12, 31)))
    total, rows = documents.count_and_page(query.order_by(PayDocument.period_end.desc(), PayDocument.version.desc()),
                                           skip, limit)
    return {"total": total, "items": [{**documents.document_json(r, as_staff=False), **_summary(r)} for r in rows]}


@router.get("/me/payroll/payslips/{document_id}")
def my_payslip(document_id: int, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    doc = documents.get_own(db, principal, document_id, doc_types=(PAYSLIP,))
    doc = downloads.record_view(db, principal, doc, as_staff=False, ip=client_ip(request))
    body = {**documents.document_json(doc, as_staff=False), **_summary(doc), "content": doc.content}
    db.commit()
    return body


@router.get("/me/payroll/payslips/{document_id}/download")
def my_payslip_download_link(document_id: int, principal: Principal = Depends(get_principal),
                             db: Session = Depends(get_db)):
    limiter.check(f"payslip-link:{principal.company_id}:{principal.user_id}", 120, 60)
    doc = documents.get_own(db, principal, document_id, doc_types=(PAYSLIP,))
    return downloads.issue_download(db, principal, doc)


@router.get("/me/payroll/payslip-password")
def my_payslip_password(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Whether a PDF password is set (never the password itself)."""
    return publication.secret_status(db, principal)


@router.put("/me/payroll/payslip-password")
def set_payslip_password(data: PasswordIn, principal: Principal = Depends(step_up.require_step_up()),
                         db: Session = Depends(get_db)):
    """Set or reset the payslip PDF password after step-up (EC-DOC-15); HR never sees it."""
    result = publication.set_employee_secret(db, principal, data.password)
    db.commit()
    return result
