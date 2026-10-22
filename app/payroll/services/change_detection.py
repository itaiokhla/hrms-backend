"""Change detection: a source record changed after a run locked its inputs (3.4 §16, 3.5 §4.1; EC-RUN-09).

Every stored snapshot keeps ``source_hashes`` ({"<source type>:<id>": sha256 of the record's canonical
content}, ``snapshot.build``). A change is found by rebuilding the employee's source hashes from the
live tables without writing anything (``snapshot.build(probe=True)``) and comparing:

* ``diff_sources(old, new)`` — pure: ADDED, UPDATED and REMOVED source keys;
* ``changed_sources(db, run, run_employee)`` — the changes of one run employee since its snapshot;
* ``detect_changes(db, run, full=False)`` — ``{employee_id: [changes]}``. Without ``full`` only employees
  named by ``source_change_log`` rows written after their snapshot (attendance, leave, holidays, LOP,
  suspensions, on-duty, optional holidays, overtime, tasks, wage inputs, placements; employee-less rows
  such as holidays reach everyone) are rebuilt; ``full`` rebuilds everyone (claims, loans, bank accounts
  and YTD are not in the log). A country pack whose version or fingerprint differs from the frozen one
  changes every employee (EC-STA-03);
* ``match_run`` / ``match_source_changes`` mark the affected runs stale through ``runs.mark_stale`` (the one
  stale writer) with the changed sources as the reason: submit and finalize stay blocked until the
  processor recalculates those employees or defers the change to retro (EC-ATT-05, EC-RUN-09).
  Approved compensation changes, settings and policy activations mark runs stale at once through the hooks
  ``runs.install_hooks`` wires (EC-CMP-13, EC-CFG-14);
* the periodic job ``payroll.match_source_changes`` runs ``match_source_changes`` for a tenant once per
  hour while runs with locked inputs exist (``schedule_matching``; ``snapshot.lock_inputs`` schedules it).

Services take (db, ...) and never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from app.core import clock
from app.models.jobs import Job
from app.models.org import LegalEntity
from app.models.pay_runs import PayPeriod, PayrollInputSnapshot, PayrollRun, PayrollRunEmployee
from app.payroll.services import jobs, snapshot, time_inputs

MATCH_JOB = "payroll.match_source_changes"
MATCH_INTERVAL = timedelta(hours=1)
MAX_REASON_ITEMS = 50
ADDED, UPDATED, REMOVED, INVALID = "ADDED", "UPDATED", "REMOVED", "INVALID"


@dataclass(frozen=True)
class SourceChange:
    """One source of a frozen snapshot that is no longer what the snapshot holds."""

    source: str
    change: str  # ADDED | UPDATED | REMOVED | INVALID

    @property
    def source_type(self) -> str:
        return self.source.split(":", 1)[0]

    def to_json(self) -> dict:
        return {"source": self.source, "type": self.source_type, "change": self.change}


def diff_sources(old: Mapping[str, str], new: Mapping[str, str]) -> List[SourceChange]:
    """Source keys added, updated or removed between two ``source_hashes`` maps (sorted, deterministic)."""
    changes = [SourceChange(k, UPDATED) for k in sorted(set(old) & set(new)) if old[k] != new[k]]
    changes += [SourceChange(k, ADDED) for k in sorted(set(new) - set(old))]
    changes += [SourceChange(k, REMOVED) for k in sorted(set(old) - set(new))]
    return sorted(changes, key=lambda c: (c.source, c.change))


def _period(db: Session, run: PayrollRun) -> PayPeriod:
    return db.query(PayPeriod).filter(PayPeriod.company_id == run.company_id, PayPeriod.id == run.pay_period_id).one()


def _locked_rows(db: Session, run: PayrollRun) -> List[tuple]:
    """(run employee, its current snapshot) for every included run employee with frozen inputs."""
    rows = (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
                    PayrollRunEmployee.status != "EXCLUDED", PayrollRunEmployee.snapshot_id.isnot(None))
            .order_by(PayrollRunEmployee.id).all())
    if not rows:
        return []
    stored = {s.id: s for s in db.query(PayrollInputSnapshot).filter(
        PayrollInputSnapshot.company_id == run.company_id,
        PayrollInputSnapshot.id.in_([r.snapshot_id for r in rows]))}
    return [(r, stored[r.snapshot_id]) for r in rows if r.snapshot_id in stored]


def changed_sources(db: Session, run: PayrollRun, run_employee: PayrollRunEmployee, *,
                    stored: Optional[PayrollInputSnapshot] = None) -> List[SourceChange]:
    """What changed for one run employee since its latest snapshot (empty without a snapshot)."""
    stored = stored or snapshot.latest_snapshot(db, run_employee)
    if stored is None:
        return []
    try:
        current = snapshot.build(db, run, run_employee, probe=True).source_hashes
    except snapshot.SnapshotBuildError:
        return [SourceChange("snapshot", INVALID)]
    return diff_sources(stored.source_hashes or {}, current)


def _log_candidates(db: Session, run: PayrollRun, period: PayPeriod, locked: Sequence[tuple]) -> set:
    """Run employee ids named by source_change_log rows written after their snapshot."""
    if not locked:
        return set()
    since = min(s.created_at for _, s in locked)
    start = min(period.period_start, period.attendance_start)
    by_employee: Dict[int, List[tuple]] = {}
    for row, stored in locked:
        by_employee.setdefault(row.employee_id, []).append((row, stored))
    found = set()
    for change in time_inputs.changes_since(db, run.company_id, since, employee_ids=sorted(by_employee),
                                            start=start, end=period.period_end):
        targets = by_employee.values() if change.employee_id is None else [by_employee.get(change.employee_id, [])]
        for items in targets:
            for row, stored in items:
                if change.changed_at > stored.created_at:
                    found.add(row.id)
    return found


def pack_change(db: Session, run: PayrollRun, period: PayPeriod, locked: Sequence[tuple]) -> Optional[SourceChange]:
    """EC-STA-03: the pack version (or its rules) differs from the one the snapshots froze."""
    if not locked:
        return None
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == run.company_id,
                                          LegalEntity.id == run.legal_entity_id).one()
    current = snapshot.pack_reference(run, period, entity)
    frozen = {(s.content or {}).get("pack") and (s.content["pack"].get("version"), s.content["pack"].get("fingerprint"))
              for _, s in locked}
    now = current and (current.get("version"), current.get("fingerprint"))
    if frozen == {now}:
        return None
    code = (current or {}).get("code") or next((s.content["pack"]["code"] for _, s in locked
                                                if (s.content or {}).get("pack")), "none")
    return SourceChange(snapshot.source_key("pack", code), UPDATED)


def detect_changes(db: Session, run: PayrollRun, *, full: bool = False) -> Dict[int, List[dict]]:
    """``{employee_id: [changes]}`` for the run's frozen inputs (see the module docstring)."""
    period = _period(db, run)
    locked = _locked_rows(db, run)
    result: Dict[int, List[dict]] = {}
    pack = pack_change(db, run, period, locked)
    candidates = {row.id for row, _ in locked} if full else _log_candidates(db, run, period, locked)
    for row, stored in locked:
        changes: List[SourceChange] = []
        if row.id in candidates:
            changes = changed_sources(db, run, row, stored=stored)
        if pack is not None and pack not in changes:
            changes.append(pack)
        if changes:
            result.setdefault(row.employee_id, []).extend(c.to_json() for c in changes)
    return result


def _reason(changes: Mapping[int, Sequence[Mapping[str, Any]]]) -> str:
    kinds = sorted({c["type"] for items in changes.values() for c in items})
    return (f"Inputs changed after they were locked ({', '.join(kinds)}) for {len(changes)} employee(s); "
            "recalculate them or defer the change to retro.")


def match_run(db: Session, run: PayrollRun, *, full: bool = False) -> dict:
    """Mark ``run`` stale when its frozen inputs changed (a run already stale is left as it is)."""
    from app.payroll.services import runs

    summary = {"run_id": run.id, "marked": False, "changes": {}}
    if run.status not in runs.LOCKED_STATES or run.stale:
        return summary
    changes = detect_changes(db, run, full=full)
    if not changes:
        return summary
    period = _period(db, run)
    compact = [{"employee_id": e, **c} for e, items in sorted(changes.items()) for c in items][:MAX_REASON_ITEMS]
    runs.mark_stale(db, run.company_id, _reason(changes), pay_group_id=run.pay_group_id,
                    employee_ids=sorted(changes), from_date=period.period_start,
                    details={"source": "change_detection", "run_id": run.id, "changes": compact,
                             "employees": len(changes)})
    summary.update(marked=True, changes={str(k): v for k, v in sorted(changes.items())})
    return summary


def _runs_with_locked_inputs(db: Session, company_id: int) -> List[PayrollRun]:
    from app.payroll.services import runs

    return (db.query(PayrollRun)
            .filter(PayrollRun.company_id == company_id, PayrollRun.status.in_(runs.LOCKED_STATES))
            .order_by(PayrollRun.id).all())


def match_source_changes(db: Session, company_id: int, *, full: bool = False) -> dict:
    """Check every run of the tenant with locked, unfinalized inputs; returns what was marked stale."""
    checked, marked = 0, []
    for run in _runs_with_locked_inputs(db, company_id):
        if run.stale:
            continue
        checked += 1
        outcome = match_run(db, run, full=full)
        if outcome["marked"]:
            marked.append({"run_id": run.id, "employees": sorted(int(e) for e in outcome["changes"])})
    return {"checked": checked, "marked": marked}


def next_slot(after: datetime) -> datetime:
    """The next whole hour (UTC) strictly after ``after``."""
    base = after.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return base + MATCH_INTERVAL


def schedule_matching(db: Session, company_id: int, *, after: Optional[datetime] = None) -> Job:
    """Queue the tenant's next change-detection pass (one job per tenant and hour, idempotent)."""
    slot = next_slot(after or clock.utcnow())
    return jobs.enqueue(db, company_id=company_id, type=MATCH_JOB, payload={"slot": slot.isoformat()},
                        idempotency_key=f"match-source-changes:{slot.strftime('%Y%m%dT%H')}", run_at=slot,
                        priority=150)


@jobs.register(MATCH_JOB)
def _match_job(ctx: jobs.JobContext) -> dict:
    outcome = match_source_changes(ctx.db, ctx.company_id)
    if _runs_with_locked_inputs(ctx.db, ctx.company_id):
        schedule_matching(ctx.db, ctx.company_id)
    return {"checked": outcome["checked"], "marked_count": len(outcome["marked"]),
            "marked_run_ids": [m["run_id"] for m in outcome["marked"]]}


__all__ = [
    "ADDED", "INVALID", "MATCH_JOB", "REMOVED", "SourceChange", "UPDATED", "changed_sources", "detect_changes",
    "diff_sources", "match_run", "match_source_changes", "next_slot", "pack_change", "schedule_matching",
]
