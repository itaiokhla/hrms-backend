from app.routes import (
    attendance,
    attendance_capture,
    attendance_cutover,
    attendance_days,
    attendance_devices,
    attendance_insights,
    attendance_policies,
    attendance_privacy,
    attendance_requests,
    attendance_team,
    shift_requests,
    team_payroll,
    integrations,
    attendance_readiness,
    attendance_review,
    attendance_sites,
    auth,
    calendar_days,
    branding,
    dashboard,
    device_ingest,
    employees,
    holiday_calendars,
    holiday_work_weeks,
    leaves,
    operations,
    org_structure,
    organization,
    partner,
    partner_delegation,
    payroll,
    payroll_sync,
    platform,
    rosters,
    reports,
    saved_views,
    settings,
    shift_catalogue,
    shift_rotations,
    teams,
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
    rosters.router,
    shift_rotations.router,
    teams.router,  # X-M1 (before employees.router, IG-AS-09)
    employees.router,
    organization.router,
    # New attendance-shift / cross-module routers go before attendance.router (AS 14 IG-AS-09).
    saved_views.router,
    # Leave-holiday v2 routers go before the legacy leaves/operations routers (LH 14 IG-05).
    holiday_work_weeks.router,
    holiday_calendars.router,
    holiday_calendars.assignments_router,
    holiday_calendars.branches_router,
    calendar_days.router,
    # AS-M2 capture v2 (before attendance.router: /attendance/kiosk/* and /attendance/review literals win).
    attendance_capture.router,
    attendance_devices.router,
    device_ingest.agent,  # AS-M7 agent batches (HMAC; no session)
    device_ingest.iclock,  # AS-M7 ADMS push: /iclock/* and /api/v1/iclock/* (reviewed non-API routes)
    attendance_review.router,
    attendance_sites.router,
    attendance_privacy.router,
    attendance_policies.router,
    attendance_days.router,  # AS-M3 evaluations (V2-only)
    attendance_cutover.router,  # AS-M3 cut-over (/attendance/setup/*)
    attendance_insights.router,  # AS-M8 overtime review, AS-M9 live board and registers
    attendance_team.router,  # team board, punch for a team member, delete a session, team settings
    attendance_requests.router,  # AS-M4 requests, on-duty, muster, period close, /approvals inbox
    shift_requests.router,  # AS-M6 swaps, open shifts, availability, skill tags, shift acknowledgement
    team_payroll.router,  # manager-prepared payroll batches, payroll-cost invoices (before legacy /payroll/{id})
    integrations.router,  # channels, web push, calendar feed (G-24)
    integrations.public,  # /calendar/{token}.ics (no session; the token is the secret)
    attendance_readiness.router,  # packs, bundles, cut-over readiness (G-22 / G-23)
    attendance.router,
    leaves.router,
    payroll_sync.router,  # G-16: /cutoff and /payroll/run-employees/{id}/retro-breakdown (before legacy payroll)
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
