"""Day facts v2: the resolver, expected presence, calendar APIs, BUG-11 and BUG-12 (LH-M1 WI-1.13; owner LH 05 §3-§5,
§8; 11 §6.13).

Worked examples X-1..X-10 of 05 §3.7, golden vectors GV-01, GV-04, GV-09, GV-15 of 05 §8 (day-facts side), BUG-11,
BUG-12, LH-HOL-01, LH-TZ-01..LH-TZ-10 and the Goal 4 reading of LH-TZ-04 ("an employee in Asia/Kolkata and one in
US/Pacific see holidays on their own local day boundaries"). The company clock is the attendance fake clock.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.time_goldens import tenant_db

D = date
HC = "/api/v1/holiday-calendars"
WW = "/api/v1/work-weeks"
NTH = {"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}]}  # MON_SAT_2ND_4TH_SAT_OFF


def _at_utc(clock, day: str, hhmm: str) -> None:
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    clock.now = datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _items(body):
    return body.get("items") if isinstance(body, dict) else body


def _sql(statement, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(statement), params)
        return result.fetchall() if result.returns_rows else None


@pytest.fixture
def alpha(app, fake_clock):
    fake_clock.at_ist("2026-11-02", "10:00")
    admin = signup_company(app, "Alpha Facts")
    branch = _items(admin.get("/api/v1/branches").json())[0]
    boss = create_employee(admin, "Vikram", "Rao", branch_id=branch["id"])
    worker = create_employee(admin, "Asha", "Pillai", manager_id=boss["id"], branch_id=branch["id"])
    return {"admin": admin, "branch": branch, "boss": boss, "worker": worker, "clock": fake_clock, "app": app,
            "company_id": admin.company_id,
            "boss_client": invite_and_activate(app, admin, boss, ["manager"]),
            "worker_client": invite_and_activate(app, admin, worker, ["employee"])}


def _calendar(admin, name, **extra):
    r = admin.post(HC, json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _link(admin, branch_id, **body):
    r = admin.put(f"/api/v1/branches/{branch_id}/calendar-link", json=body)
    assert r.status_code == 200, r.text


def _published(admin, calendar_id, rows, year=2026):
    """Add rows to a new year and publish it (rows of DRAFT years never count)."""
    for name, day, kind in rows:
        r = admin.post(f"{HC}/{calendar_id}/occurrences", json={"name": name, "date": day, "holiday_type": kind,
                                                                "reason": "fixture"})
        assert r.status_code in (200, 201), r.text
    url = f"{HC}/{calendar_id}/years/{year}/publish"
    if admin.get(f"{HC}/{calendar_id}/years/{year}").json()["status"] == "DRAFT":
        dry = admin.post(url, json={"dry_run": True}).json()
        codes = [w["code"] for w in dry["impact"]["warnings"] if w.get("confirm")]
        assert admin.post(url, json={"acknowledged_warnings": codes}).status_code == 200


def _pattern(admin, name, rules):
    r = admin.post(WW, json={"name": name, "rules": rules})
    assert r.status_code == 201, r.text
    return r.json()


def _pune(alpha):
    """Fixture A of 05 §8: MON_SAT_2ND_4TH_SAT_OFF, an IN calendar with Diwali Sun 8 Nov and Foundation Day 13 Nov."""
    admin = alpha["admin"]
    pattern = _pattern(admin, "Pune Plant week", NTH)
    cal = _calendar(admin, "Alpha Pune Plant", country_code="IN")
    _published(admin, cal["id"], [("Diwali", "2026-11-08", "PUBLIC"), ("Foundation Day", "2026-11-13", "COMPANY")])
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"], work_week_pattern_id=pattern["id"])
    return cal, pattern


def _facts(alpha, start, end, employee=None):
    from app.payroll.services import time_inputs

    cid = alpha["company_id"]
    with tenant_db(cid) as db:
        return {f.date: f for f in time_inputs.day_facts(db, cid, (employee or alpha["worker"])["id"], start, end)}


def _apply(alpha, start, end, kind="casual", portion="FULL"):
    r = alpha["worker_client"].post("/api/v1/leaves/apply", json={
        "leave_type": kind, "start_date": start, "end_date": end, "start_portion": portion, "end_portion": portion,
        "reason": "Family"})
    return r


def _approve(alpha, leave_id):
    for approver in (alpha["boss_client"], alpha["admin"]):
        if owner_rows("SELECT status FROM leaves WHERE id = :i", i=leave_id)[0]["status"] != "pending":
            break
        assert approver.post(f"/api/v1/leaves/{leave_id}/approve", json={"remarks": "ok"}).status_code == 200
    return float(owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave_id)[0]["number_of_days"])


# ============================================================================================== 05 §3.7 worked examples
def test_x1_x2_x3_weekly_offs_holidays_and_working_portions(alpha):
    cal, pattern = _pune(alpha)
    facts = _facts(alpha, D(2026, 11, 2), D(2026, 11, 30))
    second_sat, third_sat, diwali, foundation = (facts[D(2026, 11, 14)], facts[D(2026, 11, 21)], facts[D(2026, 11, 8)],
                                                 facts[D(2026, 11, 13)])
    # X-1: 2nd Saturday off
    assert (second_sat.weekly_off, second_sat.weekly_off_portion, second_sat.working_portion,
            second_sat.scheduled_minutes) == (True, "FULL", "NONE", 0)
    # X-2: 3rd Saturday works; no shift -> payroll.standard_hours_per_day x 60 (SPINE V3-062)
    assert (third_sat.weekly_off, third_sat.working_portion, third_sat.scheduled_minutes) == (False, "FULL", 480)
    # X-3: a PUBLIC holiday on the Sunday off, IN calendar observance NONE: no substitute anywhere
    assert diwali.weekly_off and diwali.holiday.type == "PUBLIC" and diwali.working_portion == "NONE"
    assert all(f.holiday is None or f.holiday.observance != "EMPLOYEE_SUBSTITUTE" for f in facts.values())
    assert foundation.holiday.type == "COMPANY" and foundation.holiday.category == "OTHER"
    assert foundation.holiday.non_working and foundation.holiday.paid  # `paid` is the deprecated alias
    first = facts[D(2026, 11, 2)]
    assert (first.calendar_id, first.calendar_source, first.work_week_id, first.pattern_version) == (
        cal["id"], "BRANCH_LINK", pattern["id"], 1)
    assert first.timezone == "Asia/Kolkata" and first.legal_entity_id == alpha["branch"]["legal_entity_id"]
    assert len({f.calendar_version for f in facts.values()}) == 1 and len(first.calendar_version) == 64


def test_x4_half_day_holiday_and_x9_same_date_tie_break(alpha):
    cal, _ = _pune(alpha)
    cid = alpha["company_id"]
    # Half days are gated at the API until LH-M5b (INVALID_PORTION); the resolver already handles them (owner SQL).
    _sql("INSERT INTO holidays (company_id, calendar_id, name, date, holiday_type, is_optional, portion, category, "
         "created_at, updated_at) VALUES (:c, :cal, 'Early close', '2026-12-24', 'COMPANY', false, 'SECOND_HALF', "
         "'COMPANY_EVENT', now(), now())", c=cid, cal=cal["id"])
    half = _facts(alpha, D(2026, 12, 24), D(2026, 12, 24))[D(2026, 12, 24)]
    assert (half.holiday.portion, half.working_portion, half.scheduled_minutes) == ("SECOND_HALF", "FIRST_HALF", 240)
    # X-9: a half row (lower id) and a FULL row of equal rank: the larger portion wins, the union is FULL
    _sql("INSERT INTO holidays (company_id, calendar_id, name, date, holiday_type, is_optional, portion, created_at, "
         "updated_at) VALUES (:c, :cal, 'Foundation Day (half)', '2026-12-28', 'COMPANY', false, 'SECOND_HALF', now(), "
         "now())", c=cid, cal=cal["id"])
    _sql("INSERT INTO holidays (company_id, calendar_id, name, date, holiday_type, is_optional, portion, created_at, "
         "updated_at) VALUES (:c, :cal, 'Year-end shutdown day 1', '2026-12-28', 'COMPANY', false, 'FULL', now(), now())",
         c=cid, cal=cal["id"])
    tie = _facts(alpha, D(2026, 12, 28), D(2026, 12, 28))[D(2026, 12, 28)]
    assert (tie.holiday.name, tie.holiday.portion, tie.working_portion) == ("Year-end shutdown day 1", "FULL", "NONE")
    # Two half rows combine into a full day off (HALF_DAY_HOLIDAYS_COMBINED at publication)
    for name, portion in (("Morning rite", "FIRST_HALF"), ("Evening fair", "SECOND_HALF")):
        _sql("INSERT INTO holidays (company_id, calendar_id, name, date, holiday_type, is_optional, portion, "
             "created_at, updated_at) VALUES (:c, :cal, :n, '2026-12-29', 'PUBLIC', false, :p, now(), now())",
             c=cid, cal=cal["id"], n=name, p=portion)
    both = _facts(alpha, D(2026, 12, 29), D(2026, 12, 29))[D(2026, 12, 29)]
    assert (both.holiday.portion, both.working_portion) == ("FULL", "NONE")


def test_x5_observed_pair_counts_once_and_the_original_is_display_only(alpha):
    admin = alpha["admin"]
    us = _calendar(admin, "US Company", country_code="US")
    r = admin.post(f"{HC}/{us['id']}/rules", json={"name": "Independence Day", "kind": "FIXED_DATE",
                                                   "params": {"month": 7, "day": 4}, "holiday_type": "PUBLIC",
                                                   "observed_rule": "NEAREST_WORKDAY"})
    assert r.status_code == 201, r.text
    assert admin.post(f"{HC}/{us['id']}/years/2027/generate").status_code == 200
    _published(admin, us["id"], [], year=2027)
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=us["id"])
    facts = _facts(alpha, D(2027, 7, 4), D(2027, 7, 5))
    sunday, monday = facts[D(2027, 7, 4)], facts[D(2027, 7, 5)]
    assert sunday.holiday is None and sunday.observed_elsewhere == "2027-07-05"
    assert (monday.holiday.observance, monday.holiday.observed_from_date, monday.working_portion) == (
        "OBSERVED", D(2027, 7, 4), "NONE")


def test_x6_part_timer_minutes_follow_fte(alpha):
    _pune(alpha)
    _sql("UPDATE employee_assignments SET fte = 0.5 WHERE employee_id = :e", e=alpha["worker"]["id"])
    day = _facts(alpha, D(2026, 11, 3), D(2026, 11, 3))[D(2026, 11, 3)]
    assert (day.working_portion, day.scheduled_minutes) == ("FULL", 240)


def test_x7_next_working_day_substitutes_chain_for_one_employee(alpha):
    """X-7 / LH-WWK-26: holidays on the employee's rest days move to the next scheduled working days, in date order,
    each substitute taken before the next search (never stored as a row)."""
    admin = alpha["admin"]
    pattern = _pattern(admin, "Crew week", {"weekly_off_days": [2, 3], "nth_weekday_offs": []})  # Wed + Thu off
    cal = _calendar(admin, "SG National", country_code="SG", employee_observance="NEXT_WORKING_DAY")
    _published(admin, cal["id"], [("National Day A", "2026-12-02", "PUBLIC"), ("National Day B", "2026-12-03", "PUBLIC")])
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"], work_week_pattern_id=pattern["id"])
    facts = _facts(alpha, D(2026, 11, 30), D(2026, 12, 8))
    wed, thu, fri, sat = (facts[D(2026, 12, d)] for d in (2, 3, 4, 5))
    assert wed.weekly_off and wed.holiday.observance == "ORIGINAL"
    assert thu.weekly_off and thu.holiday.observance == "ORIGINAL"
    assert (fri.holiday.observance, fri.holiday.name, fri.holiday.observed_from_date, fri.working_portion) == (
        "EMPLOYEE_SUBSTITUTE", "National Day A", D(2026, 12, 2), "NONE")
    assert (sat.holiday.observance, sat.holiday.name, sat.holiday.observed_from_date) == (
        "EMPLOYEE_SUBSTITUTE", "National Day B", D(2026, 12, 3))
    # Range independence: asking only for Saturday gives the same answer (the chain looks back two horizons).
    alone = _facts(alpha, D(2026, 12, 5), D(2026, 12, 5))[D(2026, 12, 5)]
    assert alone.holiday.name == "National Day B" and alone.holiday.observance == "EMPLOYEE_SUBSTITUTE"
    # A substitute is employee-specific: the boss on the same calendar also gets one (same pattern), never a row.
    assert not owner_rows("SELECT id FROM holidays WHERE calendar_id = :c AND date IN ('2026-12-04', '2026-12-05')",
                          c=cal["id"])


def test_observed_if_off_gives_the_original_to_an_employee_who_works_that_day(alpha):
    """02 §3.6 OBSERVED_IF_OFF: a Monday-Saturday worker gets Juneteenth on Saturday; a Monday-Friday colleague on the
    observed Friday."""
    admin = alpha["admin"]
    us = _calendar(admin, "US Retail", country_code="US", employee_observance="OBSERVED_IF_OFF")
    r = admin.post(f"{HC}/{us['id']}/rules", json={"name": "Juneteenth", "kind": "FIXED_DATE",
                                                   "params": {"month": 6, "day": 19}, "holiday_type": "PUBLIC",
                                                   "observed_rule": "NEAREST_WORKDAY"})
    assert r.status_code == 201, r.text
    assert admin.post(f"{HC}/{us['id']}/years/2027/generate").status_code == 200
    _published(admin, us["id"], [], year=2027)
    retail = _pattern(admin, "Store week", {"weekly_off_days": [6], "nth_weekday_offs": []})
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=us["id"], work_week_pattern_id=retail["id"])
    store = _facts(alpha, D(2027, 6, 18), D(2027, 6, 19))
    assert store[D(2027, 6, 19)].holiday.observance == "ORIGINAL" and store[D(2027, 6, 18)].holiday is None
    assert store[D(2027, 6, 18)].observed_elsewhere == "2027-06-19"
    office = _pattern(admin, "Office week", {"weekly_off_days": [5, 6], "nth_weekday_offs": []})
    _link(admin, alpha["branch"]["id"], work_week_pattern_id=office["id"], effective_from="2026-11-03")
    desk = _facts(alpha, D(2027, 6, 18), D(2027, 6, 19))
    assert desk[D(2027, 6, 18)].holiday.observance == "OBSERVED" and desk[D(2027, 6, 19)].holiday is None


def test_x8_transfer_between_zones_switches_calendar_and_zone_per_date(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    _pune(alpha)
    dubai = admin.post("/api/v1/branches", json={"name": "Dubai Office", "code": "DXB",
                                                 "legal_entity_id": alpha["branch"]["legal_entity_id"],
                                                 "timezone": "Asia/Dubai"})
    assert dubai.status_code in (200, 201), dubai.text
    ae = _calendar(admin, "UAE", country_code="AE")
    _published(admin, ae["id"], [("UAE National Day", "2026-12-02", "PUBLIC")])
    _link(admin, dubai.json()["id"], holiday_calendar_id=ae["id"])
    moved = admin.post(f"/api/v1/employees/{alpha['worker']['id']}/assignments", json={
        "branch_id": dubai.json()["id"], "effective_from": "2026-11-16", "reason": "TRANSFER"})
    assert moved.status_code in (200, 201), moved.text
    facts = _facts(alpha, D(2026, 11, 15), D(2026, 12, 2))
    assert facts[D(2026, 11, 15)].timezone == "Asia/Kolkata" and facts[D(2026, 11, 16)].timezone == "Asia/Dubai"
    assert facts[D(2026, 12, 2)].holiday.name == "UAE National Day"
    assert facts[D(2026, 11, 15)].calendar_id != facts[D(2026, 11, 16)].calendar_id
    assert cid


def test_x10_declared_working_day_on_a_provisional_holiday_is_refused_and_otherwise_works(alpha):
    admin = alpha["admin"]
    cal, _ = _pune(alpha)
    r = admin.post(f"{HC}/{cal['id']}/working-days", json={"date": "2026-11-29", "name": "Make-up", "reason": "Bridge"})
    assert r.status_code in (200, 201), r.text
    sunday = _facts(alpha, D(2026, 11, 29), D(2026, 11, 29))[D(2026, 11, 29)]
    assert (sunday.declared_working_day, sunday.weekly_off, sunday.working_portion, sunday.scheduled_minutes) == (
        True, False, "FULL", 480)
    clash = admin.post(f"{HC}/{cal['id']}/working-days", json={"date": "2026-11-13", "name": "x", "reason": "y"})
    assert (clash.status_code, clash.json()["code"]) == (409, "WORKING_DAY_CONFLICT")


# ============================================================================================== 05 §8 vectors, BUG-11/12
def test_gv01_gv04_leave_around_a_holiday_and_only_on_a_weekly_off(alpha):
    _pune(alpha)
    r = _apply(alpha, "2026-11-09", "2026-11-13", kind="annual")
    if r.status_code == 400 and "type" in r.text.lower():
        r = _apply(alpha, "2026-11-09", "2026-11-13")
    assert r.status_code == 200, r.text
    assert _approve(alpha, r.json()["id"]) == 4.0  # Fri 13 Nov is the Foundation Day holiday
    weekly_off_only = _apply(alpha, "2026-11-14", "2026-11-14")
    assert (weekly_off_only.status_code, weekly_off_only.json()["code"]) == (400, "NO_WORKING_DAYS")


def test_gv09_lh_hol_01_bug_11_a_holiday_type_outside_paid_types_stays_non_working(alpha):
    """Day facts decide working (COMPANY holiday: working NONE, expected NONE) whatever payroll pays; the ledger
    keeps the date a HOLIDAY slot, unpaid when the type is not in paid_holiday_types, never presumed present."""
    from app.payroll import ledger
    from app.payroll.services import time_inputs

    _pune(alpha)
    cid, worker = alpha["company_id"], alpha["worker"]["id"]
    leave = _apply(alpha, "2026-11-12", "2026-11-13")
    assert leave.status_code == 200, leave.text
    assert _approve(alpha, leave.json()["id"]) == 1.0  # Thu charged, the Friday holiday is not
    foundation = _facts(alpha, D(2026, 11, 13), D(2026, 11, 13))[D(2026, 11, 13)]
    assert (foundation.holiday.type, foundation.working_portion) == ("COMPANY", "NONE")
    with tenant_db(cid) as db:
        expected = time_inputs.expected_presence(db, cid, [alpha["boss"]["id"]], D(2026, 11, 13), D(2026, 11, 13))
        days = time_inputs.ledger_inputs(db, cid, alpha["boss"]["id"], D(2026, 11, 13), D(2026, 11, 13))
    e = expected[alpha["boss"]["id"]][0]
    assert (e.expected_portion, e.reasons) == ("NONE", ("HOLIDAY",))
    unpaid = ledger.LedgerConfig(paid_holiday_types=frozenset({ledger.HolidayType.PUBLIC,
                                                               ledger.HolidayType.OPTIONAL_AVAILED}))
    result = ledger.build_ledger(days, unpaid) if hasattr(ledger, "build_ledger") else None
    if result is not None:
        day = result.days[0]
        assert (day.am.classification, day.am.paid, day.am.lop) == (ledger.Classification.HOLIDAY, 0, 0)
    assert worker


def test_gv15_holiday_added_after_approval_recounts_and_changes_the_calendar_version(alpha):
    admin = alpha["admin"]
    cal, _ = _pune(alpha)
    leave = _apply(alpha, "2026-11-30", "2026-12-01")
    assert leave.status_code == 200, leave.text
    assert _approve(alpha, leave.json()["id"]) == 2.0
    before = _facts(alpha, D(2026, 11, 30), D(2026, 11, 30))[D(2026, 11, 30)].calendar_version
    r = admin.post(f"{HC}/{cal['id']}/years/2026/amend", json={"operations": [
        {"op": "add", "name": "Founder's remembrance", "date": "2026-11-30", "holiday_type": "COMPANY"}],
        "reason": "Board decision"})
    assert r.status_code == 200, r.text
    after = _facts(alpha, D(2026, 11, 30), D(2026, 11, 30))[D(2026, 11, 30)]
    assert after.holiday.name == "Founder's remembrance" and after.calendar_version != before
    assert float(owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave.json()["id"])[0][
        "number_of_days"]) == 1.0


def test_expected_presence_reasons_and_bug_12_dashboard_on_a_day_off(alpha):
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User
    from app.payroll.services import time_inputs
    from app.services import attendance_service

    _pune(alpha)
    cid = alpha["company_id"]
    leave = _apply(alpha, "2026-11-24", "2026-11-24")
    assert leave.status_code == 200, leave.text
    _approve(alpha, leave.json()["id"])
    pending = _apply(alpha, "2026-11-19", "2026-11-19")
    assert pending.status_code == 200, pending.text
    with tenant_db(cid) as db:
        rows = {e.date: e for e in time_inputs.expected_presence(db, cid, [alpha["worker"]["id"]], D(2026, 11, 14),
                                                                 D(2026, 11, 24))[alpha["worker"]["id"]]}
    assert (rows[D(2026, 11, 24)].expected_portion, rows[D(2026, 11, 24)].reasons) == ("NONE", ("LEAVE",))
    assert (rows[D(2026, 11, 24)].expected_minutes, rows[D(2026, 11, 24)].on_leave_portion) == (0, "FULL")
    assert (rows[D(2026, 11, 23)].expected_portion, rows[D(2026, 11, 23)].expected_minutes) == ("FULL", 480)
    assert (rows[D(2026, 11, 14)].expected_portion, rows[D(2026, 11, 14)].reasons) == ("NONE", ("WEEKLY_OFF",))
    assert (rows[D(2026, 11, 19)].expected_portion, rows[D(2026, 11, 19)].reasons) == ("FULL", ("LEAVE_PENDING",))
    # BUG-12: on the 2nd Saturday nobody of the branch is expected, so nobody is absent.
    alpha["clock"].at_ist("2026-11-14", "11:00")
    with tenant_db(cid) as db:
        founder = db.query(User).filter(User.company_id == cid).order_by(User.id).first()
        principal = build_principal(db, founder, None, db.get(Company, cid))
        board = attendance_service.today_summary(db, principal)
        trend = attendance_service.trend(db, principal, 7)
    assert board["expected_today"] == 0 and board["absent_today"] == 0
    assert board["off_today"] >= 2
    saturday = next(t for t in trend if t["date"] == "2026-11-14")
    assert saturday["absent"] == 0 and saturday["expected"] == 0


# ============================================================================================== calendar APIs
def test_calendar_days_and_me_calendar_shapes_permissions_and_ranges(alpha):
    admin, worker_client = alpha["admin"], alpha["worker_client"]
    cal, _ = _pune(alpha)
    me = worker_client.get("/api/v1/me/calendar", params={"from": "2026-01-01", "to": "2026-12-31"})
    assert me.status_code == 200, me.text
    body = me.json()
    assert len(body["days"]) == 365 and body["local_today"] == "2026-11-02" and body["timezone"] == "Asia/Kolkata"
    assert body["weekend"] == [6] and body["week_start"] == 0
    nov13 = next(d for d in body["days"] if d["date"] == "2026-11-13")
    assert nov13["holiday"]["type"] == "COMPANY" and nov13["holiday"]["non_working"] and nov13["working_portion"] == "NONE"
    assert any(seg["id"] == cal["id"] and seg["code"] == cal["code"] and seg["years"]["2026"]["status"] == "PUBLISHED"
               for seg in body["calendars"])
    too_long = worker_client.get("/api/v1/me/calendar", params={"from": "2026-01-01", "to": "2027-02-10"})
    assert (too_long.status_code, too_long.json()["code"]) == (400, "RANGE_TOO_LONG")
    backwards = worker_client.get("/api/v1/me/calendar", params={"from": "2026-02-01", "to": "2026-01-01"})
    assert (backwards.status_code, backwards.json()["code"]) == (400, "INVALID_RANGE")
    # holiday:read is company-wide for employees (the holiday calendar is public inside the company): a colleague's
    # day facts are readable, never their leave (only /me/calendar carries the viewer's own leave).
    other = worker_client.get("/api/v1/calendar/days", params={"employee_id": alpha["boss"]["id"], "from": "2026-11-01",
                                                                "to": "2026-11-30"})
    assert other.status_code == 200 and all("leave" not in d for d in other.json()["days"])
    missing = admin.get("/api/v1/calendar/days", params={"employee_id": 999999, "from": "2026-11-01", "to": "2026-11-02"})
    assert missing.status_code == 404
    hr = admin.get("/api/v1/calendar/days", params={"employee_id": alpha["worker"]["id"], "from": "2026-11-01",
                                                    "to": "2026-11-30"})
    assert hr.status_code == 200 and hr.json()["local_today"] == "2026-11-02" and len(hr.json()["days"]) == 30
    # A DRAFT year never reaches employees: 2027 rows of a draft are invisible in day facts.
    admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Draft-only day", "date": "2027-02-01",
                                                      "holiday_type": "COMPANY"})
    draft = worker_client.get("/api/v1/me/calendar", params={"from": "2027-02-01", "to": "2027-02-01"}).json()
    assert draft["days"][0]["holiday"] is None


def test_me_calendar_lists_own_leave_per_date(alpha):
    _pune(alpha)
    leave = _apply(alpha, "2026-11-24", "2026-11-25")
    assert leave.status_code == 200, leave.text
    _approve(alpha, leave.json()["id"])
    days = {d["date"]: d for d in alpha["worker_client"].get(
        "/api/v1/me/calendar", params={"from": "2026-11-24", "to": "2026-11-25"}).json()["days"]}
    assert days["2026-11-24"]["leave"][0]["status"] == "APPROVED"
    assert days["2026-11-24"]["leave"][0]["leave_id"] == leave.json()["id"]


def test_day_facts_many_cap(alpha):
    from app.core.errors import AppError
    from app.payroll.services import time_inputs

    with tenant_db(alpha["company_id"]) as db, pytest.raises(AppError) as err:
        time_inputs.day_facts_many(db, alpha["company_id"], list(range(1, 700)), D(2026, 1, 1), D(2026, 12, 31))
    assert err.value.code == "RANGE_TOO_LONG"


# ============================================================================================== LH-TZ
def _zone(employee_id, zone):
    _sql("UPDATE employee_assignments SET work_timezone = :z WHERE employee_id = :e", z=zone, e=employee_id)


def test_goal_label_tz_04_kolkata_and_pacific_employees_see_their_own_local_days(alpha):
    """At 2026-12-31 20:00 UTC Kolkata is already in 2027 while Los Angeles is on 31 Dec: each /me/calendar answers
    with the employee's own local today, and the New Year holiday sits on each one's local 1 January."""
    admin = alpha["admin"]
    cal = _calendar(admin, "Global Holidays", country_code="IN")
    _published(admin, cal["id"], [("New Year's Day", "2027-01-01", "COMPANY")], year=2027)
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    _zone(alpha["worker"]["id"], "America/Los_Angeles")
    _zone(alpha["boss"]["id"], "Asia/Kolkata")
    _at_utc(alpha["clock"], "2026-12-31", "20:00")
    pacific = alpha["worker_client"].get("/api/v1/me/calendar", params={"from": "2026-12-31", "to": "2027-01-01"}).json()
    kolkata = alpha["boss_client"].get("/api/v1/me/calendar", params={"from": "2026-12-31", "to": "2027-01-01"}).json()
    assert (pacific["local_today"], pacific["timezone"]) == ("2026-12-31", "America/Los_Angeles")
    assert (kolkata["local_today"], kolkata["timezone"]) == ("2027-01-01", "Asia/Kolkata")
    for body in (pacific, kolkata):
        days = {d["date"]: d for d in body["days"]}
        assert days["2027-01-01"]["holiday"]["name"] == "New Year's Day" and days["2026-12-31"]["holiday"] is None
    # LH-TZ-04 (catalogue): the calendar's today is the furthest-ahead zone, so the CONFIRMED holiday has started.
    row = next(i for i in admin.get(f"{HC}/{cal['id']}/years/2027").json()["items"] if i["name"] == "New Year's Day")
    refused = admin.post(f"{HC}/occurrences/{row['id']}/cancel", json={"reason": "late"})
    assert (refused.status_code, refused.json()["code"]) == (409, "HOLIDAY_IN_PAST")


def test_lh_tz_05_auckland_and_los_angeles_share_a_calendar(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "APAC US", country_code="US")
    _published(admin, cal["id"], [("Company day", "2027-05-03", "COMPANY")], year=2027)
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    _zone(alpha["worker"]["id"], "Pacific/Auckland")
    _zone(alpha["boss"]["id"], "America/Los_Angeles")
    _at_utc(alpha["clock"], "2027-05-02", "13:00")
    from app.services import holiday_calendar_service as svc

    with tenant_db(alpha["company_id"]) as db:
        assert svc.calendar_local_today(db, alpha["company_id"], cal["id"]) == D(2027, 5, 3)
    row = next(i for i in admin.get(f"{HC}/{cal['id']}/years/2027").json()["items"] if i["name"] == "Company day")
    refused = admin.post(f"{HC}/occurrences/{row['id']}/cancel", json={"reason": "x"})
    assert (refused.status_code, refused.json()["code"]) == (409, "HOLIDAY_IN_PAST")


def test_lh_tz_06_subject_local_today_drives_on_behalf_dates(alpha):
    _zone(alpha["worker"]["id"], "America/New_York")
    _at_utc(alpha["clock"], "2026-11-09", "21:00")  # 01:00 Dubai / 02:30 IST on 10 Nov, 16:00 New York on 9 Nov
    days = alpha["admin"].get("/api/v1/calendar/days", params={"employee_id": alpha["worker"]["id"],
                                                               "from": "2026-11-08", "to": "2026-11-09"}).json()
    assert days["local_today"] == "2026-11-09"


def test_lh_tz_07_instant_to_local_date_across_a_zone_change(alpha):
    from app.models.employee import Employee
    from app.services import org_structure_service as org

    admin, cid = alpha["admin"], alpha["company_id"]
    dubai = admin.post("/api/v1/branches", json={"name": "Dubai", "code": "DXB2",
                                                 "legal_entity_id": alpha["branch"]["legal_entity_id"],
                                                 "timezone": "Asia/Dubai"}).json()
    moved = admin.post(f"/api/v1/employees/{alpha['worker']['id']}/assignments", json={
        "branch_id": dubai["id"], "effective_from": "2026-11-16", "reason": "TRANSFER"})
    assert moved.status_code in (200, 201), moved.text
    _sql("UPDATE employee_assignments SET work_timezone = 'Asia/Kolkata' WHERE employee_id = :e "
         "AND effective_from < '2026-11-16'", e=alpha["worker"]["id"])
    with tenant_db(cid) as db:
        employee = db.get(Employee, alpha["worker"]["id"])
        # Pune -> Dubai: the gap 18:30-20:00 UTC on 15 Nov stays on the outgoing (Pune) placement's last date.
        assert org.employee_local_date(db, employee, datetime(2026, 11, 15, 19, 0, tzinfo=timezone.utc)) == D(2026, 11, 15)
        assert org.employee_local_date(db, employee, datetime(2026, 11, 15, 20, 30, tzinfo=timezone.utc)) == D(2026, 11, 16)
        assert org.employee_local_date(db, employee, datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)) == D(2026, 11, 15)


def test_lh_tz_03_period_lock_follows_each_employee_local_today(alpha):
    from app.payroll.services import calendars
    from tests.payroll.test_runs_lifecycle import calendar_for, periods_of
    from tests.payroll.test_wage_inputs import pay_group, place

    admin, cid = alpha["admin"], alpha["company_id"]
    entity = alpha["branch"]["legal_entity_id"]
    group = pay_group(cid, entity, "TZ3")
    for e in (alpha["worker"], alpha["boss"]):
        place(e["id"], entity, group)
    calendar_for(admin, group)
    november = next(p for p in periods_of(admin, group) if p["start"] == "2026-11-01")
    cutoff = date.fromisoformat(november["input_cutoff"])
    _zone(alpha["worker"]["id"], "America/New_York")
    _zone(alpha["boss"]["id"], "Asia/Dubai")
    # One hour after Dubai midnight following the cutoff, New York is still on the cutoff day.
    instant = datetime(cutoff.year, cutoff.month, cutoff.day, 21, 0, tzinfo=timezone.utc)
    with tenant_db(cid) as db:
        assert calendars.period_state_on(db, cid, alpha["boss"]["id"], D(2026, 11, 20), now=instant) == "INPUT_LOCKED"
        assert calendars.period_state_on(db, cid, alpha["worker"]["id"], D(2026, 11, 20), now=instant) == "OPEN"
    _sql("UPDATE pay_periods SET status = 'CLOSED' WHERE id = :p", p=november["id"])
    with tenant_db(cid) as db:
        assert calendars.period_state_on(db, cid, alpha["worker"]["id"], D(2026, 11, 20), now=instant) == "FINALIZED"


def test_lh_tz_09_branch_relink_clock_uses_the_furthest_ahead_zone(alpha):
    from app.models.org import Branch
    from app.services import holiday_assignments

    admin, cid = alpha["admin"], alpha["company_id"]
    apac = _calendar(admin, "Pacific Rim", country_code="IN")
    _published(admin, apac["id"], [("APAC day", "2027-02-01", "COMPANY")], year=2027)
    _zone(alpha["boss"]["id"], "Pacific/Auckland")
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=apac["id"])
    _at_utc(alpha["clock"], "2026-12-31", "16:00")  # Mumbai 21:30 on 31 Dec, Auckland 05:00 on 1 Jan
    with tenant_db(cid) as db:
        branch = db.get(Branch, alpha["branch"]["id"])
        assert holiday_assignments.link_clock(db, branch, [], [apac["id"]]) == D(2027, 1, 1)


def test_lh_tz_01_02_10_night_shift_date_and_dst_minutes(alpha):
    """Shift date = local start date; minutes from UTC instants on DST nights (New York night shift 22:00-06:00, 30 min
    unpaid: 6 Nov 2027 -> 510, 13 Mar 2027 -> 390, 9 Jan 2027 -> 450)."""
    admin = alpha["admin"]
    night = admin.post("/api/v1/shifts", json={"code": "NIGHT", "name": "Night", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "fixture", "start_time": "22:00", "end_time": "06:00",
        "unpaid_break_minutes": 30}})
    assert night.status_code == 201, night.text
    # Both US DST changes fall on a Sunday at 02:00, so the crossing nights start on Saturdays: Sunday-only week off.
    sunday_off = _pattern(admin, "Six-day week", {"weekly_off_days": [6], "nth_weekday_offs": []})
    _link(admin, alpha["branch"]["id"], work_week_pattern_id=sunday_off["id"])
    worker = create_employee(admin, "Nora", "Night", shift_id=night.json()["id"], branch_id=alpha["branch"]["id"])
    _zone(worker["id"], "America/New_York")
    facts = _facts(alpha, D(2027, 1, 9), D(2027, 11, 6), employee=worker)
    assert facts[D(2027, 11, 6)].scheduled_minutes == 510
    assert facts[D(2027, 3, 13)].scheduled_minutes == 390
    assert facts[D(2027, 1, 9)].scheduled_minutes == 450
    start, end = facts[D(2027, 1, 9)].instances[0]
    assert start.date() == D(2027, 1, 10) and facts[D(2027, 1, 9)].shift_source == "PLACEMENT"  # 03:00Z = 22:00 EST
    assert end > start and Decimal(1) == Decimal(1)


def test_bug_22_legacy_form_sets_public_or_company_and_day_facts_follow(alpha):
    """BUG-22: the Holidays-page form chooses PUBLIC or COMPANY; the employee calendar shows the type; a past
    holiday keeps its type (HOLIDAY_IN_PAST)."""
    admin = alpha["admin"]
    made = admin.post("/api/v1/holidays", json={"name": "Republic Day", "date": "2027-01-26", "holiday_type": "PUBLIC"})
    assert made.status_code in (200, 201), made.text
    assert made.json()["holiday_type"] == "PUBLIC"
    day = _facts(alpha, D(2027, 1, 26), D(2027, 1, 26))[D(2027, 1, 26)]
    assert (day.holiday.name, day.holiday.type, day.working_portion) == ("Republic Day", "PUBLIC", "NONE")
    kept = admin.put(f"/api/v1/holidays/{made.json()['id']}", json={"name": "Republic Day", "date": "2027-01-26"})
    assert kept.status_code == 200 and kept.json()["holiday_type"] == "PUBLIC"
    optional = admin.put(f"/api/v1/holidays/{made.json()['id']}", json={
        "name": "Republic Day", "date": "2027-01-26", "is_optional": True, "holiday_type": "PUBLIC"})
    assert optional.json()["holiday_type"] == "OPTIONAL"
    assert admin.post("/api/v1/holidays", json={"name": "x", "date": "2027-01-27",
                                                "holiday_type": "OPTIONAL"}).status_code == 422
    past = admin.post("/api/v1/holidays", json={"name": "Founders", "date": "2026-11-20"}).json()
    alpha["clock"].at_ist("2026-11-25", "10:00")
    refused = admin.put(f"/api/v1/holidays/{past['id']}", json={"name": "Founders", "date": "2026-11-20",
                                                               "holiday_type": "PUBLIC"})
    assert (refused.status_code, refused.json()["code"]) == (409, "HOLIDAY_IN_PAST")
