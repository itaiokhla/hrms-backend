"""Central, versioned permission registry.

This module is the single source of truth for permission codes. The
frontend constants are generated from it (scripts/export_permissions.py),
so permission strings are never hand-maintained in two places.

Permission = resource:action (payroll v2: payroll:object:action). Every grant
also carries a data scope, and a permission may only be granted at the scopes
listed here.

Full access (the protected Company Administrator role) implies every permission
at every non-targeted scope, except payroll-confidential permissions (payroll
3.9 §5.1, contract §9): an administrator holds those only through an explicit
payroll role, which is visible and audited (EC-SEC-03).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

REGISTRY_VERSION = "2026.10.8"


class Scope(str, Enum):
    SELF = "SELF"
    DIRECT_REPORTS = "DIRECT_REPORTS"
    REPORTING_TREE = "REPORTING_TREE"
    # Teams the principal leads (LEAD / DEPUTY_LEAD), and with TEAM_TREE their sub-teams (X 01 §4; X-M1).
    TEAM = "TEAM"
    TEAM_TREE = "TEAM_TREE"
    DEPARTMENT = "DEPARTMENT"
    ASSIGNED_GROUP = "ASSIGNED_GROUP"
    # Members of the teams named as targets of the role assignment (targeted, like LEGAL_ENTITY).
    ASSIGNED_TEAMS = "ASSIGNED_TEAMS"
    # Employees currently placed in the listed legal entities / branches / pay groups (for payroll
    # objects also the runs and batches of those entities or pay groups). Targets are stored per
    # role assignment; a targeted scope without targets grants nothing.
    LEGAL_ENTITY = "LEGAL_ENTITY"
    BRANCH = "BRANCH"
    PAY_GROUP = "PAY_GROUP"
    COMPANY = "COMPANY"


SCOPE_VALUES: Tuple[str, ...] = tuple(s.value for s in Scope)

SCOPE_LABELS = {
    Scope.SELF: "Own records",
    Scope.DIRECT_REPORTS: "Direct reports",
    Scope.REPORTING_TREE: "Entire reporting tree",
    Scope.TEAM: "Teams I lead",
    Scope.TEAM_TREE: "Teams I lead and their sub-teams",
    Scope.DEPARTMENT: "Own and headed departments",
    Scope.ASSIGNED_GROUP: "Explicitly assigned employees",
    Scope.ASSIGNED_TEAMS: "Selected teams",
    Scope.LEGAL_ENTITY: "Selected legal entities",
    Scope.BRANCH: "Selected branches",
    Scope.PAY_GROUP: "Selected pay groups",
    Scope.COMPANY: "Whole company",
}

# Scopes whose coverage comes from explicit targets on the role assignment.
TARGETED_SCOPES = (Scope.ASSIGNED_TEAMS, Scope.LEGAL_ENTITY, Scope.BRANCH, Scope.PAY_GROUP)

# The employee_assignments column each targeted scope matches (employee scopes, payroll object filters).
# ASSIGNED_TEAMS has none: it matches team_memberships (X 01 §4.2), so loops over this map skip it by design.
TARGET_ASSIGNMENT_COLUMNS: Dict[Scope, str] = {
    Scope.LEGAL_ENTITY: "legal_entity_id",
    Scope.BRANCH: "branch_id",
    Scope.PAY_GROUP: "pay_group_id",
}

_S, _DR, _RT, _D, _AG = Scope.SELF, Scope.DIRECT_REPORTS, Scope.REPORTING_TREE, Scope.DEPARTMENT, Scope.ASSIGNED_GROUP
_TM, _TT, _AT = Scope.TEAM, Scope.TEAM_TREE, Scope.ASSIGNED_TEAMS
_LE, _BR, _PG, _C = Scope.LEGAL_ENTITY, Scope.BRANCH, Scope.PAY_GROUP, Scope.COMPANY

# Spelled out on purpose: PAY_GROUP is offered only where 3.9 §5.1 lists it (payroll permissions).
_ALL = (_S, _DR, _RT, _D, _AG, _LE, _BR, _C)
_TEAM = (_DR, _RT, _D, _AG, _LE, _BR, _C)
_COMPANY = (_C,)
_SELF = (_S,)
_SELF_OR_COMPANY = (_S, _C)
_TEAM_APPROVAL = (_DR, _RT, _D, _AG, _C)
_SELF_OR_TEAM_APPROVAL = (_S,) + _TEAM_APPROVAL
_ENTITY = (_LE, _C)
_ORG_UNITS = (_LE, _BR, _C)
_PAY_GROUPS = (_LE, _PG, _C)
_OWN_PAY_GROUPS = (_S, _LE, _PG, _C)
_OWN_ENTITY = (_S, _LE, _C)
# AS spine §7.1 bundles with the team scopes (X-M1, G-05); _ALL and _TEAM are not edited.
_ALL_T = (_S, _DR, _RT, _TM, _TT, _D, _AG, _AT, _LE, _BR, _C)
_TEAM_T = (_DR, _RT, _TM, _TT, _D, _AG, _AT, _LE, _BR, _C)
_ALL_WITH_PG_T = (_S, _DR, _RT, _TM, _TT, _D, _AG, _AT, _LE, _BR, _PG, _C)
_ROSTER_PLANNING = (_D, _LE, _BR, _C)

# Explicit-only markers for PermissionDef.full_access_scopes (payroll confidentiality and its generic reuse).
_NEVER: Tuple[Scope, ...] = ()
_OWN_ONLY: Tuple[Scope, ...] = (_S,)
PAYROLL_MODULES = ("Payroll", "Payroll (legacy)")


@dataclass(frozen=True)
class PermissionDef:
    code: str
    module: str
    label: str
    description: str
    scopes: Tuple[Scope, ...]
    # standard | confidential | restricted (payroll, compensation, biometrics)
    sensitivity: str = "standard"
    # Scopes full access implies: None = every allowed non-targeted scope; () = none
    # (payroll-confidential); (SELF,) = the administrator's own records only.
    full_access_scopes: Optional[Tuple[Scope, ...]] = None
    # Kept registered and grantable for one release so roles holding it stay valid (EC-MIG-07).
    deprecated: bool = False

    @property
    def resource(self) -> str:
        return self.code.split(":", 1)[0]

    @property
    def action(self) -> str:
        return self.code.split(":", 1)[1]

    @property
    def explicit_only(self) -> bool:
        """Excluded from implicit full access (entirely, or except SELF); held only through an explicit grant.

        The generic mechanism (AS spine A-09, LH V3-073): payroll codes use it today, attendance and leave
        codes such as ``roster:exempt`` reuse it without being payroll-confidential.
        """
        return self.full_access_scopes is not None

    @property
    def payroll_confidential(self) -> bool:
        """An explicit-only code of the Payroll modules (shown and audited as payroll access)."""
        return self.explicit_only and self.module in PAYROLL_MODULES

    @property
    def implied_scopes(self) -> Tuple[Scope, ...]:
        """Scopes a full-access role holds without a grant row; targeted scopes always need targets."""
        allowed = tuple(s for s in self.scopes if s not in TARGETED_SCOPES)
        if self.full_access_scopes is None:
            return allowed
        return tuple(s for s in allowed if s in self.full_access_scopes)


def _pay(code: str, label: str, description: str, scopes, sensitivity: str = "restricted", full_access=_NEVER):
    return PermissionDef(code, "Payroll", label, description, scopes, sensitivity, full_access)


def _legacy(code: str, label: str, description: str, scopes, full_access=_NEVER):
    return PermissionDef(code, "Payroll (legacy)", label, description, scopes, "restricted", full_access, deprecated=True)


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
    PermissionDef("attendance:read", "Attendance", "View attendance", "View attendance records.", _ALL_WITH_PG_T),
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
    # Capture v2 (AS-M2; spine §7.1, AS 11 §11.2 note C-3, SP-AS-169, -220).
    PermissionDef("attendance:review", "Attendance", "Review attendance flags",
                  "Decide review-queue items other than location flags and see the raw punch log.", _TEAM_T),
    PermissionDef("attendance:evidence:read", "Attendance", "Reveal attendance evidence",
                  "Reveal punch photos and face-match scores one punch at a time. Needs step-up; every reveal is "
                  "recorded.", _TEAM_T, "confidential", ()),
    PermissionDef("attendance:manage_policy", "Attendance", "Configure attendance",
                  "Manage attendance policies, capture settings, consents recorded by HR and privacy notices.", _ENTITY),
    PermissionDef("attendance:manage_devices", "Attendance", "Manage attendance devices",
                  "Register, pair, suspend and retire kiosks and devices.", _ORG_UNITS, "restricted"),
    PermissionDef("attendance:manage_sites", "Attendance", "Manage work sites",
                  "Manage sites, geofences, office networks and QR posters.", _ORG_UNITS),
    # Leave
    PermissionDef("leave:read", "Leave", "View leave", "View leave requests and balances.", _ALL_T),
    PermissionDef("leave:apply", "Leave", "Apply for leave", "Submit and cancel own leave requests.", _SELF),
    PermissionDef("leave:approve", "Leave", "Approve leave", "Act on leave requests routed to you.", _TEAM_T),
    PermissionDef("leave:manage_policy", "Leave", "Configure leave", "Manage leave types and approval chains.", _COMPANY),
    # Holidays
    PermissionDef("holiday:read", "Holidays", "View holidays", "View the company holiday calendar.", _COMPANY),
    PermissionDef("holiday:manage", "Holidays", "Manage holidays",
                  "Create and edit holiday calendars, work weeks and their links for the entities or branches in scope.",
                  _ORG_UNITS),
    PermissionDef("holiday:publish", "Holidays", "Publish and backdate holidays",
                  "Publish holiday years, amend published years and post backdated calendar or work-week changes.",
                  _ORG_UNITS, "restricted"),
    PermissionDef("holiday:choose_optional", "Holidays", "Choose optional holidays",
                  "Pick own optional or floating holidays within the quota.", _SELF),
    # Shifts and rosters (AS 06 §3 S1-S9; module "Shifts"). roster:* codes are granted now and used from AS-M5.
    PermissionDef("shift:read", "Shifts", "View shifts",
                  "See the shift catalogue and the shifts that apply to people in scope.", _ALL_T),
    PermissionDef("shift:manage", "Shifts", "Manage shifts",
                  "Manage shifts and their versions, rotation patterns, skill tags, scheduling policies and the Shifts "
                  "settings section (company-wide).", _ORG_UNITS),
    PermissionDef("shift:assign", "Shifts", "Assign shifts",
                  "Manage shift assignment rules, rotation assignments, placement shifts and employee skill tags for "
                  "people in scope.", _TEAM_T),
    PermissionDef("roster:read", "Shifts", "View schedules",
                  "See published schedules of people in scope. Your own schedule always.", _ALL_T),
    PermissionDef("roster:plan", "Shifts", "Plan schedules",
                  "Edit roster drafts, post open shifts, see others' availability and issue schedule estimates.",
                  _ROSTER_PLANNING),
    PermissionDef("roster:publish", "Shifts", "Publish schedules",
                  "Publish roster periods and approve publication requests.", _ROSTER_PLANNING),
    PermissionDef("roster:request", "Shifts", "Request shift changes",
                  "Ask for swaps, drops, releases, pickups and changes; state availability; acknowledge your shifts.",
                  _SELF),
    PermissionDef("roster:approve", "Shifts", "Approve shift requests",
                  "Act on shift-request and availability approval steps routed to you.", _TEAM_T),
    PermissionDef("roster:exempt", "Shifts", "Publish with exemption",
                  "Publish over a blocking roster validation result with an exemption reference. Needs step-up.",
                  _ORG_UNITS, "restricted", ()),
    # Overtime and tasks (approvals never include the requester's own records)
    # JC-AS-24: team scopes on the payroll-registered overtime codes (module and money semantics unchanged).
    PermissionDef("overtime:request", "Overtime", "Request overtime", "Submit own overtime for approval.",
                  (_S,) + _TEAM_APPROVAL + (_TM, _TT, _AT)),
    PermissionDef("overtime:approve", "Overtime", "Approve overtime", "Approve overtime requests routed to you.", _TEAM_T),
    PermissionDef("task:read", "Tasks", "View tasks", "View tasks, assignees and completed units.", _SELF_OR_TEAM_APPROVAL),
    PermissionDef("task:manage", "Tasks", "Manage tasks", "Create, assign and update tasks.", _SELF_OR_TEAM_APPROVAL),
    PermissionDef("task:approve", "Tasks", "Approve tasks", "Approve completed tasks for task pay.", _TEAM_APPROVAL),
    # Payroll (3.9 §5.1). Restricted codes are never implied by full access (except own records).
    _pay("payroll:configuration:read", "View payroll configuration",
         "View payroll settings, policies, calendars and pay groups.", _PAY_GROUPS, "confidential", None),
    _pay("payroll:configuration:write", "Configure payroll",
         "Change payroll settings, policies, calendars and pay groups.", _PAY_GROUPS, "confidential", None),
    _pay("payroll:salary_structure:read", "View salary structures",
         "View pay components and salary structure templates.", _ENTITY, "confidential", None),
    _pay("payroll:salary_structure:edit", "Edit salary structures",
         "Create and version pay components and salary structure templates.", _ENTITY, "confidential", None),
    _pay("payroll:salary_structure:approve", "Approve salary structures",
         "Approve and publish salary structure versions edited by someone else.", _ENTITY, "confidential", None),
    _pay("payroll:compensation:read", "View compensation",
         "View salaries, pay components and revision history.", (_S, _D, _AG, _LE, _PG, _C), full_access=_OWN_ONLY),
    _pay("payroll:compensation:edit", "Edit compensation", "Propose salary assignments and revisions.", _PAY_GROUPS),
    _pay("payroll:compensation:approve", "Approve compensation",
         "Approve salary assignments and revisions proposed by someone else.", _PAY_GROUPS),
    _pay("payroll:inputs:read", "View payroll inputs",
         "View wage inputs, overtime, task pay and arrears that feed payroll.", _PAY_GROUPS),
    _pay("payroll:inputs:edit", "Edit payroll inputs", "Enter and correct one-time payroll inputs.", _PAY_GROUPS),
    _pay("payroll:inputs:import", "Import payroll inputs", "Import payroll inputs from files.", _PAY_GROUPS),
    _pay("payroll:inputs:approve", "Approve payroll inputs", "Approve payroll inputs entered by someone else.", _PAY_GROUPS),
    _pay("payroll:run:read", "View payroll runs", "View payroll runs, their employees, lines and exceptions.", _PAY_GROUPS),
    _pay("payroll:run:create", "Create payroll runs", "Start regular and off-cycle payroll runs.", _PAY_GROUPS),
    _pay("payroll:run:calculate", "Calculate payroll", "Run pre-checks and calculate payroll runs.", _PAY_GROUPS),
    _pay("payroll:run:review", "Review payroll",
         "Resolve exceptions, hold employees and submit runs for approval.", _PAY_GROUPS),
    _pay("payroll:run:approve", "Approve payroll runs", "Approve payroll runs prepared by someone else.", _PAY_GROUPS),
    _pay("payroll:run:finalize", "Finalize payroll runs", "Finalize approved runs, which locks their results.", _PAY_GROUPS),
    _pay("payroll:run:reverse", "Reverse payroll runs", "Reverse finalized runs through correction runs.", _PAY_GROUPS),
    _pay("payroll:run:close", "Close payroll runs", "Close paid and reconciled runs.", _PAY_GROUPS),
    _pay("payroll:payslip:read", "View payslips", "View payslips and other payroll documents.", _OWN_PAY_GROUPS,
         full_access=_OWN_ONLY),
    _pay("payroll:payslip:publish", "Publish payslips", "Publish payslips of finalized runs to employees.", _PAY_GROUPS),
    _pay("payroll:payment:prepare", "Prepare payments", "Create payment batches from finalized runs.", _ENTITY),
    _pay("payroll:payment:approve", "Approve payments", "Approve payment batches prepared by someone else.", _ENTITY),
    _pay("payroll:payment:submit", "Release payments", "Generate bank files and release approved payment batches.", _ENTITY),
    _pay("payroll:payment:reconcile", "Reconcile payments",
         "Import bank statements and reconcile payment results.", _ENTITY),
    _pay("payroll:funding_accounts:manage", "Manage funding accounts",
         "Manage the company bank accounts that fund payroll.", _ENTITY),
    _pay("payroll:bank_details:read_masked", "View bank details (masked)",
         "View payees' bank accounts with account numbers masked.", _OWN_PAY_GROUPS, full_access=_OWN_ONLY),
    _pay("payroll:bank_details:edit", "Edit bank details",
         "Submit changes to payees' bank accounts; every change needs approval.", _OWN_ENTITY, full_access=_OWN_ONLY),
    _pay("payroll:bank_details:approve", "Approve bank details",
         "Approve or reject bank account changes made by someone else.", _ENTITY),
    _pay("payroll:bank_details:read_full", "Reveal bank account numbers",
         "Reveal full bank account numbers, with a recorded reason.", _ENTITY),
    _pay("payroll:loans:read", "View loans and advances",
         "View loans, salary advances and their repayment schedules.", _OWN_PAY_GROUPS, full_access=_OWN_ONLY),
    _pay("payroll:loans:manage", "Manage loans and advances",
         "Request, create and restructure loans and salary advances.", _OWN_PAY_GROUPS, full_access=_OWN_ONLY),
    _pay("payroll:loans:approve", "Approve loans and advances",
         "Approve loans and advances requested by someone else.", _PAY_GROUPS),
    _pay("payroll:reimbursements:read", "View reimbursement claims",
         "View expense claims and their payout status.", _OWN_PAY_GROUPS, full_access=_OWN_ONLY),
    _pay("payroll:reimbursements:manage", "Manage reimbursement claims",
         "Submit and correct expense claims.", _OWN_PAY_GROUPS, full_access=_OWN_ONLY),
    _pay("payroll:reimbursements:approve", "Approve reimbursement claims",
         "Approve expense claims submitted by someone else.", _PAY_GROUPS),
    _pay("payroll:tax_profile:read", "View tax profiles",
         "View tax regimes, tax identifiers and declarations.", _OWN_ENTITY, full_access=_OWN_ONLY),
    _pay("payroll:tax_profile:verify", "Verify tax declarations",
         "Verify tax declarations and proofs submitted by employees.", _ENTITY),
    _pay("payroll:tax_profile:edit", "Submit tax declarations",
         "Submit and update your own tax declarations and proofs.", _SELF, full_access=_OWN_ONLY),
    _pay("payroll:tax_profile:review", "Review tax declarations",
         "Review, approve or reject tax declarations and proofs submitted by employees of a legal entity.", _ENTITY),
    _pay("payroll:statutory:review", "Review statutory filings",
         "Review statutory reports and contribution returns.", _ENTITY),
    _pay("payroll:statutory:submit", "Submit statutory filings",
         "Mark statutory reports as filed and record amendments.", _ENTITY),
    _pay("payroll:reports:read", "View payroll reports",
         "View payroll registers, summaries and variance reports.", _PAY_GROUPS),
    _pay("payroll:reports:export", "Export payroll reports", "Download payroll registers and reports.", _PAY_GROUPS),
    _pay("payroll:audit:read", "View payroll audit trail", "View payroll events in the audit log.", _COMPANY,
         "confidential", None),
    _pay("payroll:legal_hold:manage", "Manage legal holds",
         "Place legal holds on payroll documents and release them (releasing needs a second person and a "
         "recent step-up), run the retention sweep and process employee erasure requests.", _COMPANY),
    # Legacy payroll screens: deprecated aliases for one release (3.9 §5.2), never implied by full access.
    _legacy("payroll:read", "View payroll (legacy)",
            "Legacy payroll screens: view payroll runs and payslips.", _SELF_OR_COMPANY, _OWN_ONLY),
    _legacy("payroll:prepare", "Prepare payroll (legacy)", "Legacy payroll screens: calculate and generate payroll.",
            _COMPANY),
    _legacy("payroll:approve", "Approve payroll (legacy)",
            "Legacy payroll screens: approve payroll prepared by someone else.", _COMPANY),
    _legacy("payroll:process", "Process payments (legacy)", "Legacy payroll screens: mark approved payroll as paid.",
            _COMPANY),
    _legacy("payroll:export", "Export payroll (legacy)", "Legacy payroll screens: download payroll reports.", _COMPANY),
    _legacy("compensation:read", "View compensation (legacy)",
            "Legacy screens: view salary structures and base pay.", _SELF_OR_COMPANY, _OWN_ONLY),
    _legacy("compensation:manage", "Manage compensation (legacy)",
            "Legacy screens: create and revise salary structures.", _COMPANY),
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
    PermissionDef(
        "settings:read", "Administration", "View company settings",
        "View company settings and policies, and where each effective value comes from.", _COMPANY,
    ),
    PermissionDef("settings:manage", "Administration", "Company settings", "Edit company profile and policies.", _COMPANY),
    PermissionDef(
        "settings:approve_changes", "Administration", "Approve setting changes",
        "Approve or reject changes to sensitive settings made by someone else.", _COMPANY, "confidential",
    ),
    PermissionDef(
        "settings:partner_access:manage", "Administration", "Manage partner access",
        "Grant and revoke time-boxed, read-only access for the company's partner (reseller or bureau).",
        _COMPANY, "confidential",
    ),
    PermissionDef("audit:read", "Administration", "View audit log", "View the company audit trail.", _COMPANY, "confidential"),
    # White label
    PermissionDef(
        "branding:manage", "Administration", "Manage branding",
        "Edit, preview, publish and roll back the company brand.", _COMPANY,
    ),
    PermissionDef(
        "domain:manage", "Administration", "Manage domains",
        "Add, verify and remove custom domains and choose the primary address.", _COMPANY, "confidential",
    ),
    PermissionDef(
        "email_template:manage", "Administration", "Manage email templates",
        "Edit the wording of transactional emails.", _COMPANY,
    ),
    # Teams (X 01 §10.3; X-M1)
    PermissionDef("team:read", "People", "View teams",
                  "See teams, their leads and members for people in scope; your own teams always.", _ALL_T),
    PermissionDef("team:manage", "People", "Manage teams", "Create, edit, re-parent and archive teams.", _ORG_UNITS),
    PermissionDef("team:membership:manage", "People", "Manage team members",
                  "Add and end members, deputies and leads of teams in scope; set primary teams and routing leads. "
                  "Holders at Teams I lead and their sub-teams change members only.", (_TT, _AT, _LE, _BR, _C)),
    # Organisation structure
    PermissionDef(
        "org:read", "Organization", "View organization structure",
        "View legal entities, branches and where people are placed.", _ORG_UNITS,
    ),
    PermissionDef(
        "org:manage_entities", "Organization", "Manage legal entities",
        "Create and edit legal entities, their registrations and document branding.", _COMPANY,
    ),
    PermissionDef(
        "org:manage_dimensions", "Organization", "Manage pay groups and job data",
        "Create and edit pay groups, grades, cost centers, employment types and designations.", _COMPANY,
    ),
    PermissionDef(
        "org:manage_branches", "Organization", "Manage branches", "Create and edit branches (work locations).", _ENTITY,
    ),
]

PERMISSIONS_BY_CODE: Dict[str, PermissionDef] = {p.code: p for p in PERMISSIONS}
ALL_PERMISSION_CODES = tuple(PERMISSIONS_BY_CODE)
DEPRECATED_CODES = tuple(p.code for p in PERMISSIONS if p.deprecated)

# 3.9 §5.2: what each grant of a deprecated payroll code becomes in payroll v2.
LEGACY_PAYROLL_MAPPING: Dict[Tuple[str, str], Tuple[Tuple[str, str], ...]] = {
    ("payroll:read", "SELF"): (("payroll:payslip:read", "SELF"),),
    ("payroll:read", "COMPANY"): (
        ("payroll:run:read", "COMPANY"), ("payroll:payslip:read", "COMPANY"), ("payroll:reports:read", "COMPANY"),
    ),
    ("payroll:prepare", "COMPANY"): (
        ("payroll:run:create", "COMPANY"), ("payroll:run:calculate", "COMPANY"), ("payroll:run:review", "COMPANY"),
        ("payroll:inputs:edit", "COMPANY"), ("payroll:inputs:import", "COMPANY"),
    ),
    ("payroll:approve", "COMPANY"): (("payroll:run:approve", "COMPANY"),),
    ("payroll:process", "COMPANY"): (("payroll:payment:prepare", "COMPANY"), ("payroll:payment:submit", "COMPANY")),
    ("payroll:export", "COMPANY"): (("payroll:reports:export", "COMPANY"),),
    ("compensation:read", "SELF"): (("payroll:compensation:read", "SELF"),),
    ("compensation:read", "COMPANY"): (("payroll:compensation:read", "COMPANY"),),
    ("compensation:manage", "COMPANY"): (
        ("payroll:compensation:edit", "COMPANY"), ("payroll:salary_structure:edit", "COMPANY"),
    ),
}

PAYROLL_RUN_APPROVE = "payroll:run:approve"
PAYROLL_RUN_CREATE = "payroll:run:create"

# Scopes whose coverage is data (targets, assigned employees) the delegator cannot see from the grant
# itself: only COMPANY holders (or administrators) may delegate them (audit 01 D-2/D-3).
COMPANY_DELEGATED_SCOPES = TARGETED_SCOPES + (Scope.ASSIGNED_GROUP,)


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
    if wanted == Scope.TEAM.value and Scope.TEAM_TREE.value in held:  # COMPANY ⊇ TEAM_TREE ⊇ TEAM (X 01 §4.1)
        return True
    return False


def grants_cover(grants: Iterable[Tuple[str, str]], code: str, scope: str) -> bool:
    """True when (code, scope) grants hold `code` at a scope covering `scope`."""
    return scope_covers({s for c, s in grants if c == code}, scope)


_FULL_ACCESS_GRANTS: FrozenSet[Tuple[str, str]] = frozenset(
    (p.code, s.value) for p in PERMISSIONS for s in p.implied_scopes
)


def full_access_grants() -> FrozenSet[Tuple[str, str]]:
    """Grants a full-access role holds implicitly. Authorization and role display both use this set."""
    return _FULL_ACCESS_GRANTS


def implied_by_full_access(code: str, scope: str) -> bool:
    return (code, scope) in _FULL_ACCESS_GRANTS


def is_payroll_grant(code: str, scope: str) -> bool:
    """A payroll-confidential grant that only an explicit payroll role gives (never full access)."""
    perm = PERMISSIONS_BY_CODE.get(code)
    return perm is not None and perm.payroll_confidential and not implied_by_full_access(code, scope)


def is_explicit_access_grant(code: str, scope: str) -> bool:
    """A grant of any module that full access never implies (payroll-confidential ones included)."""
    perm = PERMISSIONS_BY_CODE.get(code)
    return perm is not None and perm.explicit_only and not implied_by_full_access(code, scope)


def can_approve_payroll_runs(grants: Iterable[Tuple[str, str]]) -> bool:
    return grants_cover(grants, PAYROLL_RUN_APPROVE, Scope.COMPANY.value)


def can_create_payroll_runs(grants: Iterable[Tuple[str, str]]) -> bool:
    return any(code == PAYROLL_RUN_CREATE for code, _ in grants)


def loses_last_payroll_approver(
    before: Dict[int, Iterable[Tuple[str, str]]], after: Dict[int, Iterable[Tuple[str, str]]]
) -> bool:
    """Contract §28.9: True when a change leaves payroll runs that someone can create but nobody can approve.

    `before`/`after` map each active user to their effective grants. A tenant that had at least one
    company-wide run approver must keep one as long as any user can still create runs.
    """
    had_approver = any(can_approve_payroll_runs(g) for g in before.values())
    keeps_approver = any(can_approve_payroll_runs(g) for g in after.values())
    still_creates = any(can_create_payroll_runs(g) for g in after.values())
    return had_approver and not keeps_approver and still_creates


# ---------------------------------------------------------------------------
# Default roles seeded for every new company. Companies may edit or delete
# these (except the protected Company Administrator) and add their own.
# Existing companies receive the same changes through migration 0010.
# ---------------------------------------------------------------------------
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
    ("holiday:choose_optional", _S),
    ("payroll:read", _S),
    ("compensation:read", _S),
    ("asset:read", _S),
    ("asset:acknowledge", _S),
    # Payroll v2 self-service (3.9 §5.3); the legacy payroll:read / compensation:read stay for one release.
    ("payroll:payslip:read", _S),
    ("payroll:compensation:read", _S),
    ("payroll:bank_details:read_masked", _S),
    ("payroll:bank_details:edit", _S),
    ("payroll:tax_profile:read", _S),
    ("payroll:tax_profile:edit", _S),
    ("payroll:loans:read", _S),
    ("payroll:reimbursements:read", _S),
    ("overtime:request", _S),
    ("task:read", _S),
    ("shift:read", _S),
    ("roster:read", _S),
    ("roster:request", _S),
    ("team:read", _S),
]


def _company_wide(*codes: str) -> Tuple[Tuple[str, Scope], ...]:
    return tuple((code, _C) for code in codes)


_PAYROLL_PROCESSOR = _company_wide(
    "payroll:run:read", "payroll:run:create", "payroll:run:calculate", "payroll:run:review",
    "payroll:inputs:read", "payroll:inputs:edit", "payroll:inputs:import", "payroll:inputs:approve",
    "payroll:compensation:read", "payroll:compensation:edit", "payroll:salary_structure:read",
    "payroll:salary_structure:edit", "payroll:payslip:read", "payroll:bank_details:read_masked",
    "payroll:bank_details:edit", "payroll:loans:read", "payroll:loans:manage", "payroll:loans:approve",
    "payroll:reimbursements:read", "payroll:reimbursements:manage", "payroll:reimbursements:approve",
    "payroll:tax_profile:verify", "payroll:tax_profile:review", "payroll:reports:read", "payroll:reports:export",
    "payroll:configuration:read",
)


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
        "Full control of this company's workspace except payroll data, which needs an explicit payroll role. "
        "Only administrators can grant this role.",
        is_protected=True,
        is_full_access=True,
    ),
    DefaultRole(
        "hr_admin",
        "HR Administrator",
        "Runs people operations: employees, attendance, leave, assets and user access.",
        tuple(SELF_SERVICE)
        + _company_wide(
            "employee:read", "employee:read_confidential", "employee:create", "employee:update",
            "employee:deactivate", "org:manage_hierarchy", "department:manage", "face:enroll",
            "attendance:read", "attendance:regularize", "attendance:kiosk", "location:read", "location:review", "leave:read",
            "leave:approve", "leave:manage_policy", "holiday:manage", "holiday:publish", "asset:read", "asset:manage",
            "asset:assign", "report:read", "report:export", "user:read", "user:invite",
            "user:update", "user:deactivate", "user:assign_role", "role:read",
            "delegation:manage", "audit:read", "email_template:manage", "org:read", "org:manage_branches",
            "org:manage_entities", "org:manage_dimensions", "overtime:approve", "task:approve",
            "shift:read", "shift:manage", "shift:assign", "roster:read", "roster:plan", "roster:publish",
            "roster:approve", "team:read", "team:manage", "team:membership:manage",
            # AS-M2 (migration 0050): the capture-side codes; attendance:evidence:read has no default grant.
            "attendance:review", "attendance:manage_devices", "attendance:manage_sites", "attendance:manage_policy",
        ),
    ),
    DefaultRole(
        "payroll_manager",
        "Payroll Manager",
        "Runs payroll end to end: inputs, compensation, calculation, review, finalization, payslips, statutory "
        "filings and payroll settings. Approving runs needs the Payroll Approver role; the legacy payroll "
        "screens work as before.",
        tuple(SELF_SERVICE)
        + _company_wide(
            "employee:read", "payroll:read", "payroll:prepare", "payroll:approve",
            "payroll:process", "payroll:export", "compensation:read", "compensation:manage",
            "report:read", "report:export",
        )
        + _PAYROLL_PROCESSOR
        + (("attendance:read", _PG),)  # JC-AS-24
        + _company_wide(
            "payroll:run:finalize", "payroll:run:reverse", "payroll:run:close", "payroll:configuration:write",
            "payroll:payslip:publish", "payroll:statutory:review", "payroll:statutory:submit",
        ),
    ),
    DefaultRole(
        "payroll_processor",
        "Payroll Processor",
        "Prepares payroll: inputs, compensation changes, calculation and review of runs, loans, claims and "
        "payroll reports. Cannot approve runs or release payments.",
        _PAYROLL_PROCESSOR + (("attendance:read", _PG),),  # JC-AS-24: targets = pay groups (migration 0048)
    ),
    DefaultRole(
        "payroll_approver",
        "Payroll Approver",
        "Approves payroll runs, compensation changes, salary structures and bank detail changes prepared by others.",
        _company_wide(
            "payroll:run:read", "payroll:run:approve", "payroll:compensation:read", "payroll:compensation:approve",
            "payroll:salary_structure:approve", "payroll:bank_details:approve", "payroll:payslip:read",
            "payroll:reports:read",
        ),
    ),
    DefaultRole(
        "finance_payments",
        "Finance & Payments",
        "Prepares, approves, releases and reconciles salary payments of finalized runs. Sees masked bank details only.",
        _company_wide(
            "payroll:payment:prepare", "payroll:payment:approve", "payroll:payment:submit", "payroll:payment:reconcile",
            "payroll:funding_accounts:manage", "payroll:bank_details:read_masked", "payroll:run:read",
            "payroll:reports:read",
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
            ("overtime:approve", _RT),
            ("task:approve", _RT),
            ("shift:read", _RT),
            ("roster:read", _RT),
            ("roster:approve", _RT),
            ("team:read", _RT),
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
        _company_wide(
            "audit:read", "employee:read", "attendance:read", "leave:read", "holiday:read",
            "report:read", "report:export", "role:read", "user:read", "payroll:audit:read", "payroll:reports:read",
            "shift:read", "roster:read",
        ),
    ),
]

DEFAULT_ROLES_BY_KEY = {r.system_key: r for r in DEFAULT_ROLES}
# Default roles that exist to hold payroll-confidential access (3.9 §5.3).
PAYROLL_ROLE_KEYS = ("payroll_manager", "payroll_processor", "payroll_approver", "finance_payments")
