"""Self-service My Payments: GET /api/v1/me/payroll/payments (3.7 §11.1).

The caller's own payment obligations only (never another payee's): per pay period the net owed and each
payment with its employee label ("Scheduled", "Processing", "Submitted for manual payment — unverified",
"Paid on {date}" only with verified evidence, ...), the masked account, amounts and a timeline of status
timestamps. Sandbox payments carry the SANDBOX badge. No payroll permission is needed; there is nothing to
see without an employee record.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import obligations

router = APIRouter(prefix="/api/v1", tags=["My payments"])


@router.get("/me/payroll/payments")
def my_payments(year: Optional[int] = Query(None, ge=1900, le=2200), principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    return obligations.my_payments(db, principal, year=year)
