"""Payroll notifications: the ``notify.payroll.*`` settings (3.1 §5.17, 3.8 §12, 3.7 §4 and §10; F-68).

Every message goes in-app through ``NotificationService`` and by e-mail through ``EmailService`` (the branded
templates of ``app.services.email_templates``). Messages are produced by background jobs queued in the business
transaction (``jobs.enqueue``), so a rolled-back change notifies nobody.

* ``notify.payroll.approval_requested`` (set of ``IN_APP`` / ``EMAIL``; empty = no message): when a step of a
  payroll approval becomes pending, the approvals engine asks ``approval_channels`` and creates the in-app
  message itself (as before) and/or ``queue_approval_emails`` queues one e-mail per approver. It covers runs,
  revisions, inputs and payments: every engine module except ``bank_change`` (bank changes have their own locked
  alerts) and ``settings`` (setting change requests), which keep the in-app message.
* ``notify.payroll.input_cutoff_reminder_days`` (int 0-31, resolved for the pay group): the daily job
  ``CUTOFF_JOB`` reminds the managers of the employees placed in the pay group and the holders of
  ``payroll:inputs:edit`` / ``payroll:inputs:approve`` covering it, once per open pay period, on the first run date
  within [cutoff - days, cutoff] (a missed day is caught up the next day; 0 = on the cutoff day itself). The
  registry defines no OFF value for this key, so none is invented.
* ``notify.payroll.payment_status_to_employee`` (bool, resolved for the employee): "Salary credited" once per
  fully settled instruction (``payment_credited``), and "Payment failed — please update your bank details" when a
  payment fails or is returned (``bank_accounts._notify`` asks ``payment_status_enabled``). OFF: neither message;
  the account still becomes FAILED and finance still sees the failure.
* ``notify.payroll.bank_change_alert`` (bool, locked ON: ``const`` True and no level may store it): read before
  every bank change alert by ``bank_change_alert_required``, which is fail-safe: a value other than True (possible
  only by writing the table directly) is logged and never silences the alert.
* ``notify.payroll.exceptions_digest`` (``DAILY`` / ``OFF``, resolved for each run's pay group): the daily job
  ``DIGEST_JOB`` sends each holder of ``payroll:run:create`` covering a not-yet-finalized run one digest per date
  counting the run's open exceptions. Nobody is told about runs outside their scope; nothing is sent when nothing
  is open.

Privacy: no message carries an amount, another employee's name, an exception text or a full account number (a
payment names the account masked, as 3.7 §11 allows). No notify setting allows amounts, so none is ever included.

Idempotency ("no duplicate notifications"): each event is one job whose idempotency key is the event key
(approval:<request>:<step>:<user>, cutoff:<period>, digest:<date>, credited:<instruction>) and each e-mail is one
job per event and recipient; ``jobs.enqueue`` returns the first job for the same (company, type, key), and a job's
work commits at most once. Re-running a daily job, a duplicate or later settlement event (RECONCILED after
SETTLED) or a reminder window spanning several days never notifies twice.

Tenant isolation: jobs run in a session bound to their tenant (RLS) and every query filters ``company_id``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytz
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.principal import Principal, build_principal
from app.models.company import Company
from app.models.employee import Employee
from app.models.org import EmployeeAssignment
from app.models.pay_org import PayGroup
from app.models.pay_runs import PayPeriod, PayrollException, PayrollRun
from app.models.user import User
from app.payroll import settings_registry as reg
from app.payroll import state_machines as sm
from app.payroll.services import access, jobs
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

APPROVAL_KEY = "notify.payroll.approval_requested"
CUTOFF_KEY = "notify.payroll.input_cutoff_reminder_days"
PAYMENT_STATUS_KEY = "notify.payroll.payment_status_to_employee"
BANK_ALERT_KEY = "notify.payroll.bank_change_alert"
DIGEST_KEY = "notify.payroll.exceptions_digest"

NOTIFY_JOB = "payroll.notify"                         # one per event (idempotency key = event key)
EMAIL_JOB = "payroll.notify_email"                    # one per event and recipient
CUTOFF_JOB = "payroll.input_cutoff_reminders"         # daily per tenant
DIGEST_JOB = "payroll.exceptions_digest"              # daily per tenant

APPROVAL, CUTOFF, DIGEST, CREDITED = "APPROVAL_REQUESTED", "INPUT_CUTOFF", "EXCEPTIONS_DIGEST", "PAYMENT_CREDITED"
CHANNELS = ("IN_APP", "EMAIL")
# Approval modules outside "runs, revisions, inputs and payments" (3.1 §5.17): in-app as before.
UNCOVERED_APPROVAL_MODULES = frozenset(("bank_change", "settings"))
CUTOFF_STAFF_PERMISSIONS = ("payroll:inputs:edit", "payroll:inputs:approve")
DIGEST_PERMISSION = "payroll:run:create"
MAX_CUTOFF_DAYS = 31
DIGEST_RUN_STATES = tuple(sm.RUN_PRE_FINALIZATION) + ("REOPENED_FOR_CORRECTION",)
DIGEST_MAX_RUNS = 10
DAILY_AT = time(8, 0)                                 # company-local time of the daily jobs
FULLY_SETTLED = ("SETTLED", "RECONCILED")
TEMPLATES = {
    APPROVAL: "payroll.approval_requested",
    CUTOFF: "payroll.input_cutoff_reminder",
    DIGEST: "payroll.exceptions_digest",
    CREDITED: "payroll.payment_status",
}


# ---------------------------------------------------------------------------
# Settings and people
# ---------------------------------------------------------------------------
def _setting(db: Session, company_id: int, key: str, on: date, **scope: Optional[int]) -> Any:
    """The value resolved through the settings engine for ``scope``; the registry default when it cannot be
    resolved (an invalid stored value never silences a notification nor raises in the business transaction)."""
    from app.payroll.services import settings as settings_engine

    try:
        resolved = settings_engine.resolve(db, company_id, key, on=on,
                                           **{k: v for k, v in scope.items() if v is not None})
        if resolved.ok:
            return resolved.value
    except Exception:  # noqa: BLE001 - SettingConfigError, a vanished scope object
        logger.exception("%s could not be resolved for company %s; using the registry default", key, company_id)
    return reg.definition(key).default


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def _resolve_on(db: Session, company_id: int, run_date: date) -> date:
    """The notify keys are not effective-dated (a change applies from the day it is saved): a late daily job
    (``run_date`` in the past) uses today's value, never the one in force on a day already gone."""
    return max(run_date, _today(db, company_id))


class _People:
    """Live principals and users of one tenant (cached per call)."""

    def __init__(self, db: Session, company_id: int):
        self.db = db
        self.company_id = company_id
        self._company: Optional[Company] = None
        self._users: Dict[int, Optional[User]] = {}
        self._principals: Dict[int, Optional[Principal]] = {}
        self._holders: Dict[str, List[int]] = {}

    def user(self, user_id: Optional[int]) -> Optional[User]:
        if user_id is None:
            return None
        if user_id not in self._users:
            user = self.db.query(User).filter(User.company_id == self.company_id, User.id == user_id).one_or_none()
            self._users[user_id] = user if user is not None and user.status == "active" else None
        return self._users[user_id]

    def principal(self, user_id: int) -> Optional[Principal]:
        if user_id not in self._principals:
            user = self.user(user_id)
            if self._company is None:
                self._company = self.db.get(Company, self.company_id)
            self._principals[user_id] = build_principal(self.db, user, None, self._company) if user else None
        return self._principals[user_id]

    def holders(self, permissions: Sequence[str], legal_entity_id: int, pay_group_id: Optional[int]) -> List[int]:
        """Active users holding any of ``permissions`` at a scope covering the entity or pay group."""
        from app.payroll.services.approvals import _permission_holder_user_ids

        found = set()
        for permission in permissions:
            if permission not in self._holders:
                self._holders[permission] = _permission_holder_user_ids(self.db, permission)
            for user_id in self._holders[permission]:
                principal = self.principal(user_id)
                if principal is not None and principal.company_id == self.company_id and \
                        access.target_scope(principal, permission).covers(legal_entity_id, pay_group_id):
                    found.add(user_id)
        return sorted(found)


def _recipient(user: User) -> dict:
    name = (user.full_name or "").strip() or user.email
    return {"first_name": name.split()[0], "name": name, "email": user.email}


def _day(value: date) -> str:
    return value.strftime("%d %b %Y")


def _continued(sentence: str) -> str:
    """A message continuing "Hello <name>, ..." in an e-mail intro."""
    return sentence[:1].lower() + sentence[1:]


def _period_text(period: PayPeriod) -> str:
    return f"{period.period_start.strftime('%d %b')} – {_day(period.period_end)}"


# ---------------------------------------------------------------------------
# Approval requested (notify.payroll.approval_requested)
# ---------------------------------------------------------------------------
def approval_channels(db: Session, request: Any) -> Tuple[str, ...]:
    """The channels that tell approvers a step of ``request`` awaits them (resolved for the request's scope)."""
    if request.module in UNCOVERED_APPROVAL_MODULES:
        return ("IN_APP",)
    context = request.context or {}
    scope: Dict[str, Optional[int]] = {}
    employee_id = context.get("subject_employee_id")
    if isinstance(employee_id, int) and not isinstance(employee_id, bool):
        scope["employee_id"] = employee_id
    elif isinstance(context.get("pay_group_id"), int):
        scope["pay_group_id"] = context["pay_group_id"]
    elif isinstance(context.get("legal_entity_id"), int):
        scope["legal_entity_id"] = context["legal_entity_id"]
    value = _setting(db, request.company_id, APPROVAL_KEY, _today(db, request.company_id), **scope)
    chosen = set(value or ())
    return tuple(c for c in CHANNELS if c in chosen)


def queue_approval_emails(db: Session, request: Any, step: Any, user_ids: Iterable[int]) -> int:
    """One e-mail per approver of the step that just became pending (idempotent per request, step and user)."""
    queued = 0
    for user_id in user_ids:
        jobs.enqueue(db, company_id=request.company_id, type=EMAIL_JOB,
                     payload={"kind": APPROVAL, "request_id": request.id, "step_order": step.step_order,
                              "user_id": user_id},
                     idempotency_key=f"approval:{request.id}:{step.step_order}:{user_id}")
        queued += 1
    return queued


def _approval_email(db: Session, company_id: int, payload: Mapping[str, Any]) -> Optional[Tuple[str, dict]]:
    from app.models.approvals import ApprovalRequest
    from app.payroll.services.approvals import MODULE_LABELS, PENDING
    from app.services.email_branding import link

    request = db.query(ApprovalRequest).filter(ApprovalRequest.company_id == company_id,
                                               ApprovalRequest.id == payload.get("request_id")).one_or_none()
    if request is None or request.status != PENDING or request.current_step != payload.get("step_order"):
        return None  # decided (or moved on) before the e-mail went out
    user = _People(db, company_id).user(payload.get("user_id"))
    if user is None or not user.email:
        return None
    label = MODULE_LABELS.get(request.module, "payroll item")
    period = ""
    if request.subject_type == "payroll_run":
        run = db.query(PayrollRun).filter(PayrollRun.company_id == company_id,
                                          PayrollRun.id == request.subject_id).one_or_none()
        found = (db.query(PayPeriod).filter(PayPeriod.company_id == company_id, PayPeriod.id == run.pay_period_id)
                 .one_or_none() if run is not None else None)
        period = _period_text(found) if found is not None else ""
    return user.email, {
        "recipient": _recipient(user),
        "approval": {"label": label[:1].upper() + label[1:],
                     "description": f"approval request #{request.id} ({label})"},
        "period": {"label": period},
        "link": {"url": link(company_id, f"/payroll/approvals?id={request.id}")},
    }


# ---------------------------------------------------------------------------
# Input cutoff reminders (notify.payroll.input_cutoff_reminder_days)
# ---------------------------------------------------------------------------
def queue_cutoff_reminders(db: Session, company_id: int, run_date: date) -> dict:
    """Queue one reminder event per open period whose window [cutoff - days, cutoff] contains ``run_date``."""
    periods = (db.query(PayPeriod)
               .join(PayGroup, (PayGroup.company_id == PayPeriod.company_id) & (PayGroup.id == PayPeriod.pay_group_id))
               .filter(PayPeriod.company_id == company_id, PayPeriod.status == "OPEN", PayGroup.status == "ACTIVE",
                       PayPeriod.input_cutoff_on >= run_date,
                       PayPeriod.input_cutoff_on <= run_date + timedelta(days=MAX_CUTOFF_DAYS))
               .order_by(PayPeriod.input_cutoff_on, PayPeriod.id).all())
    days_of: Dict[int, int] = {}
    queued: List[int] = []
    on = _resolve_on(db, company_id, run_date)
    for period in periods:
        if period.pay_group_id not in days_of:
            days_of[period.pay_group_id] = int(_setting(db, company_id, CUTOFF_KEY, on,
                                                        pay_group_id=period.pay_group_id))
        if period.input_cutoff_on - timedelta(days=days_of[period.pay_group_id]) <= run_date:
            jobs.enqueue(db, company_id=company_id, type=NOTIFY_JOB, payload={"kind": CUTOFF, "period_id": period.id},
                         idempotency_key=f"cutoff:{period.id}")
            queued.append(period.id)
    return {"run_date": run_date.isoformat(), "queued_period_ids": queued}


def _cutoff_audience(db: Session, company_id: int, period: PayPeriod, group: PayGroup,
                     people: _People) -> Dict[int, str]:
    """{user_id: "STAFF" | "MANAGER"}: input staff covering the pay group, and the managers of its employees."""
    audience: Dict[int, str] = {}
    on = period.input_cutoff_on
    placed = (db.query(Employee.manager_id)
              .join(EmployeeAssignment, (EmployeeAssignment.company_id == Employee.company_id)
                    & (EmployeeAssignment.employee_id == Employee.id))
              .filter(Employee.company_id == company_id, Employee.is_active.is_(True),
                      Employee.manager_id.isnot(None), EmployeeAssignment.pay_group_id == group.id,
                      EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
                      or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))
              .distinct().all())
    manager_ids = sorted({m for (m,) in placed})
    if manager_ids:
        for (user_id,) in (db.query(Employee.user_id)
                           .filter(Employee.company_id == company_id, Employee.id.in_(manager_ids),
                                   Employee.is_active.is_(True), Employee.user_id.isnot(None)).all()):
            if people.user(user_id) is not None:
                audience[user_id] = "MANAGER"
    for user_id in people.holders(CUTOFF_STAFF_PERMISSIONS, group.legal_entity_id, group.id):
        audience[user_id] = "STAFF"
    return audience


def _cutoff_text(period: PayPeriod, group: PayGroup, role: str) -> Tuple[str, str, str]:
    when = _day(period.input_cutoff_on)
    title = f"Payroll inputs close on {when}"
    if role == "STAFF":
        message = (f"Inputs for pay group {group.name} ({_period_text(period)}) close on {when}. Enter and approve "
                   "pending inputs before then; later inputs go to the next period or become exceptions.")
        return title, message, "/payroll/inputs"
    message = (f"Payroll inputs for your team in {group.name} ({_period_text(period)}) close on {when}. Approve "
               "pending leave, overtime and tasks before then.")
    return title, message, "/payroll/approvals"


def _load_cutoff(db: Session, company_id: int, period_id: Any) -> Optional[Tuple[PayPeriod, PayGroup]]:
    period = db.query(PayPeriod).filter(PayPeriod.company_id == company_id, PayPeriod.id == period_id).one_or_none()
    if period is None or period.status != "OPEN":
        return None
    group = db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.id == period.pay_group_id).one()
    return period, group


def _send_cutoff(db: Session, company_id: int, payload: Mapping[str, Any]) -> dict:
    loaded = _load_cutoff(db, company_id, payload.get("period_id"))
    if loaded is None:
        return {"sent": 0}
    period, group = loaded
    people = _People(db, company_id)
    audience = _cutoff_audience(db, company_id, period, group, people)
    for user_id, role in audience.items():
        title, message, path = _cutoff_text(period, group, role)
        NotificationService.create(db, user_id, title, message, "warning", path)
        jobs.enqueue(db, company_id=company_id, type=EMAIL_JOB,
                     payload={"kind": CUTOFF, "period_id": period.id, "user_id": user_id},
                     idempotency_key=f"cutoff:{period.id}:{user_id}")
    return {"sent": len(audience), "period_id": period.id}


def _cutoff_email(db: Session, company_id: int, payload: Mapping[str, Any]) -> Optional[Tuple[str, dict]]:
    from app.services.email_branding import link

    loaded = _load_cutoff(db, company_id, payload.get("period_id"))
    people = _People(db, company_id)
    user = people.user(payload.get("user_id"))
    if loaded is None or user is None or not user.email:
        return None
    period, group = loaded
    role = _cutoff_audience(db, company_id, period, group, people).get(user.id)
    if role is None:
        return None
    _, message, path = _cutoff_text(period, group, role)
    return user.email, {
        "recipient": _recipient(user),
        "cutoff": {"date": _day(period.input_cutoff_on), "group": group.name, "period": _period_text(period),
                   "message": _continued(message)},
        "link": {"url": link(company_id, path)},
    }


# ---------------------------------------------------------------------------
# Exceptions digest (notify.payroll.exceptions_digest)
# ---------------------------------------------------------------------------
def queue_exceptions_digest(db: Session, company_id: int, run_date: date) -> dict:
    jobs.enqueue(db, company_id=company_id, type=NOTIFY_JOB, payload={"kind": DIGEST, "run_date": run_date.isoformat()},
                 idempotency_key=f"digest:{run_date.isoformat()}")
    return {"run_date": run_date.isoformat()}


def digest_by_user(db: Session, company_id: int, run_date: date) -> Dict[int, List[dict]]:
    """{user_id: [{run_id, label, open, blocking}]}: open exceptions of the not-yet-finalized runs whose scope
    resolves DAILY, for each holder of payroll:run:create covering the run. Counts only, never texts."""
    counts = (db.query(PayrollException.run_id, func.count(PayrollException.id),
                       func.count(PayrollException.id).filter(PayrollException.severity == "BLOCKING"))
              .join(PayrollRun, (PayrollRun.company_id == PayrollException.company_id)
                    & (PayrollRun.id == PayrollException.run_id))
              .filter(PayrollException.company_id == company_id, PayrollException.status == "OPEN",
                      PayrollRun.status.in_(DIGEST_RUN_STATES))
              .group_by(PayrollException.run_id).order_by(PayrollException.run_id).all())
    if not counts:
        return {}
    people = _People(db, company_id)
    daily: Dict[int, bool] = {}
    result: Dict[int, List[dict]] = defaultdict(list)
    on = _resolve_on(db, company_id, run_date)
    for run_id, total, blocking in counts:
        run = db.query(PayrollRun).filter(PayrollRun.company_id == company_id, PayrollRun.id == run_id).one()
        if run.pay_group_id not in daily:
            daily[run.pay_group_id] = _setting(db, company_id, DIGEST_KEY, on, pay_group_id=run.pay_group_id) == "DAILY"
        if not daily[run.pay_group_id]:
            continue
        group = db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.id == run.pay_group_id).one()
        item = {"run_id": run.id, "label": f"{run.number} {run.label} · {group.name}", "open": int(total),
                "blocking": int(blocking)}
        for user_id in people.holders((DIGEST_PERMISSION,), run.legal_entity_id, run.pay_group_id):
            result[user_id].append(item)
    return dict(result)


def _digest_text(items: Sequence[dict]) -> Tuple[str, str, str, str]:
    total = sum(i["open"] for i in items)
    blocking = sum(i["blocking"] for i in items)
    runs = len(items)
    title = f"Payroll exceptions: {total} open"
    summary = (f"{total} open exception{'s' if total != 1 else ''} ({blocking} blocking) in {runs} payroll "
               f"run{'s' if runs != 1 else ''}.")
    lines = [f"{i['label']}: {i['open']} open, {i['blocking']} blocking" for i in items[:DIGEST_MAX_RUNS]]
    if runs > DIGEST_MAX_RUNS:
        lines.append(f"and {runs - DIGEST_MAX_RUNS} more runs")
    path = f"/payroll/runs/{items[0]['run_id']}" if runs == 1 else "/payroll/runs"
    return title, summary, "\n".join(lines), path


def _send_digest(db: Session, company_id: int, payload: Mapping[str, Any]) -> dict:
    run_date = date.fromisoformat(str(payload.get("run_date")))
    digests = digest_by_user(db, company_id, run_date)
    for user_id, items in digests.items():
        title, summary, lines, path = _digest_text(items)
        NotificationService.create(db, user_id, title, f"{summary}\n{lines}\nOpen the runs to fix, waive or defer "
                                   "them.", "warning", path)
        jobs.enqueue(db, company_id=company_id, type=EMAIL_JOB,
                     payload={"kind": DIGEST, "run_date": run_date.isoformat(), "user_id": user_id},
                     idempotency_key=f"digest:{run_date.isoformat()}:{user_id}")
    return {"sent": len(digests), "run_date": run_date.isoformat()}


def _digest_email(db: Session, company_id: int, payload: Mapping[str, Any]) -> Optional[Tuple[str, dict]]:
    from app.services.email_branding import link

    user = _People(db, company_id).user(payload.get("user_id"))
    if user is None or not user.email:
        return None
    items = digest_by_user(db, company_id, date.fromisoformat(str(payload.get("run_date")))).get(user.id)
    if not items:
        return None  # everything was resolved before the e-mail went out
    title, summary, lines, path = _digest_text(items)
    return user.email, {
        "recipient": _recipient(user),
        "digest": {"title": title, "summary": summary, "runs": lines},
        "link": {"url": link(company_id, path)},
    }


# ---------------------------------------------------------------------------
# Payment status to employees (notify.payroll.payment_status_to_employee)
# ---------------------------------------------------------------------------
def payment_status_enabled(db: Session, company_id: int, employee_id: Optional[int]) -> bool:
    return _setting(db, company_id, PAYMENT_STATUS_KEY, _today(db, company_id), employee_id=employee_id) is True


def payment_credited(db: Session, row: Any) -> bool:
    """Queue "Salary credited" for a fully settled instruction (once per instruction), when the setting is ON."""
    if row.employee_id is None or row.status not in FULLY_SETTLED:
        return False
    if not payment_status_enabled(db, row.company_id, row.employee_id):
        return False
    jobs.enqueue(db, company_id=row.company_id, type=NOTIFY_JOB, payload={"kind": CREDITED, "instruction_id": row.id},
                 idempotency_key=f"credited:{row.id}")
    return True


def _credited_text(row: Any) -> Tuple[str, str]:
    masked = (row.beneficiary_snapshot or {}).get("account_masked")
    where = f"your bank account {masked}" if masked else "your bank account"
    return ("Salary credited",
            f"Your payment {row.reference or ''}".rstrip() + f" was credited to {where}. "
            "Open Payments in the app for the details.")


def _load_credited(db: Session, company_id: int, instruction_id: Any) -> Optional[Tuple[Any, Employee]]:
    from app.models.payment_flows import PaymentInstruction

    row = (db.query(PaymentInstruction)
           .filter(PaymentInstruction.company_id == company_id, PaymentInstruction.id == instruction_id).one_or_none())
    if row is None or row.status not in FULLY_SETTLED:
        return None
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == row.employee_id).one()
    return row, employee


def _send_credited(db: Session, company_id: int, payload: Mapping[str, Any]) -> dict:
    loaded = _load_credited(db, company_id, payload.get("instruction_id"))
    if loaded is None:
        return {"sent": 0}
    row, employee = loaded
    title, message = _credited_text(row)
    in_app = NotificationService.create(db, employee.user_id, title, message, "success", "/me/payments")
    jobs.enqueue(db, company_id=company_id, type=EMAIL_JOB, payload={"kind": CREDITED, "instruction_id": row.id},
                 idempotency_key=f"credited:{row.id}")
    return {"sent": 1, "in_app": in_app is not None}


def _credited_email(db: Session, company_id: int, payload: Mapping[str, Any]) -> Optional[Tuple[str, dict]]:
    from app.services.email_branding import link

    loaded = _load_credited(db, company_id, payload.get("instruction_id"))
    if loaded is None:
        return None
    row, employee = loaded
    user = _People(db, company_id).user(employee.user_id)
    address = (employee.email or (user.email if user is not None else "") or "").strip()
    if "@" not in address:
        return None
    title, message = _credited_text(row)
    return address, {
        "recipient": {"first_name": employee.first_name, "name": employee.full_name, "email": address},
        "payment": {"title": title, "message": _continued(message)},
        "link": {"url": link(company_id, "/me/payments")},
    }


# ---------------------------------------------------------------------------
# Bank change alerts (notify.payroll.bank_change_alert, locked ON)
# ---------------------------------------------------------------------------
def bank_change_alert_required(db: Session, company_id: int) -> bool:
    """Always True: the alert is locked ON (registry ``const`` True; the settings API refuses every write with
    FIXED_SETTING). The value is still resolved so that a tampered row is noticed, and it never silences an alert."""
    value = _setting(db, company_id, BANK_ALERT_KEY, _today(db, company_id))
    if value is not True:
        logger.warning("%s resolved to %r for company %s; the alert is locked ON and is sent anyway",
                       BANK_ALERT_KEY, value, company_id)
    return True


# ---------------------------------------------------------------------------
# Scheduling and jobs
# ---------------------------------------------------------------------------
def schedule_daily(db: Session, company_id: int, *, on: date) -> None:
    """Queue the tenant's daily notification jobs for ``on`` at 08:00 company time (idempotent per date).
    Called with the daily payroll maintenance (``calendars.schedule_generation``), which re-queues every day."""
    company = db.get(Company, company_id)
    zone = pytz.timezone((company.timezone if company is not None else None) or "UTC")
    run_at = zone.localize(datetime.combine(on, DAILY_AT)).astimezone(pytz.utc)
    for job_type, prefix in ((CUTOFF_JOB, "cutoff-reminders"), (DIGEST_JOB, "exceptions-digest")):
        jobs.enqueue(db, company_id=company_id, type=job_type, payload={"run_date": on.isoformat()},
                     idempotency_key=f"{prefix}:{on.isoformat()}", run_at=run_at, priority=180)


def _run_date(ctx: jobs.JobContext) -> date:
    try:
        return date.fromisoformat(str(ctx.payload.get("run_date")))
    except ValueError as exc:
        raise jobs.PermanentJobError("run_date must be an ISO date") from exc


@jobs.register(CUTOFF_JOB)
def _cutoff_job(ctx: jobs.JobContext) -> dict:
    return queue_cutoff_reminders(ctx.db, ctx.company_id, _run_date(ctx))


@jobs.register(DIGEST_JOB)
def _digest_job(ctx: jobs.JobContext) -> dict:
    return queue_exceptions_digest(ctx.db, ctx.company_id, _run_date(ctx))


_SENDERS: Dict[str, Callable[[Session, int, Mapping[str, Any]], dict]] = {
    CUTOFF: _send_cutoff, DIGEST: _send_digest, CREDITED: _send_credited,
}
_EMAILS: Dict[str, Callable[[Session, int, Mapping[str, Any]], Optional[Tuple[str, dict]]]] = {
    APPROVAL: _approval_email, CUTOFF: _cutoff_email, DIGEST: _digest_email, CREDITED: _credited_email,
}


@jobs.register(NOTIFY_JOB)
def _notify_job(ctx: jobs.JobContext) -> dict:
    sender = _SENDERS.get(ctx.payload.get("kind"))
    if sender is None:
        raise jobs.PermanentJobError(f"Unknown payroll notification {ctx.payload.get('kind')!r}")
    return sender(ctx.db, ctx.company_id, ctx.payload)


@jobs.register(EMAIL_JOB)
def _email_job(ctx: jobs.JobContext) -> dict:
    from app.services.email_service import EmailService

    kind = ctx.payload.get("kind")
    builder = _EMAILS.get(kind)
    if builder is None:
        raise jobs.PermanentJobError(f"Unknown payroll notification {kind!r}")
    content = builder(ctx.db, ctx.company_id, ctx.payload)
    if content is None:
        return {"sent": False, "skipped": True}
    address, context = content
    message = EmailService.compose(TEMPLATES[kind], address, context, ctx.company_id)
    return {"sent": EmailService.deliver(message), "template": TEMPLATES[kind]}


__all__ = [
    "APPROVAL_KEY", "BANK_ALERT_KEY", "CUTOFF_JOB", "CUTOFF_KEY", "DIGEST_JOB", "DIGEST_KEY", "EMAIL_JOB", "NOTIFY_JOB",
    "PAYMENT_STATUS_KEY", "approval_channels", "bank_change_alert_required", "digest_by_user", "payment_credited",
    "payment_status_enabled", "queue_approval_emails", "queue_cutoff_reminders", "queue_exceptions_digest",
    "schedule_daily",
]
