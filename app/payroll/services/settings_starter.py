"""Starter payroll configuration for a newly provisioned tenant (decision I-13).

Materializes the STARTER_V1 profile at signup: explicit TENANT setting rows from the signup day
(the company's today) and the catch-all starter policies (LEAVE_PAY_POLICY from the leave types just
created). No statutory scheme is switched ON and no job is scheduled.
"""

from sqlalchemy.orm import Session

from app.models.company import Company

PROFILE = "STARTER_V1"


def apply(db: Session, company: Company) -> None:
    from app.payroll.services import profiles

    profiles.apply_profile(db, company, PROFILE)
