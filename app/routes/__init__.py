from app.routes import (
    attendance,
    auth,
    dashboard,
    employees,
    leaves,
    operations,
    organization,
    payroll,
    platform,
    reports,
    settings,
    users,
)

ROUTERS = [
    auth.router,
    platform.router,
    users.router,
    employees.router,
    organization.router,
    attendance.router,
    leaves.router,
    payroll.router,
    operations.router,
    reports.router,
    settings.router,
    dashboard.router,
]
