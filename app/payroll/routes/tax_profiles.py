"""Employee tax profile API (3.2 §7, 3.9 §4 Tax & statutory; app.payroll.services.tax_profiles).

Employee (own record only; ``payroll:tax_profile:read`` / ``:edit`` at SELF):
``GET /api/v1/me/payroll/tax-profile`` (the pack-defined form, status, history; ``available: false`` with the
reason when the country's pack declares nothing), ``PUT`` the same path (submit the next version: the declarations
as a full replacement, ``effective_from``; step-up when a tax identifier is sent; 409 PENDING_EXISTS while one waits),
``GET …/versions/{id}``, ``POST …/versions/{id}/withdraw``, ``POST …/versions/{id}/proofs`` (multipart ``field_code`` +
``file``) and ``GET …/versions/{id}/proofs/{proof_id}``.

HR (``payroll:tax_profile:review`` in legal-entity scope; out-of-scope ids are 404):
``GET /api/v1/payroll/tax-profiles`` (filters status, employee, legal entity), ``GET …/{id}``,
``POST …/{id}/approve`` (step-up; never the submitter's own), ``POST …/{id}/reject`` (note required) and
``GET …/{id}/proofs/{proof_id}`` (audited).

State-changing requests accept ``Idempotency-Key`` and ``row_version`` (409 on a stale version). Identifiers are
returned masked only; money values are decimal strings.
"""

import hashlib
from datetime import date
from typing import Dict, Literal, Optional, Union

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field, StrictBool, StrictInt, StrictStr
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core.principal import Principal, get_principal
from app.core.rate_limit import limiter
from app.database.db import get_db
from app.models.tax_profiles import NOTE_MAX, PROFILE_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import tax_profiles as service

router = APIRouter(prefix="/api/v1", tags=["Tax profiles"])

ProfileStatus = Literal[PROFILE_STATUSES]  # type: ignore[valid-type]
FILE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
Scalar = Union[StrictStr, StrictInt, StrictBool, None]


class SubmitIn(StrictModel):
    values: Dict[str, Scalar] = Field(default_factory=dict, max_length=60)
    effective_from: Optional[date] = None


class VersionIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


class ApproveIn(VersionIn):
    note: Optional[str] = Field(None, max_length=NOTE_MAX)


class RejectIn(VersionIn):
    note: str = Field(min_length=1, max_length=NOTE_MAX)


def _result(row) -> dict:
    return {"id": row.id, "version_no": row.version_no, "status": row.status, "row_version": row.row_version,
            "effective_from": row.effective_from.isoformat()}


async def _read(file: UploadFile) -> bytes:
    return await file.read(app_settings.max_upload_size + 1)


# ---------------------------------------------------------------------------- employee


@router.get("/me/payroll/tax-profile")
def my_tax_profile(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.context(db, principal)


@router.put("/me/payroll/tax-profile")
def submit_tax_profile(data: SubmitIn, request: Request, principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)):
    limiter.check(f"tax-profile:{principal.company_id}:{principal.user_id}", 20, 3600)

    def handler():
        return 200, _result(service.submit(db, principal, data.values, data.effective_from))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/me/payroll/tax-profile/versions/{profile_id}")
def my_tax_profile_version(profile_id: int, principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)):
    return service.get_mine(db, principal, profile_id)


@router.post("/me/payroll/tax-profile/versions/{profile_id}/withdraw")
def withdraw_tax_profile(profile_id: int, request: Request, data: Optional[VersionIn] = None,
                         principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionIn()

    def handler():
        return 200, _result(service.withdraw(db, principal, profile_id, expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body={"profile_id": profile_id, **data.model_dump()})


@router.post("/me/payroll/tax-profile/versions/{profile_id}/proofs", status_code=201)
async def add_tax_proof(profile_id: int, request: Request, field_code: str = Form(..., min_length=1, max_length=81),
                        file: UploadFile = File(...), principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    limiter.check(f"tax-proof:{principal.company_id}:{principal.user_id}", 60, 3600)
    data = await _read(file)

    def handler():
        proof = service.add_proof(db, principal, profile_id, field_code, file.filename or "", data)
        return 201, {"id": proof.id, "profile_id": proof.profile_id, "field_code": proof.field_code,
                     "file_name": proof.file_name, "mime": proof.mime, "bytes": proof.bytes, "sha256": proof.sha256}

    return idempotency.execute(db, principal, request, handler,
                               body={"profile_id": profile_id, "field_code": field_code, "file_name": file.filename,
                                     "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})


@router.get("/me/payroll/tax-profile/versions/{profile_id}/proofs/{proof_id}")
def download_my_tax_proof(profile_id: int, proof_id: int, principal: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    path, proof = service.proof_file(db, principal, profile_id, proof_id, own=True)
    db.commit()
    return FileResponse(path, media_type=proof.mime, filename=proof.file_name, headers=FILE_HEADERS)


# ---------------------------------------------------------------------------- HR review


@router.get("/payroll/tax-profiles")
def list_tax_profiles(
    status: Optional[ProfileStatus] = Query(None),  # type: ignore[valid-type]
    employee_id: Optional[int] = Query(None, ge=1),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return service.list_staff(db, principal, status=status, employee_id=employee_id,
                              legal_entity_id=legal_entity_id, skip=skip, limit=limit)


@router.get("/payroll/tax-profiles/{profile_id}")
def get_tax_profile(profile_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_staff(db, principal, profile_id)


@router.post("/payroll/tax-profiles/{profile_id}/approve")
def approve_tax_profile(profile_id: int, data: ApproveIn, request: Request,
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.approve(db, principal, profile_id, data.note, expected_version=data.row_version)
        return 200, _result(row)

    return idempotency.execute(db, principal, request, handler, body={"profile_id": profile_id, **data.model_dump()})


@router.post("/payroll/tax-profiles/{profile_id}/reject")
def reject_tax_profile(profile_id: int, data: RejectIn, request: Request,
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.reject(db, principal, profile_id, data.note, expected_version=data.row_version)
        return 200, _result(row)

    return idempotency.execute(db, principal, request, handler, body={"profile_id": profile_id, **data.model_dump()})


@router.get("/payroll/tax-profiles/{profile_id}/proofs/{proof_id}")
def download_tax_proof(profile_id: int, proof_id: int, principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)):
    path, proof = service.proof_file(db, principal, profile_id, proof_id)
    db.commit()
    return FileResponse(path, media_type=proof.mime, filename=proof.file_name, headers=FILE_HEADERS)
