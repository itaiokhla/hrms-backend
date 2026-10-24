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

PENDING, APPROVED, REJECTED, CANCELLED = APPROVAL_REQUEST_STATUSES
DECISIONS = ("APPROVE", "REJECT")

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
    "attendance": "attendance:regularize",
    "settings": "settings:approve_changes",
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

AUDIT_MODULES: Dict[str, str] = {
    "payroll_run": payroll_audit.PAYROLL_RUNS,
    "off_cycle": payroll_audit.PAYROLL_RUNS,
    "final_settlement": payroll_audit.PAYROLL_RUNS,
    "compensation": payroll_audit.COMPENSATION,
    "wage_input": payroll_audit.PAYROLL_INPUTS,
    "overtime": payroll_audit.PAYROLL_INPUTS,
    "task": payroll_audit.PAYROLL_INPUTS,
    "lop": payroll_audit.PAYROLL_INPUTS,
    "attendance": payroll_audit.PAYROLL_INPUTS,
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
    "attendance": "attendance correction",
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
}

APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
APPROVAL_APPROVED = "APPROVAL_APPROVED"
APPROVAL_REJECTED = "APPROVAL_REJECTED"
APPROVAL_CANCELLED = "APPROVAL_CANCELLED"
APPROVAL_WITHDRAWN = "APPROVAL_WITHDRAWN"
APPROVAL_REROUTED = "APPROVAL_REROUTED"

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
    return leave_service.resolve_step_employee(ctx.db, subject, step)


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


def _candidate_user_ids(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> List[int]:
    kind = step.approver_type
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
    kind = step.approver_type
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
                "approval", f"{INBOX_URL}?id={request.id}",
            )
    if "EMAIL" in channels:
        payroll_notifications.queue_approval_emails(ctx.db, request, step, user_ids)


def _set_pending(ctx: _Context, request: ApprovalRequest, step: ApprovalRequestStep) -> None:
    was_waiting = step.status == "waiting"
    step.status = "pending"
    request.current_step = step.step_order
    _set_reason(request, None)
    if was_waiting:
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
            "approval", f"{INBOX_URL}?id={request.id}",
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
) -> ApprovalRequest:
    """Open an approval request for one subject and route it to its first approvers.

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

    chain = (
        db.query(ApprovalChainStep)
        .filter(ApprovalChainStep.company_id == company_id, ApprovalChainStep.module == module)
        .order_by(ApprovalChainStep.step_order)
        .all()
    )
    applicable = [s for s in chain if _step_applies(s, value, currency, data)]
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


def _deny(ctx: _Context, request: ApprovalRequest, actor: Principal, visible: bool) -> None:
    if not visible:
        raise not_found("Approval request")
    if request.status != PENDING:
        raise conflict(f"This request is already {request.status.lower()}.", "APPROVAL_ALREADY_DECIDED")
    makers = set(_int_list((request.context or {}).get("maker_user_ids")))
    if actor.user_id == request.requested_by_user_id or actor.user_id in makers:
        raise forbidden("You cannot approve a request you prepared or submitted.")
    subject = subject_employee_id(request)
    if subject is not None and actor.employee_id == subject:
        raise forbidden("You cannot approve a request about your own record.")
    if actor.user_id in _barred_user_ids(request):
        raise forbidden("You already approved an earlier step of this request.")
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
        raise bad_request("Decision must be APPROVE or REJECT.", "INVALID_DECISION")
    remarks = (remarks or "").strip() or None
    if decision == "REJECT" and not remarks:
        raise bad_request("Give a reason for the rejection.", "REMARKS_REQUIRED")

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

    step = _current_step(request)
    approve = decision == "APPROVE"
    step.status = "approved" if approve else "rejected"
    step.acted_by_user_id = actor.user_id
    step.acted_as = acting
    step.acted_at = utcnow()
    step.remarks = remarks
    if approve:
        _advance(ctx, request, actor)
    else:
        _finish(ctx, request, REJECTED, actor)
    db.flush()
    label = MODULE_LABELS[request.module]
    _audit(
        db, actor, request, APPROVAL_APPROVED if approve else APPROVAL_REJECTED,
        f"{'Approved' if approve else 'Rejected'} step {step.step_order} of {label} request #{request.id}"
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
