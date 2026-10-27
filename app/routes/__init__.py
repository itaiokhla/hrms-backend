from app.routes import (
    attendance,
    auth,
    branding,
    dashboard,
    employees,
    holiday_work_weeks,
    leaves,
    operations,
    org_structure,
    organization,
    partner,
    partner_delegation,
    payroll,
    platform,
    reports,
    saved_views,
    settings,
    shift_catalogue,
    users,
)
from app.payroll.routes import ROUTERS as PAYROLL_V2_ROUTERS

ROUTERS = [
    branding.public,
    auth.router,
    platform.router,
    partner.router,
    partner_delegation.partner_router,
    partner_delegation.tenant_router,
    users.router,
    # The shift catalogue owns /shifts (legacy shapes kept) and is registered before employees.router (IG-AS-09).
    shift_catalogue.router,
    employees.router,
    organization.router,
    # New attendance-shift / cross-module routers go before attendance.router (AS 14 IG-AS-09).
    saved_views.router,
    # Leave-holiday v2 routers go before the legacy leaves/operations routers (LH 14 IG-05).
    holiday_work_weeks.router,
    holiday_work_weeks.branches_router,
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
