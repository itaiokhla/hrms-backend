"""Holiday calendars: structure, flattening and scoped administration (LH-M1 WI-1.8; owner 04-holiday/01 §3, §8).

A calendar holds its **own** occurrences (``holidays.calendar_id``), **includes** other calendars (a DAG ordered by
priority; priority 0 mirrors the legacy ``parent_calendar_id``) and **excludes** inherited items. ``flatten`` is the
single resolver that replaces ``holiday_service.calendar_lineage``, ``pay_org.calendar_lineage_select`` and
``org_dimensions._calendar_ancestors``; day facts, the banking calendar and ``/holidays`` read through it.

Flattening is compositional (DECISIONS IG-LH-11): flat(C) = own(C) ∪ ⋃ flat(I) for I in includes(C) (priority
order), minus C's exclusions, which only ever remove rows C inherits. A calendar reached twice (diamond) contributes
once; the first path in priority order is the "source" shown. Pure helpers (``flatten_rows``, ``include_path``) take
plain data; the rest take ``(db, principal, ...)`` and never commit.

Structure changes (includes, exclusions) follow the structure-change backdating rule of §3.3 rule 6: the dates whose
holiday set changes for a calendar that has users are classified (gain = addition, loss = edit) against
``calendar_local_today`` (T) and ``holiday.backdate_days`` (W): after T normal; T-W..T needs ``holiday:publish`` and a
reason (a CONFIRMED holiday cannot be lost there: ``HOLIDAY_IN_PAST``); before T-W refused (``BACKDATE_LIMIT`` for
additions, ``HOLIDAY_IN_PAST`` for edits). All or nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.holiday import Holiday, HolidayCalendarExclusion, HolidayCalendarInclude
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import (
    EMPLOYEE_OBSERVANCE,
    MAX_CALENDAR_DEPTH,
    BranchCalendarLink,
    HolidayCalendar,
    HolidayCalendarAssignment,
)
from app.payroll import countries
from app.payroll.countries import CountryError
from app.services.audit_log_service import AuditLogService

AUDIT_MODULE = "Holidays"
READ_CODES = ("holiday:read", "holiday:manage", "holiday:publish", "org:read", "org:manage_dimensions")
FAR_PAST, FAR_FUTURE = date(2000, 1, 1), date(2100, 12, 31)
DEFAULT_WEEKEND = (5, 6)
_ONE_DAY = timedelta(days=1)


# ============================================================================================== pure core
@dataclass(frozen=True)
class Row:
    """One occurrence candidate (a ``holidays`` row reduced to what flattening needs)."""

    id: int
    calendar_id: int
    date: date
    name: str
    holiday_type: str
    rule_id: Optional[int] = None
    portion: str = "FULL"
    status: str = "CONFIRMED"


@dataclass(frozen=True)
class Exclusion:
    id: int
    calendar_id: int
    holiday_id: Optional[int] = None
    rule_id: Optional[int] = None
    effective_from_year: Optional[int] = None

    def removes(self, row: Row) -> bool:
        if self.holiday_id is not None:
            return row.id == self.holiday_id
        return row.rule_id is not None and row.rule_id == self.rule_id and (
            self.effective_from_year is None or row.date.year >= self.effective_from_year)


@dataclass(frozen=True)
class FlatRow:
    row: Row
    path: Tuple[int, ...]  # C ... source calendar (first path in priority order)

    @property
    def inherited(self) -> bool:
        return len(self.path) > 1


@dataclass(frozen=True)
class FlatCalendar:
    calendar_id: int
    rows: Tuple[FlatRow, ...]
    excluded: Tuple[Tuple[FlatRow, int], ...]  # (row, exclusion id) removed on the way
    order: Tuple[int, ...]  # calendars visited, first-visit order
    calendar_version: str = ""

    def by_date(self) -> Dict[date, List[FlatRow]]:
        out: Dict[date, List[FlatRow]] = defaultdict(list)
        for flat in self.rows:
            out[flat.row.date].append(flat)
        return out

    def ids(self) -> Set[int]:
        return {f.row.id for f in self.rows}


class CalendarGraphError(ValueError):
    def __init__(self, code: str, message: str, path: Sequence[int] = ()):
        super().__init__(message)
        self.code, self.message, self.path = code, message, tuple(path)


def include_path(includes: Mapping[int, Sequence[Tuple[int, int]]], start: int, target: int) -> Optional[List[int]]:
    """A path start -> ... -> target through ``includes`` ({calendar: [(priority, included)]}), or None."""
    stack: List[Tuple[int, List[int]]] = [(start, [start])]
    seen: Set[int] = set()
    while stack:
        node, path = stack.pop()
        if node == target and len(path) > 1:
            return path
        if node in seen:
            continue
        seen.add(node)
        for _, child in sorted(includes.get(node, ()), reverse=True):
            stack.append((child, path + [child]))
    return None


def depth_below(includes: Mapping[int, Sequence[Tuple[int, int]]], start: int, limit: int = MAX_CALENDAR_DEPTH + 2) -> int:
    """Longest include chain below ``start`` (edges), bounded."""
    memo: Dict[int, int] = {}

    def walk(node: int, guard: int) -> int:
        if guard > limit:
            return guard
        if node in memo:
            return memo[node]
        best = 0
        for _, child in includes.get(node, ()):
            best = max(best, 1 + walk(child, guard + 1))
        memo[node] = best
        return best

    return walk(start, 0)


def check_graph(includes: Mapping[int, Sequence[Tuple[int, int]]], calendar_id: int,
                names: Mapping[int, str] = {}) -> None:
    """Refuse a cycle through ``calendar_id`` or a chain deeper than MAX_CALENDAR_DEPTH (``ORG_CALENDAR_CYCLE``)."""
    for _, child in includes.get(calendar_id, ()):
        back = include_path(includes, child, calendar_id) if child != calendar_id else [calendar_id]
        if back is not None:
            cycle = [calendar_id] + back
            label = " → ".join(names.get(c, str(c)) for c in cycle)
            raise CalendarGraphError("ORG_CALENDAR_CYCLE", f"These includes would create a cycle: {label}.", cycle)
    parents: Dict[int, List[int]] = defaultdict(list)
    for parent, items in includes.items():
        for _, child in items:
            parents[child].append(parent)

    def height(node: int, guard: int = 0) -> int:
        if guard > MAX_CALENDAR_DEPTH + 2:
            return guard
        return max((1 + height(p, guard + 1) for p in parents.get(node, ())), default=0)

    if height(calendar_id) + depth_below(includes, calendar_id) > MAX_CALENDAR_DEPTH:
        raise CalendarGraphError("ORG_CALENDAR_CYCLE",
                                 f"The include chain would be too deep (more than {MAX_CALENDAR_DEPTH} levels).")


def flatten_rows(calendar_id: int, includes: Mapping[int, Sequence[Tuple[int, int]]],
                 exclusions: Mapping[int, Sequence[Exclusion]], rows_by_calendar: Mapping[int, Sequence[Row]],
                 *, max_depth: int = MAX_CALENDAR_DEPTH) -> FlatCalendar:
    """The flattened calendar (own rows plus included calendars' flattened rows, minus exclusions; §3.3, §3.4)."""
    memo: Dict[int, Tuple[List[FlatRow], List[Tuple[FlatRow, int]]]] = {}
    order: List[int] = []

    def visit(cal: int, depth: int, stack: Tuple[int, ...]) -> Tuple[List[FlatRow], List[Tuple[FlatRow, int]]]:
        if cal in stack or depth > max_depth:
            return [], []  # defensive: the database refuses cycles and over-deep chains
        if cal in memo:
            return memo[cal]
        if cal not in order:
            order.append(cal)
        own = [FlatRow(r, (cal,)) for r in sorted(rows_by_calendar.get(cal, ()), key=lambda r: (r.date, r.id))]
        result: Dict[int, FlatRow] = {f.row.id: f for f in own}
        removed: List[Tuple[FlatRow, int]] = []
        rules = list(exclusions.get(cal, ()))
        for _, child in sorted(includes.get(cal, ())):
            child_rows, child_removed = visit(child, depth + 1, stack + (cal,))
            removed.extend((FlatRow(f.row, (cal,) + f.path), eid) for f, eid in child_removed)
            for flat in child_rows:
                if flat.row.id in result:
                    continue  # diamond: first path in priority order wins
                hit = next((e for e in rules if e.removes(flat.row)), None)
                lifted = FlatRow(flat.row, (cal,) + flat.path)
                if hit is not None:
                    removed.append((lifted, hit.id))
                    continue
                result[flat.row.id] = lifted
        rows = sorted(result.values(), key=lambda f: (f.row.date, f.row.id))
        memo[cal] = (rows, removed)
        return memo[cal]

    rows, removed = visit(calendar_id, 0, ())
    seen_removed: Dict[int, Tuple[FlatRow, int]] = {}
    for flat, eid in removed:
        seen_removed.setdefault(flat.row.id, (flat, eid))
    kept = {f.row.id for f in rows}
    excluded = tuple(v for k, v in sorted(seen_removed.items()) if k not in kept)
    version = _version(calendar_id, rows, includes, exclusions, order)
    return FlatCalendar(calendar_id, tuple(rows), excluded, tuple(order), version)


def _version(calendar_id, rows, includes, exclusions, order) -> str:
    payload = {
        "c": calendar_id,
        "rows": [[f.row.id, f.row.date.isoformat(), f.row.name, f.row.holiday_type, f.row.portion, f.row.status]
                 for f in rows],
        "inc": {str(c): sorted(includes.get(c, ())) for c in order},
        "exc": {str(c): sorted((e.id, e.holiday_id or 0, e.rule_id or 0, e.effective_from_year or 0)
                               for e in exclusions.get(c, ())) for c in order},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def diff_dates(before: FlatCalendar, after: FlatCalendar) -> Dict[date, str]:
    """{date: 'addition' | 'edit'} where the flattened holiday set differs (gain only = addition)."""
    a, b = before.by_date(), after.by_date()
    out: Dict[date, str] = {}
    for day in set(a) | set(b):
        old = {(f.row.id, f.row.holiday_type, f.row.portion) for f in a.get(day, [])}
        new = {(f.row.id, f.row.holiday_type, f.row.portion) for f in b.get(day, [])}
        if old == new:
            continue
        out[day] = "addition" if old <= new else "edit"
    return out


# ============================================================================================== loaders
def _graph(db: Session, company_id: int) -> Dict[int, List[Tuple[int, int]]]:
    """{calendar: [(priority, included)]} of the tenant; a legacy parent without its mirror row counts as priority 0.
    Inside a day-facts batch (read-only) the graph is read once (``db.info["lh_graph_memo"]``)."""
    memo = db.info.get("lh_graph_memo")
    if memo is not None and company_id in memo:
        return memo[company_id]
    includes: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for cal, inc, prio in db.query(HolidayCalendarInclude.calendar_id, HolidayCalendarInclude.included_calendar_id,
                                   HolidayCalendarInclude.priority).filter(
            HolidayCalendarInclude.company_id == company_id):
        includes[cal].append((prio, inc))
    for cal, parent in db.query(HolidayCalendar.id, HolidayCalendar.parent_calendar_id).filter(
            HolidayCalendar.company_id == company_id, HolidayCalendar.parent_calendar_id.isnot(None)):
        if all(child != parent for _, child in includes.get(cal, ())):
            includes[cal].append((0, parent))
    if memo is not None:
        memo[company_id] = includes
    return includes


def _exclusions(db: Session, company_id: int) -> Dict[int, List[Exclusion]]:
    out: Dict[int, List[Exclusion]] = defaultdict(list)
    for row in db.query(HolidayCalendarExclusion).filter(HolidayCalendarExclusion.company_id == company_id):
        out[row.calendar_id].append(Exclusion(row.id, row.calendar_id, row.holiday_id, row.holiday_rule_id,
                                              row.effective_from_year))
    return out


def reachable(includes: Mapping[int, Sequence[Tuple[int, int]]], calendar_id: int) -> Set[int]:
    out, stack = set(), [calendar_id]
    while stack:
        node = stack.pop()
        if node in out or len(out) > 10000:
            continue
        out.add(node)
        stack.extend(child for _, child in includes.get(node, ()))
    return out


def dependents(includes: Mapping[int, Sequence[Tuple[int, int]]], calendar_id: int) -> Set[int]:
    """Calendars that include ``calendar_id`` directly or through others (including itself)."""
    parents: Dict[int, Set[int]] = defaultdict(set)
    for cal, items in includes.items():
        for _, child in items:
            parents[child].add(cal)
    out, stack = set(), [calendar_id]
    while stack:
        node = stack.pop()
        if node in out:
            continue
        out.add(node)
        stack.extend(parents.get(node, ()))
    return out


def _to_row(h: Holiday) -> Row:
    return Row(h.id, h.calendar_id, h.date, h.name, h.holiday_type or "COMPANY",
               getattr(h, "rule_id", None), getattr(h, "portion", None) or "FULL",
               getattr(h, "status", None) or "CONFIRMED")


def _pair_key(h: Holiday) -> Optional[Tuple[int, int, Any, int]]:
    """(calendar, rule, rule_year, sequence) shared by an ORIGINAL row and its OBSERVED partner (02 §3.5 rule 3)."""
    if h.rule_id is None:
        return None
    return h.calendar_id, h.rule_id, (h.source_ref or {}).get("rule_year"), h.sequence_index


def original_partners(db: Session, company_id: int, observed: Iterable[Holiday]) -> Dict[int, Holiday]:
    """``{observed row id: its ORIGINAL row}`` (same calendar, rule, rule-year, sequence; 02 §3.5 rule 3), for the
    employee-observance choice OBSERVED_IF_OFF (02 §3.6). CANCELLED originals are left out."""
    rows = [h for h in observed if h.observance == "OBSERVED" and h.rule_id is not None]
    if not rows:
        return {}
    keys = {_pair_key(h): h.id for h in rows}
    out: Dict[int, Holiday] = {}
    for h in db.query(Holiday).filter(Holiday.company_id == company_id,
                                      Holiday.rule_id.in_(sorted({r.rule_id for r in rows})),
                                      Holiday.observance == "ORIGINAL", Holiday.status != "CANCELLED"):
        observed_id = keys.get(_pair_key(h))
        if observed_id is not None:
            out[observed_id] = h
    return out


def draft_years(db: Session, company_id: int, calendar_ids: Iterable[int]) -> Set[Tuple[int, int]]:
    """(calendar, year) pairs whose year record is DRAFT; their rows never count (01 §6.1, SPINE §7 step 5)."""
    from app.models.holiday import HolidayCalendarYear

    ids = sorted(set(calendar_ids))
    if not ids:
        return set()
    return {(c, y) for c, y in db.query(HolidayCalendarYear.calendar_id, HolidayCalendarYear.year).filter(
        HolidayCalendarYear.company_id == company_id, HolidayCalendarYear.calendar_id.in_(ids),
        HolidayCalendarYear.status == "DRAFT")}


def provisional_counts(db: Session, company_id: int) -> bool:
    """``holiday.provisional_counts`` (default true): PROVISIONAL rows of PUBLISHED years count on their date."""
    from app.payroll import settings_registry

    if "holiday.provisional_counts" not in settings_registry.REGISTRY:
        return True
    from app.services.holiday_years import holiday_setting

    return bool(holiday_setting(db, company_id, "holiday.provisional_counts", True))  # fails closed (LH-WL-02)


class Flattener:
    """Per-request cache of the tenant's include graph, exclusions and flattened calendars.

    Rows that count (01 §6.1, 02 §4.7, SPINE §7 step 5): not CANCELLED; not in a DRAFT year (a year without a record
    is a legacy year and counts, IG-LH-15); PROVISIONAL only while ``holiday.provisional_counts``; an ORIGINAL row
    whose OBSERVED partner counts is replaced by it. ``drafts`` = {(calendar, year)} shown as if published (the
    admin's draft view and the publish preview).
    """

    def __init__(self, db: Session, company_id: int, *, drafts: Iterable[Tuple[int, int]] = (),
                 include_provisional: Optional[bool] = None):
        self.db, self.company_id = db, company_id
        self.includes = _graph(db, company_id)
        self.exclusions = _exclusions(db, company_id)
        self.drafts = set(drafts)
        self._provisional = include_provisional
        self._holidays: Dict[int, Holiday] = {}
        self._cache: Dict[Tuple[int, date, date], FlatCalendar] = {}

    @property
    def counts_provisional(self) -> bool:
        if self._provisional is None:
            self._provisional = provisional_counts(self.db, self.company_id)
        return self._provisional

    def counting_rows(self, calendar_ids: Sequence[int], date_from: date, date_to: date) -> List[Holiday]:
        """The rows of ``calendar_ids`` that count between two dates (rules in the class docstring)."""
        if not calendar_ids:
            return []
        candidates = self.db.query(Holiday).filter(
            Holiday.company_id == self.company_id, Holiday.calendar_id.in_(list(calendar_ids)),
            Holiday.status != "CANCELLED",
            or_((Holiday.date >= date_from) & (Holiday.date <= date_to),
                (Holiday.observance == "OBSERVED") & (Holiday.observed_from_date >= date_from)
                & (Holiday.observed_from_date <= date_to))).all()
        hidden = draft_years(self.db, self.company_id, calendar_ids) - self.drafts
        counts_provisional = self.counts_provisional
        visible = [h for h in candidates if (h.calendar_id, h.date.year) not in hidden
                   and (counts_provisional or h.status != "PROVISIONAL")]
        observed = {_pair_key(h) for h in visible if h.observance == "OBSERVED"}
        return [h for h in visible if date_from <= h.date <= date_to
                and not (h.observance == "ORIGINAL" and _pair_key(h) in observed)]

    def flatten(self, calendar_id: int, date_from: date = FAR_PAST, date_to: date = FAR_FUTURE, *,
                includes: Optional[Mapping[int, Sequence[Tuple[int, int]]]] = None,
                exclusions: Optional[Mapping[int, Sequence[Exclusion]]] = None) -> FlatCalendar:
        key = (calendar_id, date_from, date_to)
        if includes is None and exclusions is None and key in self._cache:
            return self._cache[key]
        graph = includes if includes is not None else self.includes
        excl = exclusions if exclusions is not None else self.exclusions
        cals = sorted(reachable(graph, calendar_id))
        rows: Dict[int, List[Row]] = defaultdict(list)
        for h in self.counting_rows(cals, date_from, date_to):
            self._holidays[h.id] = h
            rows[h.calendar_id].append(_to_row(h))
        flat = flatten_rows(calendar_id, graph, excl, rows)
        if includes is None and exclusions is None:
            self._cache[key] = flat
        return flat

    def holiday(self, holiday_id: int) -> Holiday:
        return self._holidays[holiday_id]

    def holidays_on(self, calendar_id: int, days: Iterable[date]) -> Dict[date, List[Holiday]]:
        wanted = set(days)
        if not wanted:
            return {}
        flat = self.flatten(calendar_id, min(wanted), max(wanted))
        out: Dict[date, List[Holiday]] = defaultdict(list)
        for f in flat.rows:
            if f.row.date in wanted:
                out[f.row.date].append(self._holidays[f.row.id])
        return out


def flatten(db: Session, company_id: int, calendar_id: int, date_from: date = FAR_PAST,
            date_to: date = FAR_FUTURE) -> FlatCalendar:
    """The flattened calendar (§3 box; draft years arrive with ``holiday_calendar_years``)."""
    return Flattener(db, company_id).flatten(calendar_id, date_from, date_to)


def lineage_ids(db: Session, company_id: int, calendar_id: Optional[int]) -> List[int]:
    """Every calendar whose rows can reach ``calendar_id`` (itself first, then includes in priority order)."""
    if calendar_id is None:
        return []
    graph = _graph(db, company_id)
    out: List[int] = []

    def walk(node: int, depth: int) -> None:
        if node in out or depth > MAX_CALENDAR_DEPTH:
            return
        out.append(node)
        for _, child in sorted(graph.get(node, ())):
            walk(child, depth + 1)

    walk(calendar_id, 0)
    return out


# ============================================================================================== weekend, clocks
def effective_weekend(db: Session, calendar: HolidayCalendar) -> List[int]:
    """``weekend_days`` if set, else the calendar country's weekend, else its legal entity's, else Sat+Sun (V3-013)."""
    if calendar.weekend_days:
        return sorted(int(d) for d in calendar.weekend_days)
    for code in (calendar.country_code,
                 db.get(LegalEntity, calendar.legal_entity_id).country_code if calendar.legal_entity_id else None):
        if code:
            try:
                return sorted(countries.default_weekend(code))
            except Exception:  # noqa: BLE001 - unknown country data: next fallback
                continue
    return list(DEFAULT_WEEKEND)


def _zone_today(zone: Optional[str]) -> date:
    from app.services.holiday_assignments import zone_today

    return zone_today(zone)


def calendar_users(db: Session, company_id: int, calendar_id: int, on_or_after: date) -> Tuple[Set[int], Set[int]]:
    """(branch ids, employee ids) that resolve to ``calendar_id`` or a calendar including it on or after a date."""
    from app.services.holiday_assignments import WorkWeekResolver

    graph = _graph(db, company_id)
    targets = dependents(graph, calendar_id)
    resolver = WorkWeekResolver(db, company_id)
    branches: Set[int] = set()
    for branch in db.query(Branch).filter(Branch.company_id == company_id):
        links = resolver.links(branch.id)
        if links:
            if any(l.holiday_calendar_id in targets and (l.effective_to is None or l.effective_to >= on_or_after)
                   for l in links):
                branches.add(branch.id)
        elif branch.holiday_calendar_id in targets:
            branches.add(branch.id)
    employees: Set[int] = set()
    rows = (db.query(EmployeeAssignment)
            .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
                    or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on_or_after))
            .all())
    resolver.prime_shift_criterion({a.employee_id for a in rows}, on_or_after, on_or_after)  # shift_ids: one read
    for a in rows:
        day = max(a.effective_from, on_or_after)
        if resolver.calendar_on(a, day)[0] in targets:
            employees.add(a.employee_id)
    return branches, employees


def calendar_local_today(db: Session, company_id: int, calendar_id: int) -> date:
    """The latest local date among the zones of the calendar's users; else its entity's zone; else the company's
    (§3.7, V3-042)."""
    company_today = clock.company_today(db, company_id)
    branches, employees = calendar_users(db, company_id, calendar_id, company_today - _ONE_DAY)
    zones: Set[str] = set()
    for branch in db.query(Branch).filter(Branch.company_id == company_id,
                                          Branch.id.in_(sorted(branches) or [-1])):
        if branch.timezone:
            zones.add(branch.timezone)
    if employees:
        for a in db.query(EmployeeAssignment).filter(
                EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(sorted(employees)),
                EmployeeAssignment.is_primary.is_(True),
                or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= company_today)):
            zone = a.work_timezone
            if not zone:
                branch = db.get(Branch, a.branch_id)
                zone = branch.timezone if branch is not None else None
            if zone:
                zones.add(zone)
    if zones:
        return max(_zone_today(z) for z in zones)
    calendar = db.get(HolidayCalendar, calendar_id)
    if calendar is not None and calendar.legal_entity_id:
        entity = db.get(LegalEntity, calendar.legal_entity_id)
        if entity is not None and entity.timezone:
            return _zone_today(entity.timezone)
    return company_today


# ============================================================================================== scope (§8)
def can_act(db: Session, principal: Principal, calendar: Optional[HolidayCalendar], code: str = "holiday:manage",
            *, entity_id: Optional[int] = None) -> bool:
    """§8.2: COMPANY scope always; tenant-wide calendars need it; LE scope for the owner entity; BR scope when every
    branch that can resolve to the calendar is the actor's."""
    if principal.is_company_admin or principal.has_company_scope(code):
        return True
    owner = calendar.legal_entity_id if calendar is not None else entity_id
    if owner is None:
        return False
    if owner in principal.scope_targets(code, "LEGAL_ENTITY"):
        return True
    targets = set(principal.scope_targets(code, "BRANCH"))
    if not targets:
        return False
    if calendar is None:
        own = {b for (b,) in db.query(Branch.id).filter(Branch.company_id == principal.company_id,
                                                         Branch.legal_entity_id == owner)}
        return bool(own & targets)
    branches, _ = calendar_users(db, principal.company_id, calendar.id,
                                 calendar_local_today(db, principal.company_id, calendar.id))
    return bool(branches) and branches <= targets


def can_read(principal: Principal) -> bool:
    return principal.is_company_admin or any(principal.scopes(c) for c in READ_CODES)


def _visible_entities(db: Session, principal: Principal) -> Optional[Set[int]]:
    """None = every calendar; else the entities whose calendars the reader may see (§8.4)."""
    if principal.is_company_admin or any(principal.has_company_scope(c) for c in
                                         ("holiday:manage", "holiday:publish", "org:read", "org:manage_dimensions")):
        return None
    entities: Set[int] = set()
    for code in ("holiday:manage", "holiday:publish", "org:read"):
        entities |= set(principal.scope_targets(code, "LEGAL_ENTITY"))
        branches = principal.scope_targets(code, "BRANCH")
        if branches:
            entities |= {e for (e,) in db.query(Branch.legal_entity_id).filter(
                Branch.company_id == principal.company_id, Branch.id.in_(sorted(branches)))}
    if principal.employee_id:
        entities |= {e for (e,) in db.query(EmployeeAssignment.legal_entity_id).filter(
            EmployeeAssignment.company_id == principal.company_id,
            EmployeeAssignment.employee_id == principal.employee_id)}
    return entities


def _get_visible(db: Session, principal: Principal, calendar_id: int, *, lock: bool = False) -> HolidayCalendar:
    query = db.query(HolidayCalendar).filter(HolidayCalendar.company_id == principal.company_id,
                                             HolidayCalendar.id == int(calendar_id))
    row = (query.with_for_update() if lock else query).first()
    if row is None or not can_read(principal):
        raise not_found("Holiday calendar")
    entities = _visible_entities(db, principal)
    if entities is not None and row.legal_entity_id is not None and row.legal_entity_id not in entities:
        raise not_found("Holiday calendar")
    return row


def _get_managed(db: Session, principal: Principal, calendar_id: int) -> HolidayCalendar:
    row = _get_visible(db, principal, calendar_id, lock=True)
    if not can_act(db, principal, row):
        raise forbidden(f"Holiday calendar {row.code} is managed by another scope.")
    return row


# ============================================================================================== validation helpers
def _clean_code(value: Any) -> str:
    code = "".join(str(value or "").split()).upper()
    if not code or len(code) > 50 or not all(c.isalnum() or c in "-_." for c in code):
        raise bad_request("Code must be 1-50 letters, digits, '-', '_' or '.'.", "ORG_CODE_INVALID")
    return code


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > 150:
        raise bad_request("Name must be 1-150 characters.", "ORG_NAME_INVALID")
    return name


def code_from_name(name: str) -> str:
    return (re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-") or "CALENDAR")[:50].rstrip("-")


def _country(value: Any) -> Optional[str]:
    if not value:
        return None
    info = countries.find(str(value).upper())
    if info is None:
        raise bad_request("Choose a country (ISO 3166-1 code).", "ORG_COUNTRY_INVALID")
    return info.alpha2


def _subdivision(value: Any, country: Optional[str]) -> Optional[str]:
    if not value:
        return None
    sub = str(value).upper()
    try:
        countries.subdivision(sub)
    except CountryError:
        raise bad_request("Choose a state or region (ISO 3166-2).", "ORG_SUBDIVISION_INVALID") from None
    if country and not sub.startswith(country + "-"):
        raise bad_request("The region must belong to the calendar's country.", "ORG_SUBDIVISION_INVALID")
    return sub


def _weekend(value: Any) -> Optional[List[int]]:
    if value in (None, []):
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= 3 or len(set(value)) != len(value) or any(
            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 6 for v in value):
        raise bad_request("Weekend days are 1-3 distinct weekdays (0 = Monday ... 6 = Sunday).", "INVALID_WEEKEND")
    return sorted(value)


def _flush(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        code = getattr(exc.orig, "pgcode", None)
        text = str(exc.orig)
        if code == "23505":
            raise conflict("Another holiday calendar already uses this code.", "ORG_CODE_TAKEN") from None
        if "ORG_CALENDAR_CYCLE" in text:
            raise bad_request("These includes would create a cycle or a chain that is too deep.",
                              "ORG_CALENDAR_CYCLE") from None
        if "CALENDAR_ASSIGNMENT_CONFLICT" in text:
            raise conflict("Another assignment with this priority overlaps these dates.",
                           "CALENDAR_ASSIGNMENT_CONFLICT") from None
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        raise bad_request(f"Holiday calendar: the change breaks a data rule ({name or 'constraint'}).",
                          "ORG_CONSTRAINT") from None


def _audit(db: Session, principal: Optional[Principal], action: str, text: str, calendar: HolidayCalendar,
           details: Mapping[str, Any]) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, text, "holiday_calendar", calendar.id,
                        details=dict(details))


def _bump(row: Any) -> None:
    row.row_version = (row.row_version or 1) + 1


def _check_version(row: Any, expected: Optional[int]) -> None:
    if expected is not None and expected != row.row_version:
        raise conflict("This record was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")


def backdate_days(db: Session, company_id: int) -> int:
    from app.services.holiday_work_weeks import backdate_days as days

    return days(db, company_id)


# ============================================================================================== structure-change rule
def check_structure_change(db: Session, principal: Optional[Principal], calendar: HolidayCalendar,
                           changes: Mapping[date, str], lost: Mapping[date, List[Row]], *,
                           reason: Optional[str]) -> Dict[str, Any]:
    """§3.3 rule 6 over the affected dates; raises on the first refused class; returns the classification."""
    if not changes:
        return {"affected": [], "backdated": [], "t": None}
    today = calendar_local_today(db, calendar.company_id, calendar.id)
    window = backdate_days(db, calendar.company_id)
    start = today - timedelta(days=window)
    in_past, too_old, backdated = [], [], []
    for day, kind in sorted(changes.items()):
        if day > today:
            continue
        if day < start:
            (too_old if kind == "addition" else in_past).append(day)
            continue
        if kind == "edit" and any(r.status != "PROVISIONAL" for r in lost.get(day, [])):
            in_past.append(day)
            continue
        backdated.append(day)
    details = {"dates": [d.isoformat() for d in in_past + too_old], "calendar_local_today": today.isoformat(),
               "window_start": start.isoformat()}
    if in_past:
        raise AppError(409, f"The change would remove a confirmed holiday on {in_past[0].isoformat()}, which has "
                            "already started.", "HOLIDAY_IN_PAST", extra={"details": details})
    if too_old:
        raise AppError(422, f"The change reaches {too_old[0].isoformat()}, before the backdating window "
                            f"({start.isoformat()} onward).", "BACKDATE_LIMIT", extra={"details": details})
    if backdated:
        if principal is not None and not can_act(db, principal, calendar, "holiday:publish"):
            raise forbidden("Backdated calendar changes need the 'Publish and backdate holidays' permission.")
        if not (reason or "").strip():
            raise bad_request("Give a reason for a backdated change.", "REASON_REQUIRED")
    return {"affected": [d.isoformat() for d in sorted(changes)], "backdated": [d.isoformat() for d in backdated],
            "t": today.isoformat()}


def _users_exist(db: Session, company_id: int, calendar_id: int) -> bool:
    branches, employees = calendar_users(db, company_id, calendar_id, FAR_PAST)
    return bool(branches or employees)


def _impact(db: Session, flattener: Flattener, calendar: HolidayCalendar,
            new_includes: Optional[Mapping[int, Sequence[Tuple[int, int]]]] = None,
            new_exclusions: Optional[Mapping[int, Sequence[Exclusion]]] = None
            ) -> Tuple[Dict[date, str], Dict[date, List[Row]]]:
    """Dates whose flattened set changes for any calendar with users that is or includes ``calendar``."""
    changes: Dict[date, str] = {}
    lost: Dict[date, List[Row]] = defaultdict(list)
    for cal in sorted(dependents(new_includes or flattener.includes, calendar.id) |
                      dependents(flattener.includes, calendar.id)):
        if not _users_exist(db, calendar.company_id, cal):
            continue
        before = flattener.flatten(cal)
        after = flattener.flatten(cal, includes=new_includes or flattener.includes,
                                  exclusions=new_exclusions or flattener.exclusions)
        for day, kind in diff_dates(before, after).items():
            changes[day] = "edit" if "edit" in (kind, changes.get(day)) else "addition"
            after_ids = {f.row.id for f in after.by_date().get(day, [])}
            lost[day].extend(f.row for f in before.by_date().get(day, []) if f.row.id not in after_ids)
    return changes, lost


def _after_change(db: Session, principal: Optional[Principal], calendar: HolidayCalendar, changes: Mapping[date, str],
                  record_id: int, content: Any) -> Dict[str, Any]:
    """Change signal and recount for the changed dates (§3.3 rule 6; SPINE §6.12)."""
    from app.payroll.services import time_inputs
    from app.services import leave_jobs

    actor = principal.user_id if principal else None
    if changes:
        time_inputs.log_change(db, calendar.company_id, record_type="calendar_link", record_id=record_id,
                               change="UPDATED", content=content, date_from=min(changes), date_to=max(changes),
                               actor_user_id=actor, details={"calendar_id": calendar.id})
    return leave_jobs.recount_dates(db, calendar.company_id, list(changes),
                                    key=f"recalc:calendar:{calendar.id}:{calendar.row_version}:{record_id}",
                                    actor_user_id=actor)


# ============================================================================================== serializers
def serialize(db: Session, principal: Principal, row: HolidayCalendar, *, counts: Optional[Mapping[int, dict]] = None,
              detail: bool = False) -> dict:
    entity = db.get(LegalEntity, row.legal_entity_id) if row.legal_entity_id else None
    graph = _graph(db, row.company_id)
    out = {
        "id": row.id, "code": row.code, "name": row.name, "name_i18n": row.name_i18n or {},
        "description": row.description, "legal_entity_id": row.legal_entity_id,
        "owner": entity.legal_name if entity is not None else "Tenant-wide",
        "country_code": row.country_code, "subdivision_code": row.subdivision_code,
        "weekend_days": row.weekend_days, "effective_weekend": effective_weekend(db, row),
        "employee_observance": row.employee_observance, "status": row.status, "row_version": row.row_version,
        "parent_calendar_id": row.parent_calendar_id,
        "includes_count": len(graph.get(row.id, ())),
        "used_by": dict((counts or {}).get(row.id, {"branches": 0, "employees": 0, "criteria": 0})),
        "can_manage": can_act(db, principal, row) if detail else _can_manage_fast(principal, row),
    }
    if detail:
        out["includes"] = [{"included_calendar_id": inc, "priority": prio,
                            "code": _code(db, row.company_id, inc), "name": _name(db, row.company_id, inc)}
                           for prio, inc in sorted(graph.get(row.id, ()))]
        out["included_by"] = [{"id": c, "code": _code(db, row.company_id, c)}
                              for c in sorted(dependents(graph, row.id) - {row.id})]
        out["local_today"] = calendar_local_today(db, row.company_id, row.id).isoformat()
        out["in_use"] = usage_reason(db, row.company_id, row.id)
    return out


def _can_manage_fast(principal: Principal, row: HolidayCalendar) -> bool:
    if principal.is_company_admin or principal.has_company_scope("holiday:manage"):
        return True
    return row.legal_entity_id is not None and row.legal_entity_id in principal.scope_targets("holiday:manage",
                                                                                               "LEGAL_ENTITY")


def _code(db: Session, company_id: int, calendar_id: int) -> Optional[str]:
    return db.query(HolidayCalendar.code).filter(HolidayCalendar.company_id == company_id,
                                                 HolidayCalendar.id == calendar_id).scalar()


def _name(db: Session, company_id: int, calendar_id: int) -> Optional[str]:
    return db.query(HolidayCalendar.name).filter(HolidayCalendar.company_id == company_id,
                                                 HolidayCalendar.id == calendar_id).scalar()


def serialize_exclusion(db: Session, row: HolidayCalendarExclusion) -> dict:
    holiday = db.get(Holiday, row.holiday_id) if row.holiday_id else None
    return {"id": row.id, "calendar_id": row.calendar_id, "holiday_id": row.holiday_id,
            "holiday_rule_id": row.holiday_rule_id, "effective_from_year": row.effective_from_year,
            "reason": row.reason, "holiday": None if holiday is None else {
                "id": holiday.id, "name": holiday.name, "date": holiday.date.isoformat(),
                "calendar_id": holiday.calendar_id, "holiday_type": holiday.holiday_type,
                "calendar_code": _code(db, row.company_id, holiday.calendar_id) if holiday.calendar_id else None},
            "created_at": row.created_at.isoformat() if row.created_at else None}


# ============================================================================================== usage
def usage_counts(db: Session, company_id: int, on: Optional[date] = None) -> Dict[int, Dict[str, int]]:
    """{calendar_id: {branches, employees, criteria}} resolving to each calendar on ``on``."""
    from app.services.holiday_assignments import WorkWeekResolver

    on = on or clock.company_today(db, company_id)
    out: Dict[int, Dict[str, int]] = defaultdict(lambda: {"branches": 0, "employees": 0, "criteria": 0})
    resolver = WorkWeekResolver(db, company_id)
    for branch in db.query(Branch).filter(Branch.company_id == company_id, Branch.status == "ACTIVE"):
        cal = resolver.branch_calendar_on(branch.id, on)
        if cal:
            out[cal]["branches"] += 1
    for row in resolver.criteria_rows(calendars=True):
        if row.effective_to is None or row.effective_to >= on:
            out[row.calendar_id]["criteria"] += 1
    seen: Set[int] = set()
    current = db.query(EmployeeAssignment).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.effective_from <= on,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on)).all()
    resolver.prime_shift_criterion({a.employee_id for a in current}, on, on)  # shift_ids: one read for everyone
    for a in current:
        if a.employee_id in seen:
            continue
        seen.add(a.employee_id)
        cal = resolver.calendar_on(a, on)[0]
        if cal:
            out[cal]["employees"] += 1
    return dict(out)


def usage_reason(db: Session, company_id: int, calendar_id: int) -> Optional[str]:
    """What keeps a calendar in use on or after today (§3.9), or None."""
    from app.services.holiday_assignments import WorkWeekResolver

    today = clock.company_today(db, company_id)
    if db.query(BranchCalendarLink.id).filter(
            BranchCalendarLink.company_id == company_id, BranchCalendarLink.holiday_calendar_id == calendar_id,
            or_(BranchCalendarLink.effective_to.is_(None), BranchCalendarLink.effective_to >= today)).first():
        return "a current or future branch link"
    resolver = WorkWeekResolver(db, company_id)
    if any(not resolver.links(b) for (b,) in db.query(Branch.id).filter(
            Branch.company_id == company_id, Branch.status == "ACTIVE", Branch.holiday_calendar_id == calendar_id)):
        return "a branch"
    if db.query(HolidayCalendarAssignment.id).filter(
            HolidayCalendarAssignment.company_id == company_id, HolidayCalendarAssignment.calendar_id == calendar_id,
            or_(HolidayCalendarAssignment.effective_to.is_(None), HolidayCalendarAssignment.effective_to >= today)
    ).first():
        return "a current or future calendar assignment"
    if db.query(EmployeeAssignment.id).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.holiday_calendar_id == calendar_id,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= today)).first():
        return "an employee calendar override"
    from app.models.payroll_config import SettingValue

    banking = db.query(SettingValue.value).filter(
        SettingValue.company_id == company_id, SettingValue.key == "payroll.calendar.banking_calendar_id",
        or_(SettingValue.effective_to.is_(None), SettingValue.effective_to >= today)).all()
    if any(str(value) == str(calendar_id) or (isinstance(value, Mapping) and str(value.get("value")) == str(calendar_id))
           for (value,) in banking):
        return "the payroll banking-calendar setting"
    includers = [c for c in dependents(_graph(db, company_id), calendar_id) if c != calendar_id]
    if includers and db.query(HolidayCalendar.id).filter(HolidayCalendar.company_id == company_id,
                                                          HolidayCalendar.id.in_(includers),
                                                          HolidayCalendar.status == "ACTIVE").first():
        return "a calendar that includes it"
    return None


# ============================================================================================== reads
def list_calendars(db: Session, principal: Principal, *, q: Optional[str] = None, status: Optional[str] = None,
                   legal_entity_id: Optional[int] = None, owner: Optional[str] = None,
                   country_code: Optional[str] = None, skip: int = 0, limit: int = 50) -> dict:
    if not can_read(principal):
        raise forbidden()
    cid = principal.company_id
    query = db.query(HolidayCalendar).filter(HolidayCalendar.company_id == cid)
    entities = _visible_entities(db, principal)
    if entities is not None:
        query = query.filter(or_(HolidayCalendar.legal_entity_id.is_(None),
                                 HolidayCalendar.legal_entity_id.in_(sorted(entities) or [-1])))
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(HolidayCalendar.code.ilike(like, escape="\\"),
                                 HolidayCalendar.name.ilike(like, escape="\\")))
    if status:
        if status not in ("ACTIVE", "INACTIVE"):
            raise bad_request("Status must be ACTIVE or INACTIVE.", "INVALID_FILTER")
        query = query.filter(HolidayCalendar.status == status)
    if owner == "TENANT":
        query = query.filter(HolidayCalendar.legal_entity_id.is_(None))
    elif legal_entity_id:
        query = query.filter(HolidayCalendar.legal_entity_id == legal_entity_id)
    if country_code:
        query = query.filter(HolidayCalendar.country_code == str(country_code).upper())
    total = query.count()
    rows = query.order_by(HolidayCalendar.code, HolidayCalendar.id).offset(skip).limit(limit).all()
    counts = usage_counts(db, cid)
    return {"items": [serialize(db, principal, r, counts=counts) for r in rows], "total": total, "skip": skip,
            "limit": limit, "local_today": clock.company_today(db, cid).isoformat(),
            "can_create": principal.is_company_admin or bool(principal.scopes("holiday:manage"))}


def get_calendar(db: Session, principal: Principal, calendar_id: int) -> dict:
    row = _get_visible(db, principal, calendar_id)
    return serialize(db, principal, row, counts=usage_counts(db, principal.company_id), detail=True)


def calendar_holidays(db: Session, principal: Principal, calendar_id: int, year: int) -> dict:
    """The flattened occurrences of a year with their source calendar and path, plus the excluded rows."""
    row = _get_visible(db, principal, calendar_id)
    flat = Flattener(db, principal.company_id).flatten(row.id, date(year, 1, 1), date(year, 12, 31))
    codes = {c: _code(db, row.company_id, c) for c in set(flat.order) | {e[0].row.calendar_id for e in flat.excluded}}

    def item(f: FlatRow, excluded_by: Optional[int] = None) -> dict:
        return {"id": f.row.id, "date": f.row.date.isoformat(), "name": f.row.name, "holiday_type": f.row.holiday_type,
                "portion": f.row.portion, "status": f.row.status, "calendar_id": f.row.calendar_id,
                "holiday_rule_id": f.row.rule_id,
                "source_code": codes.get(f.row.calendar_id), "own": not f.inherited,
                "path": [codes.get(c) or str(c) for c in f.path], "excluded_by": excluded_by}

    own = sum(1 for f in flat.rows if not f.inherited)
    by_source: Dict[str, int] = defaultdict(int)
    for f in flat.rows:
        if f.inherited:
            by_source[codes.get(f.row.calendar_id) or str(f.row.calendar_id)] += 1
    return {"calendar_id": row.id, "year": year, "items": [item(f) for f in flat.rows],
            "excluded": [item(f, eid) for f, eid in flat.excluded],
            "summary": {"total": len(flat.rows), "own": own, "inherited": dict(by_source),
                        "excluded": len(flat.excluded)},
            "calendar_version": flat.calendar_version}


def lineage(db: Session, principal: Principal, calendar_id: int, year: Optional[int] = None) -> dict:
    """The include tree (priority order, depth-limited) with own and flattened counts per node for ``year``."""
    row = _get_visible(db, principal, calendar_id)
    cid = principal.company_id
    year = year or clock.company_today(db, cid).year
    flattener = Flattener(db, cid)
    graph = flattener.includes
    exclusions = flattener.exclusions
    calendars = {c.id: c for c in db.query(HolidayCalendar).filter(
        HolidayCalendar.company_id == cid, HolidayCalendar.id.in_(sorted(reachable(graph, row.id))))}
    start, end = date(year, 1, 1), date(year, 12, 31)
    own_counts: Dict[int, int] = defaultdict(int)
    for (cal,) in db.query(Holiday.calendar_id).filter(Holiday.company_id == cid,
                                                       Holiday.calendar_id.in_(sorted(calendars)),
                                                       Holiday.date >= start, Holiday.date <= end):
        own_counts[cal] += 1

    def node(cal: int, priority: Optional[int], path: Tuple[int, ...]) -> dict:
        c = calendars.get(cal)
        repeated = cal in path
        out = {"id": cal, "code": c.code if c else str(cal), "name": c.name if c else None,
               "status": c.status if c else None, "priority": priority, "own_count": own_counts.get(cal, 0),
               "exclusions": len(exclusions.get(cal, ())), "repeated": repeated, "children": []}
        if not repeated and len(path) < MAX_CALENDAR_DEPTH:
            out["children"] = [node(child, prio, path + (cal,)) for prio, child in sorted(graph.get(cal, ()))]
        return out

    flat = flattener.flatten(row.id, start, end)
    return {"calendar_id": row.id, "year": year, "tree": node(row.id, None, ()),
            "exclusions": [serialize_exclusion(db, e) for e in db.query(HolidayCalendarExclusion).filter(
                HolidayCalendarExclusion.company_id == cid, HolidayCalendarExclusion.calendar_id == row.id)
                .order_by(HolidayCalendarExclusion.id)],
            "flattened": {"total": len(flat.rows), "own": sum(1 for f in flat.rows if not f.inherited),
                          "inherited": sum(1 for f in flat.rows if f.inherited), "excluded": len(flat.excluded)},
            "included_by": [{"id": c, "code": _code(db, cid, c)} for c in sorted(dependents(graph, row.id) - {row.id})],
            "max_depth": MAX_CALENDAR_DEPTH, "calendar_version": flat.calendar_version}


# ============================================================================================== writes: calendars
def _entity(db: Session, company_id: int, entity_id: Any) -> Optional[LegalEntity]:
    if entity_id in (None, ""):
        return None
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id,
                                          LegalEntity.id == int(entity_id)).first()
    if entity is None:
        raise not_found("Legal entity")
    return entity


def create_calendar(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    cid = principal.company_id
    entity = _entity(db, cid, data.get("legal_entity_id"))
    entity_id = entity.id if entity else None
    if not can_act(db, principal, None, entity_id=entity_id):
        raise forbidden("Only company-wide holiday administrators can create tenant-wide calendars."
                        if entity_id is None else "You cannot manage calendars of this legal entity.")
    name = _clean_name(data.get("name"))
    code = _clean_code(data.get("code") or code_from_name(name))
    if db.query(HolidayCalendar.id).filter(HolidayCalendar.company_id == cid, HolidayCalendar.code == code).first():
        raise conflict("Another holiday calendar already uses this code.", "ORG_CODE_TAKEN")
    country = _country(data.get("country_code")) or (entity.country_code if entity else None)
    observance = data.get("employee_observance") or "NONE"
    if observance not in EMPLOYEE_OBSERVANCE:
        raise bad_request("Employee observance must be NONE, NEXT_WORKING_DAY or OBSERVED_IF_OFF.", "INVALID_OBSERVANCE")
    description = (data.get("description") or "").strip() or None
    if description and len(description) > 2000:
        raise bad_request("The description is at most 2000 characters.", "INVALID_DESCRIPTION")
    row = HolidayCalendar(company_id=cid, code=code, name=name, name_i18n=dict(data.get("name_i18n") or {}),
                          description=description, legal_entity_id=entity_id, country_code=country,
                          subdivision_code=_subdivision(data.get("subdivision_code"), country),
                          weekend_days=_weekend(data.get("weekend_days")), employee_observance=observance,
                          status="ACTIVE", row_version=1)
    db.add(row)
    _flush(db)
    _audit(db, principal, "SAVE", f"Created holiday calendar {row.code} ({row.name})", row,
           {"after": {"code": code, "name": name, "legal_entity_id": entity_id, "country_code": country}})
    if data.get("includes"):
        set_includes(db, principal, row.id, list(data["includes"]), reason=data.get("reason"), _row=row)
    return serialize(db, principal, row, detail=True)


def update_calendar(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any], *,
                    expected_row_version: Optional[int] = None) -> dict:
    row = _get_managed(db, principal, calendar_id)
    _check_version(row, expected_row_version)
    cid = principal.company_id
    before = {k: getattr(row, k) for k in ("code", "name", "description", "legal_entity_id", "country_code",
                                           "subdivision_code", "weekend_days", "employee_observance", "status")}
    weekend_before = effective_weekend(db, row)
    if "code" in data and data["code"] and data["code"] != row.code:
        code = _clean_code(data["code"])
        if usage_reason(db, cid, row.id) or db.query(Holiday.id).filter(Holiday.company_id == cid,
                                                                          Holiday.calendar_id == row.id).first():
            raise conflict("The code of a calendar in use cannot change.", "CODE_IMMUTABLE")
        if db.query(HolidayCalendar.id).filter(HolidayCalendar.company_id == cid, HolidayCalendar.code == code,
                                               HolidayCalendar.id != row.id).first():
            raise conflict("Another holiday calendar already uses this code.", "ORG_CODE_TAKEN")
        row.code = code
    if "name" in data and data["name"] is not None:
        row.name = _clean_name(data["name"])
    if "name_i18n" in data and data["name_i18n"] is not None:
        row.name_i18n = dict(data["name_i18n"])
    if "description" in data:
        description = (data["description"] or "").strip() or None
        if description and len(description) > 2000:
            raise bad_request("The description is at most 2000 characters.", "INVALID_DESCRIPTION")
        row.description = description
    if "country_code" in data:
        row.country_code = _country(data["country_code"])
    if "subdivision_code" in data or "country_code" in data:
        row.subdivision_code = _subdivision(data.get("subdivision_code", row.subdivision_code), row.country_code)
    if "weekend_days" in data:
        row.weekend_days = _weekend(data["weekend_days"])
    if "employee_observance" in data and data["employee_observance"]:
        if data["employee_observance"] not in EMPLOYEE_OBSERVANCE:
            raise bad_request("Employee observance must be NONE, NEXT_WORKING_DAY or OBSERVED_IF_OFF.",
                              "INVALID_OBSERVANCE")
        row.employee_observance = data["employee_observance"]
    if "legal_entity_id" in data and data["legal_entity_id"] != row.legal_entity_id:
        entity = _entity(db, cid, data["legal_entity_id"])
        new_owner = entity.id if entity else None
        if not can_act(db, principal, None, entity_id=new_owner):
            raise forbidden("You cannot move the calendar to that owner.")
        _check_owner_move(db, row, new_owner)
        row.legal_entity_id = new_owner
    if "status" in data and data["status"] and data["status"] != row.status:
        if data["status"] not in ("ACTIVE", "INACTIVE"):
            raise bad_request("Status must be ACTIVE or INACTIVE.", "ORG_STATUS_INVALID")
        if data["status"] == "INACTIVE":
            where = usage_reason(db, cid, row.id)
            if where:
                raise conflict(f"Holiday calendar {row.code} is still used by {where}.", "ORG_DIMENSION_IN_USE")
        row.status = data["status"]
    after = {k: getattr(row, k) for k in before}
    changed = sorted(k for k in after if after[k] != before[k])
    if changed:
        _bump(row)
        _flush(db)
        _audit(db, principal, "SAVE", f"Updated holiday calendar {row.code}: {', '.join(changed)}", row,
               {"before": {k: before[k] for k in changed}, "after": {k: after[k] for k in changed}})
    out = serialize(db, principal, row, detail=True)
    if effective_weekend(db, row) != weekend_before:
        out["weekend_change"] = weekend_changed(db, principal, row)
    return out


def weekend_changed(db: Session, principal: Principal, row: HolidayCalendar) -> dict:
    """LH-REC-13 (02 §4.6 "Change of the effective weekend", V3-013): every DRAFT year from this year on is
    regenerated (observed rows depend on the weekend); for PUBLISHED years the rows the rules would now move are
    listed for an amendment (``holiday:publish``) and never changed here."""
    from app.models.holiday import HolidayCalendarYear
    from app.services import holiday_rollover

    today = calendar_local_today(db, row.company_id, row.id)
    regenerated, review = [], []
    rules = holiday_rollover.rules_of(db, row.company_id, row.id)
    for record in (db.query(HolidayCalendarYear)
                   .filter(HolidayCalendarYear.company_id == row.company_id, HolidayCalendarYear.calendar_id == row.id,
                           HolidayCalendarYear.year >= today.year)
                   .order_by(HolidayCalendarYear.year)):
        if not holiday_rollover.active_rules_in(rules, record.year):
            continue
        if record.status == "DRAFT":
            result = holiday_rollover.generate(db, row.company_id, row.id, record.year, principal)
            regenerated.append({"year": record.year, "counts": result["counts"]})
        elif record.status == "PUBLISHED":
            changes = [c for c in holiday_rollover.published_difference(db, row.company_id, row, record.year)
                       if c["change"] in ("add", "update")]
            if changes:
                review.append({"year": record.year, "changes": changes})
    return {"weekend": effective_weekend(db, row), "regenerated": regenerated, "published_review": review}


def _check_owner_move(db: Session, row: HolidayCalendar, new_owner: Optional[int]) -> None:
    """A calendar moved to an entity must not be used or included outside it (§3.6, CALENDAR_ENTITY_MISMATCH)."""
    if new_owner is None:
        graph = _graph(db, row.company_id)
        owned = [c for _, c in graph.get(row.id, ()) if (db.get(HolidayCalendar, c).legal_entity_id is not None)]
        if owned:
            raise AppError(422, "A tenant-wide calendar can include only tenant-wide calendars.",
                           "CALENDAR_ENTITY_MISMATCH")
        return
    branches, employees = calendar_users(db, row.company_id, row.id, clock.company_today(db, row.company_id))
    outside = db.query(Branch.id).filter(Branch.company_id == row.company_id, Branch.id.in_(sorted(branches) or [-1]),
                                         Branch.legal_entity_id != new_owner).first()
    if outside is None and employees:
        outside = db.query(EmployeeAssignment.id).filter(
            EmployeeAssignment.company_id == row.company_id, EmployeeAssignment.employee_id.in_(sorted(employees)),
            EmployeeAssignment.legal_entity_id != new_owner).first()
    if outside is not None:
        raise AppError(422, "People outside that legal entity use this calendar.", "CALENDAR_ENTITY_MISMATCH")
    graph = _graph(db, row.company_id)
    for includer in dependents(graph, row.id) - {row.id}:
        owner = db.get(HolidayCalendar, includer).legal_entity_id
        if owner != new_owner:
            raise AppError(422, "A calendar of another owner includes this calendar.", "CALENDAR_ENTITY_MISMATCH")


def entity_compatible(calendar: HolidayCalendar, entity_id: Optional[int]) -> bool:
    """A link, assignment or override may point to a tenant-wide calendar or one of the same entity (§3.6)."""
    return calendar.legal_entity_id is None or calendar.legal_entity_id == entity_id


# ============================================================================================== writes: includes
def set_includes(db: Session, principal: Principal, calendar_id: int, includes: Sequence[Mapping[str, Any]], *,
                 reason: Optional[str] = None, expected_row_version: Optional[int] = None,
                 _row: Optional[HolidayCalendar] = None) -> dict:
    """PUT /{id}/includes (§3.3): replace the include list (ordered, priority unique, 0 = the legacy parent)."""
    row = _row or _get_managed(db, principal, calendar_id)
    _check_version(row, expected_row_version)
    cid = principal.company_id
    wanted: List[Tuple[int, int]] = []
    seen_cals: Set[int] = set()
    seen_prio: Set[int] = set()
    flattener = Flattener(db, cid)
    current = {c for _, c in flattener.includes.get(row.id, ())}
    for item in includes:
        try:
            included = int(item["included_calendar_id"])
            priority = int(item.get("priority", 0) if item.get("priority") is not None else len(wanted) + 1)
        except (KeyError, TypeError, ValueError):
            raise bad_request("Each include needs included_calendar_id and priority.", "INVALID_INCLUDES") from None
        if included in seen_cals:
            raise bad_request("A calendar is included more than once.", "INVALID_INCLUDES")
        if priority < 0 or priority in seen_prio:
            raise bad_request("Include priorities must be distinct numbers from 0.", "INVALID_INCLUDES")
        if priority == 0 and included != row.parent_calendar_id:
            raise bad_request("Priority 0 is kept for the legacy parent calendar; use 1 or more.", "INVALID_INCLUDES")
        target = db.query(HolidayCalendar).filter(HolidayCalendar.company_id == cid,
                                                  HolidayCalendar.id == included).first()
        if target is None:
            raise not_found("Holiday calendar")
        if target.status != "ACTIVE" and included not in current:
            raise bad_request(f"Holiday calendar {target.code} is inactive.", "ORG_DIMENSION_INACTIVE")
        if not entity_compatible(target, row.legal_entity_id):
            raise AppError(422, f"{row.code} cannot include {target.code}: "
                                + ("a tenant-wide calendar includes only tenant-wide calendars."
                                   if row.legal_entity_id is None else "it belongs to another legal entity."),
                           "CALENDAR_ENTITY_MISMATCH")
        seen_cals.add(included)
        seen_prio.add(priority)
        wanted.append((priority, included))
    new_graph = {k: list(v) for k, v in flattener.includes.items()}
    new_graph[row.id] = sorted(wanted)
    names = {c.id: c.code for c in db.query(HolidayCalendar).filter(HolidayCalendar.company_id == cid)}
    try:
        check_graph(new_graph, row.id, names)
    except CalendarGraphError as exc:
        raise AppError(400, exc.message, exc.code, extra={"details": {"path": [names.get(c, c) for c in exc.path]}})
    before = sorted(flattener.includes.get(row.id, ()))
    if before == sorted(wanted):
        return lineage(db, principal, row.id)
    changes, lost = _impact(db, flattener, row, new_includes=new_graph)
    classification = check_structure_change(db, principal, row, changes, lost, reason=reason)
    db.query(HolidayCalendarInclude).filter(HolidayCalendarInclude.company_id == cid,
                                            HolidayCalendarInclude.calendar_id == row.id).delete(
        synchronize_session=False)
    _flush(db)
    for priority, included in sorted(wanted):
        db.add(HolidayCalendarInclude(company_id=cid, calendar_id=row.id, included_calendar_id=included,
                                      priority=priority))
    if row.parent_calendar_id is not None and (0, row.parent_calendar_id) not in wanted:
        row.parent_calendar_id = None  # the legacy parent follows its mirror (§3.3 rule 4)
    _bump(row)
    _flush(db)
    recount = _after_change(db, principal, row, changes, row.id,
                            {"calendar_id": row.id, "includes": sorted(wanted)})
    _audit(db, principal, "CALENDAR_INCLUDES_SAVED", f"Includes of {row.code}: "
           + (", ".join(f"{names.get(c, c)} ({p})" for p, c in sorted(wanted)) or "none"), row,
           {"before": [{"included_calendar_id": c, "priority": p} for p, c in before],
            "after": [{"included_calendar_id": c, "priority": p} for p, c in sorted(wanted)],
            "reason": reason, **classification})
    out = lineage(db, principal, row.id)
    out["recount"] = recount
    out["affected_dates"] = classification["affected"]
    return out


# ============================================================================================== writes: exclusions
def add_exclusion(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]) -> dict:
    """POST /{id}/exclusions (§3.4): refuse one inherited occurrence (or an inherited rule from a year)."""
    row = _get_managed(db, principal, calendar_id)
    cid = principal.company_id
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise bad_request("Give a reason for the exclusion.", "REASON_REQUIRED")
    if len(reason) > 500:
        raise bad_request("The reason is at most 500 characters.", "REASON_TOO_LONG")
    holiday_id, rule_id = data.get("holiday_id"), data.get("holiday_rule_id")
    if (holiday_id is None) == (rule_id is None):
        raise bad_request("Exclude either one holiday or one rule.", "INVALID_EXCLUSION")
    from_year = data.get("effective_from_year")
    if from_year is not None and holiday_id is not None:
        raise bad_request("A from-year applies to rule exclusions only.", "INVALID_EXCLUSION")
    flattener = Flattener(db, cid)
    full = flattener.flatten(row.id)
    if holiday_id is not None:
        holiday = db.query(Holiday).filter(Holiday.company_id == cid, Holiday.id == int(holiday_id)).first()
        inherited = holiday is not None and any(f.row.id == holiday.id and f.inherited for f in full.rows)
        if not inherited:
            already = holiday is not None and any(f.row.id == holiday.id for f, _ in full.excluded)
            if already:
                raise conflict("This holiday is already excluded.", "ALREADY_EXCLUDED")
            raise AppError(422, "This holiday is not inherited by the calendar; cancel it on its own calendar "
                                "instead.", "UNKNOWN_REFERENCE")
        new = Exclusion(-1, row.id, holiday_id=holiday.id)
    else:
        # Rule exclusions need holiday_rules (lh_m1_holiday_rules_years); until then no inherited rule exists.
        rule_rows = [f for f in full.rows if f.inherited and f.row.rule_id == int(rule_id)]
        if not rule_rows:
            raise AppError(422, "This rule is not inherited by the calendar.", "UNKNOWN_REFERENCE")
        if from_year is not None:
            limit = calendar_local_today(db, cid, row.id).year + 2
            if not 2000 <= int(from_year) <= limit:
                raise AppError(422, f"The first excluded year must be 2000-{limit}.", "FUTURE_LIMIT")
        new = Exclusion(-1, row.id, rule_id=int(rule_id), effective_from_year=from_year)
    new_exclusions = {k: list(v) for k, v in flattener.exclusions.items()}
    new_exclusions.setdefault(row.id, []).append(new)
    changes, lost = _impact(db, flattener, row, new_exclusions=new_exclusions)
    classification = check_structure_change(db, principal, row, changes, lost, reason=reason)
    exclusion = HolidayCalendarExclusion(company_id=cid, calendar_id=row.id, holiday_id=new.holiday_id,
                                         holiday_rule_id=new.rule_id, effective_from_year=from_year, reason=reason,
                                         created_by_user_id=principal.user_id)
    db.add(exclusion)
    _bump(row)
    _flush(db)
    recount = _after_change(db, principal, row, changes, exclusion.id, serialize_exclusion(db, exclusion))
    _audit(db, principal, "CALENDAR_EXCLUSION_ADDED", f"{row.code} excludes "
           + (f"holiday #{new.holiday_id}" if new.holiday_id else f"rule #{new.rule_id}"), row,
           {"after": serialize_exclusion(db, exclusion), **classification})
    return {**serialize_exclusion(db, exclusion), "recount": recount, "affected_dates": classification["affected"]}


def remove_exclusion(db: Session, principal: Principal, calendar_id: int, exclusion_id: int,
                     reason: Optional[str] = None) -> dict:
    """DELETE /{id}/exclusions/{eid}: the excluded item is inherited again (its dates are additions, §3.4)."""
    row = _get_managed(db, principal, calendar_id)
    cid = principal.company_id
    exclusion = db.query(HolidayCalendarExclusion).filter(HolidayCalendarExclusion.company_id == cid,
                                                          HolidayCalendarExclusion.calendar_id == row.id,
                                                          HolidayCalendarExclusion.id == int(exclusion_id)).first()
    if exclusion is None:
        raise not_found("Exclusion")
    flattener = Flattener(db, cid)
    new_exclusions = {k: [e for e in v if e.id != exclusion.id] for k, v in flattener.exclusions.items()}
    changes, lost = _impact(db, flattener, row, new_exclusions=new_exclusions)
    classification = check_structure_change(db, principal, row, changes, lost, reason=reason or exclusion.reason)
    before = serialize_exclusion(db, exclusion)
    db.delete(exclusion)
    _bump(row)
    _flush(db)
    recount = _after_change(db, principal, row, changes, row.id, {"removed_exclusion": before["id"]})
    _audit(db, principal, "CALENDAR_EXCLUSION_REMOVED", f"{row.code} inherits "
           + (f"holiday #{before['holiday_id']}" if before["holiday_id"] else f"rule #{before['holiday_rule_id']}")
           + " again", row, {"before": before, "reason": reason, **classification})
    return {"removed": before["id"], "recount": recount, "affected_dates": classification["affected"]}


# ============================================================================================== writes: own occurrences
# ============================================================================================== years and rules
# The year, rule and amendment services live in ``holiday_years`` / ``holiday_rollover`` (DECISIONS IG-LH-16); the
# names below are the entry points WI-1.8 gives this module.
def _working_day_rows(db: Session, company_id: int, calendar_id: int, date_from: date, date_to: date):
    from app.models.holiday import CalendarWorkingDay

    cals = sorted(reachable(_graph(db, company_id), calendar_id))
    if not cals:
        return []
    hidden = draft_years(db, company_id, cals)
    rows = db.query(CalendarWorkingDay).filter(
        CalendarWorkingDay.company_id == company_id, CalendarWorkingDay.calendar_id.in_(cals),
        CalendarWorkingDay.date >= date_from, CalendarWorkingDay.date <= date_to).all()
    return [w for w in rows if (w.calendar_id, w.date.year) not in hidden]


def working_days_reaching(db: Session, company_id: int, calendar_id: int, date_from: date,
                          date_to: date) -> List[Tuple[date, int]]:
    """Declared working days of the calendar and every calendar it includes (exclusions never apply, 01 §5.5), in
    years that count (not DRAFT): ``[(date, owning calendar)]``."""
    return sorted((w.date, w.calendar_id) for w in _working_day_rows(db, company_id, calendar_id, date_from, date_to))


def declared_working_days(db: Session, company_id: int, calendar_id: int, days: Iterable[date]) -> Dict[date, str]:
    """``{date: portion}`` of declared working days reaching ``calendar_id`` on ``days`` (day facts, SPINE §7 step 4);
    two different half-day declarations on one date make it a full working day."""
    wanted = set(days)
    if not wanted:
        return {}
    out: Dict[date, str] = {}
    for w in _working_day_rows(db, company_id, calendar_id, min(wanted), max(wanted)):
        if w.date in wanted:
            previous = out.get(w.date)
            out[w.date] = w.portion if previous in (None, w.portion) else "FULL"
    return out


def add_occurrence(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]):
    from app.services import holiday_years

    return holiday_years.add_occurrence(db, principal, calendar_id, data)


def create_rule(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]) -> dict:
    from app.services import holiday_years

    return holiday_years.create_rule(db, principal, calendar_id, data)


def update_rule(db: Session, principal: Principal, rule_id: int, data: Mapping[str, Any], *,
                expected_row_version: Optional[int] = None) -> dict:
    from app.services import holiday_years

    return holiday_years.update_rule(db, principal, rule_id, data, expected_row_version=expected_row_version)


def deactivate_rule(db: Session, principal: Principal, rule_id: int, *, apply_to_draft_years: bool = True,
                    reason: Optional[str] = None) -> dict:
    from app.services import holiday_years

    return holiday_years.deactivate_rule(db, principal, rule_id, reason=reason,
                                         apply_to_draft_years=apply_to_draft_years)


def preview_year(db: Session, company_id: int, calendar_id: int, year: int, operations=None, *, principal=None):
    from app.services import holiday_years

    return holiday_years.preview_year(db, company_id, calendar_id, year, operations, principal=principal)


def publish_year(db: Session, principal: Principal, calendar_id: int, year: int, *, dry_run: bool,
                 acknowledged_warnings: Sequence[str] = (), reason: Optional[str] = None, content_hash=None):
    from app.services import holiday_years

    return holiday_years.publish_year(db, principal, calendar_id, year, dry_run=dry_run,
                                      acknowledged_warnings=acknowledged_warnings, reason=reason,
                                      content_hash_seen=content_hash)


def amend_year(db: Session, principal: Principal, calendar_id: int, year: int, operations, *, reason: Optional[str],
               dry_run: bool = False, acknowledged_warnings: Sequence[str] = ()):
    from app.services import holiday_years

    return holiday_years.amend_year(db, principal, calendar_id, year, operations, reason=reason, dry_run=dry_run,
                                    acknowledged_warnings=acknowledged_warnings)


def confirm_occurrence(db: Session, principal: Principal, holiday_id: int, *, date: Optional[Any] = None,
                       reason: Optional[str] = None, expected_row_version: Optional[int] = None):
    from app.services import holiday_years

    return holiday_years.confirm_occurrence(db, principal, holiday_id, date_=date, reason=reason,
                                            expected_row_version=expected_row_version)


def cancel_occurrence(db: Session, principal: Principal, holiday_id: int, *, reason: Optional[str],
                      expected_row_version: Optional[int] = None):
    from app.services import holiday_years

    return holiday_years.cancel_occurrence(db, principal, holiday_id, reason=reason,
                                           expected_row_version=expected_row_version)


def move_occurrence(db: Session, principal: Principal, holiday_id: int, *, to_date: Any, reason: Optional[str],
                    expected_row_version: Optional[int] = None):
    from app.services import holiday_years

    return holiday_years.move_occurrence(db, principal, holiday_id, to_date=to_date, reason=reason,
                                         expected_row_version=expected_row_version)


def backdated_closure(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]):
    """An emergency / backdated closure (01 §5.8; LH-HOL-24): dry run or commit, see ``holiday_closures``."""
    from app.services import holiday_closures

    return holiday_closures.backdated_closure(db, principal, calendar_id, data)
