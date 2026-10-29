"""Generic approvals engine for every approval module except leave (3.1 §5.14, 3.5 §11, 3.9 §3.9).

Routing follows leave approvals (audit 03 §4.3, app.services.leave_service), which keep their own tables:

* submit() instantiates the tenant's chain for the module (approval_chain_steps by step_order). A step
  with min_amount applies from that amount in min_amount_currency; an amount that is missing or in
  another currency keeps the step (fail closed). min_days works the same against context["days"].
  When no configured step applies (or none exists) the chain is one step: holders of the module's
  approve permission (MODULE_PERMISSIONS).
* Approvers are resolved when evaluated: hierarchy steps from the subject employee's current reporting
  line, role steps from current role members, finance_role from holders of payroll:payment:approve,
  specific_user from that user. Every approver must currently hold the step's permission at a scope
  covering the subject: the subject employee, or for subjects without one (runs, batches, settings)
  COMPANY scope or a LEGAL_ENTITY/PAY_GROUP target named by context["legal_entity_id"/"pay_group_id"].
* A step without an eligible approver is skipped. When nobody approved and every step was skipped, a
  step of company administrators holding the permission is appended; if none of them is eligible the
  request stays PENDING with context["pending_reason"] = NO_ELIGIBLE_APPROVER. Nothing auto-approves.
* Barred from approving, also as delegates: the requester and context["maker_user_ids"] (maker-checker,
  EC-RUN-05, EC-CFG-12), whoever approved an earlier step of the request, and the subject employee
  (self-pay, EC-RUN-06). There is no override: only the chain decides.
* decide() locks the request row and re-reads the actor's grants, scope and delegations inside the
  transaction (EC-RUN-07). A decision on a request that is no longer pending, or on another step than
  `expected_step`, is a 409, so of two concurrent approvers one wins (EC-RUN-08).
* Delegation: approval_delegations of the request's module, active on the company-local date, while the
  delegator would still be an eligible approver (EC-RUN-21, EC-SEC-18).
* Terminal outcomes (APPROVED, REJECTED, CANCELLED) run the callbacks registered for (module,
  subject_type) in the same transaction, so the owning module applies its effects atomically.

Services never commit. Context keys owned by the engine: subject_employee_id, pending_reason.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core import authz, clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.permission_registry import PERMISSIONS_BY_CODE, Scope
from app.core.principal import Principal, build_principal
from app.core.security import utcnow
from app.database.db import current_company_id
from app.models.approvals import APPROVAL_REQUEST_STATUSES, ApprovalRequest, ApprovalRequestStep
from app.models.company import Company
from app.models.employee import ApprovalDelegation, Employee
from app.models.leave import APPROVAL_MODULES, ApprovalChainStep
from app.models.rbac import Role, RolePermission, UserRoleAssignment
from app.models.user import User
from app.payroll import audit as payroll_audit
from app.payroll import currencies
from app.payroll.money import amount_str, decimal_str, is_storable, to_decimal
from app.payroll.money_errors import MoneyError
from app.services import leave_service, org_service
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

PENDING, APPROVED, REJECTED, CANCELLED, RETURNED = APPROVAL_REQUEST_STATUSES
DECISIONS = ("APPROVE", "REJECT", "RETURN")
# RETURN sends the request back to the requester to edit and resubmit (AS-M4 interim of LH-M5a, IG-AS-81).
RETURNABLE_MODULES = frozenset(("attendance", "timesheet", "team_inputs"))
# Modules with routes, team_lead steps, SLA due times, skip-level for a barred requester and the SoD error codes
# (04-attendance/04 §9; JC-AS-14, -21, -46). Other modules keep their behaviour unchanged.
ROUTED_MODULES = frozenset(("attendance", "timesheet", "shift_request"))

# Permission an approver of each module must hold (3.9 §5.1); leave keeps leave:approve in its own service.
MODULE_PERMISSIONS: Dict[str, str] = {
    "payroll_run": "payroll:run:approve",
    "off_cycle": "payroll:run:approve",
    "final_settlement": "payroll:run:approve",
    "compensation": "payroll:compensation:approve",
    "wage_input": "payroll:inputs:approve",
    "overtime": "overtime:approve",
    "task": "task:approve",
    "reimbursement": "payroll:reimbursements:approve",
    "loan": "payroll:loans:approve",
    "bank_change": "payroll:bank_details:approve",
    "payment_batch": "payroll:payment:approve",
    "lop": "attendance:regularize",
    "attendance": "attendance:approve",  # JC-AS-14 (AS-M4); lop keeps attendance:regularize
    "timesheet": "attendance:approve",
    "settings": "settings:approve_changes",
    "shift_request": "roster:approve",  # AS-M6 (migration 0058): routes, team_lead steps, SLA like attendance
    "team_inputs": "payroll:inputs:approve",  # manager-prepared batches: payroll commits them (migration 0058)
}
ENGINE_MODULES = tuple(m for m in APPROVAL_MODULES if m != "leave")
assert set(MODULE_PERMISSIONS) == set(ENGINE_MODULES), "every approval module needs an approve permission"

FINANCE_PERMISSION = "payroll:payment:approve"
# Request-step approver type of the default chain (migration 0014); never configured on a chain.
PERMISSION_HOLDERS = "permission_holders"
HIERARCHY_TYPES = ("primary_manager", "skip_level_manager", "department_head")
ADMIN_ROLE_KEY = "company_admin"
NO_ELIGIBLE_APPROVER = "NO_ELIGIBLE_APPROVER"
RESERVED_CONTEXT_KEYS = ("subject_employee_id", "pending_reason")
MAX_SCAN = 1000
NOTIFY_LIMIT = 25
INBOX_URL = "/payroll/approvals"
# Per-module inbox links (JC-AS-14): attendance items are decided in the module-neutral inbox.
MODULE_INBOX_URLS: Dict[str, str] = {"attendance": "/approvals?module=attendance",
                                     "timesheet": "/approvals?module=timesheet",
                                     "shift_request": "/attendance/shift-requests",
                                     "team_inputs": "/payroll/team-inputs?tab=approvals"}


def inbox_url(module: str) -> str:
    return MODULE_INBOX_URLS.get(module, INBOX_URL)


AUDIT_MODULES: Dict[str, str] = {
    "payroll_run": payroll_audit.PAYROLL_RUNS,
    "off_cycle": payroll_audit.PAYROLL_RUNS,
    "final_settlement": payroll_audit.PAYROLL_RUNS,
    "compensation": payroll_audit.COMPENSATION,
    "wage_input": payroll_audit.PAYROLL_INPUTS,
    "overtime": payroll_audit.PAYROLL_INPUTS,
    "task": payroll_audit.PAYROLL_INPUTS,
    "lop": payroll_audit.PAYROLL_INPUTS,
    "attendance": "Attendance",  # BUG-AS-59
    "timesheet": "Attendance",
    "shift_request": "Shifts",
    "team_inputs": payroll_audit.PAYROLL_INPUTS,
    "reimbursement": payroll_audit.REIMBURSEMENTS,
    "loan": payroll_audit.LOANS,
    "bank_change": payroll_audit.BANK_ACCOUNTS,
    "payment_batch": payroll_audit.PAYMENTS,
    "settings": payroll_audit.PAYROLL_SETTINGS,
}

MODULE_LABELS: Dict[str, str] = {
    "payroll_run": "payroll run",
    "off_cycle": "off-cycle run",
    "final_settlement": "final settlement",
    "compensation": "compensation change",
    "wage_input": "payroll input",
    "overtime": "overtime",
    "task": "task",
    "lop": "loss-of-pay adjustment",
    "attendance": "attendance request",
    "timesheet": "timesheet",
    "shift_request": "shift request",
    "team_inputs": "team payroll batch",
    "reimbursement": "reimbursement claim",
    "loan": "loan",
    "bank_change": "bank details change",
    "payment_batch": "payment batch",
    "settings": "settings change",
}

APPROVER_LABELS: Dict[str, str] = {
    **leave_service.APPROVER_LABELS,
    "finance_role": "Finance approvers",
    "specific_user": "Named approver",
    PERMISSION_HOLDERS: "Approvers",
    "team_lead": "Team lead",
}

APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
APPROVAL_APPROVED = "APPROVAL_APPROVED"
APPROVAL_REJECTED = "APPROVAL_REJECTED"
APPROVAL_CANCELLED = "APPROVAL_CANCELLED"
APPROVAL_WITHDRAWN = "APPROVAL_WITHDRAWN"
APPROVAL_REROUTED = "APPROVAL_REROUTED"
APPROVAL_RETURNED = "APPROVAL_RETURNED"

DecidedCallback = Callable[[Session, Optional[Principal], ApprovalRequest], None]
_CALLBACKS: Dict[Tuple[str, str], List[DecidedCallback]] = {}


# ---------------------------------------------------------------------------
# Callbacks of owning modules
# ---------------------------------------------------------------------------
def register_callback(module: str, subject_type: str, callback: DecidedCallback) -> None:
    """Run `callback(db, principal, request)` when a request of (module, subject_type) reaches a terminal
    status (request.status tells which). It runs inside the deciding transaction; raising aborts it."""
    _check_module(module)
    callbacks = _CALLBACKS.setdefault((module, subject_type), [])
    if callback not in callbacks:
        callbacks.append(callback)


def on_decided(module: str, subject_type: str):
    """Decorator form of register_callback."""

    def decorator(fn: DecidedCallback) -> DecidedCallback:
        register_callback(module, subject_type, fn)
        return fn

    return decorator


def _run_callbacks(db: Session, principal: Optional[Principal], request: ApprovalRequest) -> None:
    for callback in list(_CALLBACKS.get((request.module, request.subject_type), ())):
        callback(db, principal, request)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _check_module(module: str) -> None:
    if module not in MODULE_PERMISSIONS:
        raise bad_request(f"Unknown approval module {module!r}.", "INVALID_APPROVAL_MODULE")


def subject_employee_id(request: ApprovalRequest) -> Optional[int]:
    value = (request.context or {}).get("subject_employee_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_list(value: Any) -> List[int]:
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


def step_permission(request: ApprovalRequest, step: ApprovalRequestStep) -> str:
    return FINANCE_PERMISSION if step.approver_type == "finance_role" else MODULE_PERMISSIONS[request.module]


def _current_step(request: ApprovalRequest) -> Optional[ApprovalRequestStep]:
    return next((s for s in request.steps if s.status == "pending"), None)


def _barred_user_ids(request: ApprovalRequest) -> Set[int]:
    barred = set(_int_list((request.context or {}).get("maker_user_ids")))
    barred.update(s.acted_by_user_id for s in request.steps if s.status == "approved")
    if request.requested_by_user_id is not None:
        barred.add(request.requested_by_user_id)
    barred.discard(None)
    return barred


def _is_barred(request: ApprovalRequest, principal: Principal) -> bool:
    if principal.user_id in _barred_user_ids(request):
        return True
    subject = subject_employee_id(request)
    return subject is not None and principal.employee_id == subject


def _set_reason(request: ApprovalRequest, reason: Optional[str]) -> None:
    context = dict(request.context or {})
    if context.get("pending_reason") == reason or (reason is None and "pending_reason" not in context):
        return
    if reason is None:
        context.pop("pending_reason", None)
    else:
        context["pending_reason"] = reason
    request.context = context


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"approval context cannot hold {type(value).__name__} values")


def _clean_context(context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise TypeError("approval context must be a mapping")
    cleaned = json.loads(json.dumps(dict(context), default=_json_default))
    for key in RESERVED_CONTEXT_KEYS:
        cleaned.pop(key, None)
    if "maker_user_ids" in cleaned:
        cleaned["maker_user_ids"] = sorted(set(_int_list(cleaned["maker_user_ids"])))
    return cleaned


def _money_json(request: ApprovalRequest) -> Optional[dict]:
    if request.amount is None:
        return None
    return {"amount": amount_str(request.amount, request.currency), "currency": request.currency}


class _Context:
    """Per-call caches: principals of candidate approvers and scope checks, rebuilt from the database."""

    def __init__(self, db: Session, company_id: int):
        self.db = db
        self.company_id = company_id
        self._company: Optional[Company] = None
        self._today: Optional[date] = None
        self._admin_role_id: Optional[int] = None
        self._principals: Dict[int, Optional[Principal]] = {}
        self._coverage: Dict[Tuple[int, str, int], bool] = {}
        self._names: Dict[Tuple[str, int], Optional[str]] = {}
        self._delegators: Dict[Tuple[int, str], List[int]] = {}
        self._resolved: Dict[tuple, List[int]] = {}

    @property
    def company(self) -> Company:
        if self._company is None:
            self._company = self.db.get(Company, self.company_id)
        return self._company

    @property
    def today(self) -> date:
        if self._today is None:
            self._today = clock.company_today(self.db, self.company_id)
        return self._today

    @property
    def admin_role_id(self) -> int:
        if self._admin_role_id is None:
            self._admin_role_id = self.db.query(Role.id).filter(
                Role.company_id == self.company_id, Role.system_key == ADMIN_ROLE_KEY
            ).scalar()
        return self._admin_role_id

    def seed(self, principal: Principal) -> None:
        self._principals[principal.user_id] = principal

    def principal_for_user(self, user_id: Optional[int]) -> Optional[Principal]:
        """The user's live grants, or None when the user is missing or not active."""
        if user_id is None:
            return None
        if user_id not in self._principals:
            user = (
                self.db.query(User)
                .filter(User.company_id == self.company_id, User.id == user_id)
                .populate_existing()
                .one_or_none()
            )
            active = user is not None and user.status == "active"
            self._principals[user_id] = build_principal(self.db, user, None, self.company) if active else None
        return self._principals[user_id]

    def delegators(self, employee_id: int, module: str) -> List[int]:
        key = (employee_id, module)
        if key not in self._delegators:
            self._delegators[key] = org_service.active_delegators_for(self.db, employee_id, on=self.today, module=module)
        return self._delegators[key]

    def principal_for_employee(self, employee_id: int) -> Optional[Principal]:
        employee = self.db.get(Employee, employee_id)
        if employee is None or not employee.is_active or employee.user_id is None:
            return None
        return self.principal_for_user(employee.user_id)

    def covers(self, principal: Principal, permission: str, request: ApprovalRequest) -> bool:
        """`principal` holds `permission` at a scope covering the request's subject."""
        if not principal.has(permission):
            return False
        employee_id = subject_employee_id(request)
        if employee_id is not None:
            key = (principal.user_id, permission, employee_id)
            if key not in self._coverage:
                self._coverage[key] = authz.can_access_employee(self.db, principal, permission, employee_id)
            return self._coverage[key]
        if principal.has_company_scope(permission):
            return True
        context = request.context or {}
        for scope, key in ((Scope.LEGAL_ENTITY, "legal_entity_id"), (Scope.PAY_GROUP, "pay_group_id")):
            target = context.get(key)
            if isinstance(target, int) and target in principal.scope_targets(permission, scope.value):
                return True
        return False

    def user_name(self, user_id: Optional[int]) -> Optional[str]:
        if user_id is None:
            return None
        key = ("user", user_id)
        if key not in self._names:
            user = self.db.get(User, user_id)
            self._names[key] = user.full_name if user else None
        return self._names[key]

    def employee_name(self, employee_id: Optional[int]) -> Optional[str]:
        if employee_id is None:
            return None
        key = ("employee", employee_id)
        if key not in self._names:
            employee = self.db.get(Employee, employee_id)
            self._names[key] = employee.full_name if employee else None
        return self._names[key]

    def role_name(self, role_id: Optional[int]) -> Optional[str]:
        if role_id is None:
            return None
        key = ("role", role_id)
        if key not in self._names:
            role = self.db.get(Role, role_id)
            self._names[key] = role.name if role else None
        return self._names[key]


# ---------------------------------------------------------------------------
# Approver resolution
# ---------------------------------------------------------------------------
def _hierarchy_approver(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> Optional[Employee]:
    employee_id = subject_employee_id(request)
    subject = ctx.db.get(Employee, employee_id) if employee_id is not None else None
    if subject is None:
        return None
    approver = leave_service.resolve_step_employee(ctx.db, subject, step)
    if (request.module in ROUTED_MODULES and approver is not None and request.requested_by_user_id is not None
            and approver.user_id == request.requested_by_user_id):
        # Barred as REQUESTER (a manager filing for a report): the step resolves skip-level, the same approver type
        # applied to the requester (04-attendance/04 §9.9 rule 1, GV-AS-REG-15).
        requester = ctx.db.query(Employee).filter(Employee.company_id == ctx.company_id,
                                                  Employee.user_id == request.requested_by_user_id).first()
        return leave_service.resolve_step_employee(ctx.db, requester, step) if requester is not None else None
    return approver


def _permission_holder_user_ids(db: Session, permission: str) -> List[int]:
    """Active users whose active roles grant `permission` (explicitly, or implied by full access)."""
    now = utcnow()
    roles = Role.id.in_(select(RolePermission.role_id).where(RolePermission.permission_code == permission))
    definition = PERMISSIONS_BY_CODE.get(permission)
    if definition is not None and definition.implied_scopes:
        roles = or_(roles, Role.is_full_access.is_(True))
    rows = (
        db.query(UserRoleAssignment.user_id)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .join(User, User.id == UserRoleAssignment.user_id)
        .filter(
            roles,
            User.status == "active",
            UserRoleAssignment.effective_from <= now,
            or_(UserRoleAssignment.effective_until.is_(None), UserRoleAssignment.effective_until > now),
        )
        .distinct()
        .order_by(UserRoleAssignment.user_id)
        .all()
    )
    return [r[0] for r in rows]


def _work_date(request: ApprovalRequest) -> Optional[date]:
    raw = (request.context or {}).get("work_date")
    try:
        return date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _later_steps(request: ApprovalRequest, step: ApprovalRequestStep) -> bool:
    return any(s.step_order > step.step_order and s.status in ("waiting", "pending") for s in request.steps)


def _team_lead_employee_ids(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> List[int]:
    """X 01 §7.2 through team_service; with nobody resolved and no later step, the last resort is the subject's
    primary manager (04-attendance/04 §9.3, GV-AS-REG-09)."""
    key = ("team_lead", request.id, step.step_order)
    if key not in ctx._resolved:
        from app.services import team_service

        employee_id = subject_employee_id(request)
        found: List[int] = []
        if employee_id is not None:
            resolution = team_service.resolve_team_lead(
                ctx.db, ctx.company_id, employee_id, _work_date(request) or ctx.today, action_date=ctx.today,
                module=request.module)
            found = list(resolution.approver_employee_ids)
            if not found and not _later_steps(request, step):
                manager = ctx.db.query(Employee.manager_id).filter(
                    Employee.company_id == ctx.company_id, Employee.id == employee_id).scalar()
                found = [manager] if manager else []
        ctx._resolved[key] = [e for e in found if e != employee_id]
    return ctx._resolved[key]


def _escalated_user_ids(request: ApprovalRequest, step: ApprovalRequestStep) -> List[int]:
    raw = ((request.context or {}).get("escalated_user_ids") or {}).get(str(step.step_order))
    return _int_list(raw)


def _candidate_user_ids(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> List[int]:
    base = _base_candidates(ctx, request, step)
    return base + [u for u in _escalated_user_ids(request, step) if u not in base]


def _base_candidates(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> List[int]:
    kind = step.approver_type
    if kind == "team_lead":
        users = []
        for employee_id in _team_lead_employee_ids(ctx, request, step):
            employee = ctx.db.get(Employee, employee_id)
            if employee is not None and employee.user_id:
                users.append(employee.user_id)
        return users
    if kind == "role":
        return leave_service.role_member_user_ids(ctx.db, step.role_id)
    if kind == "specific_user":
        return [step.specific_user_id]
    if kind in HIERARCHY_TYPES:
        approver = _hierarchy_approver(ctx, request, step)
        return [approver.user_id] if approver is not None and approver.user_id else []
    return _permission_holder_user_ids(ctx.db, step_permission(request, step))


def _is_step_approver(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep, principal: Principal) -> bool:
    if _is_barred(request, principal):
        return False
    if principal.user_id in _escalated_user_ids(request, step):  # SLA escalation target (§9.5)
        return ctx.covers(principal, step_permission(request, step), request)
    kind = step.approver_type
    if kind == "team_lead":
        if principal.employee_id not in _team_lead_employee_ids(ctx, request, step):
            return False
        return ctx.covers(principal, step_permission(request, step), request)
    if kind == "role" and step.role_id not in principal.role_ids:
        return False
    if kind == "specific_user" and principal.user_id != step.specific_user_id:
        return False
    if kind in HIERARCHY_TYPES:
        approver = _hierarchy_approver(ctx, request, step)
        if approver is None or approver.id != principal.employee_id:
            return False
    return ctx.covers(principal, step_permission(request, step), request)


def _eligible_user_ids(
    ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep, limit: Optional[int] = None
) -> List[int]:
    found: List[int] = []
    for user_id in _candidate_user_ids(ctx, request, step):
        principal = ctx.principal_for_user(user_id)
        if principal is not None and _is_step_approver(ctx, request, step, principal):
            found.append(user_id)
            if limit is not None and len(found) >= limit:
                break
    return found


def _can_act(ctx: _Context, request: ApprovalRequest, principal: Principal) -> Optional[str]:
    """"approver", "delegate" or None for the request's current step."""
    if request.status != PENDING or _is_barred(request, principal):
        return None
    step = _current_step(request)
    if step is None:
        return None
    if _is_step_approver(ctx, request, step, principal):
        return "approver"
    if principal.employee_id is None:
        return None
    for delegator_id in ctx.delegators(principal.employee_id, request.module):
        delegator = ctx.principal_for_employee(delegator_id)
        if delegator is not None and _is_step_approver(ctx, request, step, delegator):
            return "delegate"
    return None


def _access(ctx: _Context, request: ApprovalRequest, principal: Principal) -> Tuple[bool, Optional[str]]:
    """(visible, acting_as). Visible to the requester, step actors, current approvers and delegates, and
    holders of the module or a step permission covering the subject (named role members need the step
    permission at some scope, so administrators without payroll access never see payroll requests)."""
    acting = _can_act(ctx, request, principal)
    if acting is not None:
        return True, acting
    if principal.user_id == request.requested_by_user_id:
        return True, None
    if any(s.acted_by_user_id == principal.user_id for s in request.steps):
        return True, None
    permissions = {MODULE_PERMISSIONS[request.module]} | {step_permission(request, s) for s in request.steps}
    for step in request.steps:
        named = (step.approver_type == "role" and step.role_id in principal.role_ids) or (
            step.approver_type == "specific_user" and step.specific_user_id == principal.user_id
        )
        if named and principal.has(step_permission(request, step)):
            return True, None
    return any(ctx.covers(principal, p, request) for p in sorted(permissions)), None


def holds_any_approval_right(db: Session, principal: Principal) -> bool:
    """Function-level right to decide: an approve permission of some engine module, or an active delegation."""
    if any(principal.has(p) for p in set(MODULE_PERMISSIONS.values()) | {FINANCE_PERMISSION}):
        return True
    if principal.employee_id is None:
        return False
    today = clock.company_today(db, principal.company_id)
    found = db.query(ApprovalDelegation.id).filter(
        ApprovalDelegation.delegate_employee_id == principal.employee_id,
        ApprovalDelegation.module.in_(ENGINE_MODULES),
        ApprovalDelegation.revoked_at.is_(None),
        ApprovalDelegation.starts_on <= today,
        ApprovalDelegation.ends_on >= today,
    ).first()
    return found is not None


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def _notify_step(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> None:
    """Tell the step's approvers on the channels of notify.payroll.approval_requested (no amounts)."""
    from app.payroll.services import notifications as payroll_notifications

    channels = payroll_notifications.approval_channels(ctx.db, request)
    if not channels:
        return
    label = MODULE_LABELS[request.module]
    user_ids = _eligible_user_ids(ctx, request, step, limit=NOTIFY_LIMIT)
    if "IN_APP" in channels:
        for user_id in user_ids:
            NotificationService.create(
                ctx.db, user_id, f"{label.capitalize()} awaiting your approval",
                f"Approval request #{request.id} ({label}) is waiting for your decision.",
                "approval", _link(request),
            )
    if "EMAIL" in channels:
        payroll_notifications.queue_approval_emails(ctx.db, request, step, user_ids)


def _link(request: ApprovalRequest) -> str:
    base = inbox_url(request.module)
    return f"{base}{'&' if '?' in base else '?'}id={request.id}"


def step_sla_hours(request: ApprovalRequest, step: ApprovalRequestStep) -> Optional[int]:
    raw = ((request.context or {}).get("sla_hours") or {}).get(str(step.step_order))
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else None


def _set_pending(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> None:
    was_waiting = step.status == "waiting"
    step.status = "pending"
    request.current_step = step.step_order
    _set_reason(request, None)
    if was_waiting:
        hours = step_sla_hours(request, step)
        if hours is not None and step.due_at is None:
            from datetime import timedelta

            step.due_at = clock.utcnow() + timedelta(hours=hours)
        _notify_step(ctx, request, step)


def _finish(ctx: _Context, request: ApprovalRequest, status: str, principal: Optional[Principal], *,
            notify: bool = True, callbacks: bool = True) -> None:
    request.status = status
    request.current_step = None
    request.decided_at = utcnow()
    for step in request.steps:
        if step.status in ("waiting", "pending"):
            step.status = "cancelled"
    _set_reason(request, None)
    if notify and request.requested_by_user_id and (principal is None or principal.user_id != request.requested_by_user_id):
        label = MODULE_LABELS[request.module]
        NotificationService.create(
            ctx.db, request.requested_by_user_id, f"{label.capitalize()} {status.lower()}",
            f"Your {label} approval request #{request.id} was {status.lower()}.",
            "approval", _link(request),
        )
    if callbacks:
        _run_callbacks(ctx.db, principal, request)


def _advance(ctx: _Context, request: ApprovalRequest, principal: Optional[Principal] = None) -> None:
    """Move a pending request to its next step with an eligible approver, finish it, or fall back."""
    for step in request.steps:
        if step.status in ("approved", "skipped"):
            continue
        if step.status in ("rejected", "cancelled"):
            return
        if _eligible_user_ids(ctx, request, step, limit=1):
            _set_pending(ctx, request, step)
            return
        step.status = "skipped"

    if any(s.status == "approved" for s in request.steps):
        _finish(ctx, request, APPROVED, principal)
        return
    fallback = next(
        (s for s in request.steps if s.approver_type == "role" and s.role_id == ctx.admin_role_id), None
    )
    if fallback is None:
        fallback = ApprovalRequestStep(
            company_id=request.company_id,
            step_order=max((s.step_order for s in request.steps), default=0) + 1,
            approver_type="role",
            role_id=ctx.admin_role_id,
            status="waiting",
        )
        request.steps.append(fallback)
    if _eligible_user_ids(ctx, request, fallback, limit=1):
        _set_pending(ctx, request, fallback)
        return
    # Nobody may approve right now: the request waits for an eligible administrator, never auto-approves.
    fallback.status = "pending"
    request.current_step = fallback.step_order
    _set_reason(request, NO_ELIGIBLE_APPROVER)


def _step_applies(step: ApprovalChainStep, amount: Optional[Decimal], currency: Optional[str], context: dict) -> bool:
    if step.min_amount is not None and amount is not None and currency == step.min_amount_currency:
        if amount < step.min_amount:
            return False
    if step.min_days is not None:
        try:
            days = to_decimal(context["days"])
        except (KeyError, MoneyError):
            days = None
        if days is not None and days < step.min_days:
            return False
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _audit(db: Session, principal: Optional[Principal], request: ApprovalRequest, action: str, description: str,
           **details: Any) -> None:
    AuditLogService.log(
        db, principal, AUDIT_MODULES[request.module], action, description, "approval_request", request.id,
        company_id=request.company_id, actor_type="user" if principal else "system",
        details={
            "request_id": request.id,
            "module": request.module,
            "subject_type": request.subject_type,
            "subject_id": request.subject_id,
            "subject_employee_id": subject_employee_id(request),
            "status": request.status,
            "current_step": request.current_step,
            **details,
        },
    )


def submit(
    db: Session,
    principal: Optional[Principal],
    *,
    module: str,
    subject_type: str,
    subject_id: int,
    subject_employee_id: Optional[int] = None,
    amount: Any = None,
    currency: Optional[str] = None,
    context: Optional[Mapping[str, Any]] = None,
    attributes: Optional[Mapping[str, Any]] = None,
) -> ApprovalRequest:
    """Open an approval request for one subject and route it to its first approvers.

    ``attributes`` (routed modules) are the subject's values matched against ``approval_routes.criteria``: the
    ACTIVE route with the highest priority whose criteria all match is selected now and never replaced (§9.2).

    `principal` None means the system raises the request (no requester). At most one request per subject
    is pending (409 APPROVAL_ALREADY_PENDING). `amount` (decimal string, int or Decimal, rounded to the
    currency) drives min_amount steps.
    """
    _check_module(module)
    if not isinstance(subject_type, str) or not 0 < len(subject_type) <= 40:
        raise bad_request("subject_type must be 1-40 characters.", "INVALID_APPROVAL_SUBJECT")
    if isinstance(subject_id, bool) or not isinstance(subject_id, int):
        raise bad_request("subject_id must be an integer.", "INVALID_APPROVAL_SUBJECT")
    company_id = principal.company_id if principal is not None else current_company_id(db)
    if company_id is None:
        raise RuntimeError("approvals.submit needs a principal or a tenant-bound session")

    value: Optional[Decimal] = None
    if (amount is None) != (currency is None):
        raise bad_request("An approval amount always carries its currency.", "AMOUNT_CURRENCY_REQUIRED")
    if amount is not None:
        try:
            currency = currencies.require_money_currency(currency).code
            value = to_decimal(amount)
            amount_str(value, currency)
            if not is_storable(value):
                raise MoneyError("AMOUNT_NOT_STORABLE", "The amount is too large to store.")
        except MoneyError as exc:
            raise bad_request(exc.message, exc.code)

    data = _clean_context(context)
    if subject_employee_id is not None:
        found = db.query(Employee.id).filter(Employee.company_id == company_id, Employee.id == subject_employee_id).first()
        if found is None:
            raise not_found("Employee")
        data["subject_employee_id"] = subject_employee_id

    duplicate = pending_request(db, module, subject_type, subject_id)
    if duplicate is not None:
        raise conflict("An approval request for this item is already pending.", "APPROVAL_ALREADY_PENDING")

    route = select_route(db, company_id, module, attributes or {}) if module in ROUTED_MODULES else None
    chain = (
        db.query(ApprovalChainStep)
        .filter(ApprovalChainStep.company_id == company_id, ApprovalChainStep.module == module,
                ApprovalChainStep.route_id == route.id if route is not None else ApprovalChainStep.route_id.is_(None))
        .order_by(ApprovalChainStep.step_order)
        .all()
    )
    applicable = [s for s in chain if _step_applies(s, value, currency, data)
                  and (module not in ROUTED_MODULES or _condition_applies(s.condition, attributes or {}))]
    if route is not None:
        data["route_key"] = route.route_key
    if module in ROUTED_MODULES:
        default_sla = _default_sla_hours(db, company_id)
        data["sla_hours"] = {str(order): (step.sla_hours or default_sla)
                             for order, step in enumerate(applicable, start=1)} if applicable else {"1": default_sla}
    request = ApprovalRequest(
        company_id=company_id,
        module=module,
        subject_type=subject_type,
        subject_id=subject_id,
        requested_by_user_id=principal.user_id if principal is not None else None,
        status=PENDING,
        amount=value,
        currency=currency if value is not None else None,
        context=data,
    )
    if applicable:
        for order, step in enumerate(applicable, start=1):
            request.steps.append(ApprovalRequestStep(
                company_id=company_id, step_order=order, approver_type=step.approver_type,
                role_id=step.role_id, specific_user_id=step.specific_user_id, status="waiting",
            ))
    else:
        request.steps.append(ApprovalRequestStep(
            company_id=company_id, step_order=1, approver_type=PERMISSION_HOLDERS, status="waiting",
        ))
    db.add(request)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict("An approval request for this item is already pending.", "APPROVAL_ALREADY_PENDING")

    ctx = _Context(db, company_id)
    if principal is not None:
        ctx.seed(principal)
    _advance(ctx, request, principal)
    db.flush()
    _audit(
        db, principal, request, APPROVAL_REQUESTED,
        f"Requested approval of {MODULE_LABELS[module]} {subject_type} #{subject_id} (request #{request.id})",
        amount=_money_json(request),
        steps=[s.approver_type for s in request.steps],
        pending_reason=(request.context or {}).get("pending_reason"),
    )
    return request


def _load(db: Session, company_id: int, request_id: int, lock: bool = False) -> ApprovalRequest:
    query = (
        db.query(ApprovalRequest)
        .options(selectinload(ApprovalRequest.steps))
        .filter(ApprovalRequest.company_id == company_id, ApprovalRequest.id == request_id)
    )
    if lock:
        query = query.with_for_update(of=ApprovalRequest).populate_existing()
    request = query.one_or_none()
    if request is None:
        raise not_found("Approval request")
    return request


def _self_decision(request: ApprovalRequest, message: str, cause: str) -> Exception:
    if request.module not in ROUTED_MODULES:
        return forbidden(message)
    from fastapi import status as http_status

    from app.core.errors import AppError

    return AppError(http_status.HTTP_403_FORBIDDEN, message, "SELF_DECISION_FORBIDDEN", extra={"details": {"cause": cause}})


def _deny(ctx: _Context, request: ApprovalRequest, actor: Principal, visible: bool) -> None:
    if not visible:
        raise not_found("Approval request")
    if request.status != PENDING:
        raise conflict(f"This request is already {request.status.lower()}.", "APPROVAL_ALREADY_DECIDED")
    makers = set(_int_list((request.context or {}).get("maker_user_ids")))
    subject = subject_employee_id(request)
    if subject is not None and actor.employee_id == subject:
        raise _self_decision(request, "You cannot approve a request about your own record.", "SUBJECT")
    if actor.user_id == request.requested_by_user_id:
        raise _self_decision(request, "You cannot approve a request you prepared or submitted.", "REQUESTER")
    if actor.user_id in makers:
        raise _self_decision(request, "You cannot approve a request you prepared or submitted.", "MAKER")
    if actor.user_id in _barred_user_ids(request):
        raise _self_decision(request, "You already approved an earlier step of this request.", "EARLIER_APPROVER")
    raise forbidden("This request is not awaiting your approval.")


def decide(
    db: Session,
    principal: Principal,
    request_id: int,
    decision: str,
    remarks: Optional[str] = None,
    expected_step: Optional[int] = None,
) -> ApprovalRequest:
    """Approve or reject the current step. Everything is re-checked under the request's row lock."""
    decision = str(decision).upper()
    if decision not in DECISIONS:
        raise bad_request("Decision must be APPROVE, REJECT or RETURN.", "INVALID_DECISION")
    remarks = (remarks or "").strip() or None
    if decision == "REJECT" and not remarks:
        raise bad_request("Give a reason for the rejection.", "REMARKS_REQUIRED")
    if decision == "RETURN" and not remarks:
        raise bad_request("Say what the requester should change.", "REMARKS_REQUIRED")

    ctx = _Context(db, principal.company_id)
    actor = ctx.principal_for_user(principal.user_id)
    if actor is None or not holds_any_approval_right(db, actor):
        raise forbidden()
    request = _load(db, principal.company_id, request_id, lock=True)
    # Grants, scope and delegations as of now, after the lock (EC-RUN-07).
    ctx = _Context(db, principal.company_id)
    actor = ctx.principal_for_user(principal.user_id)
    if actor is None:
        raise forbidden()
    visible, acting = _access(ctx, request, actor)
    if acting is None:
        _deny(ctx, request, actor, visible)
    if expected_step is not None and expected_step != request.current_step:
        raise conflict("This request moved on since you loaded it.", "APPROVAL_STEP_CHANGED")

    if decision == "RETURN" and request.module not in RETURNABLE_MODULES:
        raise bad_request("This request cannot be returned.", "INVALID_DECISION")

    step = _current_step(request)
    approve = decision == "APPROVE"
    step.status = {"APPROVE": "approved", "REJECT": "rejected", "RETURN": "returned"}[decision]
    step.acted_by_user_id = actor.user_id
    step.acted_as = acting
    step.acted_at = utcnow()
    step.remarks = remarks
    if approve:
        _advance(ctx, request, actor)
    else:
        _finish(ctx, request, REJECTED if decision == "REJECT" else RETURNED, actor)
    db.flush()
    label = MODULE_LABELS[request.module]
    verb = {"APPROVE": "Approved", "REJECT": "Rejected", "RETURN": "Returned"}[decision]
    _audit(
        db, actor, request,
        {"APPROVE": APPROVAL_APPROVED, "REJECT": APPROVAL_REJECTED, "RETURN": APPROVAL_RETURNED}[decision],
        f"{verb} step {step.step_order} of {label} request #{request.id}"
        + (" (delegated)" if acting == "delegate" else ""),
        step=step.step_order, approver_type=step.approver_type, acted_as=acting, decision=decision,
        remarks=remarks, final=request.status != PENDING,
    )
    return request


def cancel(db: Session, principal: Principal, request_id: int, remarks: Optional[str] = None) -> ApprovalRequest:
    """The requester withdraws a pending request; the owning module's callbacks see CANCELLED."""
    request = _load(db, principal.company_id, request_id, lock=True)
    ctx = _Context(db, principal.company_id)
    ctx.seed(principal)
    if request.requested_by_user_id != principal.user_id:
        if _access(ctx, request, principal)[0]:
            raise forbidden("Only the requester can cancel this request.")
        raise not_found("Approval request")
    if request.status != PENDING:
        raise conflict(f"This request is already {request.status.lower()}.", "APPROVAL_ALREADY_DECIDED")
    remarks = (remarks or "").strip() or None
    _finish(ctx, request, CANCELLED, principal, notify=False)
    db.flush()
    _audit(
        db, principal, request, APPROVAL_CANCELLED,
        f"Cancelled {MODULE_LABELS[request.module]} request #{request.id}", remarks=remarks,
    )
    return request


def withdraw(
    db: Session, principal: Optional[Principal], *, module: str, subject_type: str, subject_id: int, reason: str
) -> Optional[ApprovalRequest]:
    """The owning module cancels a subject's pending request (e.g. a run reopened, EC-RUN-16).

    Callbacks do not run: the caller is the owning module. Returns the request, or None if none was pending.
    """
    _check_module(module)
    request = pending_request(db, module, subject_type, subject_id, lock=True)
    if request is None:
        return None
    ctx = _Context(db, request.company_id)
    _finish(ctx, request, CANCELLED, principal, notify=True, callbacks=False)
    db.flush()
    _audit(
        db, principal, request, APPROVAL_WITHDRAWN,
        f"Withdrew {MODULE_LABELS[module]} request #{request.id}: {reason}"[:500], reason=reason,
    )
    return request


def reroute_pending(db: Session, company_id: int, *, module: Optional[str] = None) -> int:
    """Re-evaluate pending requests whose current step lost every eligible approver (a manager left, a
    user was suspended, a grant was revoked: EC-RUN-21). Returns how many requests moved."""
    query = (
        db.query(ApprovalRequest)
        .options(selectinload(ApprovalRequest.steps))
        .filter(ApprovalRequest.company_id == company_id, ApprovalRequest.status == PENDING)
    )
    if module is not None:
        _check_module(module)
        query = query.filter(ApprovalRequest.module == module)
    ctx = _Context(db, company_id)
    moved = 0
    for request in query.order_by(ApprovalRequest.id).with_for_update(of=ApprovalRequest, skip_locked=True).limit(MAX_SCAN):
        step = _current_step(request)
        if step is None:
            continue
        if _eligible_user_ids(ctx, request, step, limit=1):
            _set_reason(request, None)
            continue
        if step.approver_type == "role" and step.role_id == ctx.admin_role_id:
            _set_reason(request, NO_ELIGIBLE_APPROVER)
            continue
        before = step.step_order
        step.status = "skipped"
        _advance(ctx, request)
        moved += 1
        _audit(
            db, None, request, APPROVAL_REROUTED,
            f"Re-routed {MODULE_LABELS[request.module]} request #{request.id}: step {before} had no eligible approver",
            skipped_step=before, pending_reason=(request.context or {}).get("pending_reason"),
        )
    db.flush()
    return moved


def reroute_after_access_change(db: Session, company_id: int) -> int:
    """Hook for changes that can remove an approver (F-29, EC-RUN-21): a user suspended, deactivated or
    whose employment ended, roles or role grants or scope targets changed, a reporting line changed.

    Flushes the caller's pending change first so eligibility is evaluated against it. Idempotent: a request
    whose current step still has an eligible approver does not move. Never commits.
    """
    db.flush()
    return reroute_pending(db, company_id)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def pending_request(
    db: Session, module: str, subject_type: str, subject_id: int, lock: bool = False
) -> Optional[ApprovalRequest]:
    """The subject's pending request in the session's tenant, for owning modules."""
    query = db.query(ApprovalRequest).filter(
        ApprovalRequest.module == module,
        ApprovalRequest.subject_type == subject_type,
        ApprovalRequest.subject_id == subject_id,
        ApprovalRequest.status == PENDING,
    )
    if lock:
        query = query.with_for_update(of=ApprovalRequest)
    return query.one_or_none()


def latest_request(db: Session, module: str, subject_type: str, subject_id: int) -> Optional[ApprovalRequest]:
    return (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.module == module,
            ApprovalRequest.subject_type == subject_type,
            ApprovalRequest.subject_id == subject_id,
        )
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        .first()
    )


def _step_view(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> dict:
    kind = step.approver_type
    if kind == "role":
        approver = ctx.role_name(step.role_id)
    elif kind == "specific_user":
        approver = ctx.user_name(step.specific_user_id)
    elif kind in HIERARCHY_TYPES:
        resolved = _hierarchy_approver(ctx, request, step) if step.status in ("waiting", "pending") else None
        approver = resolved.full_name if resolved else None
    else:
        definition = PERMISSIONS_BY_CODE.get(step_permission(request, step))
        approver = definition.label if definition else None
    return {
        "step_order": step.step_order,
        "approver_type": kind,
        "approver_label": APPROVER_LABELS.get(kind, kind),
        "approver": approver,
        "role_id": step.role_id,
        "specific_user_id": step.specific_user_id,
        "status": step.status,
        "acted_by": ctx.user_name(step.acted_by_user_id),
        "acted_as": step.acted_as,
        "acted_at": step.acted_at.isoformat() if step.acted_at else None,
        "remarks": step.remarks,
        "due_at": step.due_at.isoformat() if step.due_at else None,
    }


def serialize(ctx: _Context, request: ApprovalRequest, acting_as: Optional[str]) -> dict:
    context = dict(request.context or {})
    employee_id = context.pop("subject_employee_id", None)
    pending_reason = context.pop("pending_reason", None)
    return {
        "id": request.id,
        "module": request.module,
        "module_label": MODULE_LABELS[request.module],
        "subject_type": request.subject_type,
        "subject_id": request.subject_id,
        "subject_employee_id": employee_id,
        "subject_employee_name": ctx.employee_name(employee_id),
        "status": request.status,
        "current_step": request.current_step,
        "pending_reason": pending_reason,
        "amount": _money_json(request),
        "requested_by_user_id": request.requested_by_user_id,
        "requested_by": ctx.user_name(request.requested_by_user_id),
        "context": context,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "steps": [_step_view(ctx, request, s) for s in request.steps],
        "can_act": acting_as is not None,
        "acting_as": acting_as,
    }


def action_result(request: ApprovalRequest) -> dict:
    """Compact result of a state change (stored by Idempotency-Key replays; details come from GET)."""
    return {
        "id": request.id,
        "module": request.module,
        "subject_type": request.subject_type,
        "subject_id": request.subject_id,
        "status": request.status,
        "current_step": request.current_step,
        "pending_reason": (request.context or {}).get("pending_reason"),
    }


def get_request(db: Session, principal: Principal, request_id: int) -> dict:
    request = _load(db, principal.company_id, request_id)
    ctx = _Context(db, principal.company_id)
    ctx.seed(principal)
    visible, acting = _access(ctx, request, principal)
    if not visible:
        raise not_found("Approval request")
    return serialize(ctx, request, acting)


def list_requests(
    db: Session,
    principal: Principal,
    *,
    module: Optional[str] = None,
    status: Optional[str] = None,
    mine: bool = True,
    skip: int = 0,
    limit: int = 50,
) -> Tuple[int, List[dict]]:
    """Requests awaiting the caller (mine) or visible to them. Totals count only what the caller may see
    (EC-SEC-05); the newest MAX_SCAN matching requests are considered."""
    if module is not None:
        _check_module(module)
    if status is not None and status not in APPROVAL_REQUEST_STATUSES:
        raise bad_request("Unknown approval status.", "INVALID_APPROVAL_STATUS")
    if mine and status not in (None, PENDING):
        return 0, []
    query = (
        db.query(ApprovalRequest)
        .options(selectinload(ApprovalRequest.steps))
        .filter(ApprovalRequest.company_id == principal.company_id)
    )
    if module is not None:
        query = query.filter(ApprovalRequest.module == module)
    if mine:
        query = query.filter(ApprovalRequest.status == PENDING)
    elif status is not None:
        query = query.filter(ApprovalRequest.status == status)
    rows = query.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc()).limit(MAX_SCAN).all()

    ctx = _Context(db, principal.company_id)
    ctx.seed(principal)
    matched: List[Tuple[ApprovalRequest, Optional[str]]] = []
    for request in rows:
        visible, acting = _access(ctx, request, principal)
        if (acting is not None) if mine else visible:
            matched.append((request, acting))
    page = matched[skip: skip + limit]
    return len(matched), [serialize(ctx, request, acting) for request, acting in page]


def subjects_awaiting(db: Session, principal: Principal, module: str, subject_type: str) -> List[int]:
    """Subject ids of `module` requests the caller can act on now (for owning modules' list badges)."""
    _check_module(module)
    ctx = _Context(db, principal.company_id)
    ctx.seed(principal)
    rows = (
        db.query(ApprovalRequest)
        .options(selectinload(ApprovalRequest.steps))
        .filter(
            ApprovalRequest.company_id == principal.company_id,
            ApprovalRequest.module == module,
            ApprovalRequest.subject_type == subject_type,
            ApprovalRequest.status == PENDING,
        )
        .limit(MAX_SCAN)
        .all()
    )
    return [r.subject_id for r in rows if _can_act(ctx, r, principal) is not None]


def request_counts(db: Session, principal: Principal, *, mine: bool = True, status: Optional[str] = None) -> dict:
    """Per-module counts for inbox badges (F-44) with exactly the rules of `list_requests`: requests awaiting
    the caller (mine, PENDING only) or visible to them, within the newest MAX_SCAN requests, so a count
    never reveals requests the caller could not open (EC-SEC-05)."""
    if status is not None and status not in APPROVAL_REQUEST_STATUSES:
        raise bad_request("Unknown approval status.", "INVALID_APPROVAL_STATUS")
    counts = {module: 0 for module in ENGINE_MODULES}
    if mine and status not in (None, PENDING):
        return {"mine": mine, "status": status, "counts": counts, "total": 0, "truncated": False}
    query = (
        db.query(ApprovalRequest)
        .options(selectinload(ApprovalRequest.steps))
        .filter(ApprovalRequest.company_id == principal.company_id, ApprovalRequest.module.in_(ENGINE_MODULES))
    )
    if mine:
        query = query.filter(ApprovalRequest.status == PENDING)
    elif status is not None:
        query = query.filter(ApprovalRequest.status == status)
    rows = query.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc()).limit(MAX_SCAN).all()
    ctx = _Context(db, principal.company_id)
    ctx.seed(principal)
    for request in rows:
        visible, acting = _access(ctx, request, principal)
        if (acting is not None) if mine else visible:
            counts[request.module] += 1
    return {"mine": mine, "status": PENDING if mine else status, "counts": counts, "total": sum(counts.values()),
            "truncated": len(rows) >= MAX_SCAN}


# ---------------------------------------------------------------------------
# Approval chains (read-only list for settings pickers, F-25)
# ---------------------------------------------------------------------------
CHAIN_READ_PERMISSIONS = ("payroll:configuration:read", "payroll:configuration:write", "settings:approve_changes")


def _chain_step_json(ctx: _Context, step: ApprovalChainStep) -> dict:
    return {
        "id": step.id, "step_order": step.step_order, "approver_type": step.approver_type,
        "approver_label": APPROVER_LABELS.get(step.approver_type, step.approver_type),
        "role_id": step.role_id, "role_name": ctx.role_name(step.role_id),
        "specific_user_id": step.specific_user_id, "specific_user_name": ctx.user_name(step.specific_user_id),
        "min_days": step.min_days,
        "min_amount": None if step.min_amount is None else {
            "amount": amount_str(step.min_amount, step.min_amount_currency), "currency": step.min_amount_currency},
        "condition": step.condition,
    }


def list_chains(db: Session, principal: Principal, *, module: Optional[str] = None) -> List[dict]:
    """The approval chain of every engine module (approval_chain_steps grouped by module). A chain is
    identified by its module; `id` is the id of its first step (null for a module without configured
    steps, which routes to holders of the module's approve permission)."""
    if not any(principal.has(code) for code in CHAIN_READ_PERMISSIONS):
        raise forbidden()
    if module is not None:
        _check_module(module)
    modules = [module] if module is not None else list(ENGINE_MODULES)
    rows = (db.query(ApprovalChainStep)
            .filter(ApprovalChainStep.company_id == principal.company_id, ApprovalChainStep.module.in_(modules))
            .order_by(ApprovalChainStep.module, ApprovalChainStep.step_order).all())
    grouped: Dict[str, List[ApprovalChainStep]] = {}
    for row in rows:
        grouped.setdefault(row.module, []).append(row)
    ctx = _Context(db, principal.company_id)
    result = []
    for name in modules:
        steps = grouped.get(name, [])
        permission = MODULE_PERMISSIONS[name]
        definition = PERMISSIONS_BY_CODE.get(permission)
        result.append({
            "id": steps[0].id if steps else None, "module": name, "label": MODULE_LABELS.get(name, name),
            "permission": permission, "configured": bool(steps),
            "steps": [_chain_step_json(ctx, s) for s in steps],
            "default_step": None if steps else {
                "step_order": 1, "approver_type": PERMISSION_HOLDERS,
                "approver_label": APPROVER_LABELS[PERMISSION_HOLDERS],
                "approver": definition.label if definition else permission},
        })
    return result


# ---------------------------------------------------------------------------
# Routes (AS-M4 interim store of LH-M5a, IG-AS-81; 04-attendance/04 §9.2, §9.4)
# ---------------------------------------------------------------------------
ROUTE_LIST_KEYS = {"team_ids": "team_ids", "legal_entity_ids": "legal_entity_id", "branch_ids": "branch_id",
                   "request_kinds": "request_kind", "on_duty_kinds": "on_duty_kind", "reason_codes": "reason_code",
                   "affects_payroll_state": "payroll_state", "subject_kinds": "subject_kind",
                   "employee_ids": "employee_id"}
ROUTE_MAX_KEYS = {"days_back_max": "days_back", "minutes_changed_max": "minutes"}
ROUTE_KEYS = frozenset(ROUTE_LIST_KEYS) | frozenset(ROUTE_MAX_KEYS) | {"everyone"}


def validate_criteria(criteria: Mapping[str, Any]) -> Dict[str, Any]:
    """Known keys only (``ROUTE_INVALID`` otherwise); lists of values or integer maxima."""
    if not isinstance(criteria, Mapping):
        raise bad_request("Route criteria must be an object.", "ROUTE_INVALID")
    unknown = sorted(set(criteria) - ROUTE_KEYS)
    if unknown:
        raise bad_request(f"Unknown route criteria: {', '.join(unknown)}.", "ROUTE_INVALID")
    cleaned: Dict[str, Any] = {}
    for key, value in criteria.items():
        if key == "everyone":
            continue
        if key in ROUTE_MAX_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise bad_request(f"{key} must be a whole number of at least 0.", "ROUTE_INVALID")
        elif not isinstance(value, list) or not value:
            raise bad_request(f"{key} must be a non-empty list.", "ROUTE_INVALID")
        cleaned[key] = value
    return cleaned


def _condition_applies(condition: Optional[Mapping[str, Any]], attributes: Mapping[str, Any]) -> bool:
    """Step or route criteria against the subject's attributes; a missing value fails closed (§9.4)."""
    for key, expected in (condition or {}).items():
        if key == "everyone":
            continue
        if key in ROUTE_MAX_KEYS:
            actual = attributes.get(ROUTE_MAX_KEYS[key])
            if not isinstance(actual, int) or actual > expected:
                return False
            continue
        attribute = ROUTE_LIST_KEYS.get(key)
        if attribute is None:
            return False
        actual = attributes.get(attribute)
        values = set(actual) if isinstance(actual, (list, tuple, set, frozenset)) else {actual}
        values.discard(None)
        if not values or not values & set(expected or ()):
            return False
    return True


def select_route(db: Session, company_id: int, module: str, attributes: Mapping[str, Any]):
    from app.models.attendance_requests import ApprovalRoute

    routes = (db.query(ApprovalRoute)
              .filter(ApprovalRoute.company_id == company_id, ApprovalRoute.module == module,
                      ApprovalRoute.status == "ACTIVE")
              .order_by(ApprovalRoute.priority.desc(), ApprovalRoute.id).all())
    return next((r for r in routes if _condition_applies(r.criteria, attributes)), None)


def _default_sla_hours(db: Session, company_id: int) -> int:
    from app.payroll.services import settings as settings_engine

    try:
        value = settings_engine.resolve(db, company_id, "attendance.approval.sla_hours",
                                         on=clock.company_today(db, company_id)).value
    except Exception:  # noqa: BLE001 - registry without the key (older test registries)
        value = None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 48


def preview_route(db: Session, company_id: int, module: str, subject_employee_id: Optional[int],
                  attributes: Mapping[str, Any], *, requester_user_id: Optional[int] = None) -> dict:
    """The route and step approvers a request would get now, without writing (``preview`` of the request form)."""
    route = select_route(db, company_id, module, attributes)
    chain = (db.query(ApprovalChainStep)
             .filter(ApprovalChainStep.company_id == company_id, ApprovalChainStep.module == module,
                     ApprovalChainStep.route_id == route.id if route is not None else ApprovalChainStep.route_id.is_(None))
             .order_by(ApprovalChainStep.step_order).all())
    applicable = [s for s in chain if _condition_applies(s.condition, attributes)]
    probe = ApprovalRequest(id=0, company_id=company_id, module=module, subject_type="preview", subject_id=0,
                            requested_by_user_id=requester_user_id, status=PENDING,
                            context={"subject_employee_id": subject_employee_id,
                                     "work_date": attributes.get("work_date")})
    probe.steps = [ApprovalRequestStep(company_id=company_id, step_order=i, approver_type=s.approver_type,
                                       role_id=s.role_id, specific_user_id=s.specific_user_id, status="waiting")
                   for i, s in enumerate(applicable, start=1)] or [
        ApprovalRequestStep(company_id=company_id, step_order=1, approver_type=PERMISSION_HOLDERS, status="waiting")]
    ctx = _Context(db, company_id)
    steps = []
    for step in probe.steps:
        users = _eligible_user_ids(ctx, probe, step, limit=5)
        steps.append({"step_order": step.step_order, "approver_type": step.approver_type,
                      "label": APPROVER_LABELS.get(step.approver_type, step.approver_type),
                      "approvers": [ctx.user_name(u) for u in users]})
    return {"route_key": route.route_key if route is not None else None, "steps": steps,
            "sla_hours": _default_sla_hours(db, company_id)}


# ---------------------------------------------------------------------------
# SLA scan (job attendance.sla_scan, HOURLY; 04-attendance/04 §9.5). Never approves anything (D-AS-72).
# ---------------------------------------------------------------------------
def _setting_value(db: Session, company_id: int, key: str, default: Any) -> Any:
    from app.payroll.services import settings as settings_engine

    try:
        value = settings_engine.resolve(db, company_id, key, on=clock.company_today(db, company_id)).value
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def sla_scan(db: Session, company_id: int, *, now: Optional[datetime] = None) -> dict:
    """Pending steps of routed modules past ``due_at``: REMIND sends a reminder to the step's eligible users (at most
    ``attendance.approval.max_reminders``, one per SLA period); ESCALATE also adds the escalation target (the
    approver's own manager, else the subject's manager) as an eligible approver of the step, at most
    ``attendance.approval.max_escalations`` levels."""
    from datetime import timedelta

    now = now or clock.utcnow()
    action = _setting_value(db, company_id, "attendance.approval.sla_action", "REMIND")
    max_reminders = int(_setting_value(db, company_id, "attendance.approval.max_reminders", 3))
    max_escalations = int(_setting_value(db, company_id, "attendance.approval.max_escalations", 1))
    result = {"reminded": 0, "escalated": 0, "action": action}
    if action == "NONE":
        return result
    rows = (db.query(ApprovalRequestStep)
            .join(ApprovalRequest, ApprovalRequest.id == ApprovalRequestStep.request_id)
            .filter(ApprovalRequestStep.company_id == company_id, ApprovalRequestStep.status == "pending",
                    ApprovalRequestStep.due_at.isnot(None), ApprovalRequestStep.due_at <= now,
                    ApprovalRequest.status == PENDING, ApprovalRequest.module.in_(sorted(ROUTED_MODULES)))
            .order_by(ApprovalRequestStep.due_at).limit(MAX_SCAN).all())
    ctx = _Context(db, company_id)
    for step in rows:
        request = step.request
        hours = step_sla_hours(request, step) or _default_sla_hours(db, company_id)
        label = MODULE_LABELS[request.module]
        users = _eligible_user_ids(ctx, request, step, limit=NOTIFY_LIMIT)
        if step.reminders_sent < max_reminders:
            for user_id in users:
                NotificationService.create(
                    db, user_id, f"Reminder: {label} awaiting your approval",
                    f"Approval request #{request.id} ({label}) is past its due time.", "approval", _link(request))
            step.reminders_sent += 1
            result["reminded"] += 1
        if action == "ESCALATE" and step.escalation_level < max_escalations:
            target = _escalation_target(ctx, request, step, users)
            if target is not None:
                context = dict(request.context or {})
                escalated = dict(context.get("escalated_user_ids") or {})
                key = str(step.step_order)
                escalated[key] = sorted(set(_int_list(escalated.get(key))) | {target})
                context["escalated_user_ids"] = escalated
                request.context = context
                step.escalation_level += 1
                NotificationService.create(
                    db, target, f"Escalated: {label} awaiting approval",
                    f"Approval request #{request.id} ({label}) passed its due time and was escalated to you.",
                    "approval", _link(request))
                _audit(db, None, request, "APPROVAL_ESCALATED",
                       f"Escalated step {step.step_order} of {label} request #{request.id}",
                       step=step.step_order, level=step.escalation_level, escalated_to_user_id=target)
                result["escalated"] += 1
        step.due_at = now + timedelta(hours=hours)
    db.flush()
    return result


def _escalation_target(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep,
                       approvers: List[int]) -> Optional[int]:
    """``next_manager``: the manager of the step's (first) approver, else the subject's manager; never barred."""
    candidates: List[int] = []
    for user_id in approvers:
        employee = ctx.db.query(Employee).filter(Employee.company_id == ctx.company_id,
                                                 Employee.user_id == user_id).first()
        if employee is not None and employee.manager_id:
            candidates.append(employee.manager_id)
    subject = subject_employee_id(request)
    if subject is not None:
        manager = ctx.db.query(Employee.manager_id).filter(Employee.company_id == ctx.company_id,
                                                           Employee.id == subject).scalar()
        if manager:
            candidates.append(manager)
    barred = _barred_user_ids(request) | set(approvers)
    for employee_id in candidates:
        if employee_id == subject:
            continue
        manager = ctx.db.get(Employee, employee_id)
        if manager is None or not manager.user_id or manager.user_id in barred:
            continue
        principal = ctx.principal_for_user(manager.user_id)
        if principal is not None and ctx.covers(principal, step_permission(request, step), request):
            return manager.user_id
    return None
