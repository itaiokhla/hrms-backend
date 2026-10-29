"""Country packs, industry bundles and cut-over readiness (G-22 / G-23).

``GET /attendance/packs`` · ``PUT /attendance/packs/{legal_entity_id}`` (adopt / release) ·
``GET /attendance/packs/{legal_entity_id}/fair-workweek?from&to`` · ``GET /attendance/bundles`` ·
``POST /attendance/bundles/{code}/apply`` · ``GET /attendance/readiness`` · ``POST /attendance/readiness/acceptance``
(202, job) · ``GET /attendance/readiness/acceptance/{job_id}`` · ``POST …/{job_id}/sign-off``.
Permissions are checked by the service; writes commit through ``idempotency.execute``.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import idempotency
from app.services import attendance_readiness as service

router = APIRouter(prefix="/api/v1", tags=["Attendance readiness"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdoptIn(_Strict):
    enabled: bool
    reason: str = Field(..., min_length=3, max_length=500)


class SignOffIn(_Strict):
    note: Optional[str] = Field(None, max_length=500)


def _write(db: Session, principal: Principal, request: Request, body, run, status_code: int = 200):
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else (body or {})
    return idempotency.execute(db, principal, request, lambda: (status_code, run()), body=payload)


@router.get("/attendance/packs")
def packs(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.packs_view(db, principal)


@router.put("/attendance/packs/{legal_entity_id:int}")
def adopt(legal_entity_id: int, data: AdoptIn, request: Request, principal: Principal = Depends(get_principal),
          db: Session = Depends(get_db)):
    return _write(db, principal, request, data,
                  lambda: service.set_adoption(db, principal, legal_entity_id, data.enabled, data.reason))


@router.get("/attendance/packs/{legal_entity_id:int}/fair-workweek")
def fair_workweek(legal_entity_id: int, from_: date = Query(..., alias="from"), to: date = Query(...),
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.fww_estimate(db, principal, legal_entity_id, from_, to)


@router.get("/attendance/bundles")
def bundles(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.bundles(db, principal)


@router.post("/attendance/bundles/{code}/apply", status_code=201)
def apply_bundle(code: str, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    return _write(db, principal, request, {"code": code}, lambda: service.apply_bundle(db, principal, code), 201)


@router.get("/attendance/readiness")
def readiness(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.readiness(db, principal)


@router.post("/attendance/readiness/acceptance", status_code=202)
def start_acceptance(request: Request, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: service.start_acceptance(db, principal), 202)


@router.get("/attendance/readiness/acceptance/{job_id:int}")
def get_acceptance(job_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_acceptance(db, principal, job_id)


@router.post("/attendance/readiness/acceptance/{job_id:int}/sign-off")
def sign_off(job_id: int, data: SignOffIn, request: Request, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: service.sign_off(db, principal, job_id, data.note))
