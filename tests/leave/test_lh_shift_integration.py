"""LH-owned halves of the leave / holiday x shift / roster edge cases (FU-AS-53; shift halves in
tests/shifts/test_shift_leave_holiday_ecs.py, pure halves in tests/leave_unit/test_lh_shift_integration_units.py).

* AS-INT-07 — the holiday-calendar criterion ``shift_ids`` matches the criterion shift of the date: a HOLIDAY_RULE OFF
  keeps the night calendar (HOLIDAY, not a rostered weekly off); a MANUAL OFF does not (04-holiday/01 §4.4.1).
* AS-INT-08 — roster publish inside approved leave (GV-AS-INT-04): V2_ACTIVE recount 1.00 -> 2.00 with one ledger debit,
  a rerun posts nothing, beyond the negative allowance BALANCE_CONFLICT + pending item ``leave_balance_conflict`` and the
  leave stays approved; ``calendar_version`` changes (roster key); the publish preview helper
  ``leave_recalc.preview_units``; LEGACY_V1 keeps its per-date recount.
* X-INT-56 — leave approval vs roster publish: the leave takes the employee lock (409 LOCK_TIMEOUT after 5 s, nothing
  written) and the final charge does not depend on which came first.
* X-INT-58 — a shift version (480 -> 360 min) recounts the half- and full-day leave of its people through the
  ``recalc:shift:shift_version:{id}:{v}`` job and the DIRECT re-versioning publication. HOUR-type leave is not built
  in leave v2 (days only), so its case does not apply yet.

Clock: the attendance fake clock (Mon 2026-10-19 09:00 IST); roster period Mon 16 - Sun 29 Nov 2026.
"""

import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database.db import system_engine
from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.leave.test_v2_core import L, apply, backdate_settings, go_live, ok
from tests.shifts.test_rosters import _cell, _drafts, _publish, plant  # noqa: F401  (plant is a fixture)
from tests.shifts.test_shift_leave_holiday_ecs import _direct, _publish_all, _settled
from tests.time_goldens import tenant_db

D = date
THU, FRI, SAT = D(2026, 11, 19), D(2026, 11, 20), D(2026, 11, 21)


def _items(body):
    return body.get("items") if isinstance(body, dict) else body


@pytest.fixture
def works(app, fake_clock):  # noqa: F811
    """Alpha-style plant: shift A 06:00-14:00, Sunita / Meera / Imran on the branch (no placement shift), Layla on
    placement shift A, Kavya HR,
    roster period RP-PUNE-NOV (16-29 Nov 2026). LEGACY_V1 until a test calls ``go_live``."""
    admin = signup_company(app, "Roster Leave Works")
    branch = _items(admin.get("/api/v1/branches").json())[0]
    r = admin.post("/api/v1/shifts", json={"code": "A", "name": "Shift A", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "fixture", "start_time": "06:00", "end_time": "14:00"}})
    assert r.status_code == 201, r.text
    people = {name: create_employee(admin, name, last, branch_id=branch["id"])
              for name, last in (("Sunita", "Patil"), ("Meera", "Joshi"), ("Imran", "Shaikh"))}
    people["Layla"] = create_employee(admin, "Layla", "Hassan", branch_id=branch["id"], shift_id=r.json()["id"])
    hr = create_employee(admin, "Kavya", "Rao")
    hr_client = invite_and_activate(app, admin, hr, ["hr_admin"])
    backdate_settings(admin.company_id)
    period = admin.post("/api/v1/rosters", json={"code": "RP-PUNE-NOV", "name": "Pune plant November",
                                                  "branch_id": branch["id"], "start_date": "2026-11-16",
                                                  "end_date": "2026-11-29"})
    assert period.status_code == 201, period.text
    return {"admin": admin, "hr": hr_client, "cid": admin.company_id, "branch": branch, "A": r.json()["id"],
            "period": period.json(), "clock": fake_clock, "app": app, **people}


def _record(t, person, start, end=None, **extra):
    """HR records approved leave (v2: HR_RECORDED, APPROVED at once)."""
    return ok(t["hr"].post(f"{L}/record", json={"leave_type": "casual", "start_date": str(start),
                                                "end_date": str(end or start), "employee_id": person["id"],
                                                **extra}), 201)


def _leave(leave_id):
    row = owner_rows("SELECT number_of_days, version, flags, status FROM leaves WHERE id = :i", i=leave_id)[0]
    return Decimal(row["number_of_days"]), row["version"], list(row["flags"] or []), row["status"]


def _charged(leave_id):
    return {r["date"]: (r["portion"], Decimal(r["units"]), r["counts_against_balance"]) for r in owner_rows(
        "SELECT date, portion, units, counts_against_balance FROM leave_days WHERE leave_id = :i AND voided_at IS NULL",
        i=leave_id)}


def _uses(leave_id):
    return [(Decimal(r["delta"]), r["idempotency_key"]) for r in owner_rows(
        "SELECT delta, idempotency_key FROM leave_balance_transactions WHERE ref_type = 'leave' AND ref_id = :i "
        "AND reason = 'USE' ORDER BY id", i=leave_id)]


def _fact(cid, employee_id, day):
    from app.payroll.services import time_inputs

    with tenant_db(cid) as db:
        return time_inputs.day_facts(db, cid, employee_id, day, day)[0]


# ------------------------------------------------------------------------------------------------ AS-INT-07
def test_as_int_07_a_holiday_rule_off_keeps_the_night_calendar_holiday_a_manual_off_does_not(plant, monkeypatch):
    """GV-HOL-AS-01 on the plant: BR-PLANT-NIGHT (holiday "Night crew day" on Tue) by criteria {branch_ids, shift_ids
    [NIGHT]}. Ravi's OFF made by the holiday rule (planned NIGHT) keeps the holiday: day facts show the night calendar
    by CRITERIA, a HolidayFact and expectation reason HOLIDAY (ledger HOLIDAY, not WEEKLY_OFF). Sita's MANUAL OFF falls
    back to the branch calendar (a rostered weekly off). Kiran's WORK entry on NIGHT matches too. One day-facts batch
    resolves the criterion once."""
    from app.payroll.services import time_inputs
    from app.services import holiday_assignments, holiday_years, shift_resolution
    from tests.holiday.test_day_facts_v2 import _calendar, _published

    monkeypatch.setattr(holiday_years, "require_publish_step_up", lambda db, principal: None)
    admin, s, cid = plant["admin"], plant["shifts"], plant["admin"].company_id
    ravi, sita = plant["ravi"], plant["sita"]
    kiran = create_employee(admin, "Kiran", branch_id=plant["branch"]["id"])
    tue = plant["start"] + timedelta(days=1)
    night = _calendar(admin, "BR-PLANT-NIGHT", country_code="IN")
    _published(admin, night["id"], [("Night crew day", tue.isoformat(), "PUBLIC")], year=tue.year)
    r = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": night["id"], "criteria": {"branch_ids": [plant["branch"]["id"]], "shift_ids": [s["NIGHT"]]},
        "priority": 10, "reason": "Night crew"})
    assert r.status_code == 201, r.text
    _direct(admin, [_cell(ravi, tue, kind="OFF", source="HOLIDAY_RULE"), _cell(sita, tue, kind="OFF", source="MANUAL"),
                    _cell(kiran, tue, s["NIGHT"])])
    with system_engine.begin() as conn:
        conn.execute(text("UPDATE roster_entries SET planned_shift_id = :n WHERE employee_id = :e AND shift_date = :d"),
                     {"n": s["NIGHT"], "e": ravi["id"], "d": tue})
    calls = []
    real = shift_resolution.criterion_shifts_many

    def spy(db, company_id, ids, start, end):
        calls.append((sorted(ids), start, end))
        return real(db, company_id, ids, start, end)

    monkeypatch.setattr(shift_resolution, "criterion_shifts_many", spy)
    ids = [ravi["id"], sita["id"], kiran["id"]]
    with tenant_db(cid) as db:
        facts = time_inputs.day_facts_many(db, cid, ids, tue - timedelta(days=1), tue + timedelta(days=1))
        assert len(calls) == 1 and calls[0][0] == sorted(ids), calls  # once for the batch
        expected = time_inputs.expected_presence(db, cid, ids, tue, tue)
        single = holiday_assignments.resolve(db, cid, ravi["id"], tue)  # the unbatched path agrees
    on = {e: {f.date: f for f in rows}[tue] for e, rows in facts.items()}
    r_, s_, k_ = on[ravi["id"]], on[sita["id"]], on[kiran["id"]]
    assert (r_.calendar_id, r_.calendar_source, r_.work_source, r_.weekly_off) == (night["id"], "CRITERIA", "ROSTER",
                                                                                   True)
    assert r_.holiday is not None and r_.holiday.name == "Night crew day" and r_.holiday.non_working
    assert (single.calendar_id, single.calendar_source) == (night["id"], "CRITERIA")
    assert s_.calendar_id != night["id"] and (s_.holiday is None or s_.holiday.name != "Night crew day")
    assert s_.weekly_off and s_.work_source == "ROSTER"
    assert (k_.calendar_id, k_.holiday.name, k_.weekly_off, k_.working_portion) == (night["id"], "Night crew day",
                                                                                    False, "NONE")
    after = {f.date: f for f in facts[ravi["id"]]}[tue + timedelta(days=1)]
    assert after.calendar_id != night["id"]  # no entry and no shift: nothing to match
    reasons = {e: rows[0].reasons for e, rows in expected.items()}
    assert "HOLIDAY" in reasons[ravi["id"]] and "HOLIDAY" not in reasons[sita["id"]]
    assert reasons[sita["id"]] == ("WEEKLY_OFF",)


# ------------------------------------------------------------------------------------------------ AS-INT-08
def test_as_int_08_v2_republished_work_day_recounts_debits_once_and_flags_balance_conflict(works):
    """GV-AS-INT-04 on a V2_ACTIVE tenant. Sunita (casual 10) and Meera (casual 1.50, the variant) have Thu-Sat leave
    charged 1.00 (Fri, Sat rostered OFF). Republishing Fri as WORK: the publication's recount
    (``recalc:roster:{publication}``) regenerates both requests (version + 1) at 2.00 with one ledger debit of 1.00;
    Meera goes below her negative allowance (0): BALANCE_CONFLICT + pending item ``leave_balance_conflict``, the leave
    stays approved. ``calendar_version`` of Fri changes (roster key) and is stable; a rerun posts nothing. X-INT-56:
    Imran's leave recorded after the same publication is charged 2.00 at once — the order does not matter."""
    from app.payroll.services import time_inputs
    from app.models.time_inputs import LeaveBalanceTransaction
    from app.services import leave_jobs, leave_recalc

    t = works
    go_live(t)
    cid, a = t["cid"], t["A"]
    sunita, meera, imran = t["Sunita"], t["Meera"], t["Imran"]
    ok(t["hr"].post(f"{L}/adjustments", json={"employee_id": meera["id"], "leave_type": "casual", "delta": "-8.50",
                                              "effective_date": "2026-10-19", "reason": "Variant: 1.50 left"}), 201)
    people = (sunita, meera, imran)
    _drafts(t, [c for p in people for c in (_cell(p, THU, a), _cell(p, FRI, kind="OFF"), _cell(p, SAT, kind="OFF"))])
    _publish_all(t)
    leaves = {p["first_name"]: _record(t, p, THU, SAT)["id"] for p in (sunita, meera)}
    for leave_id in leaves.values():
        assert _leave(leave_id)[:2] == (Decimal(1), 1)
        assert {d: c for d, (_, _, c) in _charged(leave_id).items()} == {THU: True, FRI: False, SAT: False}
    pending = ok(apply(t["hr"], "casual", "2026-11-25", "2026-11-29", employee_id=sunita["id"]), 201)
    assert pending["status_v2"] == "PENDING"
    with tenant_db(cid) as db:
        preview = leave_recalc.preview_units(db, cid, leaves["Sunita"], {FRI: "WORK"})
        weekend = leave_recalc.preview_units(db, cid, pending["id"], {D(2026, 11, 28): "WORK"})
        unchanged = leave_recalc.preview_units(db, cid, leaves["Sunita"], {THU: "WORK"})
    assert (preview["old_units"], preview["new_units"], preview["booked_units"]) == (1, 2, 1)
    assert preview["days"] == [{"date": FRI.isoformat(), "old": 0, "new": 1, "charge_reason": "WORKING"}]
    assert (weekend["status"], weekend["old_units"], weekend["new_units"]) == ("pending", 3, 4)
    assert unchanged["changed"] is False and unchanged["days"] == []
    before = _fact(cid, sunita["id"], FRI).calendar_version

    _drafts(t, [_cell(p, FRI, a) for p in people])
    dry = _publish(t, dry_run=True)
    hits = sorted((h["employee_id"], h["limit"], h["measured"]) for h in dry["results"]
                  if h["rule_key"] == "ROSTER_AFFECTS_APPROVED_LEAVE")
    assert hits == sorted([(sunita["id"], "1.00", "2.00"), (meera["id"], "1.00", "2.00")])
    done = _publish(t, **_settled(dry["results"]))
    after = _fact(cid, sunita["id"], FRI).calendar_version
    assert after != before and after == _fact(cid, sunita["id"], FRI).calendar_version

    for name, leave_id in leaves.items():
        units, version, flags, status = _leave(leave_id)
        assert (units, version, status) == (Decimal(2), 2, "approved"), name
        assert _charged(leave_id)[FRI] == ("FULL", Decimal(1), True)
        assert [d for d, _ in _uses(leave_id)] == [Decimal(-1), Decimal(-1)], name
        assert _uses(leave_id)[1][1].startswith(f"leave:{leave_id}:v2:use:"), name
        assert ("BALANCE_CONFLICT" in flags) is (name == "Meera"), (name, flags)
    note = owner_rows("SELECT body FROM leave_messages WHERE leave_id = :i AND kind = 'SYSTEM_EVENT' ORDER BY id DESC "
                      "LIMIT 1", i=leaves["Sunita"])[0]["body"]
    assert note.startswith("Roster changed: charged days changed from 1 to 2")
    with tenant_db(cid) as db:
        items = time_inputs.pending_inputs(db, cid, meera["id"], THU, SAT)
        assert [(i["type"], i["id"]) for i in items] == [("leave_balance_conflict", leaves["Meera"])]
        assert time_inputs.pending_inputs(db, cid, sunita["id"], THU, SAT) == []
        # A rerun of the publication's recount posts nothing (the ledger already nets to the booked charge).
        count = db.query(LeaveBalanceTransaction).filter(LeaveBalanceTransaction.company_id == cid).count()
        leave_jobs.recount_dates(db, cid, [FRI], key=f"recalc:roster:{done['publication_id']}")
        assert db.query(LeaveBalanceTransaction).filter(LeaveBalanceTransaction.company_id == cid).count() == count

    # X-INT-56: the same final charge when the leave comes after the publication.
    late = _record(t, imran, THU, SAT)["id"]
    assert _leave(late)[0] == Decimal(2) and sum(d for d, _ in _uses(late)) == sum(
        d for d, _ in _uses(leaves["Sunita"])) == Decimal(-2)


def test_as_int_08_legacy_v1_recount_flips_the_date_and_debits_once(works):
    """LEGACY_V1 (no go-live): the same publication re-decides Fri through the legacy writer — the row becomes
    charged, one USE transaction ``leave_day:{row}:recount:1`` of -1.00, the request 2.00, no v2 version or flag; a
    rerun posts nothing."""
    from app.services import leave_jobs

    t = works
    cid, a, sunita = t["cid"], t["A"], t["Sunita"]
    _drafts(t, [_cell(sunita, THU, a), _cell(sunita, FRI, kind="OFF"), _cell(sunita, SAT, kind="OFF")])
    _publish_all(t)
    worker = invite_and_activate(t["app"], t["admin"], sunita, ["employee"])
    leave = ok(worker.post(f"{L}/apply", json={"leave_type": "casual", "start_date": str(THU), "end_date": str(SAT),
                                               "reason": "family"}))
    assert t["admin"].post(f"{L}/{leave['id']}/approve").status_code == 200
    assert _leave(leave["id"])[0] == Decimal(1)
    _drafts(t, [_cell(sunita, FRI, a)])
    dry = _publish(t, dry_run=True)
    done = _publish(t, **_settled(dry["results"]))
    units, version, flags, _ = _leave(leave["id"])
    assert (units, version, flags) == (Decimal(2), 1, [])
    fri_row = owner_rows("SELECT id, counts_against_balance FROM leave_days WHERE leave_id = :i AND date = :d "
                         "AND voided_at IS NULL", i=leave["id"], d=FRI)[0]
    assert fri_row["counts_against_balance"] is True
    recounts = owner_rows("SELECT delta, idempotency_key FROM leave_balance_transactions WHERE ref_type = 'leave_day' "
                          "AND ref_id = :r", r=fri_row["id"])
    assert [(Decimal(r["delta"]), r["idempotency_key"]) for r in recounts] == [
        (Decimal(-1), f"leave_day:{fri_row['id']}:recount:1")]
    with tenant_db(cid) as db:
        assert leave_jobs.recount_dates(db, cid, [FRI], key=f"recalc:roster:{done['publication_id']}")["changed"] == 0


# ------------------------------------------------------------------------------------------------ X-INT-56
def test_x_int_56_leave_approval_takes_the_employee_lock_and_times_out_without_writing(works):
    """LH side of X-INT-56 (LCK-02): approving (here: HR recording) leave locks the employee's attendance lock — the
    one a roster publication takes in ascending id before its period row. Held elsewhere for more than 5 s the
    approval answers 409 LOCK_TIMEOUT with nothing written; the retry succeeds."""
    from app.services.attendance_service import ATTENDANCE_LOCK_NAMESPACE

    t = works
    go_live(t)
    imran = t["Imran"]
    outcome = {}
    holder = system_engine.connect()
    held = holder.begin()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:ns, :id)"), {"ns": ATTENDANCE_LOCK_NAMESPACE,
                                                                        "id": imran["id"]})
        worker = threading.Thread(target=lambda: outcome.update(r=t["hr"].post(f"{L}/record", json={
            "leave_type": "casual", "start_date": str(THU), "end_date": str(SAT), "employee_id": imran["id"]})))
        worker.start()
        worker.join(30)
    finally:
        held.rollback()
        holder.close()
    worker.join(30)
    assert "r" in outcome, "the approval did not give up waiting for the lock"
    assert outcome["r"].status_code == 409 and outcome["r"].json()["code"] == "LOCK_TIMEOUT", outcome["r"].text
    assert owner_rows("SELECT count(*) AS n FROM leaves WHERE employee_id = :e", e=imran["id"])[0]["n"] == 0
    assert _leave(_record(t, imran, THU, SAT)["id"])[0] == Decimal(1)


# ------------------------------------------------------------------------------------------------ X-INT-58
def test_x_int_58_a_shift_version_recounts_the_half_and_full_day_leave_of_its_people(works):
    """Ramadan-style temporary version of A (06:00-12:00, 17-20 Nov). Layla (placement shift A) has FULL leave on
    Wed 18, FIRST_HALF on Thu 19 and FULL on Wed 25 Nov; Sunita (rostered WORK A 17-20 Nov) has FULL leave on Wed 18.
    The version's recount job ``recalc:shift:shift_version:{id}:{v}`` (employees + from, inside the version window)
    and the DIRECT re-versioning publication regenerate the requests on 17-20 Nov from the new scheduled minutes
    (480 -> 360; a half 240 -> 180): version + 1, rows re-written with the same day units (1.00 / 0.50), no ledger
    change; the 25 Nov request is left alone. HOUR-type leave: not built in leave v2 (not applicable)."""
    from app.payroll.services import time_inputs

    t = works
    go_live(t)
    admin, cid, a, sunita, layla = t["admin"], t["cid"], t["A"], t["Sunita"], t["Layla"]
    window = [D(2026, 11, 17) + timedelta(days=i) for i in range(4)]
    _drafts(t, [_cell(sunita, d, a) for d in window])
    _publish_all(t)
    wed, thu, later = D(2026, 11, 18), D(2026, 11, 19), D(2026, 11, 25)
    full = _record(t, layla, wed)["id"]
    half = _record(t, layla, thu, thu, end_portion="FIRST_HALF")["id"]
    outside = _record(t, layla, later)["id"]
    rostered = _record(t, sunita, wed)["id"]
    assert _charged(half) == {thu: ("FIRST_HALF", Decimal("0.5"), True)}
    assert _fact(cid, layla["id"], wed).scheduled_minutes == 480
    ledger_before = {i: _uses(i) for i in (full, half, outside, rostered)}
    versions_before = {i: _leave(i)[1] for i in (full, half, outside, rostered)}

    r = admin.post(f"/api/v1/shifts/{a}/versions", json={
        "effective_from": "2026-11-17", "effective_to": "2026-11-20", "start_time": "06:00", "end_time": "12:00",
        "reason": "Ramadan hours"})
    assert r.status_code == 201, r.text
    effects = r.json()["effects"]
    assert effects["leave_recalculation_queued"] is True and effects["roster"]["entries_reversioned"] == 4
    job = owner_rows("SELECT idempotency_key, status, result, payload FROM jobs WHERE company_id = :c AND type = "
                     "'leave.recalculate' AND payload->>'source_kind' = 'shift_version'", c=cid)
    assert len(job) == 1 and job[0]["status"] == "SUCCEEDED", job
    assert job[0]["idempotency_key"].startswith("recalc:shift:shift_version:")
    assert sorted(job[0]["payload"]["employee_ids"]) == sorted([layla["id"], sunita["id"]])
    assert job[0]["result"]["dates"] == 4  # clamped to the version's own window

    for leave_id in (full, half, rostered):
        assert _leave(leave_id)[1] > versions_before[leave_id], leave_id  # regenerated
        assert _uses(leave_id) == ledger_before[leave_id], leave_id  # same day units: no ledger change
    assert _leave(outside)[1] == versions_before[outside] and _uses(outside) == ledger_before[outside]
    assert _charged(full) == {wed: ("FULL", Decimal(1), True)}
    assert _charged(half) == {thu: ("FIRST_HALF", Decimal("0.5"), True)}
    assert _charged(rostered) == {wed: ("FULL", Decimal(1), True)}
    voided = owner_rows("SELECT count(*) AS n FROM leave_days WHERE leave_id = :i AND void_reason = 'CHANGED'", i=half)
    assert voided[0]["n"] >= 1
    assert _fact(cid, layla["id"], wed).scheduled_minutes == 360
    assert _fact(cid, sunita["id"], wed).scheduled_minutes == 360
    with tenant_db(cid) as db:
        expected = {e.date: e for e in time_inputs.expected_presence(db, cid, [layla["id"]], thu, thu)[layla["id"]]}
    assert (expected[thu].expected_portion, expected[thu].expected_minutes) == ("SECOND_HALF", 180)
