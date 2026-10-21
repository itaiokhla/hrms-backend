from app.routes import (
    attendance,
    auth,
    branding,
    dashboard,
    employees,
    leaves,
    operations,
    org_structure,
    organization,
    partner,
    payroll,
    platform,
    reports,
    settings,
    users,
)
from app.payroll.routes import ROUTERS as PAYROLL_V2_ROUTERS

ROUTERS = [
    branding.public,
    auth.router,
    platform.router,
    partner.router,
    users.router,
    employees.router,
    organization.router,
    attendance.router,
    leaves.router,
    # Payroll v2 first: the legacy router's /payroll/{payroll_id} would shadow /payroll/runs etc.
    *PAYROLL_V2_ROUTERS,
    payroll.router,
    operations.router,
    reports.router,
    settings.router,
    dashboard.router,
    branding.router,
    org_structure.router,
]
