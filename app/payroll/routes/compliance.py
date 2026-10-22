"""Compliance API (3.8 §5.15, 3.2 §8): the compliance page, statutory reports and statutory liabilities.

* ``GET /api/v1/payroll/compliance``: per legal entity in scope — country, capability badge (production,
  fixture or not verified), pack versions with review dates, attestation status (decision I-15),
  registrations, liabilities with remittance status, report definitions and the due-date calendar.
* ``/api/v1/payroll/statutory-reports``: list / create (DRAFT) / read, and the lifecycle actions
  generate, regenerate, submit-for-review, verify, export (READY_TO_FILE — never SUBMITTED, EC-STA-11),
  mark-submitted (filing reference, ``payroll:statutory:submit``), record-accepted (acknowledgement),
  record-rejected (authority response) and amend (reason; creates the linked amendment, EC-STA-12);
  ``GET .../file`` downloads the exported file (hash-checked, audited).
* ``/api/v1/payroll/liabilities``: statutory liabilities with per-currency totals; ``remit`` records a
  remittance reference (OPEN → REMITTED) and ``reconcile`` closes it (REMITTED → RECONCILED).

Every state change goes through one transition function with a row lock and ``row_version`` (409 on
conflict). Responses of state changes are compact (ids, statuses, row versions).
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.pay_runs import LIABILITY_STATUSES
from app.models.reports_compliance import STATUTORY_REPORT_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import compliance as service
from app.payroll.services import idempotency
from app.payroll.services import statutory_reports as reports_service

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll compliance"])

FILE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
ReportStatus = Literal[STATUTORY_REPORT_STATUSES]  # type: ignore[valid-type]
LiabilityStatus = Literal[LIABILITY_STATUSES]  # type: ignore[valid-type]


def _text(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


class ReportCreateIn(StrictModel):
    legal_entity_id: int = Field(ge=1)
    report_code: str = Field(min_length=1, max_length=60, pattern=r"^[A-Z][A-Z0-9_]{0,59}$")
    period_start: date


class VersionBody(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


class ReasonBody(VersionBody):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give a reason.")
        return value.strip()


class OptionalReasonBody(VersionBody):
    reason: Optional[str] = Field(None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _text(value)


class SubmittedIn(VersionBody):
    filing_ref: str = Field(min_length=1, max_length=100)
    note: Optional[str] = Field(None, max_length=1000)

    @field_validator("filing_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give the filing reference from the authority.")
        return value.strip()


class AcceptedIn(VersionBody):
    acknowledgement_ref: str = Field(min_length=1, max_length=100)

    @field_validator("acknowledgement_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give the authority's acknowledgement reference.")
        return value.strip()


class RejectedIn(VersionBody):
    authority_response: str = Field(min_length=1, max_length=2000)

    @field_validator("authority_response")
    @classmethod
    def _response(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give the authority's response.")
        return value.strip()


class RemitIn(VersionBody):
    remittance_ref: str = Field(min_length=1, max_length=100)
    remitted_on: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)

    @field_validator("remittance_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give the payment or challan reference.")
        return value.strip()


class ReconcileIn(VersionBody):
    note: Optional[str] = Field(None, max_length=500)


def _report_result(report, amendment=None) -> dict:
    result = {"id": report.id, "status": report.status, "version": report.version,
              "row_version": report.row_version}
    if amendment is not None:
        result["amendment"] = {"id": amendment.id, "status": amendment.status, "version": amendment.version,
                               "amends_report_id": amendment.amends_report_id, "row_version": amendment.row_version}
    return result


# ---------------------------------------------------------------------------
# Compliance page
# ---------------------------------------------------------------------------
@router.get("/compliance")
def compliance(legal_entity_id: Optional[int] = Query(None, ge=1), on: Optional[date] = Query(None),
               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.overview(db, principal, legal_entity_id=legal_entity_id, on=on)


# ---------------------------------------------------------------------------
# Statutory reports
# ---------------------------------------------------------------------------
@router.get("/statutory-reports")
def list_reports(
    legal_entity_id: Optional[int] = Query(None, ge=1),
    status: Optional[ReportStatus] = Query(None),  # type: ignore[valid-type]
    report_code: Optional[str] = Query(None, max_length=60),
    period_from: Optional[date] = Query(None),
    period_to: Optional[date] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = reports_service.list_reports(db, principal, legal_entity_id=legal_entity_id, status=status,
                                                report_code=report_code, period_from=period_from,
                                                period_to=period_to, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/statutory-reports", status_code=201)
def create_report(data: ReportCreateIn, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    def handler():
        report = reports_service.create_report(db, principal, legal_entity_id=data.legal_entity_id,
                                               report_code=data.report_code, period_start=data.period_start)
        return 201, _report_result(report)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/statutory-reports/{report_id}")
def get_report(report_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return reports_service.get_report(db, principal, report_id)


@router.get("/statutory-reports/{report_id}/file")
def download_report(report_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data, report = reports_service.file_bytes(db, principal, report_id)
    db.commit()  # the download audit event
    file_name = report.export_file_name or f"statutory-report-{report.id}.csv"
    return Response(data, media_type="text/csv",
                    headers={**FILE_HEADERS, "Content-Disposition": f'attachment; filename="{file_name}"'})


def _transition(db: Session, principal: Principal, request: Request, report_id: int, action: str, body: dict,
                **kwargs):
    def handler():
        report, amendment = reports_service.transition(db, principal, report_id, action, **kwargs)
        return (201 if amendment is not None else 200), _report_result(report, amendment)

    return idempotency.execute(db, principal, request, handler, body=body)


@router.post("/statutory-reports/{report_id}/generate")
def generate_report(report_id: int, request: Request, data: Optional[VersionBody] = None,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionBody()
    return _transition(db, principal, request, report_id, "GENERATE", data.model_dump(),
                       expected_version=data.row_version)


@router.post("/statutory-reports/{report_id}/regenerate")
def regenerate_report(report_id: int, data: ReasonBody, request: Request,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _transition(db, principal, request, report_id, "REGENERATE", data.model_dump(),
                       expected_version=data.row_version, reason=data.reason)


@router.post("/statutory-reports/{report_id}/submit-for-review")
def submit_for_review(report_id: int, request: Request, data: Optional[OptionalReasonBody] = None,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or OptionalReasonBody()
    return _transition(db, principal, request, report_id, "SUBMIT_FOR_REVIEW", data.model_dump(),
                       expected_version=data.row_version, reason=data.reason)


@router.post("/statutory-reports/{report_id}/verify")
def verify_report(report_id: int, request: Request, data: Optional[OptionalReasonBody] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or OptionalReasonBody()
    return _transition(db, principal, request, report_id, "VERIFY", data.model_dump(),
                       expected_version=data.row_version, reason=data.reason)


@router.post("/statutory-reports/{report_id}/export")
def export_report(report_id: int, request: Request, data: Optional[VersionBody] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """EC-STA-11: exporting makes the report READY_TO_FILE; it is never marked submitted by an export."""
    data = data or VersionBody()
    return _transition(db, principal, request, report_id, "EXPORT", data.model_dump(),
                       expected_version=data.row_version)


@router.post("/statutory-reports/{report_id}/mark-submitted")
def mark_submitted(report_id: int, data: SubmittedIn, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _transition(db, principal, request, report_id, "MARK_SUBMITTED", data.model_dump(),
                       expected_version=data.row_version, filing_ref=data.filing_ref, reason=_text(data.note))


@router.post("/statutory-reports/{report_id}/record-accepted")
def record_accepted(report_id: int, data: AcceptedIn, request: Request,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _transition(db, principal, request, report_id, "RECORD_ACCEPTED", data.model_dump(),
                       expected_version=data.row_version, acknowledgement_ref=data.acknowledgement_ref)


@router.post("/statutory-reports/{report_id}/record-rejected")
def record_rejected(report_id: int, data: RejectedIn, request: Request,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _transition(db, principal, request, report_id, "RECORD_REJECTED", data.model_dump(),
                       expected_version=data.row_version, authority_response=data.authority_response)


@router.post("/statutory-reports/{report_id}/amend")
def amend_report(report_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    """EC-STA-12: the filed report becomes AMENDED and a linked amendment (version + 1) starts as DRAFT."""
    return _transition(db, principal, request, report_id, "AMEND", data.model_dump(),
                       expected_version=data.row_version, reason=data.reason)


# ---------------------------------------------------------------------------
# Statutory liabilities and remittances
# ---------------------------------------------------------------------------
@router.get("/liabilities")
def list_liabilities(
    legal_entity_id: Optional[int] = Query(None, ge=1),
    status: Optional[LiabilityStatus] = Query(None),  # type: ignore[valid-type]
    scheme: Optional[str] = Query(None, max_length=60),
    currency: Optional[str] = Query(None, pattern=r"^[A-Z]{3}$"),
    due_from: Optional[date] = Query(None),
    due_to: Optional[date] = Query(None),
    overdue: Optional[bool] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return service.list_liabilities(db, principal, legal_entity_id=legal_entity_id, status=status, scheme=scheme,
                                    currency=currency, due_from=due_from, due_to=due_to, overdue=overdue, skip=skip,
                                    limit=limit)


@router.get("/liabilities/{liability_id}")
def get_liability(liability_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_liability(db, principal, liability_id)


def _liability_result(row) -> dict:
    return {"id": row.id, "status": row.status, "remittance_ref": row.remittance_ref, "row_version": row.row_version}


@router.post("/liabilities/{liability_id}/remit")
def remit_liability(liability_id: int, data: RemitIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    def handler():
        row = service.liability_transition(db, principal, liability_id, "RECORD_REMITTANCE",
                                           remittance_ref=data.remittance_ref, remitted_on=data.remitted_on,
                                           note=_text(data.note), expected_version=data.row_version)
        return 200, _liability_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/liabilities/{liability_id}/reconcile")
def reconcile_liability(liability_id: int, request: Request, data: Optional[ReconcileIn] = None,
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or ReconcileIn()

    def handler():
        row = service.liability_transition(db, principal, liability_id, "RECONCILE", note=_text(data.note),
                                           expected_version=data.row_version)
        return 200, _liability_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())
