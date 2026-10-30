"""Saved list views, the LIST_KEYS registry and the bulk-scope rule (X-M3; X 04 §2.2, §2.8, §2.10, §2.13; spine
§3.10, §8.1, §11.5; SP-AS-122..125, SP-AS-231).

* ``LIST_KEYS``: one ``ListDef`` per list — read permission, allow-listed query keys, sort keys and default, ``q``
  fields, max date range, report code, system views. A list registers here in the goal that ships its endpoint (the
  DB CHECK of ``saved_views.list_key`` already knows every key of X 04 §2.13).
* Views: a user's own rows plus views shared by others on the same list; at most 20 named views per user per list
  (422 ``SAVED_VIEW_LIMIT_REACHED``), unique names (409 ``SAVED_VIEW_NAME_TAKEN``), one default per list. A shared
  view is edited only by its owner. System views (``sys:<key>``) are read-only and listed per persona; a user may hide
  or position one (a row with ``system_key``).
* ``resolve_view``: ``view_id`` (integer or ``sys:<key>``) -> the stored query re-validated against the list's
  allow-list; a private view of someone else, another list's view or another tenant's view is 404 like an unknown id
  (X-UI-02; RLS hides other tenants).
* ``bulk_scope``: up to 100 explicit ids, or a filter with ``expected_total`` and the snapshot ``{as_of, ids_hash}``
  the user saw; rows created after ``as_of`` are out, withdrawn / decided rows are skipped, and any other set
  difference is 409 ``BULK_SCOPE_CHANGED`` (X-UI-01: same count, different set).

Services never commit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import concurrency
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.views import LIST_KEY_VALUES, VIEWS_PER_LIST, SavedView
from app.services import personas as persona_rules
from app.services.audit_log_service import AuditLogService

PAGE_SIZES = (25, 50)
BULK_MAX_ITEMS = 100
QUERY_META_KEYS = frozenset({"sort", "columns", "limit", "q"})


@dataclass(frozen=True)
class SystemView:
    key: str  # without the "sys:" prefix
    label: str
    personas: FrozenSet[str]
    query: Mapping[str, Any] = field(default_factory=dict)

    @property
    def view_id(self) -> str:
        return f"sys:{self.key}"


@dataclass(frozen=True)
class ListDef:
    list_key: str
    endpoint: str
    read_permissions: Tuple[str, ...]  # any of them opens the list
    query_keys: FrozenSet[str]  # filter keys a view may store (besides sort, columns, limit, q)
    sort_keys: Tuple[str, ...]  # ascending names; "-" prefix = descending
    default_sort: str
    q_fields: Tuple[str, ...] = ()
    columns: Tuple[str, ...] = ()  # optional columns a view may choose
    max_range_days: Optional[int] = None
    report_code: Optional[str] = None
    bulk_actions: Tuple[str, ...] = ()
    system_views: Tuple[SystemView, ...] = ()

    def allowed_sort(self, value: str) -> bool:
        return value.lstrip("-") in self.sort_keys


def _views(*rows) -> Tuple[SystemView, ...]:
    return tuple(SystemView(key, label, frozenset(personas), query) for key, label, personas, query in rows)


# The lists whose endpoints exist (G-02); later goals add their keys here (shift.*, attendance.*, team.*, ...).
LIST_KEYS: Mapping[str, ListDef] = {
    "overtime.entries": ListDef(
        list_key="overtime.entries", endpoint="GET /overtime/entries",
        read_permissions=("overtime:request", "overtime:approve", "payroll:inputs:read"),
        query_keys=frozenset({"status", "employee_id", "start", "end", "mine", "awaiting"}),
        sort_keys=("date", "employee", "minutes", "status"), default_sort="-date",
        # The optional columns of the /overtime "All entries" table (the identifier columns are always shown).
        q_fields=("employee_name", "employee_code"), columns=("minutes", "approved", "source", "status"),
        max_range_days=400, report_code="ATT_OT_REGISTER",
        system_views=_views(
            ("my_overtime", "My overtime", ("EMPLOYEE",), {"mine": True, "sort": "-date"}),
            ("waiting_for_me", "Waiting for me", ("TEAM_LEAD", "MANAGER", "HR"), {"awaiting": True, "sort": "date"}),
            ("in_my_scope", "In my scope", ("HR", "PAYROLL"), {"status": "PENDING_APPROVAL", "sort": "date"}),
        ),
    ),
    "audit.logs": ListDef(
        list_key="audit.logs", endpoint="GET /audit-logs", read_permissions=("audit:read",),
        query_keys=frozenset({"module"}), sort_keys=("created_at",), default_sort="-created_at",
    ),
    # Shift module (G-04 / G-15; FU-AS-51 g): the shift catalogue and rule lists already offered views; without a
    # registration every call answered 422 INVALID_LIST_KEY.
    "shift.shifts": ListDef(
        list_key="shift.shifts", endpoint="GET /shifts", read_permissions=("shift:read",),
        query_keys=frozenset({"kind", "status", "branch_id", "legal_entity_id", "night", "in_use"}),
        sort_keys=("code", "name", "kind", "status"), default_sort="code", q_fields=("code", "name"),
        columns=("name", "kind", "times", "net", "breaks", "owner", "version", "status"),
        # X 04 §2.8 catalogue rows (AS-UX-01).
        system_views=_views(
            ("active_shifts", "Active shifts", ("HR",), {"status": "ACTIVE", "sort": "code"}),
            ("night_shifts", "Night shifts", ("HR",), {"night": True}),
            ("not_used", "Not used", ("HR",), {"in_use": False}),
            ("archived", "Archived", ("HR",), {"status": "ARCHIVED"}),
        ),
    ),
    "shift.rules": ListDef(
        list_key="shift.rules", endpoint="GET /shift-assignment-rules", read_permissions=("shift:read",),
        query_keys=frozenset({"shift_id", "status", "in_force_on", "ending_within_days"}), sort_keys=("priority",),
        default_sort="-priority",
        system_views=_views(
            ("in_force_today", "In force today", ("HR",), {"in_force_on": "today", "sort": "-priority"}),
            ("ending_in_30_days", "Ending in 30 days", ("HR",), {"ending_within_days": 30}),
            ("inactive", "Inactive", ("HR",), {"status": "INACTIVE"}),
        ),
    ),
    "roster.open_shifts": ListDef(
        list_key="roster.open_shifts", endpoint="GET /open-shifts",
        read_permissions=("roster:request", "roster:plan", "roster:approve"),
        query_keys=frozenset({"from", "to", "status", "eligible_only", "has_pending_claims", "expires_on",
                              "filled_within"}),
        sort_keys=("shift_date",), default_sort="shift_date", max_range_days=92,
        system_views=_views(
            ("available_to_me", "Available to me", ("EMPLOYEE",), {"eligible_only": True, "sort": "shift_date"}),
            ("all_open", "All open", ("TEAM_LEAD", "HR"), {"status": "OPEN"}),
            ("needs_approval", "Needs approval", ("TEAM_LEAD", "HR"), {"has_pending_claims": True}),
            ("expiring_today", "Expiring today", ("TEAM_LEAD", "HR"), {"status": "OPEN", "expires_on": "today"}),
            ("filled_this_week", "Filled this week", ("TEAM_LEAD", "HR"), {"status": "FILLED", "filled_within": "this_week"}),
            ("expired", "Expired", ("HR",), {"status": "EXPIRED"}),
        ),
    ),
}
assert set(LIST_KEYS) <= set(LIST_KEY_VALUES), "every registered list key is allowed by the saved_views CHECK"


# --------------------------------------------------------------------------------------------------- helpers
def list_def(list_key: str) -> ListDef:
    found = LIST_KEYS.get(list_key)
    if found is None:
        raise AppError(422, "This list does not support saved views.", "INVALID_LIST_KEY")
    return found


def _can_open(principal: Principal, definition: ListDef) -> bool:
    return any(principal.has(code) for code in definition.read_permissions)


def _require_list(principal: Principal, list_key: str) -> ListDef:
    definition = list_def(list_key)
    if not _can_open(principal, definition):
        raise forbidden()
    return definition


def clean_query(definition: ListDef, query: Mapping[str, Any], *, strict: bool) -> Dict[str, Any]:
    """Keep the allow-listed keys of a view query. ``strict`` (create / update) refuses anything else with 422;
    otherwise (applying a stored view) unknown keys, sorts and columns are dropped silently (X 04 §2.9 rule 4)."""
    cleaned: Dict[str, Any] = {}
    rejected: List[str] = []
    for key, value in (query or {}).items():
        if key == "sort":
            if isinstance(value, str) and definition.allowed_sort(value):
                cleaned[key] = value
            else:
                rejected.append(key)
        elif key == "limit":
            if value in PAGE_SIZES:
                cleaned[key] = value
            else:
                rejected.append(key)
        elif key == "columns":
            if isinstance(value, list) and all(isinstance(c, str) for c in value):
                kept = [c for c in value if c in definition.columns]
                if len(kept) != len(value) and strict:
                    rejected.append(key)
                cleaned[key] = kept
            else:
                rejected.append(key)
        elif key == "q":
            if isinstance(value, str) and len(value) <= 100 and definition.q_fields:
                cleaned[key] = value.strip()
            else:
                rejected.append(key)
        elif key in definition.query_keys and isinstance(value, (str, int, bool, list)) and not isinstance(value, dict):
            cleaned[key] = value
        else:
            rejected.append(key)
    if strict and rejected:
        raise AppError(422, "The view stores settings this list does not support.", "INVALID_QUERY",
                       extra={"details": {"keys": sorted(rejected)}})
    return cleaned


def _system_views_for(principal: Principal, definition: ListDef) -> List[SystemView]:
    mine = persona_rules.personas(principal)
    return [v for v in definition.system_views if v.personas & mine]


def _visible_query(db: Session, principal: Principal, list_key: str):
    return db.query(SavedView).filter(
        SavedView.company_id == principal.company_id, SavedView.list_key == list_key,
        SavedView.system_key.is_(None),
        or_(SavedView.user_id == principal.user_id, SavedView.is_shared.is_(True)))


def serialize(view: SavedView, principal: Principal, owner_name: Optional[str] = None) -> dict:
    own = view.user_id == principal.user_id
    return {"id": view.id, "list_key": view.list_key, "name": view.name, "query": view.query or {},
            "is_shared": bool(view.is_shared), "is_default": bool(view.is_default) and own, "position": view.position,
            "own": own, "owner_name": None if own else owner_name, "row_version": view.row_version,
            "allowed_actions": ["update", "delete"] if own else []}


# ---------------------------------------------------------------------------------------------------- reads
def list_views(db: Session, principal: Principal, list_key: str) -> dict:
    """Own and shared views plus the system views of the caller's personas (with the caller's hide / position)."""
    from app.models.user import User

    definition = _require_list(principal, list_key)
    rows = _visible_query(db, principal, list_key).order_by(SavedView.position, SavedView.id).all()
    owners = {u.id: u.full_name for u in db.query(User).filter(
        User.company_id == principal.company_id, User.id.in_({r.user_id for r in rows} or {0}))}
    states = {r.system_key: r for r in db.query(SavedView).filter(
        SavedView.company_id == principal.company_id, SavedView.user_id == principal.user_id,
        SavedView.list_key == list_key, SavedView.system_key.isnot(None))}
    system = []
    for index, view in enumerate(_system_views_for(principal, definition)):
        state = states.get(view.key)
        system.append({"id": view.view_id, "key": view.key, "name": view.label, "query": dict(view.query),
                       "is_hidden": bool(state.is_hidden) if state else False,
                       "position": state.position if state else index})
    own = [serialize(r, principal) for r in rows if r.user_id == principal.user_id]
    shared = [serialize(r, principal, owners.get(r.user_id)) for r in rows if r.user_id != principal.user_id]
    return {"list_key": list_key, "default_sort": definition.default_sort, "sort_keys": list(definition.sort_keys),
            "columns": list(definition.columns), "page_sizes": list(PAGE_SIZES), "limit": VIEWS_PER_LIST,
            "system_views": system, "views": own + shared}


def resolve_view(db: Session, principal: Principal, list_key: str, view_id: Any) -> Tuple[Dict[str, Any], dict]:
    """(allow-listed query, applied_view) for ``view_id`` = an integer id or ``sys:<key>``; anything the caller may
    not see answers 404 exactly like an unknown id (X-UI-02)."""
    definition = _require_list(principal, list_key)
    if isinstance(view_id, str) and view_id.startswith("sys:"):
        found = next((v for v in _system_views_for(principal, definition) if v.view_id == view_id), None)
        if found is None:
            raise not_found("Saved view")
        return clean_query(definition, found.query, strict=False), {"id": found.view_id, "name": found.label,
                                                                   "system": True}
    try:
        numeric = int(view_id)
    except (TypeError, ValueError):
        raise not_found("Saved view")
    row = _visible_query(db, principal, list_key).filter(SavedView.id == numeric).first()
    if row is None:
        raise not_found("Saved view")
    return clean_query(definition, row.query or {}, strict=False), {"id": row.id, "name": row.name, "system": False}


def apply_view(params: Mapping[str, Any], view_query: Mapping[str, Any]) -> Dict[str, Any]:
    """The view's stored values under the explicit request parameters: explicit values win (X 04 §2.8)."""
    merged = dict(view_query)
    merged.update({k: v for k, v in params.items() if v is not None})
    return merged


# --------------------------------------------------------------------------------------------------- writes
def _owned(db: Session, principal: Principal, view_id: int) -> SavedView:
    row = db.query(SavedView).filter(SavedView.company_id == principal.company_id, SavedView.id == view_id,
                                     SavedView.system_key.is_(None)).first()
    if row is None or (row.user_id != principal.user_id and not row.is_shared):
        raise not_found("Saved view")
    if row.user_id != principal.user_id:
        raise forbidden("Only the person who made this view can change it.")
    return row


def _clear_other_defaults(db: Session, principal: Principal, list_key: str, keep_id: Optional[int]) -> None:
    query = db.query(SavedView).filter(SavedView.company_id == principal.company_id,
                                       SavedView.user_id == principal.user_id, SavedView.list_key == list_key,
                                       SavedView.is_default.is_(True))
    if keep_id is not None:
        query = query.filter(SavedView.id != keep_id)
    for other in query:
        other.is_default = False
        other.row_version += 1
    db.flush()


def _name_taken(exc: IntegrityError) -> bool:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == "uq_saved_views_name"


def create_view(db: Session, principal: Principal, data) -> SavedView:
    definition = _require_list(principal, data.list_key)
    count = db.query(func.count(SavedView.id)).filter(
        SavedView.company_id == principal.company_id, SavedView.user_id == principal.user_id,
        SavedView.list_key == data.list_key, SavedView.system_key.is_(None)).scalar()
    if count >= VIEWS_PER_LIST:
        raise AppError(422, f"You have {VIEWS_PER_LIST} views for this list. Delete one to save another.",
                       "SAVED_VIEW_LIMIT_REACHED", extra={"details": {"limit": VIEWS_PER_LIST}})
    query = clean_query(definition, data.query, strict=True)
    if data.is_default:
        _clear_other_defaults(db, principal, data.list_key, None)
    row = SavedView(company_id=principal.company_id, user_id=principal.user_id, list_key=data.list_key,
                    name=data.name.strip(), query=query, is_shared=data.is_shared, is_default=data.is_default,
                    position=count)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        if _name_taken(exc):
            raise conflict("You already have a view with this name for this list.", "SAVED_VIEW_NAME_TAKEN")
        raise
    AuditLogService.log(db, principal, "Views", "SAVED_VIEW_CREATED", f"Saved a view of {data.list_key}",
                        "saved_view", row.id, details={"list_key": data.list_key, "shared": row.is_shared})
    return row


def update_view(db: Session, principal: Principal, view_id: int, data, expected_version: Optional[int]) -> SavedView:
    row = _owned(db, principal, view_id)
    concurrency.check_row_version(row, expected_version)
    definition = list_def(row.list_key)
    fields = data.model_fields_set
    if "name" in fields and data.name is not None:
        row.name = data.name.strip()
    if "query" in fields and data.query is not None:
        row.query = clean_query(definition, data.query, strict=True)
    if "is_shared" in fields and data.is_shared is not None:
        row.is_shared = data.is_shared
    if "position" in fields and data.position is not None:
        row.position = data.position
    if "is_default" in fields and data.is_default is not None:
        if data.is_default:
            _clear_other_defaults(db, principal, row.list_key, row.id)
        row.is_default = data.is_default
    row.row_version += 1
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        if _name_taken(exc):
            raise conflict("You already have a view with this name for this list.", "SAVED_VIEW_NAME_TAKEN")
        raise
    AuditLogService.log(db, principal, "Views", "SAVED_VIEW_UPDATED", f"Updated a view of {row.list_key}",
                        "saved_view", row.id, details={"list_key": row.list_key, "shared": row.is_shared})
    return row


def delete_view(db: Session, principal: Principal, view_id: int, expected_version: Optional[int]) -> None:
    """Deleting a shared view removes it for everyone (X 04 §2.8)."""
    row = _owned(db, principal, view_id)
    concurrency.check_row_version(row, expected_version)
    AuditLogService.log(db, principal, "Views", "SAVED_VIEW_DELETED", f"Deleted a view of {row.list_key}",
                        "saved_view", row.id, details={"list_key": row.list_key, "shared": row.is_shared})
    db.delete(row)
    db.flush()


def set_system_view_state(db: Session, principal: Principal, list_key: str, system_key: str,
                          is_hidden: Optional[bool], position: Optional[int]) -> dict:
    definition = _require_list(principal, list_key)
    if not any(v.key == system_key for v in _system_views_for(principal, definition)):
        raise not_found("Saved view")
    row = db.query(SavedView).filter(SavedView.company_id == principal.company_id,
                                     SavedView.user_id == principal.user_id, SavedView.list_key == list_key,
                                     SavedView.system_key == system_key).first()
    if row is None:
        row = SavedView(company_id=principal.company_id, user_id=principal.user_id, list_key=list_key,
                        system_key=system_key, query={}, position=0)
        db.add(row)
    else:
        row.row_version += 1
    if is_hidden is not None:
        row.is_hidden = is_hidden
    if position is not None:
        row.position = position
    db.flush()
    return {"key": system_key, "is_hidden": bool(row.is_hidden), "position": row.position}


# ---------------------------------------------------------------------------------------------- bulk scope
def ids_hash(ids: Iterable[int]) -> str:
    return hashlib.sha256(",".join(str(i) for i in sorted(set(ids))).encode()).hexdigest()


def list_snapshot(ids: Sequence[int], as_of: datetime) -> dict:
    """The snapshot a bulk-capable list returns with its rows: the instant and the hash of the matching ids."""
    return {"as_of": as_of.isoformat(), "ids_hash": ids_hash(ids)}


@dataclass(frozen=True)
class BulkScope:
    ids: List[int]  # act on these (each still re-checked per item)
    skipped: List[int] = field(default_factory=list)  # matched the snapshot, withdrawn or decided since


def bulk_scope(*, ids: Optional[Sequence[int]] = None, filter_query: Optional[Mapping[str, Any]] = None,
               expected_total: Optional[int] = None, snapshot: Optional[Mapping[str, Any]] = None,
               matching_ids: Optional[Callable[[datetime], Sequence[int]]] = None,
               left_since: Callable[[datetime], Iterable[int]] = lambda as_of: ()) -> BulkScope:
    """The ids a bulk action applies to (X 04 §2.10; SP-AS-231; X-UI-01).

    Explicit mode: 1..100 ids. Filter mode, with the snapshot ``{as_of, ids_hash}`` and ``expected_total`` the user
    confirmed: ``matching_ids(as_of)`` are the rows created at or before ``as_of`` that match the filter now;
    ``left_since(as_of)`` the rows that matched then and were withdrawn or decided since. Their union must be the set
    the user saw (same count and the same ids hash), else 409 BULK_SCOPE_CHANGED — so a set with the same count but
    other members is caught. The rows that left are reported as skipped, never acted on.
    """
    if ids is not None:
        if filter_query is not None:
            raise bad_request("Send ids or a filter, not both.", "INVALID_BULK_SCOPE")
        unique = list(dict.fromkeys(int(i) for i in ids))
        if not unique or len(unique) > BULK_MAX_ITEMS:
            raise AppError(422, f"Select between 1 and {BULK_MAX_ITEMS} items.", "TOO_MANY_ITEMS",
                           extra={"details": {"limit": BULK_MAX_ITEMS}})
        return BulkScope(unique)
    if (filter_query is None or expected_total is None or matching_ids is None or not snapshot
            or not snapshot.get("as_of") or not snapshot.get("ids_hash")):
        raise bad_request("A filter needs expected_total and the list snapshot.", "INVALID_BULK_SCOPE")
    try:
        as_of = datetime.fromisoformat(str(snapshot["as_of"]))
    except ValueError:
        raise bad_request("The list snapshot is not valid.", "INVALID_BULK_SCOPE")
    current = list(dict.fromkeys(matching_ids(as_of)))
    left = [i for i in dict.fromkeys(left_since(as_of)) if i not in set(current)]
    seen = current + left
    if len(seen) != expected_total or ids_hash(seen) != snapshot["ids_hash"]:
        raise AppError(409, f"The list changed: {len(current)} items match now (you saw {expected_total}). "
                            "Review and confirm again.", "BULK_SCOPE_CHANGED",
                       extra={"details": {"total": len(current), "expected_total": expected_total}})
    return BulkScope(current, left)
