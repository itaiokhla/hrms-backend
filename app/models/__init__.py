from .base import Base, BaseModel, BigIdModel, TenantMixin
from .company import Company, Partner, PlatformAdmin, PlatformSession
from .user import PasswordResetOTP, User, UserInvitation, UserSession
from .rbac import Role, RolePermission, ScopeAssignment, UserRoleAssignment
from .employee import (
    ApprovalDelegation,
    Department,
    Employee,
    FaceEncoding,
    ManagerHistory,
    SecondaryReportingLine,
    Shift,
)
from .attendance import (
    Attendance,
    AttendanceBreak,
    AttendanceDevice,
    AttendanceEvent,
    AttendancePunch,
    AttendanceReviewItem,
    LocationSample,
)
from .leave import ApprovalChainStep, Leave, LeaveApproval, LeaveType
from .payroll import Payroll, SalaryStructure
from .holiday import CalendarWorkingDay, Holiday, HolidayCalendarYear, HolidayImport, HolidayRule
from .asset import Asset, AssetAssignment
from .notification import Notification
from .audit_log import AuditLog
from .system_settings import CompanySetting
from .org import Branch, EmployeeAssignment, LegalEntity, RoleAssignmentTarget
from .branding import (
    AuthHandoffCode,
    BrandAsset,
    BrandProfile,
    CommercialBrandAsset,
    CommercialBrandProfile,
    EmailTemplateOverride,
    IssuedDocument,
    TenantDomain,
)

# Payroll v2 (docs/payroll/implementation/ARCHITECTURE.md §3): importing registers the models.
from . import (  # noqa: E402,F401
    approvals,
    catalogue,
    compensation,
    corrections,
    country_packs,
    jobs,
    legal_holds,
    pay_documents,
    pay_org,
    pay_runs,
    payment_flows,
    nominees,
    partner_delegations,
    payments,
    payroll_config,
    payslip_queries,
    reports_compliance,
    security_keys,
    tax_profiles,
    time_inputs,
    unclaimed,
    variable_inputs,
    work_items,
)
# Attendance-shift and cross-module models (AS 14 IG-AS-03), after the payroll block.
from app.models import rosters, scheduler, shifts, teams, views  # noqa: E402,F401
from app.models import attendance_requests  # noqa: E402,F401  (AS-M4, migration 0052)
from app.models import devices  # noqa: E402,F401  (AS-M7, migration 0055)
from app.models import shift_requests  # noqa: E402,F401  (AS-M6, migration 0057)
from app.models import integrations, team_payroll  # noqa: E402,F401  (migration 0058)

TENANT_TABLES = sorted(
    m.__tablename__ for base in (BaseModel, BigIdModel) for m in base.__subclasses__() if issubclass(m, TenantMixin)
)
