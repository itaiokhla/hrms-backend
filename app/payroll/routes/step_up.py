"""Step-up re-authentication endpoints (3.7 §3.2): status, emailed code, verification."""

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.core.rate_limit import client_ip
from app.database.db import get_db
from app.payroll.api_types import StrictModel
from app.payroll.services import step_up

router = APIRouter(prefix="/api/v1/payroll/step-up", tags=["Payroll security"])


class StepUpVerify(StrictModel):
    code: str = Field(..., min_length=6, max_length=10)


@router.get("")
def step_up_status(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return step_up.current(db, principal)


@router.post("/email-code")
def request_email_code(request: Request, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Users without MFA: email a one-time code bound to this session."""
    challenge = step_up.request_email_code(db, principal, client_ip(request))
    db.commit()
    step_up.send_email_code(principal.company_id, challenge["email"], challenge["name"], challenge["code"])
    return {"sent": True, "expires_in_seconds": challenge["expires_in_seconds"]}


@router.post("")
def verify(data: StepUpVerify, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    outcome = step_up.verify(db, principal, data.code, client_ip(request))
    db.commit()  # failed attempts are recorded too
    if not outcome["ok"]:
        raise step_up.invalid_code()
    return {"method": outcome["method"], "verified_at": outcome["verified_at"], "expires_at": outcome["expires_at"]}
