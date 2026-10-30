"""Shift-owned halves of the leave / holiday cross-module edge cases (API and service level; FU-AS-53).

* AS-INT-07 — holiday calendar by ``shift_ids``: ``shift_resolution.criterion_shifts_many`` reads the planned shift of a
  HOLIDAY_RULE OFF (the matcher itself is LH 04-holiday/01 §4.4.1).
* AS-INT-08 — roster publish inside approved leave: the WARN preview with the request's old -> new units and the
  recount trigger of the publication (the recount, ledger and BALANCE_CONFLICT are LH's).
* AS-INT-18 — holiday added over a published WORK entry: HOLIDAY_WORK in the preview (03-shifts/02 §8) and the rows of
  the derived cutoff item ``holiday_work_unreviewed`` (payroll's pending list and JC-AS-08 use are payroll's).
* X-INT-56 — leave approval vs roster publish: the publication locks employees in ascending id before the period row
  and answers 409 LOCK_TIMEOUT after 5 s (the leave approval's lock and recount keys are LH's).
* X-INT-58 — Ramadan hours: one DIRECT re-versioning publication, its recount trigger and the new scheduled minutes;
  RAMADAN_HOURS WARN for a 12-h shift inside the AE Ramadan dates.

Pure halves: tests/attendance_unit/test_shift_leave_holiday_ecs.py.
"""

import threading
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from tests.helpers import create_employee, invite_and_activate
from tests.shifts.test_rosters import _cell, _drafts, _publish, plant  # noqa: F401


def _sql(query, **params):
    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


def _db(plant):
    db = SessionLocal()
    bind_tenant(db, plant["admin"].company_id)
    return db


def _settled(results, reason="Planned with the crew"):
    """Acknowledge every WARN and answer every REQUIRE_REASON of a dry run."""
    return {"acknowledged_warnings": sorted({r["rule_key"] for r in results if r["severity"] == "WARN"}),
            "reasons": {r["rule_key"]: reason for r in results if r["severity"] == "REQUIRE_REASON"}}


def _publish_all(plant, **body):
    preview = _publish(plant, dry_run=True, **body)
    assert preview["blocking"] == [], preview["blocking"]
    return _publish(plant, **body, **_settled(preview["results"]))


def _direct(admin, cells, **extra):
    preview = admin.post("/api/v1/rosters/direct-publish", json={"cells": cells, "dry_run": True, **extra})
    assert preview.status_code == 200, preview.text
    body = {"cells": cells, **extra, **_settled(preview.json()["results"])}
    r = admin.post("/api/v1/rosters/direct-publish", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _recounts(monkeypatch):
    """Record the leave recount the publication triggers (``leave_jobs.recount_dates``), still running it."""
    from app.services import leave_jobs

    calls = []
    real = leave_jobs.recount_dates

    def spy(db, company_id, dates, *, key, actor_user_id=None):
        calls.append((sorted(set(dates)), key))
        return real(db, company_id, dates, key=key, actor_user_id=actor_user_id)

    monkeypatch.setattr(leave_jobs, "recount_dates", spy)
    return calls


def _leave(app, plant, employee, first, last):
    worker = invite_and_activate(app, plant["admin"], employee, ["employee"])
    r = worker.post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": first.isoformat(),
                                                  "end_date": last.isoformat(), "reason": "family"})
    assert r.status_code in (200, 201), r.text
    leave = r.json()
    assert plant["admin"].post(f"/api/v1/leaves/{leave['id']}/approve").status_code == 200
    return leave


# ------------------------------------------------------------------------------------------------ AS-INT-07
def test_as_int_07_a_holiday_rule_off_keeps_matching_the_night_calendar_on_its_planned_shift(plant):
    """EC AS-INT-07 (shift-owned half, 03-shifts/02 §8): Ravi's rotation token N -> NIGHT on the holiday became an OFF
    (source HOLIDAY_RULE, planned_shift_id NIGHT, as the generator stores it); the shift the LH ``shift_ids``
    criterion matches on stays NIGHT although the resolved shift is none, so publishing the OFF does not remove its
    own holiday. Sita's MANUAL OFF matches nothing; Kiran's legacy HOLIDAY_RULE OFF without a planned shift falls back
    to his placement shift. The criterion's use in the calendar resolver is LH's."""
    admin, s, start = plant["admin"], plant["shifts"], plant["start"]
    ravi, sita = plant["ravi"], plant["sita"]
    kiran = create_employee(admin, "Kiran", branch_id=plant["branch"]["id"], shift_id=s["LONG"])
    day = start + timedelta(days=1)
    _direct(admin, [_cell(ravi, day, kind="OFF", source="HOLIDAY_RULE"), _cell(sita, day, kind="OFF", source="MANUAL"),
                    _cell(kiran, day, kind="OFF", source="HOLIDAY_RULE")])
    _sql("UPDATE roster_entries SET planned_shift_id = :n WHERE employee_id = :e AND shift_date = :d",
         n=s["NIGHT"], e=ravi["id"], d=day)
    from app.services import shift_resolution

    db = _db(plant)
    try:
        ids = [ravi["id"], sita["id"], kiran["id"]]
        criterion = shift_resolution.criterion_shifts_many(db, admin.company_id, ids, day, day + timedelta(days=1))
        resolved = shift_resolution.shifts_for_many(db, admin.company_id, ids, day, day)
    finally:
        db.close()
    assert criterion[(ravi["id"], day)] == s["NIGHT"]
    assert criterion[(sita["id"], day)] is None
    assert criterion[(kiran["id"], day)] == s["LONG"]
    assert criterion[(kiran["id"], day + timedelta(days=1))] == s["LONG"]  # no entry: the placement shift
    assert resolved[(ravi["id"], day)] is None and resolved[(kiran["id"], day)] is None  # an OFF is still no shift


# ------------------------------------------------------------------------------------------------ AS-INT-08
def test_as_int_08_republishing_an_off_day_inside_approved_leave_warns_old_new_units_and_recounts(app, plant,
                                                                                                  monkeypatch):
    """EC AS-INT-08 (shift-owned half): Ravi's leave Thu-Sat is charged 1.00 (Fri and Sat rostered OFF); republishing
    Fri as WORK lists ROSTER_AFFECTS_APPROVED_LEAVE as a WARN with the request's units 1.00 -> 2.00 that must be
    acknowledged and never blocks; the publication asks LH to recount Fri once (key per publication) and a repeated
    publish posts nothing. The recount, ledger debit and BALANCE_CONFLICT are LH's."""
    # Golden vector GV-AS-INT-04 of AS 05 §5.3 (Sunita's case, cast as Ravi here).
    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    thu, fri, sat = (start + timedelta(days=i) for i in (3, 4, 5))
    _drafts(plant, [_cell(ravi, thu, s["EARLY"]), _cell(ravi, fri, kind="OFF"), _cell(ravi, sat, kind="OFF")])
    _publish_all(plant)
    leave = _leave(app, plant, ravi, thu, sat)
    charged = dict(_sql("SELECT date, counts_against_balance FROM leave_days WHERE leave_id = :l AND voided_at IS NULL",
                        l=leave["id"]).all())
    assert charged == {thu: True, fri: False, sat: False}, "LH precondition: the rostered OFF days are not charged"
    calls = _recounts(monkeypatch)
    _drafts(plant, [_cell(ravi, fri, s["EARLY"])])
    preview = _publish(plant, dry_run=True)
    hits = [r for r in preview["results"] if r["rule_key"] == "ROSTER_AFFECTS_APPROVED_LEAVE"]
    assert [(h["severity"], h["employee_id"], h["dates"], h["limit"], h["measured"]) for h in hits] == [
        ("WARN", ravi["id"], [fri.isoformat()], "1.00", "2.00")]
    assert hits[0]["details"] == {"leave_ref": leave["id"]} and preview["blocking"] == []
    body = _settled(preview["results"])
    body["acknowledged_warnings"].remove("ROSTER_AFFECTS_APPROVED_LEAVE")
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json=body)
    assert r.status_code == 422 and r.json()["code"] == "WARNINGS_UNACKNOWLEDGED"
    assert {i["rule_key"] for i in r.json()["issues"]} == {"ROSTER_AFFECTS_APPROVED_LEAVE"}
    assert calls == []
    done = _publish(plant, **_settled(preview["results"]))
    assert calls == [([fri], f"recalc:roster:{done['publication_id']}")]
    again = _publish(plant)
    assert again["nothing_to_publish"] is True and len(calls) == 1


# ------------------------------------------------------------------------------------------------ AS-INT-18
def test_as_int_18_a_holiday_added_over_published_work_is_unreviewed_until_the_planner_decides(plant, monkeypatch):
    """EC AS-INT-18 (shift-owned half, 03-shifts/02 §8): Ravi and Sita were published WORK on Tue;
    a COMPANY closure for Tue is added afterwards. Both entries are rows of ``holiday_work_unreviewed`` (nobody decided
    holiday work); an unrelated publication for Ravi shows HOLIDAY_WORK for Tue as a WARN, not a demanded reason;
    planning Tue again needs a HOLIDAY_WORK reason and decides it; Sita's entry withdrawn to OFF leaves the list.
    WORK_THROUGH never raises HOLIDAY_WORK. The pending item and the payroll approval rule are payroll's."""
    admin, s, start = plant["admin"], plant["shifts"], plant["start"]
    ravi, sita = plant["ravi"], plant["sita"]
    tue, wed = start + timedelta(days=1), start + timedelta(days=2)
    _drafts(plant, [_cell(ravi, tue, s["EARLY"]), _cell(sita, tue, s["EARLY"])])
    first = _publish_all(plant)
    assert all(r["rule_key"] != "HOLIDAY_WORK" for r in first["results"])  # not a holiday when it was published
    made = admin.post("/api/v1/holidays", json={"name": "Plant closure", "date": tue.isoformat(),
                                                "holiday_type": "COMPANY"})
    assert made.status_code in (200, 201), made.text
    holiday_id = made.json()["id"]
    from app.services import roster_validation

    def unreviewed():
        db = _db(plant)
        try:
            rows = roster_validation.holiday_work_unreviewed(db, admin.company_id, [ravi["id"], sita["id"]], start,
                                                             start + timedelta(days=13))
            return [(r["employee_id"], r["shift_date"], r["holiday_id"]) for r in rows]
        finally:
            db.close()

    assert unreviewed() == sorted([(ravi["id"], tue, holiday_id), (sita["id"], tue, holiday_id)])
    _drafts(plant, [_cell(ravi, wed, s["EARLY"])])
    preview = _publish(plant, dry_run=True)
    hits = [(r["severity"], r["employee_id"], r["dates"], r["message_key"]) for r in preview["results"]
            if r["rule_key"] == "HOLIDAY_WORK"]
    assert hits == [("WARN", ravi["id"], [tue.isoformat()], "roster.rule.holiday_work.unreviewed")]
    from app.services import attendance_days

    real_fields = attendance_days.scheduling_fields
    monkeypatch.setattr(attendance_days, "scheduling_fields",
                        lambda db, cid, ids, on: {e: {"holiday_mode": "WORK_THROUGH"} for e in ids})
    assert all(r["rule_key"] != "HOLIDAY_WORK" for r in _publish(plant, dry_run=True)["results"])
    monkeypatch.setattr(attendance_days, "scheduling_fields", real_fields)
    _publish(plant, **_settled(preview["results"]))  # acknowledged, not decided: still unreviewed
    assert unreviewed() == sorted([(ravi["id"], tue, holiday_id), (sita["id"], tue, holiday_id)])
    _drafts(plant, [_cell(ravi, tue, s["LONG"])])  # the planner plans the holiday work again
    replan = _publish(plant, dry_run=True)
    assert [(r["severity"], r["dates"]) for r in replan["results"] if r["rule_key"] == "HOLIDAY_WORK"] == [
        ("REQUIRE_REASON", [tue.isoformat()])]
    body = _settled(replan["results"])
    r = admin.post(f"/api/v1/rosters/{plant['period']['id']}/publish",
                   json={**body, "reasons": {k: v for k, v in body["reasons"].items() if k != "HOLIDAY_WORK"}})
    assert r.status_code == 422 and r.json()["code"] == "ROSTER_REASON_REQUIRED"
    _publish(plant, **body)
    _direct(admin, [_cell(sita, tue, kind="OFF")])  # Withdraw: Sita's holiday work is taken back
    assert unreviewed() == []


# ------------------------------------------------------------------------------------------------ X-INT-56
def test_x_int_56_a_roster_publish_locks_employees_ascending_before_the_period_and_times_out(plant, monkeypatch):
    """EC X-INT-56 (shift-owned half, 03-shifts/02 §5.1 step 1, LH LCK-02): the publication takes the attendance lock
    of every employee in ascending id before the period row (no deadlock with a leave approval that holds the same
    employee lock), and a lock held elsewhere for more than 5 s answers 409 LOCK_TIMEOUT with nothing written; the
    retry publishes. The leave approval's side of the lock and the recount keys are LH's."""
    from app.services import shift_rosters
    from app.services.attendance_service import ATTENDANCE_LOCK_NAMESPACE

    s, start, ravi, sita = plant["shifts"], plant["start"], plant["ravi"], plant["sita"]
    events = []
    real_lock, real_load = shift_rosters.lock_employees, shift_rosters.load_period

    def lock(db, ids):
        ids = list(ids)
        events.append(("employees", ids))
        return real_lock(db, ids)

    def load(db, principal, period_id, *, lock=False):
        if lock:
            events.append(("period", period_id))
        return real_load(db, principal, period_id, lock=lock)

    monkeypatch.setattr(shift_rosters, "lock_employees", lock)
    monkeypatch.setattr(shift_rosters, "load_period", load)
    _drafts(plant, [_cell(sita, start, s["EARLY"]), _cell(ravi, start, s["EARLY"])])
    preview = _publish(plant, dry_run=True)
    events.clear()
    outcome = {}
    holder = system_engine.connect()
    held = holder.begin()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:ns, :id)"), {"ns": ATTENDANCE_LOCK_NAMESPACE,
                                                                        "id": max(ravi["id"], sita["id"])})
        worker = threading.Thread(target=lambda: outcome.update(r=plant["admin"].post(
            f"/api/v1/rosters/{plant['period']['id']}/publish", json=_settled(preview["results"]))))
        worker.start()
        worker.join(30)
    finally:
        held.rollback()
        holder.close()
    worker.join(30)
    assert "r" in outcome, "the publication did not give up waiting for the lock"
    assert outcome["r"].status_code == 409 and outcome["r"].json()["code"] == "LOCK_TIMEOUT", outcome["r"].text
    assert _sql("SELECT count(*) FROM roster_entries WHERE employee_id IN (:a, :b)", a=ravi["id"], b=sita["id"]
                ).scalar() == 0
    assert events[0] == ("employees", sorted([ravi["id"], sita["id"]]))
    events.clear()
    done = _publish(plant, **_settled(preview["results"]))
    assert done["entries_created"] == 2
    assert [kind for kind, _ in events] == ["employees", "period"]
    assert events[0][1] == sorted([ravi["id"], sita["id"]])


# ------------------------------------------------------------------------------------------------ X-INT-58
def test_x_int_58_a_temporary_shift_version_reversions_published_entries_and_recounts_their_leave(app, plant,
                                                                                                monkeypatch):
    """EC X-INT-58 (shift-owned half, 03-shifts/02 §5.5): a temporary version of EARLY (06:00-12:00, the Ramadan-style
    short day) over Ravi's published week writes one DIRECT publication — the entries inside the window get version +
    1, the new shift_version_id and change rows SHIFT_VERSION_CHANGED / EMPLOYER (never an in-place edit) — asks LH
    to recount the leave on those dates, and the resolved shift's scheduled minutes go 480 -> 360; the entry after the
    window keeps 480. The recount itself (HOUR-type and half-day units, keys) is LH's."""
    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    week = [start + timedelta(days=i) for i in range(5)]
    _drafts(plant, [_cell(ravi, d, s["EARLY"]) for d in week])
    _publish_all(plant)
    before = dict(_sql("SELECT shift_date, version FROM roster_entries WHERE employee_id = :e AND withdrawn_at IS NULL",
                       e=ravi["id"]).all())
    leave_day = week[2]
    _leave(app, plant, ravi, leave_day, leave_day)
    calls = _recounts(monkeypatch)
    r = plant["admin"].post(f"/api/v1/shifts/{s['EARLY']}/versions", json={
        "effective_from": week[1].isoformat(), "effective_to": week[3].isoformat(), "start_time": "06:00",
        "end_time": "12:00", "reason": "Short days"})
    assert r.status_code == 201, r.text
    roster = r.json()["effects"]["roster"]
    assert roster["entries_reversioned"] == 3 and roster["publication_id"]
    after = dict(_sql("SELECT shift_date, version FROM roster_entries WHERE employee_id = :e AND withdrawn_at IS NULL",
                      e=ravi["id"]).all())
    assert {d: after[d] - before[d] for d in week} == {week[0]: 0, week[1]: 1, week[2]: 1, week[3]: 1, week[4]: 0}
    changes = _sql("SELECT c.shift_date, c.change_kind, c.reason_code, c.initiated_by, p.kind "
                   "FROM roster_entry_changes c JOIN roster_publications p ON p.id = c.publication_id "
                   "WHERE p.id = :p ORDER BY c.shift_date", p=roster["publication_id"]).all()
    assert [tuple(c) for c in changes] == [(d, "UPDATED", "SHIFT_VERSION_CHANGED", "EMPLOYER", "DIRECT")
                                          for d in week[1:4]]
    assert calls == [([leave_day], f"recalc:roster:{roster['publication_id']}")]
    from app.services import shift_resolution

    db = _db(plant)
    try:
        resolved = shift_resolution.shifts_for_many(db, plant["admin"].company_id, [ravi["id"]], week[0], week[4])
    finally:
        db.close()
    minutes = {d: shift_resolution.scheduled_hours(resolved[(ravi["id"], d)]) * 60 for d in week}
    assert minutes == {week[0]: 480, week[1]: 360, week[2]: 360, week[3]: 360, week[4]: 480}
    assert resolved[(ravi["id"], leave_day)].version.end_time == "12:00"


def test_x_int_58_a_twelve_hour_shift_inside_the_ae_ramadan_dates_warns_ramadan_hours(plant):
    """EC X-INT-58 (shift-owned half): Maria (AE legal entity) published on a 12-h night shift inside
    the AE pack's Ramadan dates gets RAMADAN_HOURS WARN (12.00 h above the Ramadan normal 6 h) that must be
    acknowledged; outside the dates it does not appear."""
    admin = plant["admin"]
    _sql("UPDATE legal_entities SET country_code = 'AE' WHERE company_id = :c", c=admin.company_id)
    night = admin.post("/api/v1/shifts", json={"code": "BC12N", "name": "Night 12", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "test", "start_time": "19:00", "end_time": "07:00"}})
    assert night.status_code == 201, night.text
    maria = create_employee(admin, "Maria", branch_id=plant["branch"]["id"])
    inside = [_cell(maria, date(2027, 2, 10), night.json()["id"])]
    dry = admin.post("/api/v1/rosters/direct-publish", json={"cells": inside, "dry_run": True}).json()
    hits = [r for r in dry["results"] if r["rule_key"] == "RAMADAN_HOURS"]
    assert [(h["severity"], h["dates"], h["measured"], Decimal(h["limit"]), h["details"]) for h in hits] == [
        ("WARN", ["2027-02-10"], "12.00", Decimal(6), {"window": "RAMADAN_2027"})]
    body = _settled(dry["results"])
    r = admin.post("/api/v1/rosters/direct-publish", json={
        "cells": inside, **body, "acknowledged_warnings": [k for k in body["acknowledged_warnings"]
                                                           if k != "RAMADAN_HOURS"]})
    assert r.status_code == 422 and r.json()["code"] == "WARNINGS_UNACKNOWLEDGED"
    assert "RAMADAN_HOURS" in {i["rule_key"] for i in r.json()["issues"]}
    r = admin.post("/api/v1/rosters/direct-publish", json={"cells": inside, **body})
    assert r.status_code == 200, r.text
    outside = admin.post("/api/v1/rosters/direct-publish", json={
        "cells": [_cell(maria, date(2027, 3, 15), night.json()["id"])], "dry_run": True}).json()
    assert all(x["rule_key"] != "RAMADAN_HOURS" for x in outside["results"])


def test_rotation_generation_turns_a_work_token_on_a_holiday_into_a_holiday_rule_off(plant):
    """03-shifts/02 §3.5 step 3, §8 (HOLIDAY_OFF, the default) with EC AS-INT-07: a COMPANY holiday on a WORK token
    becomes an OFF draft with source HOLIDAY_RULE that remembers the replaced shift (`planned_shift_id`); publishing it
    raises no HOLIDAY_WORK and keeps `planned_shift_id` on the entry, which the `shift_ids` criterion then matches.
    Auto-publish (job roster.rotation_generate) applies the same rule instead of stopping on HOLIDAY_WORK."""
    from app.services import shift_resolution, shift_rotations

    admin, s, start = plant["admin"], plant["shifts"], plant["start"]
    ravi, sita = plant["ravi"], plant["sita"]
    holiday = start + timedelta(days=2)  # crew B, offset 6: index 8 is a WORK token
    made = admin.post("/api/v1/holidays", json={"name": "Plant closure", "date": holiday.isoformat(),
                                                "holiday_type": "COMPANY"})
    assert made.status_code in (200, 201), made.text
    r = admin.post("/api/v1/rotations", json={
        "code": "DAY-6ON2OFF", "name": "Days 6 on 2 off", "template_key": "SIX_ON_TWO_OFF_SLOW",
        "version": {"token_map": {t: {"shift_id": s["EARLY"]} for t in ("M", "A", "N")}}})
    assert r.status_code == 201, r.text
    pattern = r.json()
    assert admin.post(f"/api/v1/rotations/{pattern['id']}/versions/1/activate").status_code == 200
    version = next(p for p in admin.get("/api/v1/rotations").json() if p["id"] == pattern["id"])["current_version_id"]
    for person, auto in ((ravi, False), (sita, True)):
        r = admin.post("/api/v1/rotations/assignments", json={
            "pattern_version_id": version, "employee_ids": [person["id"]], "crew_code": "B", "offset_days": 6,
            "anchor_date": start.isoformat(), "effective_from": start.isoformat(), "auto_publish": auto})
        assert r.status_code == 201, r.text

    # Period generation (Ravi; Sita's auto-publish rows are generated too, but only Ravi's are checked here).
    assert admin.post(f"/api/v1/rosters/{plant['period']['id']}/generate", json={}).status_code == 200
    draft = _sql("SELECT kind, source, shift_id, planned_shift_id FROM roster_drafts WHERE employee_id = :e "
                 "AND shift_date = :d", e=ravi["id"], d=holiday).one()
    assert tuple(draft) == ("OFF", "HOLIDAY_RULE", None, s["EARLY"])
    after = _sql("SELECT kind, source FROM roster_drafts WHERE employee_id = :e AND shift_date = :d",
                 e=ravi["id"], d=holiday + timedelta(days=1)).one()
    assert tuple(after) == ("WORK", "ROTATION")
    preview = _publish(plant, dry_run=True)
    assert "HOLIDAY_WORK" not in {r["rule_key"] for r in preview["results"]}
    _publish_all(plant)
    entry = _sql("SELECT kind, source, planned_shift_id FROM roster_entries WHERE employee_id = :e AND shift_date = :d "
                 "AND withdrawn_at IS NULL", e=ravi["id"], d=holiday).one()
    assert tuple(entry) == ("OFF", "HOLIDAY_RULE", s["EARLY"])
    db = _db(plant)
    try:
        assert shift_resolution.criterion_shifts_many(db, admin.company_id, [ravi["id"]], holiday, holiday)[
            (ravi["id"], holiday)] == s["EARLY"]
    finally:
        db.close()

    # Auto-publish for Sita over the same holiday: her drafts were published above as part of the period, so clear
    # them and let the job publish the horizon itself.
    _sql("DELETE FROM roster_entry_changes WHERE employee_id = :e", e=sita["id"])
    _sql("DELETE FROM roster_entries WHERE employee_id = :e", e=sita["id"])
    _sql("DELETE FROM roster_drafts WHERE employee_id = :e", e=sita["id"])
    db = _db(plant)
    try:
        result = shift_rotations.auto_publish(db, admin.company_id, start - timedelta(days=1))
        db.commit()
    finally:
        db.close()
    assert sita["id"] not in result["blocked"], result
    published = _sql("SELECT kind, source, planned_shift_id FROM roster_entries WHERE employee_id = :e "
                     "AND shift_date = :d AND withdrawn_at IS NULL", e=sita["id"], d=holiday).one()
    assert tuple(published) == ("OFF", "HOLIDAY_RULE", s["EARLY"])
