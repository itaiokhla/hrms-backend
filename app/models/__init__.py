from .base import Base, BaseModel, TenantMixin
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
from .attendance import Attendance, AttendanceBreak, AttendanceEvent, LocationSample
from .leave import ApprovalChainStep, Leave, LeaveApproval, LeaveType
from .payroll import Payroll, SalaryStructure
from .holiday import Holiday
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
    country_packs,
    jobs,
    pay_documents,
    pay_org,
    pay_runs,
    payments,
    payroll_config,
    security_keys,
    time_inputs,
)

TENANT_TABLES = sorted(
    m.__tablename__ for m in BaseModel.__subclasses__() if issubclass(m, TenantMixin)
)
