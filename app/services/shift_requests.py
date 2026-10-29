"""Shift changes, swaps and open shifts (AS-M6, G-15; owner AS 03-shifts/03; spine §4.3, §4.4).

Kinds (D-AS-23): SWAP and DROP name a coworker who accepts; RELEASE posts the entry to the open pool; PICKUP claims an
open shift; CHANGE asks for another shift or other times. ``APPROVED`` always means applied (RELEASE: posted).

* **Switches** — tenant V2_ACTIVE (409 ``ATTENDANCE_V2_REQUIRED``), ``shift.requests.enabled`` (and
  ``shift.open_shifts.enabled`` for RELEASE / PICKUP / posting) at every party (422 ``FEATURE_DISABLED``).
* **Policy** — ``SCHEDULING_POLICY.requests`` resolved for the requester and, for SWAP / DROP, the peer: both must
  enable the kind (422 ``REQUEST_KIND_DISABLED``); the largest cutoff applies to the earliest affected instance start
  (422 ``SHIFT_REQUEST_CUTOFF``); approval is required when any policy requires it; ``max_per_period`` limits
  submissions per window (422 ``REQUEST_LIMIT_REACHED``).
* **Eligibility** of the second party (§4.2): employed, same branch / team per ``open_shift_eligibility``, not on
  APPROVED or PENDING leave, no APPROVED ``UNAVAILABLE`` window (WARN), required tags (WARN), priority window, not
  self. The requester never sees peer-side reasons: they collapse into ``PEER_NOT_AVAILABLE`` (EC AS-SWP-06).
* **Validation** — the candidate schedule (published ⊕ the request's cells) through ``roster_rules`` for both
  employees at submit, acceptance and approval; BLOCK refuses (422 ``ROSTER_RULE_VIOLATION`` /
  ``INSUFFICIENT_REST_HOURS``), WARN needs acknowledgement, REQUIRE_REASON needs the reason.
* **Apply** (§5.6) — one publication of kind ``REQUEST`` (``initiated_by`` EMPLOYEE) through ``shift_rosters._write``:
  entry versions with ``source`` = the kind and ``request_id``; change rows with notice hours; leave recount; change
  log -> day re-evaluation. Any other publication touching a requested cell SUPERSEDES the request.
* **Expiry** — checked under the request lock by every transition; jobs ``shift_request.expire`` and
  ``open_shift.expire`` only sweep.

Services never commit; routes commit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.concurrency import lock_employees
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee, Shift
from app.models.leave import Leave
from app.models.rosters import RosterEntry
from app.models.shift_requests import (
    OPEN_REQUEST_STATUSES, EmployeeAvailability, EmployeeSkillTag, OpenShift, ShiftRequest, SkillTag,
)
from app.models.teams import TeamMembership
from app.services.audit_log_service import AuditLogService

MODULE = "Shifts"
REQUEST, APPROVE, PLAN, READ = "roster:request", "roster:approve", "roster:plan", "roster:read"
GIVE_AWAY = ("DROP", "RELEASE")
PEER_KINDS = ("SWAP", "DROP")
CONTENT = ("kind", "shift_id", "custom_start_time", "custom_end_time", "custom_segments", "site_id",
           "planned_work_mode")
DEFAULT_REQUESTS = {"swap": {"enabled": False, "cutoff_hours": 24, "approval_required": True},
                    "drop": {"enabled": False, "cutoff_hours": 24, "approval_required": True},
                    "release": {"enabled": False, "cutoff_hours": 48, "approval_required": True},
                    "pickup_approval_required": True, "change_enabled": True, "change_cutoff_hours": 48}
PEER_SIDE = {"EMPLOYMENT", "BRANCH", "TEAM", "LEAVE", "UNAVAILABLE", "TAG_MISSING", "TAG_EXPIRED", "PRIORITY_WINDOW",
             "NOT_ROSTERED"}
MAX_PICKER = 50


def _now() -> datetime:
    return clock.utcnow().replace(microsecond=0)


def _unprocessable(message: str, code: str, **extra) -> AppError:
    return AppError(422, message, code, extra=extra or None)


# =============================================================================================== configuration
def _setting(db: Session, company_id: int, key: str, employee_id: Optional[int], on: date) -> bool:
    from app.payroll.services import settings as settings_engine
    from app.payroll.settings_registry import UNSET

    try:
        value = settings_engine.resolve(db, company_id, key, employee_id=employee_id, on=on).value
    except Exception:  # noqa: BLE001 - registry default when nothing resolves
        value = None
    return bool(value) if value is not None and value is not UNSET else False


def scheduling(db: Session, company_id: int, employee_id: int, on: date) -> dict:
    """The resolved ``SCHEDULING_POLICY`` fields (LEGACY_V1 / none: every request kind disabled, §3.1)."""
    from app.services import attendance_policies

    found = attendance_policies.resolve(db, company_id, "SCHEDULING_POLICY", employee_id, on)
    fields = dict(found.fields or {}) if found is not None else {}
    if not fields or fields.get("profile") == "LEGACY_V1":
        return {"requests": {"swap": {"enabled": False}, "drop": {"enabled": False}, "release": {"enabled": False},
                             "pickup_approval_required": True, "change_enabled": False},
                "open_shift_eligibility": {"same_team": True, "same_branch": True, "priority": "NONE"},
                "availability": {"enabled": False}, "legacy": True}
    requests = {**DEFAULT_REQUESTS, **(fields.get("requests") or {})}
    for kind in ("swap", "drop", "release"):
        requests[kind] = {**DEFAULT_REQUESTS[kind], **(requests.get(kind) or {})}
    return {**fields, "requests": requests,
            "open_shift_eligibility": {"same_team": True, "same_branch": True, "priority": "NONE",
                                       "exclusive_window_hours": 0, **(fields.get("open_shift_eligibility") or {})}}


def _kind_rules(policy: Mapping[str, Any], kind: str) -> dict:
    req = policy["requests"]
    if kind == "CHANGE":
        return {"enabled": bool(req.get("change_enabled", False)), "cutoff_hours": int(req.get("change_cutoff_hours", 48)),
                "approval_required": True}
    if kind == "PICKUP":
        return {"enabled": True, "cutoff_hours": 0, "approval_required": bool(req.get("pickup_approval_required", True))}
    rules = req.get(kind.lower()) or {}
    return {"enabled": bool(rules.get("enabled", False)), "cutoff_hours": int(rules.get("cutoff_hours", 24)),
            "approval_required": bool(rules.get("approval_required", True)),
            "max_per_period": rules.get("max_per_period"), "limit_window": rules.get("limit_window", "MONTH"),
            "pay_effect": rules.get("pay_effect")}


def _require_v2(db: Session, company_id: int) -> None:
    from app.services import attendance_access

    if not attendance_access.v2_enabled(db, company_id):
        raise AppError(409, "Shift requests need attendance v2: run the attendance cut-over first.",
                       "ATTENDANCE_V2_REQUIRED")


def _require_switches(db: Session, company_id: int, employee_ids: Iterable[int], on: date, *, open_shifts: bool) -> None:
    for employee_id in employee_ids:
        if not _setting(db, company_id, "shift.requests.enabled", employee_id, on):
            raise _unprocessable("Shift requests are turned off.", "FEATURE_DISABLED")
        if open_shifts and not _setting(db, company_id, "shift.open_shifts.enabled", employee_id, on):
            raise _unprocessable("Open shifts are turned off.", "FEATURE_DISABLED")


# =============================================================================================== reads of cells
def _entry(db: Session, company_id: int, entry_id: int, *, lock: bool = False) -> RosterEntry:
    query = db.query(RosterEntry).filter(RosterEntry.company_id == company_id, RosterEntry.id == entry_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None or row.withdrawn_at is not None:
        raise not_found("Roster entry")
    return row


def _cell(db: Session, company_id: int, employee_id: int, day: date) -> List[RosterEntry]:
    return (db.query(RosterEntry)
            .filter(RosterEntry.company_id == company_id, RosterEntry.employee_id == employee_id,
                    RosterEntry.shift_date == day, RosterEntry.withdrawn_at.is_(None))
            .order_by(RosterEntry.entry_seq).all())


def _content(row: Any) -> dict:
    return {k: getattr(row, k) for k in CONTENT}


def _change(employee_id: int, day: date, seq: int, content: Mapping[str, Any], source: str):
    from app.services.shift_rosters import _Cell

    work = content.get("kind") == "WORK"
    return _Cell(employee_id, day, seq, content.get("kind"), content.get("shift_id") if work else None, None,
                 content.get("custom_start_time") if work else None, content.get("custom_end_time") if work else None,
                 content.get("custom_segments") if work else None, content.get("site_id"),
                 content.get("planned_work_mode") if work else None, source)


OFF = {"kind": "OFF", "shift_id": None, "custom_start_time": None, "custom_end_time": None, "custom_segments": None,
       "site_id": None, "planned_work_mode": None}


def _zone(db: Session, company_id: int, employee_id: int, day: date) -> str:
    from app.services import roster_validation

    class _C:  # zones_for reads employee_id / shift_date
        def __init__(self):
            self.employee_id, self.shift_date = employee_id, day

    return roster_validation.zones_for(db, company_id, [_C()]).get((employee_id, day), "UTC")


def instance_start(db: Session, company_id: int, employee_id: int, day: date, content: Mapping[str, Any]
                   ) -> Optional[datetime]:
    from app.services import roster_validation

    values = {**content, "employee_id": employee_id, "shift_date": day, "entry_seq": 1, "shift_version_id": None}
    return roster_validation.instance_start(db, company_id, values, _zone(db, company_id, employee_id, day))


# =============================================================================================== eligibility
def _employed(employee: Optional[Employee], day: date) -> bool:
    if employee is None or not employee.is_active:
        return False
    return employee.termination_date is None or employee.termination_date >= day


def _branch_on(db: Session, employee: Employee, day: date) -> Optional[int]:
    from app.services import attendance_service

    placement = attendance_service._placement(db, employee, day)
    return getattr(placement, "branch_id", None)


def _teams_on(db: Session, company_id: int, employee_id: int, day: date) -> set:
    return {t for (t,) in db.query(TeamMembership.team_id).filter(
        TeamMembership.company_id == company_id, TeamMembership.employee_id == employee_id,
        TeamMembership.effective_from <= day,
        or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= day))}


def _on_leave(db: Session, company_id: int, employee_id: int, day: date) -> bool:
    return db.query(Leave.id).filter(Leave.company_id == company_id, Leave.employee_id == employee_id,
                                     Leave.status.in_(("pending", "approved")), Leave.start_date <= day,
                                     Leave.end_date >= day).first() is not None


def _unavailable(db: Session, company_id: int, employee_id: int, day: date) -> bool:
    for row in db.query(EmployeeAvailability).filter(
            EmployeeAvailability.company_id == company_id, EmployeeAvailability.employee_id == employee_id,
            EmployeeAvailability.status == "APPROVED", EmployeeAvailability.kind == "UNAVAILABLE",
            EmployeeAvailability.start_date <= day,
            or_(EmployeeAvailability.end_date.is_(None), EmployeeAvailability.end_date >= day)):
        if row.weekday is None or row.weekday == day.weekday():
            return True
    return False


def _tags_missing(db: Session, company_id: int, employee_id: int, required: Sequence[str], day: date) -> Tuple[list, list]:
    if not required:
        return [], []
    held = {code: valid_to for code, valid_to in db.query(SkillTag.code, EmployeeSkillTag.valid_to)
            .join(EmployeeSkillTag, EmployeeSkillTag.tag_id == SkillTag.id)
            .filter(SkillTag.company_id == company_id, EmployeeSkillTag.employee_id == employee_id)}
    missing = [c for c in required if c not in held]
    expired = [c for c in required if c in held and held[c] is not None and held[c] < day]
    return missing, expired


def eligibility(db: Session, company_id: int, *, requester_id: Optional[int], party_id: int, day: date,
                policy: Mapping[str, Any], branch_id: Optional[int], team_id: Optional[int],
                required_tags: Sequence[str] = (), priority_until: Optional[datetime] = None) -> dict:
    """§4.2 for the second party (peer or claimant): ``{eligible, reasons[], warnings[]}``. Reasons block; warnings
    (availability, tags) are acknowledged like roster WARN results."""
    party = db.get(Employee, party_id)
    reasons: List[str] = []
    warnings: List[str] = []
    if requester_id is not None and party_id == requester_id:
        reasons.append("SELF")
    if not _employed(party, day) or party is None or party.company_id != company_id:
        reasons.append("EMPLOYMENT")
        return {"eligible": False, "reasons": reasons, "warnings": warnings}
    rules = policy.get("open_shift_eligibility") or {}
    if rules.get("same_branch", True) and branch_id is not None and _branch_on(db, party, day) != branch_id:
        reasons.append("BRANCH")
    if rules.get("same_team", True):
        teams = {team_id} if team_id else (_teams_on(db, company_id, requester_id, day) if requester_id else set())
        if teams and not (teams & _teams_on(db, company_id, party_id, day)):
            reasons.append("TEAM")
    if _on_leave(db, company_id, party_id, day):
        reasons.append("LEAVE")
    if priority_until is not None and _now() < priority_until:
        reasons.append("PRIORITY_WINDOW")
    if _unavailable(db, company_id, party_id, day):
        warnings.append("UNAVAILABLE")
    missing, expired = _tags_missing(db, company_id, party_id, required_tags, day)
    if missing:
        warnings.append("TAG_MISSING")
    if expired:
        warnings.append("TAG_EXPIRED")
    return {"eligible": not reasons, "reasons": reasons, "warnings": warnings}


def _mask(reasons: Sequence[str]) -> List[str]:
    """EC AS-SWP-06: the requester sees requester-side reasons only; peer facts read 'Not available for this swap'."""
    out = [r for r in reasons if r not in PEER_SIDE]
    if any(r in PEER_SIDE for r in reasons):
        out.append("PEER_NOT_AVAILABLE")
    return out


# =============================================================================================== validation
EXISTING_REASON = "Existing published schedule"


def _touches(result: Any, changed: Sequence[Any]) -> bool:
    """Does a rule result concern one of the request's changed cells (employee and date)?"""
    raw = result if isinstance(result, dict) else getattr(result, "__dict__", {})
    employee = raw.get("employee_id")
    dates = {d.isoformat() if hasattr(d, "isoformat") else str(d) for d in (raw.get("dates") or [])}
    cells = {(c.employee_id, c.shift_date.isoformat()) for c in changed}
    if employee is None or not dates:
        return True  # a result without a place applies to the change
    return any((employee, d) in cells for d in dates)


def _auto_settled(outcome: Any, changed: Sequence[Any]) -> Tuple[set, set]:
    """(WARN keys, REQUIRE_REASON keys) the requester need not answer: planning lints and results that only concern
    days the request does not change (they were published that way and stay so)."""
    def key(r):
        return getattr(r, "rule_key", None) or (r.get("rule_key") if isinstance(r, dict) else None)

    def severity(r):
        return getattr(r, "severity", None) or (r.get("severity") if isinstance(r, dict) else None)

    touching = {key(r) for r in outcome.results if _touches(r, changed) and not str(key(r)).startswith("LINT_")}
    warns = {key(r) for r in outcome.results if severity(r) == "WARN" and key(r) not in touching}
    reasons = {key(r) for r in outcome.results if severity(r) == "REQUIRE_REASON" and key(r) not in touching}
    return warns, reasons


def _run_checks(db: Session, company_id: int, candidate, changed, *, acknowledged: Sequence[str],
                reason: Optional[str]):
    """``roster_rules`` on the candidate with the request's acknowledgements and reason; results that do not concern
    the changed cells are settled automatically (``_auto_settled``)."""
    from app.services import shift_rosters as sr

    now = _now()
    outcome = sr.validate_cells(db, company_id, candidate, changed, publish_at=now, acknowledged=acknowledged)
    warns, reason_keys = _auto_settled(outcome, changed)
    reasons = {k: EXISTING_REASON for k in reason_keys if k}
    if reason:
        reasons.update({getattr(r, "rule_key", None): reason for r in outcome.missing_reasons
                        if getattr(r, "rule_key", None) not in reasons})
    acks = sorted(set(acknowledged) | {k for k in warns if k})
    if acks != sorted(set(acknowledged)) or reasons:
        outcome = sr.validate_cells(db, company_id, candidate, changed, publish_at=now, acknowledged=acks,
                                    reasons=reasons)
    return outcome, acks, reasons


def _validate(db: Session, company_id: int, changes: Sequence[Any], *, acknowledged: Sequence[str],
              reason: Optional[str], gate: bool, mask_employee_id: Optional[int] = None) -> List[dict]:
    """Roster rules on the candidate schedule (§4.3); returns the results (JSON). ``gate`` raises on BLOCK, missing
    reasons and unacknowledged warnings; ``mask_employee_id``: results that concern only that employee read
    ``PEER_NOT_AVAILABLE`` for the requester."""
    from app.services import shift_rosters as sr

    if not changes:
        return []
    days = [c.shift_date for c in changes]
    candidate, changed = sr._candidate(db, company_id, changes, min(days) - timedelta(days=sr.SEAM_DAYS),
                                       max(days) + timedelta(days=sr.SEAM_DAYS))
    sr._resolve_versions(db, company_id, changed)
    outcome, _, _ = _run_checks(db, company_id, candidate, changed, acknowledged=acknowledged, reason=reason)
    results = [sr._result_json(r) for r in outcome.results if _touches(r, changed)
               and not str(getattr(r, "rule_key", "")).startswith("LINT_")]
    if mask_employee_id is not None:
        masked = [r for r in results if r.get("employee_id") != mask_employee_id]
        if len(masked) != len(results):
            masked.append({"rule_key": "PEER_NOT_AVAILABLE", "severity": max(
                (r.get("severity") for r in results if r.get("employee_id") == mask_employee_id),
                key=lambda s: ("WARN", "REQUIRE_REASON", "BLOCK").index(s) if s in ("WARN", "REQUIRE_REASON", "BLOCK") else 0,
                default="WARN"), "message": "Not available for this swap."})
        results = masked
    if gate:
        class _P:  # _gate only reads .has for exemptions; requests never exempt
            def has(self, *_):
                return False

        try:
            sr._gate(outcome, _P(), {})
        except AppError as exc:
            if mask_employee_id is not None and getattr(exc, "extra", None):
                issues = exc.extra.get("issues") or []
                kept = [i for i in issues if i.get("employee_id") != mask_employee_id]
                if len(kept) != len(issues):
                    kept.append({"rule_key": "PEER_NOT_AVAILABLE", "message": "Not available for this swap."})
                exc.extra["issues"] = kept
            raise
    return results


# =============================================================================================== serializers
def _names(db: Session, company_id: int, ids: Iterable[int]) -> Dict[int, str]:
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    return {e.id: e.full_name for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                                  Employee.id.in_(wanted))}


def _shift_codes(db: Session, company_id: int) -> Dict[int, str]:
    return dict(db.query(Shift.id, Shift.code).filter(Shift.company_id == company_id).all())


def _entry_view(row: Optional[RosterEntry], codes: Mapping[int, str]) -> Optional[dict]:
    if row is None:
        return None
    return {"id": row.id, "shift_date": row.shift_date.isoformat(), "kind": row.kind, "shift_id": row.shift_id,
            "shift_code": codes.get(row.shift_id) if row.shift_id else None,
            "custom_start_time": row.custom_start_time, "custom_end_time": row.custom_end_time,
            "version": row.version, "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None}


def serialize(db: Session, row: ShiftRequest, viewer: Optional[Principal] = None) -> dict:
    codes = _shift_codes(db, row.company_id)
    names = _names(db, row.company_id, [row.requester_employee_id, row.target_employee_id])
    entry = db.get(RosterEntry, row.entry_id) if row.entry_id else None
    counter = db.get(RosterEntry, row.counter_entry_id) if row.counter_entry_id else None
    me = viewer.employee_id if viewer is not None else None
    is_requester = me == row.requester_employee_id
    checks = row.checks or []
    if is_requester and row.target_employee_id:
        checks = [c for c in checks if c.get("employee_id") != row.target_employee_id] + (
            [{"rule_key": "PEER_NOT_AVAILABLE", "message": "Not available for this swap."}]
            if any(c.get("employee_id") == row.target_employee_id for c in checks) else [])
    out = {"id": row.id, "kind": row.kind, "status": row.status, "shift_date": row.shift_date.isoformat(),
           "counter_date": row.counter_date.isoformat() if row.counter_date else None,
           "requester_employee_id": row.requester_employee_id, "requester_name": names.get(row.requester_employee_id),
           "target_employee_id": row.target_employee_id, "target_name": names.get(row.target_employee_id),
           "entry": _entry_view(entry, codes), "counter_entry": _entry_view(counter, codes),
           "open_shift_id": row.open_shift_id, "requested_change": row.requested_change,
           "requested_shift_code": codes.get((row.requested_change or {}).get("shift_id")) if row.requested_change else None,
           "reason": row.reason if viewer is None or is_requester or me == row.target_employee_id
           or viewer.has(APPROVE) else None,
           "approval_required": row.approval_required, "expires_at": row.expires_at.isoformat() if row.expires_at else None,
           "submitted_at": row.submitted_at.isoformat(), "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
           "decided_at": row.decided_at.isoformat() if row.decided_at else None, "decision_note": row.decision_note,
           "applied_publication_id": row.applied_publication_id, "close_reason": row.close_reason, "checks": checks}
    if viewer is not None:
        out["can"] = {"accept": row.status == "PENDING_ACCEPTANCE" and me == row.target_employee_id,
                      "decline": row.status == "PENDING_ACCEPTANCE" and me == row.target_employee_id,
                      "withdraw": row.status in OPEN_REQUEST_STATUSES and is_requester,
                      "approve": row.status == "PENDING_APPROVAL" and _can_decide(db, viewer, row),
                      "reject": row.status == "PENDING_APPROVAL" and _can_decide(db, viewer, row)}
    return out


def serialize_open_shift(db: Session, row: OpenShift, codes: Optional[Mapping[int, str]] = None,
                         claims: Optional[Mapping[int, int]] = None) -> dict:
    codes = codes if codes is not None else _shift_codes(db, row.company_id)
    return {"id": row.id, "shift_date": row.shift_date.isoformat(), "shift_id": row.shift_id,
            "shift_code": codes.get(row.shift_id) if row.shift_id else None,
            "custom_start_time": row.custom_start_time, "custom_end_time": row.custom_end_time,
            "branch_id": row.branch_id, "team_id": row.team_id, "site_id": row.site_id,
            "required_count": row.required_count, "filled_count": row.filled_count, "status": row.status,
            "source": row.source, "approval_required": row.approval_required,
            "required_tags": list((row.eligibility or {}).get("required_tags") or []),
            "priority_until": row.priority_until.isoformat() if row.priority_until else None,
            "posted_at": row.posted_at.isoformat(), "expires_at": row.expires_at.isoformat(),
            "filled_at": row.filled_at.isoformat() if row.filled_at else None, "note": row.note,
            "released_by_employee_id": row.released_by_employee_id,
            "pending_claims": (claims or {}).get(row.id, 0), "row_version": row.row_version}


# =============================================================================================== helpers
def _own(db: Session, principal: Principal) -> Employee:
    if not principal.has(REQUEST):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    if not employee.is_active:
        raise forbidden("Your employment record is inactive.")
    return employee


def _can_decide(db: Session, principal: Principal, row: ShiftRequest) -> bool:
    if not principal.has(APPROVE):
        return False
    parties = {row.requester_employee_id, row.target_employee_id} - {None}
    if principal.employee_id in parties:
        return False  # nobody decides a request they are part of
    return authz.can_access_employee(db, principal, APPROVE, row.requester_employee_id)


def _load(db: Session, company_id: int, request_id: int, *, lock: bool = False) -> ShiftRequest:
    query = db.query(ShiftRequest).filter(ShiftRequest.company_id == company_id, ShiftRequest.id == request_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Shift request")
    return row


def _expire_if_due(db: Session, row: ShiftRequest, now: datetime) -> None:
    """§4.4: every transition first evaluates expiry under the lock; an expired request answers 409."""
    if row.status in OPEN_REQUEST_STATUSES and row.expires_at is not None and now > row.expires_at:
        _drop_engine(db, row, "Expired at the cutoff")
        row.status, row.close_reason = "EXPIRED", "CUTOFF"
        row.row_version += 1
        db.flush()
        raise AppError(409, "This request expired at its cutoff.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": "EXPIRED", "expired_at": row.expires_at.isoformat()}})


def _transition_guard(row: ShiftRequest, *allowed: str) -> None:
    if row.status not in allowed:
        raise AppError(409, "This request can no longer change that way.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})


def _notify(db: Session, company_id: int, employee_id: Optional[int], title: str, message: str,
            link: str = "/rosters/my-schedule?tab=requests") -> None:
    from app.services.notification_service import NotificationService

    employee = db.get(Employee, employee_id) if employee_id else None
    if employee is not None and employee.user_id:
        NotificationService.create(db, employee.user_id, title, message, "roster", link)


def _limit(db: Session, company_id: int, employee_id: int, kind: str, rules: Mapping[str, Any], day: date) -> None:
    limit = rules.get("max_per_period")
    if not limit:
        return
    window = rules.get("limit_window") or "MONTH"
    if window == "WEEK":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
    else:
        start = day.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    used = db.query(func.count(ShiftRequest.id)).filter(
        ShiftRequest.company_id == company_id, ShiftRequest.requester_employee_id == employee_id,
        ShiftRequest.kind == kind, ShiftRequest.shift_date >= start, ShiftRequest.shift_date <= end,
        ~((ShiftRequest.status == "CANCELLED") & ShiftRequest.accepted_at.is_(None) & ShiftRequest.decided_at.is_(None)),
    ).scalar()
    if used >= int(limit):
        raise _unprocessable(f"You have used your {kind.lower()} requests for this {window.lower()}.",
                             "REQUEST_LIMIT_REACHED",
                             details={"kind": kind, "used": used, "limit": int(limit), "window_end": end.isoformat()})


# =============================================================================================== the request's cells
def _plan(db: Session, company_id: int, row_or_data: Mapping[str, Any]) -> Tuple[List[Any], List[date], Dict[str, Any]]:
    """(cell changes, affected dates, context) of a SWAP / DROP / CHANGE / PICKUP (RELEASE changes nothing until a
    pickup). ``row_or_data``: kind, requester, entry, target, counter entry, open shift, requested change."""
    kind = row_or_data["kind"]
    requester = row_or_data["requester_employee_id"]
    entry: Optional[RosterEntry] = row_or_data.get("entry")
    if kind == "SWAP":
        peer = row_or_data["target_employee_id"]
        d1 = entry.shift_date
        counter: Optional[RosterEntry] = row_or_data.get("counter_entry")
        d2 = counter.shift_date if counter is not None else d1
        changes = []
        for day in sorted({d1, d2}):
            mine = _cell(db, company_id, requester, day)
            theirs = _cell(db, company_id, peer, day)
            if not mine or not theirs:
                raise _unprocessable("Every day of a swap needs a published shift or day off for both of you.",
                                     "NOT_ELIGIBLE", details={"reasons": ["NOT_ROSTERED"]})
            a, b = _content(mine[0]), _content(theirs[0])
            if a == b:
                continue
            changes.append(_change(requester, day, mine[0].entry_seq, b, "SWAP"))
            changes.append(_change(peer, day, theirs[0].entry_seq, a, "SWAP"))
        if not changes:
            raise _unprocessable("The swap would change nothing.", "NOT_ELIGIBLE", details={"reasons": ["NO_CHANGE"]})
        return changes, sorted({d1, d2}), {"peer": peer}
    if kind == "DROP":
        peer = row_or_data["target_employee_id"]
        day = entry.shift_date
        theirs = _cell(db, company_id, peer, day)
        if not theirs:
            raise _unprocessable("Your coworker has no published schedule that day.", "NOT_ELIGIBLE",
                                 details={"reasons": ["PEER_NOT_AVAILABLE"]})
        mine_all = _cell(db, company_id, requester, day)
        give = _content(entry)
        changes = []
        if len([e for e in mine_all if e.kind == "WORK"]) > 1:
            changes.append(_change(requester, day, entry.entry_seq, {"kind": None}, "DROP"))
            changes[-1].kind = None
        else:
            changes.append(_change(requester, day, entry.entry_seq, OFF, "DROP"))
        if theirs[0].kind == "OFF":
            changes.append(_change(peer, day, theirs[0].entry_seq, give, "DROP"))
        else:
            seq = max(e.entry_seq for e in theirs) + 1
            if seq > 3:
                raise _unprocessable("Your coworker already has three shifts that day.", "NOT_ELIGIBLE",
                                     details={"reasons": ["PEER_NOT_AVAILABLE"]})
            changes.append(_change(peer, day, seq, give, "DROP"))
        return changes, [day], {"peer": peer}
    if kind == "CHANGE":
        asked = {k: v for k, v in (row_or_data.get("requested_change") or {}).items() if k in CONTENT and k != "kind"}
        wanted = {**_content(entry), **asked}
        if asked.get("shift_id"):  # another shift: its own times
            wanted.update(custom_start_time=None, custom_end_time=None, custom_segments=None)
        elif asked.get("custom_start_time"):  # other times on the day: a custom-time entry
            wanted["shift_id"] = None
        return [_change(requester, entry.shift_date, entry.entry_seq, wanted, "CHANGE")], [entry.shift_date], {}
    if kind == "PICKUP":
        shift: OpenShift = row_or_data["open_shift"]
        content = {"kind": "WORK", "shift_id": shift.shift_id, "custom_start_time": shift.custom_start_time,
                   "custom_end_time": shift.custom_end_time, "custom_segments": None, "site_id": shift.site_id,
                   "planned_work_mode": None}
        mine = _cell(db, company_id, requester, shift.shift_date)
        changes = []
        if not mine or mine[0].kind == "OFF":
            changes.append(_change(requester, shift.shift_date, mine[0].entry_seq if mine else 1, content, "PICKUP"))
        else:
            seq = max(e.entry_seq for e in mine) + 1
            if seq > 3:
                raise _unprocessable("You already have three shifts that day.", "NOT_ELIGIBLE",
                                     details={"reasons": ["DOUBLE_SHIFT"]})
            changes.append(_change(requester, shift.shift_date, seq, content, "PICKUP"))
        if shift.source == "RELEASED" and shift.released_from_entry_id:
            released = db.get(RosterEntry, shift.released_from_entry_id)
            if released is not None and released.withdrawn_at is None:
                others = [e for e in _cell(db, company_id, released.employee_id, released.shift_date)
                          if e.kind == "WORK"]
                if len(others) > 1:
                    gone = _change(released.employee_id, released.shift_date, released.entry_seq, OFF, "PICKUP")
                    gone.kind = None
                    changes.append(gone)
                else:
                    changes.append(_change(released.employee_id, released.shift_date, released.entry_seq, OFF, "PICKUP"))
        return changes, [shift.shift_date], {}
    return [], [entry.shift_date] if entry is not None else [], {}


def _earliest_start(db: Session, company_id: int, kind: str, data: Mapping[str, Any], dates: Sequence[date]
                    ) -> Optional[datetime]:
    """The earliest affected instance start (cutoff anchor): the requester's and the peer's cells on the dates, the
    new instance of a CHANGE."""
    starts: List[datetime] = []
    people = [data["requester_employee_id"]] + ([data["target_employee_id"]] if data.get("target_employee_id") else [])
    for employee_id in people:
        for day in dates:
            for row in _cell(db, company_id, employee_id, day):
                if row.kind == "WORK":
                    s = instance_start(db, company_id, employee_id, day, _content(row))
                    if s is not None:
                        starts.append(s)
    if kind == "CHANGE" and data.get("entry") is not None:
        entry = data["entry"]
        wanted = {**_content(entry), **(data.get("requested_change") or {})}
        s = instance_start(db, company_id, entry.employee_id, entry.shift_date, wanted)
        if s is not None:
            starts.append(s)
    return min(starts) if starts else None


# =============================================================================================== preview / submit
def _gather(db: Session, principal: Principal, body: Mapping[str, Any]) -> dict:
    """Validate the body's shape and load the parties (no writes)."""
    kind = body.get("kind")
    if kind not in ("SWAP", "DROP", "RELEASE", "CHANGE"):
        raise bad_request("Choose swap, give away, release or change.", "INVALID_FIELDS")
    me = _own(db, principal)
    entry = _entry(db, principal.company_id, int(body.get("entry_id") or 0))
    if entry.employee_id != me.id:
        raise not_found("Roster entry")
    if entry.kind != "WORK" and kind != "SWAP":
        raise _unprocessable("Only a working shift can be given away, released or changed.", "NOT_ELIGIBLE",
                             details={"reasons": ["NOT_WORK"]})
    data: Dict[str, Any] = {"kind": kind, "requester_employee_id": me.id, "entry": entry}
    if kind in PEER_KINDS:
        target = body.get("target_employee_id")
        if not isinstance(target, int) or target == me.id:
            raise bad_request("Choose a coworker.", "INVALID_FIELDS")
        peer = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.id == target).first()
        if peer is None:
            raise not_found("Employee")
        data["target_employee_id"] = target
        if kind == "SWAP" and body.get("counter_entry_id"):
            counter = _entry(db, principal.company_id, int(body["counter_entry_id"]))
            if counter.employee_id != target:
                raise not_found("Roster entry")
            data["counter_entry"] = counter
    if kind == "CHANGE":
        change = {k: v for k, v in (body.get("requested_change") or {}).items() if k in CONTENT and k != "kind"}
        if not change:
            raise bad_request("Say what should change.", "INVALID_FIELDS")
        if change.get("shift_id") is not None:
            shift = db.query(Shift).filter(Shift.company_id == principal.company_id, Shift.id == change["shift_id"]).first()
            if shift is None or getattr(shift, "status", "ACTIVE") != "ACTIVE":
                raise _unprocessable("That shift is archived or unknown.", "SHIFT_ARCHIVED")
        if all(getattr(entry, k) == v for k, v in change.items()):
            raise _unprocessable("The change would change nothing.", "NOT_ELIGIBLE", details={"reasons": ["NO_CHANGE"]})
        data["requested_change"] = change
    return data


def _checks_for(db: Session, principal: Principal, data: Dict[str, Any], body: Mapping[str, Any], *, gate: bool
                ) -> dict:
    """Preconditions §4.1 (2)–(8) for the requester; returns the policy outcome and the validation results."""
    cid = principal.company_id
    kind = data["kind"]
    entry: RosterEntry = data["entry"]
    day = entry.shift_date
    requester_policy = scheduling(db, cid, data["requester_employee_id"], day)
    rules = [_kind_rules(requester_policy, kind)]
    peer = data.get("target_employee_id")
    if peer:
        peer_policy = scheduling(db, cid, peer, day)
        rules.append(_kind_rules(peer_policy, kind))
        if not rules[1]["enabled"]:
            raise _unprocessable("This kind of request is not available for your coworker.", "REQUEST_KIND_DISABLED",
                                 details={"employee_id": peer})
    if not rules[0]["enabled"]:
        raise _unprocessable("This kind of request is not available for you.", "REQUEST_KIND_DISABLED",
                             details={"employee_id": data["requester_employee_id"]})
    if kind in GIVE_AWAY:
        effect = rules[0].get("pay_effect") or "LEAVE_REQUIRED"  # interim: period-paid unless a policy says otherwise
        if effect == "LEAVE_REQUIRED":
            raise _unprocessable("Giving a shift away is not available for salaried staff: apply for leave instead.",
                                 "NOT_ELIGIBLE", details={"reasons": ["GIVER_SALARIED"]})
    if _on_leave(db, cid, data["requester_employee_id"], day) and kind != "CHANGE":
        raise _unprocessable("You are on leave that day: the shift cannot be given away.", "NOT_ELIGIBLE",
                             details={"reasons": ["OWN_LEAVE"]})
    changes, dates, _ = _plan(db, cid, data) if kind != "RELEASE" else ([], [day], {})
    start = _earliest_start(db, cid, kind, data, dates)
    cutoff_hours = max(r["cutoff_hours"] for r in rules)
    cutoff_at = start - timedelta(hours=cutoff_hours) if start is not None else None
    if cutoff_at is not None and _now() > cutoff_at:
        raise _unprocessable("It is too late to ask for this change.", "SHIFT_REQUEST_CUTOFF",
                             details={"cutoff_at": cutoff_at.isoformat()})
    if peer:
        ctx = eligibility(db, cid, requester_id=data["requester_employee_id"], party_id=peer, day=day,
                          policy=requester_policy, branch_id=_branch_on(db, db.get(Employee, data["requester_employee_id"]), day),
                          team_id=entry.team_id)
        if not ctx["eligible"]:
            raise _unprocessable("Your coworker is not available for this.", "NOT_ELIGIBLE",
                                 details={"reasons": _mask(ctx["reasons"])})
    acknowledged = list(body.get("acknowledged_warnings") or [])
    results = _validate(db, cid, changes, acknowledged=acknowledged, reason=body.get("reason"), gate=gate,
                        mask_employee_id=peer)
    return {"approval_required": any(r["approval_required"] for r in rules), "cutoff_at": cutoff_at,
            "rules": rules, "results": results, "dates": dates}


def preview(db: Session, principal: Principal, body: Mapping[str, Any]) -> dict:
    """``POST /shift-requests/preview``: without a target for SWAP / DROP it lists eligible coworkers (≤ 50) with
    their shift on the date; with a target it runs every check without writing."""
    cid = principal.company_id
    _require_v2(db, cid)
    if body.get("kind") in PEER_KINDS and not body.get("target_employee_id"):
        return {"coworkers": coworkers(db, principal, int(body.get("entry_id") or 0), body.get("kind"))}
    data = _gather(db, principal, body)
    _require_switches(db, cid, [data["requester_employee_id"]], data["entry"].shift_date,
                      open_shifts=data["kind"] == "RELEASE")
    outcome = _checks_for(db, principal, data, body, gate=False)
    return {"kind": data["kind"], "approval_required": outcome["approval_required"],
            "cutoff_at": outcome["cutoff_at"].isoformat() if outcome["cutoff_at"] else None,
            "results": outcome["results"],
            "blocking": [r for r in outcome["results"] if r.get("severity") == "BLOCK"]}


def coworkers(db: Session, principal: Principal, entry_id: int, kind: Optional[str]) -> List[dict]:
    """The swap / drop picker: coworkers of the entry's team (or the requester's teams / branch) with their cell on
    the date and whether they are eligible (peer reasons are never shown, only the flag)."""
    me = _own(db, principal)
    cid = principal.company_id
    entry = _entry(db, cid, entry_id)
    if entry.employee_id != me.id:
        raise not_found("Roster entry")
    day = entry.shift_date
    policy = scheduling(db, cid, me.id, day)
    # The picker follows open_shift_eligibility: the team when same_team is on, else the branch (§4.2).
    same_team = bool((policy.get("open_shift_eligibility") or {}).get("same_team", True))
    teams = ({entry.team_id} if entry.team_id else _teams_on(db, cid, me.id, day)) if same_team else set()
    query = db.query(Employee).filter(Employee.company_id == cid, Employee.is_active.is_(True), Employee.id != me.id)
    if teams:
        members = db.query(TeamMembership.employee_id).filter(
            TeamMembership.company_id == cid, TeamMembership.team_id.in_(teams), TeamMembership.effective_from <= day,
            or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= day))
        query = query.filter(Employee.id.in_(members))
    else:
        branch = _branch_on(db, me, day)
        from app.models.org import EmployeeAssignment

        placed = db.query(EmployeeAssignment.employee_id).filter(
            EmployeeAssignment.company_id == cid, EmployeeAssignment.branch_id == branch,
            EmployeeAssignment.effective_from <= day,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= day))
        query = query.filter(Employee.id.in_(placed))
    codes = _shift_codes(db, cid)
    visible = (policy.get("team_visibility") or "TEAM") != "OFF"
    out = []
    branch_id = _branch_on(db, me, day)
    for person in query.order_by(Employee.first_name, Employee.last_name).limit(MAX_PICKER):
        cell = _cell(db, cid, person.id, day)
        ctx = eligibility(db, cid, requester_id=me.id, party_id=person.id, day=day, policy=policy,
                          branch_id=branch_id, team_id=entry.team_id)
        eligible = ctx["eligible"] and bool(cell)
        out.append({"employee_id": person.id, "name": person.full_name, "eligible": eligible,
                    "cell": _entry_view(cell[0], codes) if cell and visible else None,
                    "note": None if eligible else "Not available for this swap"})
    return out


def create(db: Session, principal: Principal, body: Mapping[str, Any]) -> ShiftRequest:
    """``POST /shift-requests`` (SWAP, DROP, RELEASE, CHANGE)."""
    cid = principal.company_id
    _require_v2(db, cid)
    data = _gather(db, principal, body)
    entry: RosterEntry = data["entry"]
    people = sorted({data["requester_employee_id"], data.get("target_employee_id") or data["requester_employee_id"]})
    _require_switches(db, cid, people, entry.shift_date, open_shifts=data["kind"] == "RELEASE")
    lock_employees(db, people)
    entry = _entry(db, cid, entry.id, lock=True)
    data["entry"] = entry
    open_one = db.query(ShiftRequest.id).filter(ShiftRequest.company_id == cid, ShiftRequest.entry_id == entry.id,
                                                ShiftRequest.status.in_(OPEN_REQUEST_STATUSES)).first()
    if open_one is not None:
        raise AppError(409, "There is already an open request on this shift.", "SHIFT_REQUEST_OPEN",
                       extra={"details": {"request_id": open_one[0]}})
    policy = scheduling(db, cid, data["requester_employee_id"], entry.shift_date)
    _limit(db, cid, data["requester_employee_id"], data["kind"], _kind_rules(policy, data["kind"]), entry.shift_date)
    outcome = _checks_for(db, principal, data, body, gate=True)
    now = _now()
    counter = data.get("counter_entry")
    row = ShiftRequest(
        company_id=cid, kind=data["kind"],
        status="PENDING_ACCEPTANCE" if data["kind"] in PEER_KINDS else "PENDING_APPROVAL",
        requester_employee_id=data["requester_employee_id"], entry_id=entry.id,
        target_employee_id=data.get("target_employee_id"), counter_entry_id=counter.id if counter is not None else None,
        shift_date=entry.shift_date, counter_date=counter.shift_date if counter is not None else None,
        requested_change=data.get("requested_change"), reason=(body.get("reason") or "").strip()[:500] or None,
        acknowledged_warnings=list(body.get("acknowledged_warnings") or []), checks=outcome["results"],
        approval_required=outcome["approval_required"] or data["kind"] == "CHANGE",
        expires_at=outcome["cutoff_at"], submitted_at=now)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_SUBMITTED", f"{row.kind.title()} request submitted",
                        "shift_request", row.id, details={"kind": row.kind, "entry_id": entry.id,
                                                         "target_employee_id": row.target_employee_id})
    if row.kind == "RELEASE" and not row.approval_required:
        _apply(db, principal, row)
    elif row.status == "PENDING_APPROVAL":
        _route(db, principal, row)
    elif row.kind in PEER_KINDS:
        requester = db.get(Employee, row.requester_employee_id)
        _notify(db, cid, row.target_employee_id, f"{requester.full_name} asks you to "
                f"{'swap shifts' if row.kind == 'SWAP' else 'take a shift'}",
                f"For {row.shift_date.strftime('%a %d %b')}. Open My schedule to accept or decline.")
    _emit(db, "shift_request.submitted", row)
    return row


# =============================================================================================== transitions
def accept(db: Session, principal: Principal, request_id: int, body: Mapping[str, Any]) -> ShiftRequest:
    """The coworker accepts (SWAP / DROP): checks re-run for both; WARN results are acknowledged by the coworker."""
    me = _own(db, principal)
    row = _load(db, principal.company_id, request_id)
    if row.target_employee_id != me.id:
        raise not_found("Shift request")
    lock_employees(db, sorted({row.requester_employee_id, row.target_employee_id}))
    row = _load(db, principal.company_id, request_id, lock=True)
    _expire_if_due(db, row, _now())
    _transition_guard(row, "PENDING_ACCEPTANCE")
    if _on_leave(db, principal.company_id, me.id, row.shift_date):
        raise _unprocessable("You are on leave that day.", "NOT_ELIGIBLE", details={"reasons": ["LEAVE"]})
    changes, _, _ = _plan(db, principal.company_id, _data_of(db, row))
    acknowledged = sorted(set(row.acknowledged_warnings or []) | set(body.get("acknowledged_warnings") or []))
    row.checks = _validate(db, principal.company_id, changes, acknowledged=acknowledged,
                           reason=body.get("reason") or row.reason, gate=True)
    for change in changes:  # the coworker gains a shift inside the notice window: their consent (§7.3)
        if change.employee_id == me.id and change.kind == "WORK":
            _notice_consent(db, principal, row, me.id, change.shift_date,
                            {k: getattr(change, k) for k in CONTENT}, body)
    row.acknowledged_warnings = acknowledged
    row.accepted_at = _now()
    row.row_version += 1
    AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_ACCEPTED", f"{row.kind.title()} accepted",
                        "shift_request", row.id)
    if row.approval_required:
        row.status = "PENDING_APPROVAL"
        db.flush()
        _route(db, principal, row)
        _notify(db, principal.company_id, row.requester_employee_id, "Your coworker accepted",
                f"Your {row.kind.lower()} for {row.shift_date.strftime('%a %d %b')} now waits for approval. "
                "You are still scheduled until it is approved.")
    else:
        _apply(db, principal, row)
    return row


def decline(db: Session, principal: Principal, request_id: int, note: Optional[str]) -> ShiftRequest:
    me = _own(db, principal)
    row = _load(db, principal.company_id, request_id, lock=True)
    if row.target_employee_id != me.id:
        raise not_found("Shift request")
    _expire_if_due(db, row, _now())
    _transition_guard(row, "PENDING_ACCEPTANCE")
    row.status, row.decided_at, row.decision_note = "DECLINED", _now(), (note or "")[:500] or None
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_DECLINED", f"{row.kind.title()} declined",
                        "shift_request", row.id)
    _notify(db, principal.company_id, row.requester_employee_id, "Your coworker declined",
            f"Your {row.kind.lower()} for {row.shift_date.strftime('%a %d %b')} was declined. You keep your shift.")
    return row


def withdraw(db: Session, principal: Principal, request_id: int) -> ShiftRequest:
    me = _own(db, principal)
    row = _load(db, principal.company_id, request_id, lock=True)
    if row.requester_employee_id != me.id:
        raise not_found("Shift request")
    _expire_if_due(db, row, _now())
    _transition_guard(row, *OPEN_REQUEST_STATUSES)
    _drop_engine(db, row, "Withdrawn by the requester")
    row.status, row.close_reason = "CANCELLED", "WITHDRAWN"
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_WITHDRAWN", f"{row.kind.title()} withdrawn",
                        "shift_request", row.id)
    if row.target_employee_id and row.accepted_at is None:
        _notify(db, principal.company_id, row.target_employee_id, "A shift request was withdrawn",
                f"The {row.kind.lower()} for {row.shift_date.strftime('%a %d %b')} was withdrawn.")
    return row


# ----------------------------------------------------------------------------------------------- approvals engine
ENGINE_MODULE = ENGINE_SUBJECT = "shift_request"


def _users_of(db: Session, company_id: int, employee_ids: Iterable[int]) -> List[int]:
    ids = [e for e in employee_ids if e]
    return [u for (u,) in db.query(Employee.user_id).filter(Employee.company_id == company_id, Employee.id.in_(ids or [0]),
                                                            Employee.user_id.isnot(None))]


def _route(db: Session, principal: Principal, row: ShiftRequest) -> None:
    """PENDING_APPROVAL: open the engine request (module ``shift_request``; JC-AS-14): approval routes with conditions
    (kind, notice hours), team_lead steps, SLA due times and escalation (job ``attendance.sla_scan``). Both parties are
    barred from deciding."""
    from app.payroll.services import approvals

    start = None
    if row.entry_id:
        entry = db.get(RosterEntry, row.entry_id)
        start = instance_start(db, row.company_id, entry.employee_id, entry.shift_date, _content(entry)) if entry else None
    elif row.open_shift_id:
        shift = db.get(OpenShift, row.open_shift_id)
        start = shift.expires_at if shift is not None else None
    notice = int((start - _now()).total_seconds() // 3600) if start is not None else None
    request = approvals.submit(
        db, principal, module=ENGINE_MODULE, subject_type=ENGINE_SUBJECT, subject_id=row.id,
        subject_employee_id=row.requester_employee_id,
        context={"work_date": row.shift_date.isoformat(), "kind": row.kind,
                 "maker_user_ids": _users_of(db, row.company_id, [row.requester_employee_id, row.target_employee_id])},
        attributes={"kind": row.kind, "kinds": [row.kind], "work_date": row.shift_date.isoformat(),
                    "notice_hours": notice})
    row.approval_request_id = request.id
    db.flush()
    from app.services import integrations

    integrations.emit(db, row.company_id, "shift_request.submitted", str(row.id),
                      f"{row.kind.title()} request waiting for approval",
                      f"A {row.kind.lower()} request for {row.shift_date.strftime('%a %d %b')} needs a decision.",
                      "/attendance/shift-requests")


def _drop_engine(db: Session, row: ShiftRequest, reason: str) -> None:
    """The request closed outside the engine (expiry, supersede, leave, withdrawal): withdraw its engine request."""
    from app.payroll.services import approvals

    if row.approval_request_id:
        approvals.withdraw(db, None, module=ENGINE_MODULE, subject_type=ENGINE_SUBJECT, subject_id=row.id,
                           reason=reason)


def _decided_by_engine(db: Session, principal: Optional[Principal], request) -> None:
    row = (db.query(ShiftRequest).filter(ShiftRequest.company_id == request.company_id,
                                         ShiftRequest.id == request.subject_id).with_for_update().first())
    if row is None or row.approval_request_id != request.id or row.status != "PENDING_APPROVAL":
        return
    acted = [s for s in request.steps if s.acted_at is not None and s.remarks]
    note = max(acted, key=lambda s: s.acted_at).remarks[:500] if acted else row.decision_note
    if request.status in ("APPROVED", "REJECTED"):
        from app.services import integrations

        integrations.emit(db, row.company_id, "shift_request.decided", f"{row.id}:{request.id}",
                          f"{row.kind.title()} request {request.status.lower()}",
                          f"The {row.kind.lower()} request for {row.shift_date.strftime('%a %d %b')} was "
                          f"{request.status.lower()}.", "/attendance/shift-requests")
    if request.status == "APPROVED":
        if principal is None:
            raise conflict("A shift request is approved by a person.", "APPROVER_REQUIRED")
        row.decision_note = note
        _apply(db, principal, row)
        AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_DECIDED", f"{row.kind.title()} approved",
                            "shift_request", row.id, details={"decision": "APPROVED",
                                                             "publication_id": row.applied_publication_id})
        return
    if request.status == "REJECTED":
        row.status, row.decided_at = "REJECTED", _now()
        row.decided_by_user_id = principal.user_id if principal is not None else None
        row.decision_note = note
        row.row_version += 1
        db.flush()
        if row.open_shift_id:
            _recount(db, row.company_id, row.open_shift_id)
        for employee_id in {row.requester_employee_id, row.target_employee_id} - {None}:
            _notify(db, row.company_id, employee_id, f"{row.kind.title()} request rejected",
                    f"For {row.shift_date.strftime('%a %d %b')}: {note or 'rejected'}. The published schedule stays.")
    elif request.status == "CANCELLED":
        row.status, row.close_reason = "CANCELLED", "WITHDRAWN"
        row.row_version += 1
        db.flush()


def _register_engine_callback() -> None:
    from app.payroll.services import approvals

    approvals.register_callback(ENGINE_MODULE, ENGINE_SUBJECT, _decided_by_engine)


_register_engine_callback()


def _notice_consent(db: Session, principal: Principal, row: ShiftRequest, receiver_id: int, day: date,
                    content: Mapping[str, Any], body: Mapping[str, Any]) -> None:
    """03-shifts/03 §7.3: a shift added to someone inside the pack's notice window (US Fair Workweek: 14 days) needs
    their consent (``SCHEDULE_CHANGE_ACCEPTED``, per instance) captured in the same accept / claim action."""
    from app.services import attendance_consents, attendance_service, pack_resolver

    receiver = db.get(Employee, receiver_id)
    placement = attendance_service._placement(db, receiver, day) if receiver is not None else None
    entity = getattr(placement, "legal_entity_id", None)
    notice = pack_resolver.resolve(db, row.company_id, entity, "scheduling.notice_hours", day).value
    needed = pack_resolver.resolve(db, row.company_id, entity, "scheduling.late_addition_consent", day).value
    if not (notice and needed):
        return
    start = instance_start(db, row.company_id, receiver_id, day, content)
    if start is None or (start - _now()).total_seconds() / 3600 >= float(notice):
        return
    if not body.get("consent_given"):
        raise _unprocessable("This shift is inside the schedule notice period: confirm that you agree to work it.",
                             "CONSENT_REQUIRED", details={"purpose": "SCHEDULE_CHANGE_ACCEPTED",
                                                         "notice_hours": notice})
    consent = attendance_consents.record(db, row.company_id, receiver_id, "SCHEDULE_CHANGE_ACCEPTED", "GRANTED",
                                         channel="SELF_SERVICE", on=day, scope="PER_INSTANCE",
                                         subject_ref={"shift_request_id": row.id}, principal=principal)
    row.consent_ids = list(row.consent_ids or []) + [consent.id]
    db.flush()


def decide(db: Session, principal: Principal, request_id: int, decision: str, note: Optional[str],
           acknowledged: Sequence[str] = ()) -> ShiftRequest:
    """Approve (apply) or reject a PENDING_APPROVAL request: ``roster:approve`` over the requester, never a party.
    Requests routed through the engine are decided there (its route, step and SoD rules); the callback applies."""
    from app.payroll.services import approvals

    if decision not in ("APPROVE", "REJECT"):
        raise bad_request("Approve or reject.", "INVALID_FIELDS")
    row = _load(db, principal.company_id, request_id)
    if not principal.has(APPROVE):
        raise forbidden()
    if principal.employee_id in {row.requester_employee_id, row.target_employee_id}:
        raise forbidden("You cannot decide a request you are part of.")
    if not authz.can_access_employee(db, principal, APPROVE, row.requester_employee_id):
        raise not_found("Shift request")
    people = sorted({row.requester_employee_id} | ({row.target_employee_id} if row.target_employee_id else set()))
    lock_employees(db, people)
    row = _load(db, principal.company_id, request_id, lock=True)
    _expire_if_due(db, row, _now())
    _transition_guard(row, "PENDING_APPROVAL")
    if decision == "REJECT" and not (note or "").strip():
        raise bad_request("Say why the request is rejected.", "REASON_REQUIRED")
    if row.approval_request_id:
        row.decision_note = (note or "").strip()[:500] or None
        row.acknowledged_warnings = sorted(set(row.acknowledged_warnings or []) | set(acknowledged))
        db.flush()
        approvals.decide(db, principal, row.approval_request_id, decision, note)
        db.refresh(row)
        return row
    if decision == "REJECT":
        if not (note or "").strip():
            raise bad_request("Say why the request is rejected.", "REASON_REQUIRED")
        row.status, row.decided_at, row.decided_by_user_id = "REJECTED", _now(), principal.user_id
        row.decision_note = note.strip()[:500]
        row.row_version += 1
        db.flush()
        if row.open_shift_id:
            _recount(db, row.company_id, row.open_shift_id)
        AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_DECIDED", f"{row.kind.title()} rejected",
                            "shift_request", row.id, details={"decision": "REJECTED"})
        for employee_id in people:
            _notify(db, principal.company_id, employee_id, f"{row.kind.title()} request rejected",
                    f"For {row.shift_date.strftime('%a %d %b')}: {row.decision_note}. The published schedule stays.")
        return row
    row.decision_note = (note or "").strip()[:500] or None
    row.acknowledged_warnings = sorted(set(row.acknowledged_warnings or []) | set(acknowledged))
    _apply(db, principal, row)
    AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_DECIDED", f"{row.kind.title()} approved",
                        "shift_request", row.id, details={"decision": "APPROVED",
                                                         "publication_id": row.applied_publication_id})
    return row


def _data_of(db: Session, row: ShiftRequest) -> dict:
    return {"kind": row.kind, "requester_employee_id": row.requester_employee_id,
            "entry": db.get(RosterEntry, row.entry_id) if row.entry_id else None,
            "target_employee_id": row.target_employee_id,
            "counter_entry": db.get(RosterEntry, row.counter_entry_id) if row.counter_entry_id else None,
            "requested_change": row.requested_change,
            "open_shift": db.get(OpenShift, row.open_shift_id) if row.open_shift_id else None}


def _apply(db: Session, principal: Principal, row: ShiftRequest) -> None:
    """§5.6: re-run the checks and write one REQUEST publication (or post the open shift of a RELEASE)."""
    from app.services import shift_rosters as sr

    cid = row.company_id
    now = _now()
    if row.kind == "RELEASE":
        entry = _entry(db, cid, row.entry_id)
        _post_released(db, principal, row, entry)
        row.status, row.decided_at = "APPROVED", now
        row.decided_by_user_id = principal.user_id if principal.employee_id != row.requester_employee_id else None
        row.row_version += 1
        db.flush()
        _notify(db, cid, row.requester_employee_id, "Your shift is in the open pool",
                f"{row.shift_date.strftime('%a %d %b')}: you stay scheduled until someone's pickup is approved.")
        _emit(db, "shift_request.decided", row)
        return
    data = _data_of(db, row)
    if row.kind == "PICKUP":
        shift = data["open_shift"]
        if shift is None or shift.status != "OPEN" or shift.filled_count >= shift.required_count:
            raise AppError(409, "This open shift is already filled.", "OPEN_SHIFT_FILLED")
        if shift.source == "RELEASED" and shift.released_by_employee_id and _on_leave(
                db, cid, shift.released_by_employee_id, shift.shift_date):
            row.status, row.close_reason = "SUPERSEDED", "OWN_LEAVE_OF_GIVER"
            db.flush()
            raise _unprocessable("The person who released this shift is on leave that day.", "NOT_ELIGIBLE",
                                 details={"reasons": ["OWN_LEAVE_OF_GIVER"]})
    changes, dates, _ = _plan(db, cid, data)
    days = [c.shift_date for c in changes]
    candidate, changed = sr._candidate(db, cid, changes, min(days) - timedelta(days=sr.SEAM_DAYS),
                                       max(days) + timedelta(days=sr.SEAM_DAYS))
    sr._resolve_versions(db, cid, changed)
    outcome, acknowledged, reasons = _run_checks(db, cid, candidate, changed,
                                                 acknowledged=row.acknowledged_warnings or [],
                                                 reason=row.decision_note or row.reason)

    class _P:
        def has(self, *_):
            return False

    sr._gate(outcome, _P(), {})
    employees = sorted({c.employee_id for c in changes})
    scope = {"employee_ids": employees, "from": min(days).isoformat(), "to": max(days).isoformat(),
             "request_id": row.id}
    publication = sr._write(db, principal, cid, kind="REQUEST", period=None, candidate=candidate, changed=changed,
                            scope=scope, outcome=outcome, acknowledged=acknowledged,
                            reasons=reasons, exemptions={}, initiated_by="EMPLOYEE", request_id=row.id)
    row.status, row.decided_at, row.applied_publication_id = "APPROVED", now, publication.id
    if principal.employee_id not in {row.requester_employee_id, row.target_employee_id}:
        row.decided_by_user_id = principal.user_id
    row.checks = [sr._result_json(r) for r in outcome.results if _touches(r, changed)
                  and not str(getattr(r, "rule_key", "")).startswith("LINT_")]
    row.row_version += 1
    db.flush()
    if row.kind == "PICKUP":
        _recount(db, cid, row.open_shift_id, filled=True)
    for employee_id in employees:
        _notify(db, cid, employee_id, "Your schedule changed",
                f"{row.kind.title()} for {row.shift_date.strftime('%a %d %b')} is approved: check My schedule.")
    _emit(db, "shift_request.decided", row)


def supersede_for_entries(db: Session, company_id: int, entry_ids: Sequence[int], *, keep_request_id: Optional[int]
                          ) -> int:
    """A publication changed these entries: open requests exchanging them no longer describe the schedule (§4.4)."""
    ids = [i for i in entry_ids if i]
    if not ids:
        return 0
    rows = (db.query(ShiftRequest)
            .filter(ShiftRequest.company_id == company_id, ShiftRequest.status.in_(OPEN_REQUEST_STATUSES),
                    or_(ShiftRequest.entry_id.in_(ids), ShiftRequest.counter_entry_id.in_(ids)))
            .with_for_update().all())
    count = 0
    for row in rows:
        if keep_request_id is not None and row.id == keep_request_id:
            continue
        _drop_engine(db, row, "The schedule changed")
        row.status, row.close_reason = "SUPERSEDED", "SCHEDULE_CHANGED"
        row.row_version += 1
        count += 1
        for employee_id in {row.requester_employee_id, row.target_employee_id} - {None}:
            _notify(db, company_id, employee_id, "A shift request was superseded",
                    f"The schedule of {row.shift_date.strftime('%a %d %b')} changed, so the {row.kind.lower()} "
                    "request was closed. File it again if you still need it.")
    if count:
        db.flush()
    return count


def cancel_for_leave(db: Session, company_id: int, employee_id: int, start: date, end: date) -> int:
    """§4.2 "Leave applied afterwards": the giver's pending requests on the dates are CANCELLED (``LEAVE_APPLIED``)
    and unfilled RELEASED open shifts are CANCELLED (pending claims SUPERSEDED)."""
    count = 0
    for row in db.query(ShiftRequest).filter(
            ShiftRequest.company_id == company_id, ShiftRequest.requester_employee_id == employee_id,
            ShiftRequest.status.in_(OPEN_REQUEST_STATUSES), ShiftRequest.kind != "PICKUP",
            ShiftRequest.shift_date >= start, ShiftRequest.shift_date <= end).with_for_update():
        _drop_engine(db, row, "Leave applied for the date")
        row.status, row.close_reason = "CANCELLED", "LEAVE_APPLIED"
        row.row_version += 1
        count += 1
    for shift in db.query(OpenShift).filter(
            OpenShift.company_id == company_id, OpenShift.released_by_employee_id == employee_id,
            OpenShift.status == "OPEN", OpenShift.filled_count == 0, OpenShift.shift_date >= start,
            OpenShift.shift_date <= end).with_for_update():
        _close_open_shift(db, shift, "CANCELLED")
        count += 1
    if count:
        db.flush()
    return count


# =============================================================================================== open shifts
def _post_released(db: Session, principal: Principal, row: ShiftRequest, entry: RosterEntry) -> OpenShift:
    cid = row.company_id
    policy = scheduling(db, cid, row.requester_employee_id, entry.shift_date)
    employee = db.get(Employee, row.requester_employee_id)
    start = instance_start(db, cid, entry.employee_id, entry.shift_date, _content(entry)) or (_now() + timedelta(days=1))
    shift = OpenShift(company_id=cid, team_id=entry.team_id, branch_id=_branch_on(db, employee, entry.shift_date),
                      site_id=entry.site_id, shift_date=entry.shift_date, shift_id=entry.shift_id,
                      custom_start_time=entry.custom_start_time, custom_end_time=entry.custom_end_time,
                      required_count=1, eligibility={"same_team": policy["open_shift_eligibility"].get("same_team", True),
                                                     "same_branch": policy["open_shift_eligibility"].get("same_branch", True)},
                      approval_required=bool(policy["requests"].get("pickup_approval_required", True)),
                      status="OPEN", source="RELEASED", released_from_entry_id=entry.id,
                      released_by_employee_id=row.requester_employee_id, posted_at=_now(), expires_at=start,
                      roster_period_id=entry.roster_period_id, created_by_user_id=principal.user_id)
    if shift.branch_id is None:
        raise _unprocessable("Your placement has no branch on that date.", "NOT_ELIGIBLE",
                             details={"reasons": ["BRANCH"]})
    db.add(shift)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "OPEN_SHIFT_POSTED", "Released shift posted", "open_shift", shift.id,
                        details={"source": "RELEASED", "request_id": row.id})
    return shift


def _load_open(db: Session, company_id: int, open_shift_id: int, *, lock: bool = False) -> OpenShift:
    query = db.query(OpenShift).filter(OpenShift.company_id == company_id, OpenShift.id == open_shift_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Open shift")
    return row


def _close_open_shift(db: Session, shift: OpenShift, status: str) -> None:
    shift.status = status
    shift.row_version += 1
    for claim in db.query(ShiftRequest).filter(ShiftRequest.company_id == shift.company_id,
                                               ShiftRequest.open_shift_id == shift.id,
                                               ShiftRequest.status.in_(OPEN_REQUEST_STATUSES)):
        _drop_engine(db, claim, "The open shift closed")
        claim.status, claim.close_reason = ("EXPIRED", "OPEN_SHIFT_EXPIRED") if status == "EXPIRED" else (
            "SUPERSEDED", f"OPEN_SHIFT_{status}")
        claim.row_version += 1
    db.flush()


def _recount(db: Session, company_id: int, open_shift_id: Optional[int], *, filled: bool = False) -> None:
    if not open_shift_id:
        return
    shift = _load_open(db, company_id, open_shift_id, lock=True)
    if filled:
        shift.filled_count += 1
        shift.row_version += 1
        if shift.filled_count >= shift.required_count:
            shift.status, shift.filled_at = "FILLED", _now()
            for claim in db.query(ShiftRequest).filter(ShiftRequest.company_id == company_id,
                                                       ShiftRequest.open_shift_id == shift.id,
                                                       ShiftRequest.status.in_(OPEN_REQUEST_STATUSES)):
                _drop_engine(db, claim, "The open shift was filled")
                claim.status, claim.close_reason = "SUPERSEDED", "OPEN_SHIFT_FILLED"
                claim.row_version += 1
            if shift.source == "RELEASED":
                _notify(db, company_id, shift.released_by_employee_id, "Your released shift was picked up",
                        f"{shift.shift_date.strftime('%a %d %b')} is no longer on your schedule.")
        db.flush()


def post_open_shift(db: Session, principal: Principal, body: Mapping[str, Any]) -> OpenShift:
    """``POST /open-shifts`` (§6.3): ``roster:plan`` covering the branch; the instance is in the future."""
    from app.models.org import Branch

    cid = principal.company_id
    _require_v2(db, cid)
    if not principal.has(PLAN):
        raise forbidden()
    branch_id = body.get("branch_id")
    branch = db.query(Branch).filter(Branch.company_id == cid, Branch.id == branch_id).first() if branch_id else None
    if branch is None:
        raise bad_request("Choose the branch.", "INVALID_FIELDS")
    try:
        day = date.fromisoformat(str(body.get("shift_date")))
    except ValueError:
        raise bad_request("Choose the date.", "INVALID_FIELDS")
    if not _setting(db, cid, "shift.open_shifts.enabled", None, day):
        raise _unprocessable("Open shifts are turned off.", "FEATURE_DISABLED")
    shift_id = body.get("shift_id")
    custom = (body.get("custom_start_time"), body.get("custom_end_time"))
    if shift_id is None and not all(custom):
        raise bad_request("Choose a shift or give start and end times.", "INVALID_FIELDS")
    if shift_id is not None:
        shift = db.query(Shift).filter(Shift.company_id == cid, Shift.id == shift_id).first()
        if shift is None or getattr(shift, "status", "ACTIVE") != "ACTIVE":
            raise _unprocessable("That shift is archived or unknown.", "SHIFT_ARCHIVED")
    required = int(body.get("required_count") or 1)
    if not 1 <= required <= 50:
        raise bad_request("Between 1 and 50 people.", "INVALID_FIELDS")
    content = {"kind": "WORK", "shift_id": shift_id, "custom_start_time": custom[0] if shift_id is None else None,
               "custom_end_time": custom[1] if shift_id is None else None, "custom_segments": None, "site_id": None,
               "planned_work_mode": None}
    from app.services import roster_validation
    from app.services.shift_rosters import _branch_zone

    zone = _branch_zone(db, cid, branch.id)
    start = roster_validation.instance_start(db, cid, {**content, "shift_date": day, "employee_id": 0, "entry_seq": 1,
                                                       "shift_version_id": None}, zone)
    now = _now()
    if start is None or start <= now:
        raise _unprocessable("The shift has already started.", "NOT_ELIGIBLE", details={"reasons": ["STARTED"]})
    expires = start
    if body.get("expires_at"):
        expires = min(start, datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00")))
    tags = [str(t).strip().upper() for t in (body.get("required_tags") or []) if str(t).strip()]
    known = {c for (c,) in db.query(SkillTag.code).filter(SkillTag.company_id == cid, SkillTag.code.in_(tags or [""]))}
    if set(tags) - known:
        raise bad_request(f"Unknown skill tags: {', '.join(sorted(set(tags) - known))}.", "INVALID_FIELDS")
    policy_rules = {"same_team": True, "same_branch": True, "priority": "NONE", "exclusive_window_hours": 0}
    priority_until = None
    warnings: List[dict] = []
    window = int(body.get("exclusive_window_hours") or 0)
    if window:
        priority_until = min(now + timedelta(hours=window), now + (expires - now) / 2, expires - timedelta(hours=2))
        if priority_until < now + timedelta(hours=window):
            warnings.append({"code": "PRIORITY_WINDOW_SHORTENED", "requested_hours": window})
    row = OpenShift(company_id=cid, branch_id=branch.id, team_id=body.get("team_id"), site_id=body.get("site_id"),
                    shift_date=day, shift_id=shift_id, custom_start_time=content["custom_start_time"],
                    custom_end_time=content["custom_end_time"], required_count=required,
                    eligibility={**policy_rules, "same_team": bool(body.get("same_team", bool(body.get("team_id")))),
                                 "required_tags": tags},
                    priority_until=priority_until, approval_required=bool(body.get("approval_required", True)),
                    status="OPEN", source=body.get("source") if body.get("source") in ("MANUAL", "UNCOVERED") else "MANUAL",
                    posted_at=now, expires_at=expires, note=(body.get("note") or "")[:500] or None, warnings=warnings,
                    created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "OPEN_SHIFT_POSTED", "Open shift posted", "open_shift", row.id,
                        details={"shift_date": day.isoformat(), "required_count": required, "source": row.source})
    _emit(db, "open_shift.posted", row)
    from app.services import integrations

    integrations.emit(db, cid, "open_shift.posted", str(row.id), "Open shift posted",
                      f"An open shift on {day.strftime('%a %d %b')} needs {required} "
                      f"{'person' if required == 1 else 'people'}.", "/rosters/my-schedule?tab=open")
    return row


def cancel_open_shift(db: Session, principal: Principal, open_shift_id: int) -> OpenShift:
    """``roster:plan``, or the releaser of their own RELEASED shift while unfilled (SP-AS-048)."""
    row = _load_open(db, principal.company_id, open_shift_id, lock=True)
    own_release = row.source == "RELEASED" and row.released_by_employee_id == principal.employee_id
    if not (principal.has(PLAN) or own_release):
        raise forbidden()
    if row.status != "OPEN":
        raise AppError(409, "Only an open shift can be cancelled.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})
    if own_release and not principal.has(PLAN) and row.filled_count:
        raise AppError(409, "Someone already picked it up.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})
    _close_open_shift(db, row, "CANCELLED")
    AuditLogService.log(db, principal, MODULE, "OPEN_SHIFT_CANCELLED", "Open shift cancelled", "open_shift", row.id)
    return row


def claim(db: Session, principal: Principal, open_shift_id: int, body: Mapping[str, Any]) -> ShiftRequest:
    """``POST /open-shifts/{id}/claim`` (§5.4): eligibility at claim, validation; without approval the first valid
    claim fills the slot."""
    cid = principal.company_id
    _require_v2(db, cid)
    me = _own(db, principal)
    shift = _load_open(db, cid, open_shift_id)
    _require_switches(db, cid, [me.id], shift.shift_date, open_shifts=True)
    lock_employees(db, sorted({me.id} | ({shift.released_by_employee_id} if shift.released_by_employee_id else set())))
    shift = _load_open(db, cid, open_shift_id, lock=True)
    now = _now()
    if shift.status == "OPEN" and now >= shift.expires_at:
        _close_open_shift(db, shift, "EXPIRED")
    if shift.status != "OPEN" or shift.filled_count >= shift.required_count:
        raise AppError(409, "This open shift is no longer available.", "OPEN_SHIFT_FILLED",
                       extra={"details": {"current_status": shift.status}})
    if shift.released_by_employee_id == me.id:
        raise _unprocessable("You released this shift.", "NOT_ELIGIBLE", details={"reasons": ["SELF"]})
    existing = db.query(ShiftRequest.id).filter(ShiftRequest.company_id == cid, ShiftRequest.open_shift_id == shift.id,
                                                ShiftRequest.requester_employee_id == me.id,
                                                ShiftRequest.status.in_(OPEN_REQUEST_STATUSES)).first()
    if existing is not None:
        raise AppError(409, "You already claimed this shift.", "SHIFT_REQUEST_OPEN",
                       extra={"details": {"request_id": existing[0]}})
    policy = scheduling(db, cid, me.id, shift.shift_date)
    rules = {**policy, "open_shift_eligibility": {**policy["open_shift_eligibility"],
                                                  "same_team": bool((shift.eligibility or {}).get("same_team", True)),
                                                  "same_branch": bool((shift.eligibility or {}).get("same_branch", True))}}
    ctx = eligibility(db, cid, requester_id=None, party_id=me.id, day=shift.shift_date, policy=rules,
                      branch_id=shift.branch_id, team_id=shift.team_id,
                      required_tags=(shift.eligibility or {}).get("required_tags") or [],
                      priority_until=shift.priority_until)
    if not ctx["eligible"]:
        raise _unprocessable("You cannot claim this shift.", "NOT_ELIGIBLE", details={"reasons": ctx["reasons"]})
    acknowledged = list(body.get("acknowledged_warnings") or [])
    unacked = [w for w in ctx["warnings"] if w not in acknowledged]
    if unacked:
        raise _unprocessable("Acknowledge the warnings before claiming.", "WARNINGS_UNACKNOWLEDGED",
                             issues=[{"rule_key": w, "severity": "WARN"} for w in unacked])
    data = {"kind": "PICKUP", "requester_employee_id": me.id, "open_shift": shift}
    changes, _, _ = _plan(db, cid, data)
    results = _validate(db, cid, changes, acknowledged=acknowledged, reason=body.get("reason"), gate=True)
    row = ShiftRequest(company_id=cid, kind="PICKUP", status="PENDING_APPROVAL", requester_employee_id=me.id,
                       open_shift_id=shift.id, shift_date=shift.shift_date, reason=(body.get("reason") or "")[:500] or None,
                       acknowledged_warnings=acknowledged, checks=results + [{"rule_key": w, "severity": "WARN"}
                                                                             for w in ctx["warnings"]],
                       approval_required=shift.approval_required, expires_at=shift.expires_at, submitted_at=now)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_SUBMITTED", "Open shift claimed", "shift_request", row.id,
                        details={"open_shift_id": shift.id})
    _notice_consent(db, principal, row, me.id, shift.shift_date,
                    {"kind": "WORK", "shift_id": shift.shift_id, "custom_start_time": shift.custom_start_time,
                     "custom_end_time": shift.custom_end_time, "custom_segments": None, "site_id": shift.site_id,
                     "planned_work_mode": None}, body)
    if not shift.approval_required:
        _apply(db, principal, row)
    else:
        _route(db, principal, row)
    return row


def list_open_shifts(db: Session, principal: Principal, *, start: Optional[date], end: Optional[date],
                     status: Optional[str]) -> dict:
    """Planners (``roster:plan``) see every open shift in range; employees (``roster:request``) see OPEN ones they may
    claim, each with ``can_claim`` and the reasons that concern themselves."""
    cid = principal.company_id
    if not (principal.has(PLAN) or principal.has(REQUEST) or principal.has(APPROVE)):
        raise forbidden()
    today = clock.company_today(db, cid)
    start = start or today
    end = end or start + timedelta(days=30)
    query = db.query(OpenShift).filter(OpenShift.company_id == cid, OpenShift.shift_date >= start,
                                       OpenShift.shift_date <= end)
    planner = principal.has(PLAN) or principal.has(APPROVE)
    if status:
        query = query.filter(OpenShift.status == status)
    elif not planner:
        query = query.filter(OpenShift.status == "OPEN")
    rows = query.order_by(OpenShift.shift_date, OpenShift.id).limit(200).all()
    codes = _shift_codes(db, cid)
    claims = dict(db.query(ShiftRequest.open_shift_id, func.count(ShiftRequest.id)).filter(
        ShiftRequest.company_id == cid, ShiftRequest.open_shift_id.in_([r.id for r in rows] or [0]),
        ShiftRequest.status.in_(OPEN_REQUEST_STATUSES)).group_by(ShiftRequest.open_shift_id).all())
    mine = {}
    me = principal.employee_id
    if me is not None:
        mine = {s: (rid, st) for s, rid, st in db.query(ShiftRequest.open_shift_id, ShiftRequest.id, ShiftRequest.status)
                .filter(ShiftRequest.company_id == cid, ShiftRequest.requester_employee_id == me,
                        ShiftRequest.open_shift_id.in_([r.id for r in rows] or [0]))}
    items = []
    for row in rows:
        item = serialize_open_shift(db, row, codes, claims)
        if me is not None and principal.has(REQUEST) and row.status == "OPEN":
            policy = scheduling(db, cid, me, row.shift_date)
            rules = {**policy, "open_shift_eligibility": {**policy["open_shift_eligibility"],
                                                          "same_team": bool((row.eligibility or {}).get("same_team", True)),
                                                          "same_branch": bool((row.eligibility or {}).get("same_branch", True))}}
            ctx = eligibility(db, cid, requester_id=None, party_id=me, day=row.shift_date, policy=rules,
                              branch_id=row.branch_id, team_id=row.team_id,
                              required_tags=(row.eligibility or {}).get("required_tags") or [],
                              priority_until=row.priority_until)
            if row.released_by_employee_id == me:
                ctx = {"eligible": False, "reasons": ["SELF"], "warnings": []}
            claimed = mine.get(row.id)
            item.update({"can_claim": ctx["eligible"] and not (claimed and claimed[1] in OPEN_REQUEST_STATUSES),
                         "reasons": ctx["reasons"], "warnings": ctx["warnings"],
                         "my_claim": {"id": claimed[0], "status": claimed[1]} if claimed else None})
        elif not planner:
            continue
        items.append(item)
    return {"items": items, "can_post": principal.has(PLAN), "from": start.isoformat(), "to": end.isoformat()}


def open_shift_detail(db: Session, principal: Principal, open_shift_id: int) -> dict:
    """The approver's view: the claims ordered by claim time with each claimant's checks (§6.5)."""
    row = _load_open(db, principal.company_id, open_shift_id)
    if not (principal.has(PLAN) or principal.has(APPROVE)):
        raise forbidden()
    claims = (db.query(ShiftRequest).filter(ShiftRequest.company_id == principal.company_id,
                                            ShiftRequest.open_shift_id == row.id)
              .order_by(ShiftRequest.submitted_at, ShiftRequest.id).all())
    return {**serialize_open_shift(db, row), "claims": [serialize(db, c, principal) for c in claims]}


# =============================================================================================== lists
def list_requests(db: Session, principal: Principal, *, box: str, status: Optional[str]) -> dict:
    """``mine`` (filed by me), ``incoming`` (asked of me), ``approvals`` (PENDING_APPROVAL in my roster:approve scope,
    never my own)."""
    cid = principal.company_id
    query = db.query(ShiftRequest).filter(ShiftRequest.company_id == cid)
    if box == "mine":
        me = _own(db, principal)
        query = query.filter(ShiftRequest.requester_employee_id == me.id)
    elif box == "incoming":
        me = _own(db, principal)
        query = query.filter(ShiftRequest.target_employee_id == me.id)
    elif box == "approvals":
        if not principal.has(APPROVE):
            raise forbidden()
        scope = authz.employee_scope(db, principal, APPROVE)
        query = query.filter(scope.clause(ShiftRequest.requester_employee_id))
        if principal.employee_id is not None:
            query = query.filter(ShiftRequest.requester_employee_id != principal.employee_id,
                                 or_(ShiftRequest.target_employee_id.is_(None),
                                     ShiftRequest.target_employee_id != principal.employee_id))
        if not status:
            query = query.filter(ShiftRequest.status == "PENDING_APPROVAL")
    else:
        raise bad_request("Unknown list.", "INVALID_FIELDS")
    if status:
        query = query.filter(ShiftRequest.status == status)
    rows = query.order_by(ShiftRequest.submitted_at.desc(), ShiftRequest.id.desc()).limit(200).all()
    return {"items": [serialize(db, r, principal) for r in rows], "total": len(rows)}


def get(db: Session, principal: Principal, request_id: int) -> dict:
    row = _load(db, principal.company_id, request_id)
    me = principal.employee_id
    if me not in {row.requester_employee_id, row.target_employee_id} and not (
            principal.has(APPROVE) and authz.can_access_employee(db, principal, APPROVE, row.requester_employee_id)):
        raise not_found("Shift request")
    return serialize(db, row, principal)


# =============================================================================================== availability
def _hhmm(value: Any, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value)
    if len(text) != 5 or text[2] != ":" or not (text[:2].isdigit() and text[3:].isdigit()) or int(text[:2]) > 23 \
            or int(text[3:]) > 59:
        raise bad_request(f"{field}: use HH:MM.", "INVALID_FIELDS")
    return text


def serialize_availability(row: EmployeeAvailability, name: Optional[str] = None) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "employee_name": name, "kind": row.kind,
            "weekday": row.weekday, "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat() if row.end_date else None, "start_time": row.start_time,
            "end_time": row.end_time, "note": row.note, "status": row.status,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None, "row_version": row.row_version}


def add_availability(db: Session, principal: Principal, body: Mapping[str, Any]) -> EmployeeAvailability:
    """An employee states when they cannot (or prefer to) work; approval per ``SCHEDULING_POLICY.availability``."""
    me = _own(db, principal)
    cid = principal.company_id
    kind = body.get("kind") or "UNAVAILABLE"
    if kind not in ("UNAVAILABLE", "PREFERRED"):
        raise bad_request("Unavailable or preferred.", "INVALID_FIELDS")
    try:
        start = date.fromisoformat(str(body.get("start_date")))
        end = date.fromisoformat(str(body["end_date"])) if body.get("end_date") else None
    except ValueError:
        raise bad_request("Use dates as YYYY-MM-DD.", "INVALID_FIELDS")
    if end is not None and end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    weekday = body.get("weekday")
    if weekday is not None and (not isinstance(weekday, int) or not 0 <= weekday <= 6):
        raise bad_request("Weekday 0 (Monday) to 6 (Sunday).", "INVALID_FIELDS")
    start_time, end_time = _hhmm(body.get("start_time"), "Start"), _hhmm(body.get("end_time"), "End")
    if (start_time is None) != (end_time is None):
        raise bad_request("Give both times or none (whole day).", "INVALID_FIELDS")
    policy = scheduling(db, cid, me.id, start)
    rules = policy.get("availability") or {}
    notice = int(rules.get("change_notice_days", 7) or 0)
    today = clock.company_today(db, cid)
    if start < today + timedelta(days=notice):
        raise _unprocessable(f"Availability changes need {notice} days' notice.", "NOTICE_PERIOD_SHORT",
                             details={"earliest": (today + timedelta(days=notice)).isoformat()})
    approval = bool(rules.get("approval_required", True))
    row = EmployeeAvailability(company_id=cid, employee_id=me.id, kind=kind, weekday=weekday, start_date=start,
                               end_date=end, start_time=start_time, end_time=end_time,
                               note=(body.get("note") or "")[:300] or None,
                               status="PENDING" if approval else "APPROVED",
                               decided_at=None if approval else _now())
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "AVAILABILITY_SUBMITTED", "Availability submitted",
                        "employee_availability", row.id, details={"kind": kind, "status": row.status})
    return row


def my_availability(db: Session, principal: Principal) -> List[dict]:
    me = _own(db, principal)
    rows = (db.query(EmployeeAvailability).filter(EmployeeAvailability.company_id == principal.company_id,
                                                  EmployeeAvailability.employee_id == me.id)
            .order_by(EmployeeAvailability.start_date.desc(), EmployeeAvailability.id.desc()).limit(200))
    return [serialize_availability(r) for r in rows]


def cancel_availability(db: Session, principal: Principal, availability_id: int) -> EmployeeAvailability:
    me = _own(db, principal)
    row = db.query(EmployeeAvailability).filter(EmployeeAvailability.company_id == principal.company_id,
                                                EmployeeAvailability.id == availability_id).with_for_update().first()
    if row is None or row.employee_id != me.id:
        raise not_found("Availability")
    if row.status in ("CANCELLED", "REJECTED"):
        raise AppError(409, "Already closed.", "INVALID_TRANSITION", extra={"details": {"current_status": row.status}})
    row.status = "CANCELLED"
    row.row_version += 1
    db.flush()
    return row


def team_availability(db: Session, principal: Principal, status: Optional[str]) -> List[dict]:
    if not (principal.has(PLAN) or principal.has(APPROVE)):
        raise forbidden()
    code = PLAN if principal.has(PLAN) else APPROVE
    scope = authz.employee_scope(db, principal, code)
    query = db.query(EmployeeAvailability).filter(EmployeeAvailability.company_id == principal.company_id,
                                                  scope.clause(EmployeeAvailability.employee_id))
    if status:
        query = query.filter(EmployeeAvailability.status == status)
    rows = query.order_by(EmployeeAvailability.start_date, EmployeeAvailability.id).limit(300).all()
    names = _names(db, principal.company_id, [r.employee_id for r in rows])
    return [serialize_availability(r, names.get(r.employee_id)) for r in rows]


def decide_availability(db: Session, principal: Principal, availability_id: int, decision: str) -> EmployeeAvailability:
    if decision not in ("APPROVE", "REJECT"):
        raise bad_request("Approve or reject.", "INVALID_FIELDS")
    row = db.query(EmployeeAvailability).filter(EmployeeAvailability.company_id == principal.company_id,
                                                EmployeeAvailability.id == availability_id).with_for_update().first()
    if row is None:
        raise not_found("Availability")
    if principal.employee_id == row.employee_id:
        raise forbidden("You cannot decide your own availability.")
    if not any(principal.has(c) and authz.can_access_employee(db, principal, c, row.employee_id) for c in (PLAN, APPROVE)):
        raise not_found("Availability")
    if row.status != "PENDING":
        raise AppError(409, "Already decided.", "INVALID_TRANSITION", extra={"details": {"current_status": row.status}})
    row.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    row.decided_by_user_id, row.decided_at = principal.user_id, _now()
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, "AVAILABILITY_DECIDED", f"Availability {row.status.lower()}",
                        "employee_availability", row.id)
    _notify(db, principal.company_id, row.employee_id, f"Availability {row.status.lower()}",
            f"From {row.start_date.strftime('%d %b')}.", link="/rosters/my-schedule?tab=availability")
    return row


# =============================================================================================== skill tags
def list_tags(db: Session, principal: Principal) -> List[dict]:
    if not (principal.has(READ) or principal.has(PLAN)):
        raise forbidden()
    rows = db.query(SkillTag).filter(SkillTag.company_id == principal.company_id).order_by(SkillTag.code).all()
    counts = dict(db.query(EmployeeSkillTag.tag_id, func.count(EmployeeSkillTag.id)).filter(
        EmployeeSkillTag.company_id == principal.company_id).group_by(EmployeeSkillTag.tag_id).all())
    return [{"id": r.id, "code": r.code, "name": r.name, "status": r.status, "holders": counts.get(r.id, 0)}
            for r in rows]


def create_tag(db: Session, principal: Principal, code: str, name: str) -> SkillTag:
    if not principal.has_company_scope(PLAN) and not principal.has("shift:manage"):
        raise forbidden()
    code = (code or "").strip().upper()
    if not code or len(code) > 40 or not all(ch.isalnum() or ch in "_-" for ch in code):
        raise bad_request("Code: letters, digits, dash or underscore (up to 40).", "INVALID_FIELDS")
    if db.query(SkillTag.id).filter(SkillTag.company_id == principal.company_id, SkillTag.code == code).first():
        raise AppError(409, f"Tag {code} exists.", "CONFLICT")
    row = SkillTag(company_id=principal.company_id, code=code, name=(name or code).strip()[:100], status="ACTIVE")
    db.add(row)
    db.flush()
    return row


def set_employee_tags(db: Session, principal: Principal, employee_id: int, tags: Sequence[Mapping[str, Any]]) -> List[dict]:
    if not principal.has(PLAN):
        raise forbidden()
    authz.ensure_employee_access(db, principal, PLAN, employee_id)
    cid = principal.company_id
    by_code = {t.code: t for t in db.query(SkillTag).filter(SkillTag.company_id == cid)}
    wanted = {}
    for item in tags:
        code = str(item.get("code") or "").upper()
        if code not in by_code:
            raise bad_request(f"Unknown tag {code}.", "INVALID_FIELDS")
        valid_to = date.fromisoformat(str(item["valid_to"])) if item.get("valid_to") else None
        wanted[by_code[code].id] = valid_to
    current = {r.tag_id: r for r in db.query(EmployeeSkillTag).filter(EmployeeSkillTag.company_id == cid,
                                                                      EmployeeSkillTag.employee_id == employee_id)}
    for tag_id, row in current.items():
        if tag_id not in wanted:
            db.delete(row)
    for tag_id, valid_to in wanted.items():
        if tag_id in current:
            current[tag_id].valid_to = valid_to
        else:
            db.add(EmployeeSkillTag(company_id=cid, employee_id=employee_id, tag_id=tag_id, valid_to=valid_to))
    db.flush()
    held = [{"code": t.code, "valid_to": wanted[t.id].isoformat() if wanted[t.id] else None}
            for t in by_code.values() if t.id in wanted]
    AuditLogService.log(db, principal, MODULE, "SKILL_TAGS_SET", "Employee skill tags set", "employee", employee_id,
                        details={"tags": sorted(h["code"] for h in held)})
    return held


# =============================================================================================== acknowledgement
def acknowledge_entry(db: Session, principal: Principal, entry_id: int) -> dict:
    """``SCHEDULING_POLICY.acknowledge_published_shifts``: the employee confirms they saw a published shift."""
    me = _own(db, principal) if principal.has(REQUEST) else authz.own_employee(db, principal)
    entry = _entry(db, principal.company_id, entry_id, lock=True)
    if entry.employee_id != me.id:
        raise not_found("Roster entry")
    if entry.acknowledged_at is None:
        entry.acknowledged_at = _now()
        entry.row_version += 1
        db.flush()
        AuditLogService.log(db, principal, MODULE, "SHIFT_ACKNOWLEDGED", "Published shift acknowledged",
                            "roster_entry", entry.id, details={"version": entry.version})
    return {"entry_id": entry.id, "acknowledged_at": entry.acknowledged_at.isoformat(), "version": entry.version}


# =============================================================================================== settings
SWITCHES = {"requests_enabled": "shift.requests.enabled", "open_shifts_enabled": "shift.open_shifts.enabled"}
SETTINGS_POLICY_NAME = "Shift requests & open shifts"


def _company_policy(db: Session, company_id: int):
    """The tenant's company-wide SCHEDULING_POLICY (an ACTIVE policy whose rule is "everyone"), else None."""
    from app.models.payroll_config import Policy, PolicyAssignment

    rows = (db.query(Policy).join(PolicyAssignment, PolicyAssignment.policy_id == Policy.id)
            .filter(Policy.company_id == company_id, Policy.type == "SCHEDULING_POLICY", Policy.status == "ACTIVE",
                    PolicyAssignment.status == "ACTIVE")
            .order_by(PolicyAssignment.priority.desc(), Policy.id).all())
    for row in rows:
        rules = db.query(PolicyAssignment).filter(PolicyAssignment.policy_id == row.id,
                                                  PolicyAssignment.status == "ACTIVE").all()
        if any((r.criteria or {}).get("everyone") for r in rules):
            return row
    return None


def settings_view(db: Session, principal: Principal) -> dict:
    """Settings › Attendance › Shift swaps & open shifts: the two switches and the company scheduling policy's
    request rules (03-shifts/03 §3.1), as one form."""
    from app.payroll.services import policies as policy_service

    if not (principal.has("attendance:manage_policy") or principal.has("shift:manage") or principal.has(PLAN)):
        raise forbidden()
    cid = principal.company_id
    today = clock.company_today(db, cid)
    policy = _company_policy(db, cid)
    fields: Dict[str, Any] = {}
    if policy is not None:
        active = policy_service.get_policy(db, principal, policy.id).get("active_version")
        fields = dict((active or {}).get("fields") or {})
    legacy = not fields or fields.get("profile") == "LEGACY_V1"
    requests ={**DEFAULT_REQUESTS, **(fields.get("requests") or {})}
    for kind in ("swap", "drop", "release"):
        requests[kind] = {**DEFAULT_REQUESTS[kind], **(requests.get(kind) or {})}
    if legacy:
        for kind in ("swap", "drop", "release"):
            requests[kind]["enabled"] = False
        requests["change_enabled"] = False
    return {
        **{name: _setting(db, cid, key, None, today) for name, key in SWITCHES.items()},
        "policy_id": policy.id if policy is not None else None, "policy_legacy": legacy,
        "requests": requests,
        "open_shift_eligibility": {"same_team": True, "same_branch": True, "priority": "NONE",
                                   "exclusive_window_hours": 0, **(fields.get("open_shift_eligibility") or {})},
        "availability": {"enabled": False, "change_notice_days": 7, "approval_required": True,
                         **(fields.get("availability") or {})},
        "acknowledge_published_shifts": bool(fields.get("acknowledge_published_shifts", False)),
        "can_manage": principal.has_company_scope("attendance:manage_policy") and principal.has("shift:manage"),
    }


def save_settings(db: Session, principal: Principal, data: Mapping[str, Any], reason: str) -> dict:
    """Write the switches (``settings.write_tenant_value``, change control kept) and a new version of the company
    scheduling policy (STANDARD, activated today; the rest of its fields are kept)."""
    from app.payroll.services import policies as policy_service
    from app.payroll.services import settings as settings_engine

    cid = principal.company_id
    if not (principal.has_company_scope("attendance:manage_policy") and principal.has("shift:manage")):
        raise forbidden("Changing shift request settings needs company-wide attendance and shift management access.")
    today = clock.company_today(db, cid)
    current = settings_view(db, principal)
    for name, key in SWITCHES.items():
        if name in data and data[name] is not None and bool(data[name]) != current[name]:
            settings_engine.write_tenant_value(db, principal, key, bool(data[name]), reason=reason)
    policy_keys = ("requests", "open_shift_eligibility", "availability", "acknowledge_published_shifts")
    if any(k in data and data[k] is not None for k in policy_keys):
        policy = _company_policy(db, cid)
        base: Dict[str, Any] = {}
        if policy is not None:
            active = policy_service.get_policy(db, principal, policy.id).get("active_version")
            base = dict((active or {}).get("fields") or {})
        fields = {**base, "profile": "STANDARD", "schedule_source": base.get("schedule_source") or "PLACEMENT"}
        if base.get("profile") == "LEGACY_V1":  # a LEGACY_V1 version carries only the profile and the source
            fields = {"profile": "STANDARD", "schedule_source": base.get("schedule_source") or "PLACEMENT"}
        for key in policy_keys:
            if data.get(key) is not None:
                fields[key] = data[key]
        if policy is None:
            created = policy_service.create_policy(db, principal, type_code="SCHEDULING_POLICY",
                                                   name=SETTINGS_POLICY_NAME, fields=fields, effective_from=today)
            policy_id, version = created["id"], 1
            policy_service.put_assignments(db, principal, policy_id,
                                           [policy_service.RuleInput({"everyone": True}, 10)])
        else:
            policy_id = policy.id
            version = policy_service.create_version(db, principal, policy_id, fields=fields,
                                                    effective_from=today)["version"]
        policy_service.activate_version(db, principal, policy_id, version, effective_from=today, reason=reason)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_REQUEST_SETTINGS_SAVED", "Shift request settings saved",
                        "company_setting", None, details={"changed": sorted(k for k, v in data.items() if v is not None)})
    return settings_view(db, principal)


# =============================================================================================== jobs
def expire_due(db: Session, company_id: int, now: Optional[datetime] = None) -> dict:
    """Jobs ``shift_request.expire`` / ``open_shift.expire`` (sweepers only)."""
    now = now or _now()
    requests = 0
    for row in db.query(ShiftRequest).filter(ShiftRequest.company_id == company_id,
                                             ShiftRequest.status.in_(OPEN_REQUEST_STATUSES),
                                             ShiftRequest.expires_at.isnot(None), ShiftRequest.expires_at < now
                                             ).with_for_update(skip_locked=True):
        _drop_engine(db, row, "Expired at the cutoff")
        row.status, row.close_reason = "EXPIRED", "CUTOFF"
        row.row_version += 1
        requests += 1
        _notify(db, company_id, row.requester_employee_id, "A shift request expired",
                f"The {row.kind.lower()} for {row.shift_date.strftime('%a %d %b')} reached its cutoff. "
                "You keep your published shift.")
    shifts = 0
    for shift in db.query(OpenShift).filter(OpenShift.company_id == company_id, OpenShift.status == "OPEN",
                                            OpenShift.expires_at <= now).with_for_update(skip_locked=True):
        _close_open_shift(db, shift, "EXPIRED")
        shifts += 1
        if shift.source == "RELEASED":
            _notify(db, company_id, shift.released_by_employee_id, "Nobody picked up your shift",
                    f"You keep {shift.shift_date.strftime('%a %d %b')}.")
    db.flush()
    return {"company_id": company_id, "requests_expired": requests, "open_shifts_expired": shifts}


def _emit(db: Session, topic: str, row: Any) -> None:
    from app.database.db import current_company_id
    from app.payroll.services import outbox

    if current_company_id(db) is not None:
        outbox.emit(db, topic, {"id": row.id, "status": getattr(row, "status", None)})
