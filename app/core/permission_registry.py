"""Central, versioned permission registry.

This module is the single source of truth for permission codes. The
frontend constants are generated from it (scripts/export_permissions.py),
so permission strings are never hand-maintained in two places.

Permission = resource:action. Every grant also carries a data scope, and a
permission may only be granted at the scopes listed here.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Tuple

REGISTRY_VERSION = "2026.10.2"


class Scope(str, Enum):
    SELF = "SELF"
    DIRECT_REPORTS = "DIRECT_REPORTS"
    REPORTING_TREE = "REPORTING_TREE"
    DEPARTMENT = "DEPARTMENT"
    ASSIGNED_GROUP = "ASSIGNED_GROUP"
    COMPANY = "COMPANY"


SCOPE_VALUES: Tuple[str, ...] = tuple(s.value for s in Scope)

SCOPE_LABELS = {
    Scope.SELF: "Own records",
    Scope.DIRECT_REPORTS: "Direct reports",
    Scope.REPORTING_TREE: "Entire reporting tree",
    Scope.DEPARTMENT: "Own and headed departments",
    Scope.ASSIGNED_GROUP: "Explicitly assigned employees",
    Scope.COMPANY: "Whole company",
}

_ALL = tuple(Scope)
_TEAM = (Scope.DIRECT_REPORTS, Scope.REPORTING_TREE, Scope.DEPARTMENT, Scope.ASSIGNED_GROUP, Scope.COMPANY)
_COMPANY = (Scope.COMPANY,)
_SELF = (Scope.SELF,)
_SELF_OR_COMPANY = (Scope.SELF, Scope.COMPANY)


@dataclass(frozen=True)
class PermissionDef:
    code: str
    module: str
    label: str
    description: str
    scopes: Tuple[Scope, ...]
    # standard | confidential | restricted (payroll, compensation, biometrics)
    sensitivity: str = "standard"

    @property
    def resource(self) -> str:
        return self.code.split(":", 1)[0]

    @property
    def action(self) -> str:
        return self.code.split(":", 1)[1]


PERMISSIONS: List[PermissionDef] = [
    # People
    PermissionDef("employee:read", "People", "View employees", "View directory entries and job details.", _ALL),
    PermissionDef(
        "employee:read_confidential", "People", "View personal details",
        "View date of birth, gender, phone and home address.", _ALL, "confidential",
    ),
    PermissionDef("employee:create", "People", "Create employees", "Add employee profiles.", _COMPANY),
    PermissionDef("employee:update", "People", "Edit employees", "Edit job and contact details.", _TEAM),
    PermissionDef("employee:deactivate", "People", "End employment", "Deactivate employees without deleting history.", _COMPANY),
    PermissionDef("profile:update", "People", "Edit own profile", "Update own contact details and photo.", _SELF),
    PermissionDef(
        "org:manage_hierarchy", "People", "Manage reporting lines",
        "Assign primary and secondary managers.", (Scope.REPORTING_TREE, Scope.DEPARTMENT, Scope.COMPANY),
    ),
    PermissionDef("department:manage", "People", "Manage departments & shifts", "Create and edit departments and shifts.", _COMPANY),
    PermissionDef("face:enroll", "People", "Enroll faces", "Register biometric face data for attendance.", _COMPANY, "restricted"),
    # Attendance
    PermissionDef("attendance:read", "Attendance", "View attendance", "View attendance records.", _ALL),
    PermissionDef("attendance:mark", "Attendance", "Mark own attendance", "Check in and out after face verification.", _SELF),
    PermissionDef("attendance:kiosk", "Attendance", "Operate face kiosk", "Run the shared check-in kiosk for the company.", _COMPANY),
    PermissionDef("attendance:regularize", "Attendance", "Correct attendance", "Record or correct attendance manually.", _TEAM),
    # Precise location is authorised separately from attendance and directory access.
    PermissionDef(
        "location:read", "Attendance", "View location data",
        "View coordinates and accuracy recorded at punches, lunch and during shifts.", _ALL, "restricted",
    ),
    PermissionDef(
        "location:review", "Attendance", "Review location flags",
        "Approve location exceptions and acknowledge movement flags.", _TEAM, "confidential",
    ),
    # Leave
    PermissionDef("leave:read", "Leave", "View leave", "View leave requests and balances.", _ALL),
    PermissionDef("leave:apply", "Leave", "Apply for leave", "Submit and cancel own leave requests.", _SELF),
    PermissionDef("leave:approve", "Leave", "Approve leave", "Act on leave requests routed to you.", _TEAM),
    PermissionDef("leave:manage_policy", "Leave", "Configure leave", "Manage leave types and approval chains.", _COMPANY),
    # Holidays
    PermissionDef("holiday:read", "Holidays", "View holidays", "View the company holiday calendar.", _COMPANY),
    PermissionDef("holiday:manage", "Holidays", "Manage holidays", "Create and edit holiday calendars.", _COMPANY),
    # Payroll
    PermissionDef("payroll:read", "Payroll", "View payroll", "View payroll runs and payslips.", _SELF_OR_COMPANY, "restricted"),
    PermissionDef("payroll:prepare", "Payroll", "Prepare payroll", "Calculate and generate payroll.", _COMPANY, "restricted"),
    PermissionDef("payroll:approve", "Payroll", "Approve payroll", "Approve payroll prepared by someone else.", _COMPANY, "restricted"),
    PermissionDef("payroll:process", "Payroll", "Process payments", "Mark approved payroll as paid.", _COMPANY, "restricted"),
    PermissionDef("payroll:export", "Payroll", "Export payroll", "Download payroll reports.", _COMPANY, "restricted"),
    PermissionDef("compensation:read", "Payroll", "View compensation", "View salary structures and base pay.", _SELF_OR_COMPANY, "restricted"),
    PermissionDef("compensation:manage", "Payroll", "Manage compensation", "Create and revise salary structures.", _COMPANY, "restricted"),
    # Assets
    PermissionDef("asset:read", "Assets", "View assets", "View assets and their assignments.", _ALL),
    PermissionDef("asset:manage", "Assets", "Manage inventory", "Create, edit, service and retire assets.", _COMPANY),
    PermissionDef("asset:assign", "Assets", "Assign assets", "Assign, transfer and recover assets.", _COMPANY),
    PermissionDef("asset:acknowledge", "Assets", "Acknowledge assets", "Confirm receipt of assigned assets.", _SELF),
    # Reports
    PermissionDef("report:read", "Reports", "View reports", "View analytics within your data scope.", _TEAM),
    PermissionDef("report:export", "Reports", "Export reports", "Download reports within your data scope.", _TEAM),
    # Administration
    PermissionDef("user:read", "Administration", "View users", "View login accounts and their access.", _COMPANY),
    PermissionDef("user:invite", "Administration", "Invite users", "Create accounts and send invitations.", _COMPANY),
    PermissionDef("user:update", "Administration", "Edit users", "Edit account details and employee links.", _COMPANY),
    PermissionDef("user:deactivate", "Administration", "Suspend users", "Suspend, reactivate and sign out accounts.", _COMPANY),
    PermissionDef("user:assign_role", "Administration", "Assign roles", "Grant and revoke roles you can delegate.", _COMPANY),
    PermissionDef("role:read", "Administration", "View roles", "View roles and their permissions.", _COMPANY),
    PermissionDef("role:create", "Administration", "Create roles", "Create custom roles.", _COMPANY),
    PermissionDef("role:update", "Administration", "Edit roles", "Change role permissions you can delegate.", _COMPANY),
    PermissionDef("role:delete", "Administration", "Delete roles", "Delete unused custom roles.", _COMPANY),
    PermissionDef("delegation:manage", "Administration", "Delegate approvals", "Delegate approval authority.", _SELF_OR_COMPANY),
    PermissionDef("settings:manage", "Administration", "Company settings", "Edit company profile and policies.", _COMPANY),
    PermissionDef("audit:read", "Administration", "View audit log", "View the company audit trail.", _COMPANY, "confidential"),
]

PERMISSIONS_BY_CODE: Dict[str, PermissionDef] = {p.code: p for p in PERMISSIONS}
ALL_PERMISSION_CODES = tuple(PERMISSIONS_BY_CODE)


def is_valid_grant(code: str, scope: str) -> bool:
    perm = PERMISSIONS_BY_CODE.get(code)
    if perm is None:
        return False
    try:
        return Scope(scope) in perm.scopes
    except ValueError:
        return False


def scope_covers(held: Iterable[str], wanted: str) -> bool:
    """True when holding `held` scopes is at least as broad as `wanted`.

    COMPANY covers everything; REPORTING_TREE covers DIRECT_REPORTS (direct
    reports are always part of the tree); otherwise scopes must match exactly.
    """
    held = set(held)
    if Scope.COMPANY.value in held or wanted in held:
        return True
    if wanted == Scope.DIRECT_REPORTS.value and Scope.REPORTING_TREE.value in held:
        return True
    return False


# ---------------------------------------------------------------------------
# Default roles seeded for every new company. Companies may edit or delete
# these (except the protected Company Administrator) and add their own.
# ---------------------------------------------------------------------------
_S, _DR, _RT, _C = Scope.SELF, Scope.DIRECT_REPORTS, Scope.REPORTING_TREE, Scope.COMPANY

SELF_SERVICE = [
    ("employee:read", _S),
    ("employee:read_confidential", _S),
    ("profile:update", _S),
    ("attendance:read", _S),
    ("attendance:mark", _S),
    ("location:read", _S),
    ("leave:read", _S),
    ("leave:apply", _S),
    ("holiday:read", _C),
    ("payroll:read", _S),
    ("compensation:read", _S),
    ("asset:read", _S),
    ("asset:acknowledge", _S),
]


@dataclass(frozen=True)
class DefaultRole:
    system_key: str
    name: str
    description: str
    grants: Tuple[Tuple[str, Scope], ...] = ()
    is_protected: bool = False
    is_full_access: bool = False


DEFAULT_ROLES: List[DefaultRole] = [
    DefaultRole(
        "company_admin",
        "Company Administrator",
        "Full control of this company's workspace. Only administrators can grant this role.",
        is_protected=True,
        is_full_access=True,
    ),
    DefaultRole(
        "hr_admin",
        "HR Administrator",
        "Runs people operations: employees, attendance, leave, assets and user access.",
        tuple(SELF_SERVICE)
        + tuple(
            (code, _C)
            for code in (
                "employee:read", "employee:read_confidential", "employee:create", "employee:update",
                "employee:deactivate", "org:manage_hierarchy", "department:manage", "face:enroll",
                "attendance:read", "attendance:regularize", "attendance:kiosk", "location:read", "location:review", "leave:read",
                "leave:approve", "leave:manage_policy", "holiday:manage", "asset:read", "asset:manage",
                "asset:assign", "report:read", "report:export", "user:read", "user:invite",
                "user:update", "user:deactivate", "user:assign_role", "role:read",
                "delegation:manage", "audit:read",
            )
        ),
    ),
    DefaultRole(
        "payroll_manager",
        "Payroll Manager",
        "Prepares, approves and pays payroll. Sees compensation for the whole company.",
        tuple(SELF_SERVICE)
        + tuple(
            (code, _C)
            for code in (
                "employee:read", "payroll:read", "payroll:prepare", "payroll:approve",
                "payroll:process", "payroll:export", "compensation:read", "compensation:manage",
                "report:read", "report:export",
            )
        ),
    ),
    DefaultRole(
        "manager",
        "Manager",
        "Leads a team: sees and approves for their reporting tree. No payroll access.",
        tuple(SELF_SERVICE)
        + (
            ("employee:read", _RT),
            ("attendance:read", _RT),
            ("attendance:regularize", _DR),
            ("location:read", _DR),
            ("location:review", _DR),
            ("leave:read", _RT),
            ("leave:approve", _RT),
            ("asset:read", _DR),
            ("report:read", _RT),
            ("delegation:manage", _S),
        ),
    ),
    DefaultRole(
        "employee",
        "Employee",
        "Self-service access to own profile, attendance, leave, payslips and assets.",
        tuple(SELF_SERVICE),
    ),
    DefaultRole(
        "auditor",
        "Auditor",
        "Read-only oversight of records, reports and the audit trail.",
        tuple(
            (code, _C)
            for code in (
                "audit:read", "employee:read", "attendance:read", "leave:read", "holiday:read",
                "report:read", "report:export", "role:read", "user:read",
            )
        ),
    ),
]

DEFAULT_ROLES_BY_KEY = {r.system_key: r for r in DEFAULT_ROLES}
