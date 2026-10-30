"""Leave type catalogue v2 (LH 03-leave/01 §3): full attributes, usage, frozen fields once in use, row versions.

Types are tenant-wide: every write needs ``leave:manage_policy`` at COMPANY (V3-075). A type is *in use* when a
request, a ledger row, a non-draft plan version, an event, a claim or an encashment references its code; then its
``code``, ``kind``, ``unit`` and ``is_unlimited`` never change (``LEAVE_TYPE_CODE_FROZEN`` / ``LEAVE_TYPE_FIELD_FROZEN``).
Lowering SENSITIVE → STANDARD needs a reason and the typed code (future requests become visible to colleagues).
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import cast, func, String
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.leave import CATEGORY_DEFAULTS, Leave, LeaveType
from app.models.leave_v2 import CompOffClaim, LeaveEncashmentRequest, LeaveEvent
from app.models.payroll_config import Policy, PolicyVersion
from app.models.time_inputs import LeaveBalanceTransaction
from app.services import leave_access
from app.services.audit_log_service import AuditLogService

MANAGE = "leave:manage_policy"
FROZEN_FIELDS = ("kind", "unit", "is_unlimited")
TYPE_FIELDS = ("code", "name", "description", "kind", "category", "unit", "sensitivity", "absence_class",
               "counts_toward_absence_score", "is_paid", "is_unlimited", "is_active", "counts_non_working_days",
               "allow_half_day", "color_token", "icon", "display_order", "name_i18n", "annual_quota")


def display_name(lt: LeaveType, locale: Optional[str] = None) -> str:
    names = lt.name_i18n or {}
    if locale:
        for key in (locale, locale.split("-")[0]):
            if names.get(key):
                return names[key]
    return lt.name


def type_json(lt: LeaveType, *, usage: Optional[dict] = None, locale: Optional[str] = None) -> Dict[str, Any]:
    data = {
        "id": lt.id, "code": lt.code, "name": lt.name, "display_name": display_name(lt, locale),
        "description": lt.description, "kind": lt.kind, "category": lt.category, "unit": lt.unit,
        "sensitivity": lt.sensitivity, "absence_class": lt.absence_class,
        "counts_toward_absence_score": lt.counts_toward_absence_score, "is_paid": lt.is_paid,
        "is_unlimited": lt.is_unlimited, "is_active": lt.is_active,
        "counts_non_working_days": lt.counts_non_working_days, "allow_half_day": lt.allow_half_day,
        "color_token": lt.color_token, "icon": lt.icon, "display_order": lt.display_order,
        "name_i18n": lt.name_i18n or {}, "annual_quota": lt.annual_quota, "row_version": lt.row_version,
    }
    if usage is not None:
        data["usage"] = usage
    return data


def usage(db: Session, company_id: int, code: str) -> Dict[str, Any]:
    """03-leave/01 §3.9: what references the type's code."""
    leaves = db.query(func.count(Leave.id)).filter(Leave.company_id == company_id, Leave.leave_type == code).scalar()
    ledger = db.query(func.count(LeaveBalanceTransaction.id)).filter(
        LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.leave_type == code).scalar()
    needle = f'"leave_type_code": "{code}"'
    plans = [{"id": pid, "name": name} for pid, name in (
        db.query(Policy.id, Policy.name).join(PolicyVersion, (PolicyVersion.company_id == Policy.company_id)
                                              & (PolicyVersion.policy_id == Policy.id))
        .filter(Policy.company_id == company_id, Policy.type == "LEAVE_PLAN",
                cast(PolicyVersion.fields, String).contains(needle)).distinct().all())]
    active_plans = db.query(func.count(PolicyVersion.id)).join(
        Policy, (Policy.company_id == PolicyVersion.company_id) & (Policy.id == PolicyVersion.policy_id)).filter(
        Policy.company_id == company_id, Policy.type.in_(("LEAVE_PLAN", "LEAVE_PAY_POLICY")),
        PolicyVersion.status != "DRAFT", cast(PolicyVersion.fields, String).contains(needle)).scalar()
    events = db.query(func.count(LeaveEvent.id)).filter(LeaveEvent.company_id == company_id,
                                                         LeaveEvent.leave_type_code == code).scalar()
    claims = db.query(func.count(CompOffClaim.id)).filter(CompOffClaim.company_id == company_id,
                                                           CompOffClaim.leave_type_code == code).scalar()
    encash = db.query(func.count(LeaveEncashmentRequest.id)).filter(
        LeaveEncashmentRequest.company_id == company_id, LeaveEncashmentRequest.leave_type_code == code).scalar()
    pending = db.query(func.count(Leave.id)).filter(Leave.company_id == company_id, Leave.leave_type == code,
                                                    Leave.status == "pending").scalar()
    holders = db.query(func.count(func.distinct(LeaveBalanceTransaction.employee_id))).filter(
        LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.leave_type == code).scalar()
    in_use = bool(leaves or ledger or active_plans or events or claims or encash)
    return {"leaves": leaves, "pending_requests": pending, "ledger_rows": ledger, "employees_with_balance": holders,
            "plans": plans, "events": events, "claims": claims, "encashments": encash, "in_use": in_use}


def list_types(db: Session, company_id: int, *, include_inactive: bool = False,
               locale: Optional[str] = None) -> List[dict]:
    query = db.query(LeaveType).filter(LeaveType.company_id == company_id)
    if not include_inactive:
        query = query.filter(LeaveType.is_active.is_(True))
    return [type_json(lt, locale=locale) for lt in query.order_by(LeaveType.display_order, LeaveType.name)]


def _load(db: Session, company_id: int, type_id: int, lock: bool = False) -> LeaveType:
    query = db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.id == type_id)
    if lock:
        query = query.with_for_update()
    lt = query.first()
    if lt is None:
        raise not_found("Leave type")
    return lt


def get_type(db: Session, principal: Principal, type_id: int) -> dict:
    if not principal.has(MANAGE):
        raise forbidden()
    lt = _load(db, principal.company_id, type_id)
    return type_json(lt, usage=usage(db, principal.company_id, lt.code))


def _hourly_anywhere(db: Session, company_id: int) -> bool:
    from app.models.org import LegalEntity

    if leave_access.setting(db, company_id, "leave.hourly.enabled", default=False):
        return True
    return any(leave_access.setting(db, company_id, "leave.hourly.enabled", legal_entity_id=e, default=False)
               for (e,) in db.query(LegalEntity.id).filter(LegalEntity.company_id == company_id))


def save_type(db: Session, principal: Principal, data, type_id: Optional[int] = None,
              if_match: Optional[int] = None) -> dict:
    """Create or update a type (``LeaveTypeWriteV2``)."""
    if not principal.has(MANAGE, "COMPANY"):
        raise forbidden("Leave types are company-wide: changing them needs company-wide leave configuration access.")
    cid = principal.company_id
    values = data.model_dump(exclude_unset=False)
    reason = values.pop("reason", None)
    confirm_code = values.pop("confirm_code", None)
    category = values.get("category") or "OTHER"
    defaults = CATEGORY_DEFAULTS[category]
    values["sensitivity"] = values.get("sensitivity") or defaults[0]
    values["absence_class"] = values.get("absence_class") or defaults[1]
    values["icon"] = values.get("icon") or defaults[2]
    if values.get("counts_toward_absence_score") is None:
        values["counts_toward_absence_score"] = values["absence_class"] == "UNPLANNED"
    if values["absence_class"] in ("STATUTORY", "PROTECTED") and values["counts_toward_absence_score"]:
        raise bad_request("Statutory and protected leave never counts toward absence scores.", "RULE_INVALID")
    kind = values.get("kind")
    if kind == "COMP_OFF" and category != "COMP_OFF":
        raise bad_request("A comp-off type has category COMP_OFF.", "RULE_INVALID")
    if kind == "FLOATING_HOLIDAY" and category != "FLOATING":
        raise bad_request("A floating-holiday type has category FLOATING.", "RULE_INVALID")
    if kind in ("COMP_OFF", "EVENT") and values.get("is_unlimited"):
        raise bad_request("Comp-off and event leave cannot be unlimited.", "RULE_INVALID")
    if kind == "EVENT" and values.get("unit") == "HOUR":
        raise bad_request("Event leave is counted in days.", "RULE_INVALID")
    if category == "UNPAID" and values.get("is_paid"):
        raise bad_request("Unpaid leave cannot be marked as paid.", "RULE_INVALID")
    if values.get("unit") == "HOUR" and not _hourly_anywhere(db, cid):
        raise AppError(422, "Turn on hourly leave for at least one legal entity first.", "HOURLY_DISABLED")
    clash = db.query(LeaveType.id).filter(LeaveType.company_id == cid, LeaveType.code == values["code"])
    if type_id:
        clash = clash.filter(LeaveType.id != type_id)
    if clash.first():
        raise conflict("A leave type with this code already exists.")
    before = None
    if type_id:
        lt = _load(db, cid, type_id, lock=True)
        if if_match is not None and if_match != lt.row_version:
            raise AppError(409, "This leave type was changed by someone else. Reload it and try again.",
                           "ROW_VERSION_CONFLICT")
        before = type_json(lt)
        used = usage(db, cid, lt.code)
        if used["in_use"] and values["code"] != lt.code:
            raise conflict("This leave type has history; its code cannot change. Deactivate it instead.",
                           "LEAVE_TYPE_CODE_FROZEN")
        changed = [f for f in FROZEN_FIELDS if used["in_use"] and values.get(f) != getattr(lt, f)]
        if changed:
            raise AppError(409, {"message": "This leave type is in use: create a new type instead of changing "
                                            f"{', '.join(changed)}.", "details": {"fields": changed}},
                           "LEAVE_TYPE_FIELD_FROZEN")
        if lt.sensitivity == "SENSITIVE" and values["sensitivity"] == "STANDARD":
            if not reason or len(reason.strip()) < 10 or confirm_code != lt.code:
                raise bad_request("Making a sensitive type standard needs a reason (10+ characters) and the type "
                                  "code typed to confirm.", "CONFIRMATION_REQUIRED")
        if lt.is_active and values.get("is_active") is False:
            unpaid = leave_access.setting(db, cid, "leave.unpaid_type_code", default="unpaid")
            if lt.code == unpaid:
                raise AppError(409, "This is the unpaid leave type of the company; choose another one first.",
                               "PLAN_CONFLICT")
        lt.row_version = (lt.row_version or 1) + 1
    else:
        lt = LeaveType(company_id=cid, row_version=1)
        db.add(lt)
        if values.get("color_token") is None:
            taken = db.query(func.count(LeaveType.id)).filter(LeaveType.company_id == cid).scalar() or 0
            values["color_token"] = f"leave-{taken % 12 + 1}"
    for key in TYPE_FIELDS:
        if key in values and values[key] is not None:
            setattr(lt, key, values[key])
    if values.get("description") is None and type_id:
        lt.description = None
    db.flush()
    after = type_json(lt)
    AuditLogService.log(db, principal, "Leave", "TYPE_SAVED", f"Saved leave type {lt.code}", "leave_type", lt.id,
                        details={"before": before, "after": after, "reason": reason})
    return after
