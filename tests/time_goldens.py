"""Golden baselines of today's attendance, leave, holiday and payroll-input behaviour (G-01: AS-M0 WI-AS-0.3,
LH-M0 WI-0.3; AS 13 §4.1, LH 13 §4.1).

One dataset — the time-stack seed (scripts/seed_time_three_companies.py) through the in-process client, plus a
punch matrix on a frozen attendance clock — and read-only capture functions, one per golden file. The golden tests
compare a capture with the committed JSON in ``tests/attendance/goldens`` / ``tests/leave/goldens``;
``HRMS_GOLDEN_WRITE=1`` (scripts/as_baseline.py / lh_baseline.py ``--write``) rewrites the files instead.

Determinism: the attendance clock (app.core.clock.utcnow) is frozen at Mon 2026-10-26 09:00 IST for the whole module;
signup-time values that come from the real clock (the founder's hire date) are never captured — only the seeded
cast (employee codes ``AT-``, ``BT-``, ``GT-``) is. Ids are stable because every golden module starts on an empty
database with reset sequences (tests/conftest.py).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytz

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SEED_PASSWORD = "Time-Golden-Seed-1!"  # test database only
IST = pytz.timezone("Asia/Kolkata")
NY = pytz.timezone("America/New_York")
FROZEN_AT = IST.localize(datetime(2026, 10, 26, 9, 0)).astimezone(timezone.utc)
CAST_PREFIXES = ("AT-", "BT-", "GT-")
WRITE = os.environ.get("HRMS_GOLDEN_WRITE") == "1"
YEARS = (2025, 2026)
# Pay periods per tenant: one locked (inputs read with a lock instant, live sessions closed first) and one open.
CLOSED_PERIOD = (date(2026, 10, 1), date(2026, 10, 31), IST.localize(datetime(2026, 10, 26, 8, 0)).astimezone(timezone.utc))
OPEN_PERIOD = (date(2026, 11, 1), date(2026, 11, 30), None)


class FrozenClock:
    def __init__(self, start: datetime):
        self.now = start

    def at(self, tz, day: str, hhmm: str) -> datetime:
        y, m, d = (int(x) for x in day.split("-"))
        h, mi = (int(x) for x in hhmm.split(":"))
        self.now = tz.localize(datetime(y, m, d, h, mi)).astimezone(timezone.utc)
        return self.now


# ------------------------------------------------------------------------------------------------- dataset
@contextmanager
def frozen_attendance_clock() -> Iterator[FrozenClock]:
    import pytest

    from app.core import clock

    fake = FrozenClock(FROZEN_AT)
    patch = pytest.MonkeyPatch()
    patch.setattr(clock, "utcnow", lambda: fake.now)
    try:
        yield fake
    finally:
        patch.undo()


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="http://testserver/api/v1/")


def seed(app) -> dict:
    """The time-stack dataset (goal G-01) on the empty test database; returns the seed store (ids, accounts)."""
    import seed_time_three_companies as time_seed

    from app.core.rate_limit import limiter

    store: dict = {}
    ctx = time_seed.run_seed(lambda: _client(app), store=store, password=SEED_PASSWORD, before_login=limiter.reset,
                             goal="G-01", log=lambda line: None)
    _apply_unpaid_breaks(store)
    store["seed_blocked"] = [b for b in ctx.blocked if not b["step"].startswith("unpaid break")]
    return store


def _apply_unpaid_breaks(store: dict) -> None:
    """Shift unpaid breaks as data, identical before and after BUG-AS-22 made the field writable."""
    import seed_time_three_companies as time_seed
    from sqlalchemy import text

    from app.database.db import system_engine

    with system_engine.begin() as conn:
        for key, spec in time_seed.COMPANIES.items():
            ids = store["companies"][key]["ids"]["shifts"]
            for name, *_rest in spec["shifts"]:
                unpaid = _rest[3]
                conn.execute(text("UPDATE shifts SET unpaid_break_minutes = :m WHERE id = :i"),
                             {"m": unpaid or None, "i": ids[name]})


def api_as(app, store: dict, company: str, person: str):
    """A logged-in client for a seeded account (tests.helpers.ApiClient)."""
    from app.core.rate_limit import limiter
    from tests.helpers import login

    limiter.reset()
    return login(app, store["companies"][company]["accounts"][person]["email"], SEED_PASSWORD)


# Punch matrix (AS 13 §4.1 deliverable 1): self-service punches on the frozen clock.
# company, person, zone, steps [(action, day, HH:MM)]
PUNCH_MATRIX = [
    ("alpha", "priya", IST, [("PUNCH_IN", "2026-10-19", "09:52"), ("LUNCH_IN", "2026-10-19", "13:00"),
                             ("LUNCH_OUT", "2026-10-19", "13:45"), ("PUNCH_OUT", "2026-10-19", "19:01")]),
    ("alpha", "asha", IST, [("PUNCH_IN", "2026-10-19", "09:25"), ("LUNCH_IN", "2026-10-19", "13:00"),
                            ("LUNCH_OUT", "2026-10-19", "14:00"), ("PUNCH_OUT", "2026-10-19", "18:00")]),
    ("alpha", "vikram", IST, [("PUNCH_IN", "2026-10-19", "08:55"), ("PUNCH_OUT", "2026-10-19", "16:30")]),
    ("alpha", "sunita", IST, [("PUNCH_IN", "2026-10-19", "22:05"), ("PUNCH_OUT", "2026-10-20", "06:00")]),
    ("alpha", "imran", IST, [("PUNCH_IN", "2026-10-20", "06:00")]),  # never punched out: auto-closed on lock
    ("alpha", "ravi", IST, [("PUNCH_IN", "2026-10-26", "08:30")]),  # live at the frozen instant
    ("gamma", "carlos", NY, [("PUNCH_IN", "2026-10-19", "21:55"), ("LUNCH_IN", "2026-10-20", "02:00"),
                             ("LUNCH_OUT", "2026-10-20", "02:30"), ("PUNCH_OUT", "2026-10-20", "06:05")]),
]
PATHS = {"PUNCH_IN": "check-in", "PUNCH_OUT": "check-out", "LUNCH_IN": "lunch-in", "LUNCH_OUT": "lunch-out"}


def run_punch_matrix(app, store: dict, fake: FrozenClock) -> None:
    import uuid

    for company in sorted({row[0] for row in PUNCH_MATRIX}):
        founder = api_as(app, store, company, "founder")
        r = founder.patch("/api/v1/attendance/policy", json={"requireFaceForSelfCheckIn": False})
        assert r.status_code == 200, r.text
    for company, person, zone, steps in PUNCH_MATRIX:
        client = api_as(app, store, company, person)
        for action, day, hhmm in steps:
            fake.at(zone, day, hhmm)
            body = {"idempotency_key": uuid.uuid4().hex, "client_sent_at": fake.now.isoformat(),
                    "location_consent": True}
            r = client.post(f"/api/v1/attendance/me/{PATHS[action]}", data={"meta": json.dumps(body)})
            assert r.status_code == 200, f"{person} {action} {day} {hhmm}: {r.text}"
    fake.now = FROZEN_AT


# ------------------------------------------------------------------------------------------------- reading
@contextmanager
def tenant_db(company_id: int):
    """A tenant session that never commits (captures are read-only; lock-time closes are rolled back)."""
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        yield db
    finally:
        db.rollback()
        db.close()


def cast(db, company_id: int):
    from app.models.employee import Employee

    rows = db.query(Employee).filter(Employee.company_id == company_id).order_by(Employee.employee_code).all()
    return [e for e in rows if (e.employee_code or "").startswith(CAST_PREFIXES)]


def jsonable(value: Any) -> Any:
    """Canonical JSON form: dataclasses field by field, ORM rows by id, decimals and dates as strings."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "__table__") and hasattr(value, "id"):
        return {"row": value.__table__.name, "id": value.id}
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(jsonable(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def companies(store: dict) -> Dict[str, int]:
    return {key: c["company_id"] for key, c in sorted(store["companies"].items())}


def _label(employee) -> str:
    return f"{employee.employee_code} {employee.first_name} {employee.last_name}"


# ---------------------------------------------------------------------------------------------- AS captures
def capture_sessions(store: dict) -> dict:
    """Per session: status, raw late / early-exit minutes, canonical hours at the frozen instant, content hash."""
    from app.models.attendance import Attendance
    from app.services import attendance_service

    out: Dict[str, Any] = {}
    for key, cid in companies(store).items():
        with tenant_db(cid) as db:
            people = {e.id: e for e in cast(db, cid)}
            records = (db.query(Attendance).filter(Attendance.company_id == cid,
                                                   Attendance.employee_id.in_(list(people) or [0]))
                       .order_by(Attendance.employee_id, Attendance.date, Attendance.id).all())
            breaks = attendance_service.breaks_by_session(db, [r.id for r in records])
            now = attendance_service._now()
            for r in records:
                hours = attendance_service.hours_for(r, breaks[r.id], now)
                out[f"{key}/{_label(people[r.employee_id])}/{r.date.isoformat()}"] = {
                    "status": r.status, "source": r.source, "late_minutes": r.late_minutes,
                    "early_exit": bool(r.early_exit), "early_exit_minutes": r.early_exit_minutes,
                    "closed_reason": r.closed_reason, "live": attendance_service.is_live(r),
                    "hours": jsonable(hours), "content_hash": attendance_service.content_hash(r, breaks[r.id]),
                }
    return out


def _periods():
    return {"closed": CLOSED_PERIOD, "open": OPEN_PERIOD}


def capture_payroll_inputs(store: dict) -> dict:
    """time_inputs._attendance_inputs and ledger_inputs per employee-date for a locked and an open period (T-3 oracle)."""
    from app.payroll.services import time_inputs

    out: Dict[str, Any] = {}
    for key, cid in companies(store).items():
        for name, (start, end, lock_at) in _periods().items():
            with tenant_db(cid) as db:
                for e in cast(db, cid):
                    attendance = time_inputs._attendance_inputs(db, cid, e.id, start, end, lock_at, 0)
                    days = time_inputs.ledger_inputs(db, cid, e.id, start, end, lock_at=lock_at)
                    out[f"{key}/{name}/{_label(e)}"] = {
                        "attendance": jsonable(attendance),
                        "ledger_digest": digest(days),
                        "ledger": {d.date.isoformat(): _compact_day(d) for d in days if _interesting(d)},
                    }
    return out


def _interesting(day) -> bool:
    return bool(day.leaves or day.holiday or day.attendance or day.manual_lop or day.on_duty or not day.employed)


def _compact_day(day) -> dict:
    return {k: v for k, v in jsonable(day).items() if v not in (None, [], "", False) and k != "date"}


def capture_standard_hours(store: dict) -> dict:
    """Standard hours per employed date (time_inputs.py ledger_inputs), overtime _day_types / _worked, shifts_json."""
    from app.payroll.services import overtime, snapshot, time_inputs

    out: Dict[str, Any] = {"standard_hours": {}, "overtime_day_types": {}, "overtime_worked": {}, "shifts_json": {}}
    start, end = date(2026, 10, 1), date(2026, 11, 30)
    for key, cid in companies(store).items():
        with tenant_db(cid) as db:
            for e in cast(db, cid):
                label = f"{key}/{_label(e)}"
                days = time_inputs.ledger_inputs(db, cid, e.id, start, end)
                values = sorted({jsonable(d.standard_hours) for d in days if d.employed} - {None})
                out["standard_hours"][label] = {"values": values, "digest": digest([d.standard_hours for d in days])}
                types = overtime._day_types(db, cid, e, start, end)
                out["overtime_day_types"][label] = {
                    "counts": {t: sum(1 for v in types.values() if v.day_type == t)
                               for t in sorted({v.day_type for v in types.values()})},
                    "shift_ids": sorted({v.shift_id for v in types.values() if v.shift_id is not None}),
                    "digest": digest({d.isoformat(): [v.day_type, v.shift_id] for d, v in types.items()}),
                }
                out["overtime_worked"][label] = {d.isoformat(): list(v) for d, v in
                                                 sorted(overtime._worked(db, cid, e.id, start, end).items())}
                stub = _ShiftsJsonStub(db, cid, e.id, date(2026, 10, 1), date(2026, 10, 31))
                ledger_days = [{"date": d.date.isoformat(), "employed": d.employed, "weekly_off": d.weekly_off}
                               for d in time_inputs.ledger_inputs(db, cid, e.id, stub.start, stub.end)]
                out["shifts_json"][label] = snapshot._Builder.shifts_json(stub, ledger_days)
    return out


class _ShiftsJsonStub:
    """The attributes snapshot._Builder.shifts_json reads, with the shift-allowance switch on."""

    def __init__(self, db, cid: int, eid: int, start: date, end: date):
        self.db, self.cid, self.eid, self.start, self.end = db, cid, eid, start, end

    def switch_any(self, key: str) -> bool:
        return True


# ---------------------------------------------------------------------------------------------- LH captures
def capture_content_hashes(store: dict) -> dict:
    from app.models.holiday import Holiday
    from app.models.leave import Leave
    from app.payroll.services import time_inputs
    from app.services import holiday_service

    out: Dict[str, Any] = {"leaves": {}, "holidays": {}}
    for key, cid in companies(store).items():
        with tenant_db(cid) as db:
            people = {e.id: e for e in cast(db, cid)}
            for leave in db.query(Leave).filter(Leave.company_id == cid).order_by(Leave.id):
                if leave.employee_id not in people:
                    continue
                days = time_inputs._active_leave_days(db, leave)
                content = time_inputs.leave_content(leave, days)
                out["leaves"][f"{key}/{_label(people[leave.employee_id])}/{leave.leave_type}/{leave.start_date}"] = {
                    "status": leave.status, "number_of_days": jsonable(leave.number_of_days),
                    "days": len(days), "hash": time_inputs.content_hash(content)}
            for h in db.query(Holiday).filter(Holiday.company_id == cid).order_by(Holiday.date, Holiday.id):
                out["holidays"][f"{key}/{h.date.isoformat()}/{h.name}"] = {
                    "type": h.holiday_type, "location": h.location,
                    "hash": time_inputs.content_hash(holiday_service.content(h))}
    return out


def capture_day_facts(store: dict) -> dict:
    """time_inputs.day_facts for every cast employee and every date of 2025 and 2026 (zero-diff oracle)."""
    from app.payroll.services import time_inputs

    out: Dict[str, Any] = {}
    for key, cid in companies(store).items():
        with tenant_db(cid) as db:
            for e in cast(db, cid):
                for year in YEARS:
                    facts = time_inputs.day_facts(db, cid, e.id, date(year, 1, 1), date(year, 12, 31))
                    rows = [[f.date, f.employed, f.branch_id, f.work_week_id, f.weekly_off,
                             f.holiday and [f.holiday.id, f.holiday.type, f.holiday.content_hash]] for f in facts]
                    out[f"{key}/{_label(e)}/{year}"] = {
                        "digest": digest(rows),
                        "employed": sum(1 for f in facts if f.employed),
                        "weekly_offs": sum(1 for f in facts if f.weekly_off),
                        "working": sum(1 for f in facts if f.employed and f.working),
                        "holidays": {f.date.isoformat(): f.holiday.type for f in facts if f.holiday is not None},
                    }
    return out


def capture_ledger_inputs(store: dict) -> dict:
    """Payroll day-ledger inputs for leave / holiday / weekly-off combinations (Nov 2026 – Jan 2027; LH-PAY-30)."""
    from app.payroll.services import time_inputs

    out: Dict[str, Any] = {}
    for key, cid in companies(store).items():
        with tenant_db(cid) as db:
            for e in cast(db, cid):
                days = time_inputs.ledger_inputs(db, cid, e.id, date(2026, 11, 1), date(2027, 1, 31))
                out[f"{key}/{_label(e)}"] = {"digest": digest(days),
                                             "days": {d.date.isoformat(): _compact_day(d) for d in days
                                                      if d.leaves or d.holiday}}
    return out


def capture_balances(store: dict) -> dict:
    from app.services import leave_service

    out: Dict[str, Any] = {}
    for key, cid in companies(store).items():
        with tenant_db(cid) as db:
            for e in cast(db, cid):
                for year in (2025, 2026, 2027):
                    out[f"{key}/{_label(e)}/{year}"] = jsonable(leave_service.balances(db, cid, e.id, year))
    return out


def capture_approver_sets(store: dict) -> dict:
    """User ids that may act (legacy leave_service.can_act) on every pending request, with the capacity."""
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.employee import Employee
    from app.models.leave import Leave
    from app.models.user import User
    from app.services import leave_service

    out: Dict[str, Any] = {}
    for key, cid in companies(store).items():
        with tenant_db(cid) as db:
            company = db.get(Company, cid)
            users = db.query(User).join(Employee, Employee.user_id == User.id).filter(
                Employee.company_id == cid, User.status == "active").order_by(User.id).all()
            principals = [build_principal(db, u, None, company) for u in users]
            people = {e.id: e for e in cast(db, cid)}
            pending = db.query(Leave).filter(Leave.company_id == cid, Leave.status == "pending").order_by(Leave.id)
            for leave in pending:
                requester = db.get(Employee, leave.employee_id)
                acting = {}
                for p in principals:
                    capacity = leave_service.can_act(db, p, leave, requester)
                    if capacity:
                        acting[db.get(User, p.user_id).email] = capacity
                out[f"{key}/{_label(people[leave.employee_id])}/{leave.leave_type}/{leave.start_date}"] = acting
    return out


def capture_attendance_counts(store: dict) -> dict:
    """Live board, 7-day trend and monthly statistics as each tenant's founder sees them (LH BUG-12 baseline)."""
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User
    from app.services import attendance_service

    out: Dict[str, Any] = {}
    for key, cid in companies(store).items():
        founder_user = store["companies"][key]["ids"]["people"]["founder"]["user_id"]
        with tenant_db(cid) as db:
            principal = build_principal(db, db.get(User, founder_user), None, db.get(Company, cid))
            board = attendance_service.today_summary(db, principal)
            out[f"{key}/today"] = {k: v for k, v in board.items() if k != "employees"}
            out[f"{key}/trend"] = attendance_service.trend(db, principal, 7)
            for e in cast(db, cid):
                stats = attendance_service.monthly_stats(db, principal, e.id, 2026, 10)
                if any(stats[k] for k in ("present", "late", "half_day", "absent")):
                    out[f"{key}/{_label(e)}/2026-10"] = jsonable(stats)
    return out


# ------------------------------------------------------------------------------------------------- compare
def check(directory: Path, name: str, captured: dict) -> None:
    """Compare with the committed golden (or rewrite it with HRMS_GOLDEN_WRITE=1); a diff names the first keys."""
    path = directory / f"{name}.json"
    text = json.dumps(jsonable(captured), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if WRITE:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"golden {path} is missing; generate it with scripts/as_baseline.py --write"
    expected = json.loads(path.read_text(encoding="utf-8"))
    actual = json.loads(text)
    if expected != actual:
        changed = sorted(k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k))
        sample = changed[:5]
        raise AssertionError(f"golden {name} differs in {len(changed)} keys, e.g. {sample}: "
                             f"expected {json.dumps(expected.get(sample[0]))[:600] if sample else ''} "
                             f"got {json.dumps(actual.get(sample[0]))[:600] if sample else ''}")
