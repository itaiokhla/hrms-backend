"""Background job status for JobProgress (3.9 §4, §6): own jobs or jobs readable through their permission."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import jobs

router = APIRouter(prefix="/api/v1/jobs", tags=["Jobs"])


@router.get("/{job_id}")
def get_job(job_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return jobs.serialize(jobs.get_for_principal(db, principal, job_id))
