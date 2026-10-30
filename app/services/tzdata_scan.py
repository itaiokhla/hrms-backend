"""tz database impact scan (EC AS-TZ-03; 02-system-design §7.4 runbook TZDATA-1; job ``tzdata.impact_scan``, 11 §8.2).

Shift instants are computed on read with pytz, so a tz database release that changes a zone's rules silently moves
every published shift in that zone. The daily job compares the installed rules with the company's snapshot
(``tz_zone_snapshots``) of every zone it uses — companies, legal entities, branches, sites, devices, placements and
shift anchor zones — over the next 24 months. For each changed zone it:

1. finds the published entries whose start or end instant now falls under another UTC offset;
2. writes one SYSTEM ``DIRECT`` publication re-versioning them (change rows with reason ``TZ_RULE_CHANGE``,
   ``initiated_by`` SYSTEM and notice hours); through the publication's change log the evaluation re-runs (OPEN dates
   recomputed, locked dates through retro) and leave on those dates is recounted;
3. enqueues ``attendance.recompute_scope`` from the first changed instant when that is already past (a late release);
4. tells the planners (the affected employees get ``roster.changed`` from the publication);
5. re-runs working-time validation over the published schedule (``roster.compliance_rescan``).

A zone seen for the first time is only recorded; a rerun after the snapshot is updated finds nothing. No entry is
withdrawn.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pytz
from sqlalchemy.orm import Session

from app.core import clock
from app.models.rosters import RosterEntry, TzZoneSnapshot

WINDOW_DAYS = 730  # 24 months (TZDATA-1)
REASON = "TZ_RULE_CHANGE"
JOB = "tzdata.impact_scan"
Rules = Dict[str, Any]  # {"initial": offset minutes at the window start, "transitions": [[utc ISO, minutes], ...]}


def tzdata_version() -> str:
    return pytz.OLSON_VERSION


def installed_rules(zone: str, start: datetime, end: datetime) -> Rules:
    """The installed tz database's UTC offset at ``start`` and every change of it in ``(start, end]``."""
    tz = pytz.timezone(zone)
    initial = int(start.astimezone(tz).utcoffset().total_seconds() // 60)
    lo, hi = start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)
    changes = []
    for moment, info in zip(getattr(tz, "_utc_transition_times", None) or (),
                            getattr(tz, "_transition_info", None) or ()):
        if lo < moment <= hi:
            changes.append([moment.replace(tzinfo=timezone.utc).isoformat(), int(info[0].total_seconds() // 60)])
    return {"initial": initial, "transitions": changes}


def offset_at(rules: Rules, instant: datetime) -> int:
    moments = [datetime.fromisoformat(t) for t, _ in rules["transitions"]]
    i = bisect_right(moments, instant)
    return rules["initial"] if i == 0 else rules["transitions"][i - 1][1]


def first_difference(old: Rules, new: Rules, start: datetime, end: datetime) -> Optional[datetime]:
    """The first instant in ``[start, end]`` at which the two rule sets give different offsets (both are step
    functions, so comparing at every step of either is enough)."""
    points = {start} | {datetime.fromisoformat(t) for rules in (old, new) for t, _ in rules["transitions"]}
    for point in sorted(p for p in points if start <= p <= end):
        if offset_at(old, point) != offset_at(new, point):
            return point
    return None


def zones_in_use(db: Session, company_id: int) -> List[str]:
    from app.models.attendance import AttendanceDevice, AttendanceSite
    from app.models.company import Company
    from app.models.org import Branch, EmployeeAssignment, LegalEntity
    from app.models.shifts import ShiftVersion

    found = set()
    company = db.get(Company, company_id)
    if company is not None and company.timezone:
        found.add(company.timezone)
    for model, column in ((LegalEntity, LegalEntity.timezone), (Branch, Branch.timezone),
                          (AttendanceSite, AttendanceSite.timezone), (AttendanceDevice, AttendanceDevice.timezone),
                          (EmployeeAssignment, EmployeeAssignment.work_timezone),
                          (ShiftVersion, ShiftVersion.anchor_timezone)):
        found |= {z for (z,) in db.query(column).filter(model.company_id == company_id, column.isnot(None)).distinct()}
    return sorted(z for z in found if z in pytz.all_timezones_set)


def impact_scan(db: Session, company_id: int, local_date: date, *, now: Optional[datetime] = None,
                rules_of: Callable[[str, datetime, datetime], Rules] = installed_rules) -> dict:
    """The job body (see the module doc). ``rules_of`` is the installed tz database; tests pass another."""
    now = now or clock.utcnow()
    end = now + timedelta(days=WINDOW_DAYS)
    version = tzdata_version()
    snapshots = {s.zone: s for s in db.query(TzZoneSnapshot).filter(TzZoneSnapshot.company_id == company_id)}
    changed: Dict[str, Tuple[Rules, Rules, datetime]] = {}
    recorded = []
    for zone in zones_in_use(db, company_id):
        current = rules_of(zone, now, end)
        snap = snapshots.get(zone)
        if snap is None:
            db.add(TzZoneSnapshot(company_id=company_id, zone=zone, tzdata_version=version, window_from=now,
                                  window_to=end, initial_offset_minutes=current["initial"],
                                  transitions=current["transitions"]))
            recorded.append(zone)
            continue
        old = {"initial": snap.initial_offset_minutes, "transitions": list(snap.transitions or [])}
        first = first_difference(old, current, max(now, snap.window_from), min(end, snap.window_to))
        if first is not None:
            changed[zone] = (old, current, first)
        snap.tzdata_version, snap.window_from, snap.window_to = version, now, end
        snap.initial_offset_minutes, snap.transitions = current["initial"], current["transitions"]
    db.flush()
    result: Dict[str, Any] = {"tzdata_version": version, "recorded": recorded, "changed": sorted(changed),
                              "publication_id": None, "entries": 0, "recompute_jobs": 0}
    if not changed:
        return result
    affected = _affected_entries(db, company_id, changed)
    if affected:
        result["publication_id"] = _record_change(db, company_id, affected, version)
        result["entries"] = len(affected)
        _notify(db, company_id, affected)
    result["recompute_jobs"] = _recompute_past(db, company_id, changed, now, version)
    if affected:
        from app.services import roster_consents

        result["rescan"] = roster_consents.compliance_rescan(db, company_id, local_date)
    return result


def _affected_entries(db: Session, company_id: int, changed: Mapping[str, Tuple[Rules, Rules, datetime]]
                      ) -> List[Tuple[RosterEntry, str]]:
    """Published WORK entries whose start or end instant falls under another offset in a changed zone."""
    from app.services import roster_validation
    from app.services.shift_rosters import ENTRY_KEYS

    since = min(first for _, _, first in changed.values()).date() - timedelta(days=1)
    entries = (db.query(RosterEntry)
               .filter(RosterEntry.company_id == company_id, RosterEntry.withdrawn_at.is_(None),
                       RosterEntry.kind == "WORK", RosterEntry.shift_date >= since)
               .order_by(RosterEntry.employee_id, RosterEntry.shift_date, RosterEntry.entry_seq).all())
    if not entries:
        return []
    zones = roster_validation.zones_for(db, company_id, entries)
    versions = roster_validation._Versions(db, company_id, {e.shift_id for e in entries if e.shift_id})
    out = []
    for entry in entries:
        work_zone = zones.get((entry.employee_id, entry.shift_date), "UTC")
        values = {k: getattr(entry, k) for k in ENTRY_KEYS if k != "version"}
        version_row = versions.row(entry.shift_id, entry.shift_version_id, entry.shift_date) if entry.shift_id else None
        zone = getattr(version_row, "anchor_timezone", None) or work_zone
        if zone not in changed:
            continue
        instance = roster_validation._instance(versions, values, work_zone)
        if instance is None:
            continue
        old, new, _ = changed[zone]
        if any(offset_at(old, t) != offset_at(new, t) for t in (instance.start_at, instance.end_at)):
            out.append((entry, zone))
    return out


def _record_change(db: Session, company_id: int, affected: Sequence[Tuple[RosterEntry, str]], version: str) -> int:
    from app.services import shift_rosters
    from app.services.audit_log_service import AuditLogService

    cells = []
    for entry, _ in affected:
        cell = shift_rosters._published_cell(entry)
        cell.changed = True
        cells.append(cell)
    days = [c.shift_date for c in cells]
    zones = sorted({zone for _, zone in affected})
    publication = shift_rosters._write(
        db, None, company_id, kind="DIRECT", period=None, candidate=cells, changed=cells,
        scope={"employee_ids": sorted({c.employee_id for c in cells}), "from": min(days).isoformat(),
               "to": max(days).isoformat(), "zones": zones, "tzdata_version": version},
        outcome=shift_rosters.ValidationOutcome(), acknowledged=[], reasons={}, exemptions={},
        initiated_by="SYSTEM", reason_code=REASON)
    AuditLogService.log(db, None, shift_rosters.MODULE, "ROSTER_PUBLISHED",
                        f"Time-zone rules changed ({', '.join(zones)}): {len(cells)} published shift(s) re-versioned",
                        "roster_publication", publication.id, company_id=company_id, actor_type="system",
                        details={"kind": "DIRECT", "reason_code": REASON, "zones": zones, "tzdata_version": version,
                                 "updated": publication.entries_updated})
    return publication.id


def _notify(db: Session, company_id: int, affected: Sequence[Tuple[RosterEntry, str]]) -> None:
    """Planners hear why; the employees get ``roster.changed`` from the publication itself."""
    from app.payroll.services.holders import notify_users
    from app.services.shift_rosters import _planner_user_ids

    employees = sorted({e.employee_id for e, _ in affected})
    zones = ", ".join(sorted({zone for _, zone in affected}))
    first = min(e.shift_date for e, _ in affected)
    notify_users(db, _planner_user_ids(db, company_id, employees), "Time-zone rules changed",
                 f"The clock rules for {zones} changed. {len(affected)} published shift(s) from {first:%d %b} now "
                 "fall at a different time; they are recorded as a schedule change. Check notice and rest.",
                 "/rosters", "warning")


def _recompute_past(db: Session, company_id: int, changed: Mapping[str, Tuple[Rules, Rules, datetime]],
                    now: datetime, version: str) -> int:
    """A release that arrives after the rule change: re-evaluate attendance from the first changed instant."""
    from app.payroll.services import jobs

    count = 0
    for zone, (_, _, first) in sorted(changed.items()):
        if first > now:
            continue
        jobs.enqueue(db, company_id=company_id, type="attendance.recompute_scope",
                     payload={"trigger": "MANUAL_RECOMPUTE", "reason": REASON, "zone": zone,
                              "date_from": (first.date() - timedelta(days=1)).isoformat()},
                     idempotency_key=f"{JOB}:{version}:{zone}")
        count += 1
    return count
