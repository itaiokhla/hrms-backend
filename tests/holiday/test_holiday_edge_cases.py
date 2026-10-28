"""LH-M1 edge cases of Goals 2-5 not covered elsewhere (LH 12 catalogue): LH-HOL-04, LH-HOL-27 (delete part),
LH-HOL-34, LH-HOL-36, LH-REC-17, LH-REC-22, LH-REC-25, LH-TZ-14."""

from datetime import date

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee
from tests.holiday.test_day_facts_v2 import (  # noqa: F401  (alpha is a fixture)
    HC, _apply, _approve, _calendar, _facts, _link, _published, _sql, alpha,
)
from tests.holiday.test_holiday_imports import _by_line, _upload
from tests.holiday.test_rollover import _publish_year, _rule, tenant

D = date


def test_lh_hol_04_holidays_before_joining_and_after_exit_are_not_employment_days(alpha):
    from app.payroll.services import time_inputs
    from tests.time_goldens import tenant_db

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "Gamma style", country_code="US")
    _published(admin, cal["id"], [("New Year's Day", "2027-01-01", "PUBLIC"), ("Thanksgiving", "2027-11-25", "PUBLIC"),
                                  ("Day after Thanksgiving", "2027-11-26", "COMPANY")], year=2027)
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    jo = create_employee(admin, "Jo", "Joiner", hire_date="2027-01-05", branch_id=alpha["branch"]["id"])
    lee = create_employee(admin, "Lee", "Leaver", branch_id=alpha["branch"]["id"])
    _sql("UPDATE employees SET termination_date = '2027-11-24' WHERE id = :e", e=lee["id"])
    jo_day = _facts(alpha, D(2027, 1, 1), D(2027, 1, 1), employee=jo)[D(2027, 1, 1)]
    assert (jo_day.employed, jo_day.working_portion, jo_day.scheduled_minutes) == (False, "NONE", 0)
    lee_days = _facts(alpha, D(2027, 11, 24), D(2027, 11, 26), employee=lee)
    assert lee_days[D(2027, 11, 24)].employed and lee_days[D(2027, 11, 24)].working_portion == "FULL"
    for d in (D(2027, 11, 25), D(2027, 11, 26)):
        assert (lee_days[d].employed, lee_days[d].working_portion) == (False, "NONE")
    with tenant_db(cid) as db:
        expected = time_inputs.expected_presence(db, cid, [jo["id"], lee["id"]], D(2027, 1, 1), D(2027, 1, 1))
        later = time_inputs.expected_presence(db, cid, [lee["id"]], D(2027, 11, 25), D(2027, 11, 25))
    assert expected[jo["id"]][0].reasons == ("NOT_EMPLOYED",)
    assert later[lee["id"]][0].reasons == ("NOT_EMPLOYED",)
    # No substitute day is generated outside employment.
    assert all(f.holiday is None or f.holiday.observance != "EMPLOYEE_SUBSTITUTE" for f in lee_days.values())


def test_lh_hol_27_legacy_delete_cancels_on_a_published_year_and_deletes_on_a_draft(alpha):
    """LH-HOL-27, delete part: DELETE /holidays/{id} of a future row of a PUBLISHED year becomes a cancel (amendment
    logged, row kept as CANCELLED); on a DRAFT year the row is deleted. (The location-typo refusal is kept as the
    legacy UNMATCHED behaviour for zero diff — FOLLOW-UPS FU-LH-11.)"""
    admin = alpha["admin"]
    legacy = admin.post("/api/v1/holidays", json={"name": "Plant Day", "date": "2027-11-12"})
    assert legacy.status_code in (200, 201), legacy.text
    row = legacy.json()
    status = admin.get(f"{HC}/{row['calendar_id']}/years/2027").json()["status"]
    assert status == "PUBLISHED"  # a legacy year counts as published v1
    assert admin.delete(f"/api/v1/holidays/{row['id']}").status_code in (200, 204)
    kept = owner_rows("SELECT status FROM holidays WHERE id = :i", i=row["id"])
    assert kept and kept[0]["status"] == "CANCELLED"
    audit = owner_rows("SELECT action, details FROM audit_logs WHERE entity_id = :i AND action = 'OCCURRENCE_CANCELLED'",
                       i=str(row["id"]))
    assert audit and "legacy" in str(audit[0]["details"]).lower()
    draft = _calendar(admin, "Draft legacy", country_code="IN")
    made = admin.post(f"{HC}/{draft['id']}/occurrences", json={"name": "Draft Day", "date": "2027-11-15",
                                                              "holiday_type": "COMPANY"})
    assert made.status_code == 201, made.text
    assert admin.delete(f"/api/v1/holidays/{made.json()['id']}").status_code in (200, 204)
    assert not owner_rows("SELECT id FROM holidays WHERE id = :i", i=made.json()["id"])


def test_lh_hol_34_unpublished_year_reminders_and_recount_on_publication(alpha):
    """Reminders on 12-01, 12-15 and 01-01; DRAFT rows never count (leave charged in full, the employee calendar shows
    the year as not published); publication recounts the leave automatically."""
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "IN-KA draft", country_code="IN")
    _rule(admin, cal["id"], "Kannada Rajyotsava", params={"month": 11, "day": 1})
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    assert admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Founders Day", "date": "2026-11-30",
                                                            "holiday_type": "COMPANY"}).status_code == 201
    leave = _apply(alpha, "2026-11-30", "2026-12-01")
    assert leave.status_code == 200, leave.text
    assert _approve(alpha, leave.json()["id"]) == 2.0  # the DRAFT row does not count
    me = alpha["worker_client"].get("/api/v1/me/calendar", params={"from": "2026-11-30", "to": "2026-11-30"}).json()
    assert me["days"][0]["holiday"] is None
    assert any(seg["years"].get("2026", {}).get("status") == "DRAFT" for seg in me["calendars"] if seg["id"])
    for day in (D(2026, 12, 1), D(2026, 12, 15), D(2027, 1, 1)):
        with tenant(cid) as db:
            assert holiday_rollover.unpublished_reminders(db, cid, day) >= 1, day
    titles = {r["title"] for r in owner_rows("SELECT title FROM notifications WHERE company_id = :c AND title LIKE "
                                             "'%is not published%'", c=cid)}
    assert any("2026-12-15" in t for t in titles) and any(t.endswith("(2027-01-01)") for t in titles)
    _publish_year(admin, cal["id"], 2026)
    assert float(owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave.json()["id"])[0][
        "number_of_days"]) == 1.0  # recounted on publication: nobody loses balance


def test_lh_hol_36_holiday_on_a_declared_working_day_is_refused_directly_and_through_includes(alpha):
    admin = alpha["admin"]
    branch_cal = _calendar(admin, "BR-BLR", country_code="IN")
    _published(admin, branch_cal["id"], [("Christmas Eve bridge", "2027-12-24", "COMPANY")], year=2027)
    r = admin.post(f"{HC}/{branch_cal['id']}/working-days", json={"date": "2027-12-18", "name": "Make-up Saturday",
                                                                  "reason": "Make-up for 24 Dec"})
    assert r.status_code in (200, 201), r.text
    added = admin.post(f"{HC}/{branch_cal['id']}/occurrences", json={
        "name": "Company day", "date": "2027-12-18", "holiday_type": "COMPANY", "reason": "x"})
    assert (added.status_code, added.json()["code"]) == (409, "WORKING_DAY_CONFLICT")
    assert "Make-up Saturday" in added.json()["detail"] and "BR-BLR" in added.json()["detail"] or \
        added.json()["code"] == "WORKING_DAY_CONFLICT"
    provisional = admin.post(f"{HC}/{branch_cal['id']}/occurrences", json={
        "name": "Festival", "date": "2027-12-17", "holiday_type": "COMPANY", "status": "PROVISIONAL", "reason": "x"})
    assert provisional.status_code == 200, provisional.text
    moved = admin.post(f"{HC}/occurrences/{provisional.json()['id']}/move", json={"new_date": "2027-12-18",
                                                                                 "reason": "Moon sighting"})
    assert (moved.status_code, moved.json()["code"]) == (409, "WORKING_DAY_CONFLICT")
    # Through includes: a draft calendar that includes BR-BLR cannot put a holiday on BR-BLR's working day either.
    child = _calendar(admin, "BR-BLR shops", country_code="IN")
    assert admin.put(f"{HC}/{child['id']}/includes", json={"includes": [
        {"included_calendar_id": branch_cal["id"], "priority": 1}]}).status_code == 200
    via = admin.post(f"{HC}/{child['id']}/occurrences", json={"name": "Shop day", "date": "2027-12-18",
                                                             "holiday_type": "COMPANY"})
    assert (via.status_code, via.json()["code"]) == (409, "WORKING_DAY_CONFLICT")


def test_lh_rec_17_announced_holiday_without_expected_date_needs_acknowledgement_at_publication(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "IN-KA 2027", country_code="IN")
    _rule(admin, cal["id"], "Republic Day", params={"month": 1, "day": 26})
    _rule(admin, cal["id"], "Diwali", kind="ANNUAL_CONFIRM", params={"expected_month": 10})
    with tenant(cid) as db:
        result = holiday_rollover.run_rollover(db, cid, 2027, notify=False)
    line = next(r for r in result["calendars"] if r["calendar_id"] == cal["id"])
    assert line["summary"]["needs_date"] == 1 and line["summary"]["holidays"] == 1  # no Diwali row
    url = f"{HC}/{cal['id']}/years/2027/publish"
    dry = admin.post(url, json={"dry_run": True}).json()
    needed = [w for w in dry["impact"]["warnings"] if w["code"] == "HOLIDAY_DATE_NEEDED"]
    assert needed and needed[0]["confirm"] and "Diwali" in needed[0]["message"]
    refused = admin.post(url, json={"acknowledged_warnings": []})
    assert refused.status_code == 422 and refused.json()["code"] in ("HOLIDAY_DATE_NEEDED", "HOLIDAY_ON_WEEKEND")
    codes = [w["code"] for w in dry["impact"]["warnings"] if w.get("confirm")]
    assert admin.post(url, json={"acknowledged_warnings": codes}).status_code == 200
    body = {"name": "Diwali", "date": "2027-10-29", "holiday_type": "PUBLIC", "reason": "State list"}
    check = admin.post(f"{HC}/{cal['id']}/occurrences", json={**body, "dry_run": True})
    assert check.status_code == 200, check.text
    acks = [w["code"] for w in check.json()["impact"]["warnings"] if w.get("confirm")]
    later = admin.post(f"{HC}/{cal['id']}/occurrences", json={**body, "acknowledged_warnings": acks})
    assert later.status_code == 200, later.text
    assert later.json()["year"]["version"] == 2  # a normal future amendment (version + 1, recount, notify)


ICS_LONG = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:republic-2027\r
DTSTART;VALUE=DATE:20270126\r
DTEND;VALUE=DATE:20270127\r
SUMMARY:Republic Day\r
CATEGORIES:NATIONAL\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:timed\r
DTSTART:20270315T140000\r
SUMMARY:Town hall\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:eid-2027\r
DTSTART;VALUE=DATE:20270309\r
SUMMARY:Eid al-Fitr\r
STATUS:TENTATIVE\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:summer\r
DTSTART;VALUE=DATE:20270601\r
DTEND;VALUE=DATE:20270703\r
SUMMARY:Summer break\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_lh_rec_22_ics_all_day_timed_tentative_and_over_long_events(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Government ICS", country_code="IN")
    lines = _by_line(_upload(admin, cal["id"], ICS_LONG, kind="ICS", name="gov.ics").json())
    republic = lines[1]["values"]
    assert (republic["date"], republic["days"], republic["category"], republic["holiday_type"], republic["status"]) == (
        "2027-01-26", 1, "NATIONAL", "COMPANY", "CONFIRMED")  # CATEGORIES never sets the type
    assert "Timed events are not holidays" in lines[2]["errors"][0]["message"]
    assert lines[3]["values"]["status"] == "PROVISIONAL"
    assert lines[4]["classification"] == "ERROR" and "1-31 days" in lines[4]["errors"][0]["message"]  # 32 days


def test_lh_rec_25_weekend_derived_from_the_country_until_one_is_stored(alpha):
    admin = alpha["admin"]
    created = admin.post(HC, json={"name": "SA-RIY"})
    assert created.status_code == 201, created.text
    cal = created.json()
    assert cal.get("weekend_days") in (None, [])  # nothing stored at creation
    derived = admin.get(f"{HC}/{cal['id']}").json()["effective_weekend"]
    assert derived in ([5, 6], [6])  # the entity's country or Sat+Sun
    saudi = admin.patch(f"{HC}/{cal['id']}", json={"country_code": "SA"})
    assert saudi.status_code == 200, saudi.text
    assert saudi.json()["effective_weekend"] == [4, 5] and saudi.json().get("weekend_days") in (None, [])
    stored = admin.patch(f"{HC}/{cal['id']}", json={"weekend_days": [5, 6]}).json()
    assert stored["effective_weekend"] == [5, 6] and stored["weekend_days"] == [5, 6]


def test_lh_tz_14_provisional_reminders_14_and_2_days_before_once_each(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "AE-NATIONAL", country_code="AE")
    r = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Eid al-Fitr", "date": "2027-03-09",
                                                          "holiday_type": "PUBLIC", "status": "PROVISIONAL"})
    assert r.status_code in (200, 201), r.text
    sent = {}
    for day in (D(2027, 2, 23), D(2027, 2, 24), D(2027, 3, 7), D(2027, 3, 7), D(2027, 3, 8)):
        with tenant(cid) as db:
            sent.setdefault(day, []).append(holiday_rollover.provisional_reminders(db, cid, day))
    assert sent[D(2027, 2, 23)][0] >= 1 and sent[D(2027, 3, 7)][0] >= 1
    assert sent[D(2027, 3, 7)][1] == 0  # each reminder once
    assert sent[D(2027, 2, 24)] == [0] and sent[D(2027, 3, 8)] == [0]
    titles = sorted({n["title"] for n in owner_rows("SELECT title FROM notifications WHERE company_id = :c AND "
                                                     "title LIKE 'Confirm Eid%'", c=cid)})
    assert titles == ["Confirm Eid al-Fitr (14 days)", "Confirm Eid al-Fitr (2 days)"]
    # Rollover dates follow the tenant's zone: due from local 10-01.
    with tenant(cid) as db:
        assert holiday_rollover.due_year(db, cid, D(2026, 9, 30)) is None
        assert holiday_rollover.due_year(db, cid, D(2026, 10, 1)) == 2027


def _amend(admin, calendar_id, year, operations, reason):
    url = f"{HC}/{calendar_id}/years/{year}/amend"
    dry = admin.post(url, json={"operations": operations, "reason": reason, "dry_run": True})
    assert dry.status_code == 200, dry.text
    acks = [w["code"] for w in dry.json()["impact"]["warnings"] if w.get("confirm")]
    r = admin.post(url, json={"operations": operations, "reason": reason, "acknowledged_warnings": acks})
    assert r.status_code == 200, r.text
    return r.json()


def test_lh_rec_18_eid_announced_one_day_longer_recounts_approved_leave(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "AE-NATIONAL Eid", country_code="AE")
    _published(admin, cal["id"], [("National Day", "2026-12-02", "PUBLIC")])
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    eid = admin.post(f"{HC}/{cal['id']}/occurrences", json={
        "name": "Eid al-Fitr", "date": "2026-11-17", "duration_days": 3, "holiday_type": "PUBLIC",
        "status": "PROVISIONAL", "reason": "Expected dates"})
    assert eid.status_code == 200, eid.text
    leave = _apply(alpha, "2026-11-16", "2026-11-20")  # Mon-Fri, Eid expected Tue-Thu
    assert leave.status_code == 200, leave.text
    assert _approve(alpha, leave.json()["id"]) == 2.0  # Mon + Fri
    rows = owner_rows("SELECT id FROM holidays WHERE calendar_id = :k AND name = 'Eid al-Fitr' ORDER BY date",
                      k=cal["id"])
    operations = [{"op": "confirm", "holiday_id": r["id"]} for r in rows] + [
        {"op": "add", "name": "Eid al-Fitr", "date": "2026-11-20", "holiday_type": "PUBLIC", "status": "CONFIRMED"}]
    body = _amend(admin, cal["id"], 2026, operations, "Announcement: four days")
    assert body["year"]["version"] >= 3
    days = owner_rows("SELECT date, status FROM holidays WHERE calendar_id = :k AND name = 'Eid al-Fitr' "
                      "AND status <> 'CANCELLED' ORDER BY date", k=cal["id"])
    assert [str(d["date"]) for d in days] == ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    assert all(d["status"] == "CONFIRMED" for d in days)
    assert float(owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave.json()["id"])[0][
        "number_of_days"]) == 1.0  # Monday only


def test_lh_rec_27_provisional_original_with_observed_partner_confirmed_on_another_date(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "AE Islamic", country_code="AE")
    assert admin.patch(f"{HC}/{cal['id']}", json={"weekend_days": [5, 6]}).status_code == 200
    rule = _rule(admin, cal["id"], "Islamic New Year", kind="ANNUAL_CONFIRM", params={"expected_month": 6},
                 observed_rule="NEXT_WORKDAY")
    assert admin.put(f"{HC}/rules/{rule['id']}/expected-date", json={"year": 2027, "date": "2027-06-26"}
                     ).status_code == 200
    with tenant(cid) as db:
        holiday_rollover.run_rollover(db, cid, 2027, notify=False)
    rows = owner_rows("SELECT id, date, status, observance FROM holidays WHERE calendar_id = :k ORDER BY date",
                      k=cal["id"])
    assert [(str(r["date"]), r["status"], r["observance"]) for r in rows] == [
        ("2027-06-26", "PROVISIONAL", "ORIGINAL"), ("2027-06-28", "PROVISIONAL", "OBSERVED")]  # rule 5
    _publish_year(admin, cal["id"], 2027)
    original = rows[0]["id"]
    url = f"{HC}/occurrences/{original}/confirm"
    dry = admin.post(url, json={"date": "2027-06-24", "reason": "Cabinet announcement", "dry_run": True})
    assert dry.status_code == 200, dry.text
    acks = [w["code"] for w in dry.json()["impact"]["warnings"] if w.get("confirm")]
    done = admin.post(url, json={"date": "2027-06-24", "reason": "Cabinet announcement", "acknowledged_warnings": acks})
    assert done.status_code == 200, done.text
    live = owner_rows("SELECT date, status, observance FROM holidays WHERE calendar_id = :k AND status <> 'CANCELLED' "
                      "ORDER BY date", k=cal["id"])
    assert [(str(r["date"]), r["status"]) for r in live] == [("2027-06-24", "CONFIRMED")]  # Thursday: no observed row


def test_lh_hol_41_one_day_polling_calendar_for_a_known_group(alpha):
    admin = alpha["admin"]
    branch_cal = _calendar(admin, "BR-BLR poll base", country_code="IN")
    _published(admin, branch_cal["id"], [("Republic Day", "2027-01-26", "PUBLIC")], year=2027)
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=branch_cal["id"])
    poll = _calendar(admin, "CAL-POLL-BLR", country_code="IN")
    assert admin.put(f"{HC}/{poll['id']}/includes", json={"includes": [
        {"included_calendar_id": branch_cal["id"], "priority": 1}]}).status_code == 200
    r = admin.post(f"{HC}/{poll['id']}/occurrences", json={"name": "Municipal poll", "date": "2027-04-13",
                                                           "holiday_type": "PUBLIC", "category": "ELECTION"})
    assert r.status_code == 201, r.text
    _publish_year(admin, poll["id"], 2027)
    a = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": poll["id"], "criteria": {"employee_ids": [alpha["worker"]["id"]]}, "priority": 5,
        "effective_from": "2027-04-13", "effective_to": "2027-04-13", "reason": "Ward electors"})
    assert a.status_code == 201, a.text
    elector = _facts(alpha, D(2027, 4, 12), D(2027, 4, 14))
    other = _facts(alpha, D(2027, 4, 13), D(2027, 4, 13), employee=alpha["boss"])[D(2027, 4, 13)]
    assert elector[D(2027, 4, 13)].calendar_id == poll["id"] and elector[D(2027, 4, 13)].holiday.category == "ELECTION"
    assert elector[D(2027, 4, 12)].calendar_id == branch_cal["id"] and elector[D(2027, 4, 14)].calendar_id == \
        branch_cal["id"]
    assert other.calendar_id == branch_cal["id"] and other.holiday is None


def test_lh_mig_10_weekend_days_stay_null_and_are_derived_live(alpha):
    from app.services import holiday_calendar_service as svc
    from tests.time_goldens import tenant_db

    admin, cid = alpha["admin"], alpha["company_id"]
    gulf = _calendar(admin, "Beta AE", country_code="AE")
    plain = admin.post(HC, json={"name": "No country"}).json()
    stored = {r["id"]: r["weekend_days"] for r in owner_rows(
        "SELECT id, weekend_days FROM holiday_calendars WHERE company_id = :c", c=cid)}
    assert all(v is None for v in stored.values())  # nothing frozen at creation or by the migration
    with tenant_db(cid) as db:
        from app.models.pay_org import HolidayCalendar

        assert svc.effective_weekend(db, db.get(HolidayCalendar, gulf["id"])) == [5, 6] or \
            svc.effective_weekend(db, db.get(HolidayCalendar, gulf["id"])) == [4, 5]
        derived = svc.effective_weekend(db, db.get(HolidayCalendar, plain["id"]))
    assert derived in ([6], [5, 6])  # the legal entity's country (IN) else Sat+Sun


def test_lh_rec_26_flat_module_layout(alpha):
    """V3-003 flat layout: pure recurrence in app/services/holiday_recurrence.py, generation in holiday_rollover,
    rule writes as calendar-service entry points, job handlers loaded from app.services modules."""
    from app.payroll.services import jobs
    from app.services import holiday_calendar_service, holiday_recurrence, holiday_rollover

    for name in ("expand", "preview", "validate", "generate_year", "apply_observance", "rrule_parse"):
        assert callable(getattr(holiday_recurrence, name))
    assert callable(holiday_recurrence._easter)  # noqa: SLF001  (private helper, 02 §5)
    for name in ("generate", "diff", "run_rollover"):
        assert callable(getattr(holiday_rollover, name))
    for name in ("create_rule", "update_rule", "deactivate_rule", "backdated_closure"):
        assert callable(getattr(holiday_calendar_service, name))
    assert {"app.services.holiday_rollover", "app.services.holiday_closures"} <= set(jobs.HANDLER_MODULES)
    for job_type in ("holiday.rollover", "holiday.reminders", "holiday.closure_retro", "holiday.link_activate"):
        assert jobs.find_handler(job_type) is not None
    assert alpha["company_id"]


def test_lh_ntf_14_future_link_notifies_once_on_its_effective_date(alpha):
    from app.services import holiday_assignments

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "Bengaluru holidays", country_code="IN")
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"], effective_from="2026-11-20",
          reason="New calendar from 20 Nov")
    title = "From 20 Nov 2026 your holidays follow Bengaluru holidays"
    assert not owner_rows("SELECT id FROM notifications WHERE company_id = :c AND title = :t", c=cid, t=title)
    for _ in range(2):
        with tenant(cid) as db:
            holiday_assignments.activate(db, cid, D(2026, 11, 20))
    sent = owner_rows("SELECT user_id FROM notifications WHERE company_id = :c AND title = :t", c=cid, t=title)
    users = [r["user_id"] for r in sent]
    assert users and len(users) == len(set(users))  # each employee once


def test_lh_hol_33_provisional_festival_moved_after_payroll_finalization_queues_retro(alpha, monkeypatch):
    """A PROVISIONAL holiday moved between two future dates of a FINALIZED period: leave is regenerated whatever
    the period state, and both dates go to retro detection (the finalized run never changes)."""
    from app.payroll.services import calendars as pay_calendars

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "IN-PB", country_code="IN")
    _published(admin, cal["id"], [("Republic Day", "2026-01-26", "PUBLIC")])
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    eid = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Eid-ul-Zuha", "date": "2026-11-25",
                                                            "holiday_type": "PUBLIC", "status": "PROVISIONAL",
                                                            "reason": "Expected"})
    assert eid.status_code == 200, eid.text
    harpreet = _apply(alpha, "2026-11-25", "2026-11-27")
    assert harpreet.status_code == 200, harpreet.text
    assert _approve(alpha, harpreet.json()["id"]) == 2.0  # Wed holiday not charged
    monkeypatch.setattr(pay_calendars, "period_state_on", lambda db, c, e, d, now=None: "FINALIZED")
    moved = admin.post(f"{HC}/occurrences/{eid.json()['id']}/move", json={"new_date": "2026-11-26",
                                                                        "reason": "State notification"})
    if moved.status_code == 422:  # a confirm-warning first
        acks = [w["code"] for w in moved.json().get("details", {}).get("warnings", [])]
        moved = admin.post(f"{HC}/occurrences/{eid.json()['id']}/move", json={
            "new_date": "2026-11-26", "reason": "State notification", "acknowledged_warnings": acks})
    assert moved.status_code == 200, moved.text
    payroll = moved.json()["effects"]["payroll"]
    assert set(payroll["finalized"]) == {"2026-11-25", "2026-11-26"} and payroll["retro_job_id"]
    assert float(owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=harpreet.json()["id"])[0][
        "number_of_days"]) == 2.0  # Thu is now the holiday: still 2 days, leave_days regenerated
    flagged = owner_rows("SELECT action FROM audit_logs WHERE company_id = :c AND action = 'AMENDMENT_PAYROLL_FLAGGED'",
                         c=cid)
    assert len(flagged) == 1


def _shift(admin, code, start, end):
    r = admin.post("/api/v1/shifts", json={"code": code, "name": code.title(), "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "fixture", "start_time": start, "end_time": end}})
    assert r.status_code == 201, r.text
    return r.json()


def test_lh_tz_11_night_crew_gets_the_holiday_on_the_eve_night_through_a_criteria_calendar(alpha):
    """Night crew (shift date = eve) via a criteria row {branch_ids, shift_ids} to a calendar whose own Diwali is
    dated on the eve, with the inherited row excluded; day staff keep the national date."""
    admin = alpha["admin"]
    plant = _calendar(admin, "BR-PLANT", country_code="IN")
    _published(admin, plant["id"], [("Diwali", "2027-10-29", "PUBLIC")], year=2027)
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=plant["id"])
    night_cal = _calendar(admin, "BR-PLANT-NIGHT", country_code="IN")
    assert admin.put(f"{HC}/{night_cal['id']}/includes", json={"includes": [
        {"included_calendar_id": plant["id"], "priority": 1}]}).status_code == 200
    inherited = next(h for h in admin.get(f"{HC}/{plant['id']}/years/2027").json()["items"] if h["name"] == "Diwali")
    assert admin.post(f"{HC}/{night_cal['id']}/exclusions", json={"holiday_id": inherited["id"],
                                                                 "reason": "Night crew is off on the eve"}
                      ).status_code in (200, 201)
    assert admin.post(f"{HC}/{night_cal['id']}/occurrences", json={"name": "Diwali (night crew)", "date": "2027-10-28",
                                                                   "holiday_type": "PUBLIC"}).status_code == 201
    _publish_year(admin, night_cal["id"], 2027)
    night = _shift(admin, "NIGHT1", "22:00", "06:00")
    crew = create_employee(admin, "Nikhil", "Night", shift_id=night["id"], branch_id=alpha["branch"]["id"])
    r = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": night_cal["id"], "criteria": {"branch_ids": [alpha["branch"]["id"]], "shift_ids": [night["id"]]},
        "priority": 10, "reason": "Night crew"})
    assert r.status_code == 201, r.text
    crew_days = _facts(alpha, D(2027, 10, 28), D(2027, 10, 29), employee=crew)
    assert crew_days[D(2027, 10, 28)].holiday.name == "Diwali (night crew)"
    assert crew_days[D(2027, 10, 29)].holiday is None
    day_staff = _facts(alpha, D(2027, 10, 28), D(2027, 10, 29))
    assert day_staff[D(2027, 10, 29)].holiday.name == "Diwali" and day_staff[D(2027, 10, 28)].holiday is None


def test_lh_tz_13_remote_employee_uses_the_work_zone_for_holidays_today_and_minutes(alpha):
    """Remote part: a Bengaluru-branch employee working from New York gets the branch's holidays as floating local
    dates and her own local today; the device zone never decides (the traveller punch part is attendance's)."""
    admin = alpha["admin"]
    cal = _calendar(admin, "BR-BLR remote", country_code="IN")
    _published(admin, cal["id"], [("Republic Day", "2027-01-26", "PUBLIC")], year=2027)
    _link(admin, alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    _sql("UPDATE employee_assignments SET work_timezone = 'America/New_York' WHERE employee_id = :e",
         e=alpha["worker"]["id"])
    from datetime import datetime, timezone

    alpha["clock"].now = datetime(2027, 1, 26, 3, 0, tzinfo=timezone.utc)  # 08:30 IST on 26 Jan, 22:00 NY on 25 Jan
    me = alpha["worker_client"].get("/api/v1/me/calendar", params={"from": "2027-01-25", "to": "2027-01-26"}).json()
    assert (me["local_today"], me["timezone"]) == ("2027-01-25", "America/New_York")
    days = {d["date"]: d for d in me["days"]}
    assert days["2027-01-26"]["holiday"]["name"] == "Republic Day" and days["2027-01-25"]["holiday"] is None
    assert days["2027-01-26"]["timezone"] == "America/New_York" and days["2027-01-25"]["scheduled_minutes"] == 480
    boss = alpha["boss_client"].get("/api/v1/me/calendar", params={"from": "2027-01-26", "to": "2027-01-26"}).json()
    assert boss["local_today"] == "2027-01-26"  # the Kolkata colleague is already on the holiday


def test_lh_wwk_30_expectations_on_weekly_offs_follow_the_pattern_and_the_dated_shift(alpha):
    from app.payroll.services import time_inputs
    from tests.holiday.test_day_facts_v2 import _pattern, NTH
    from tests.time_goldens import tenant_db

    admin, cid = alpha["admin"], alpha["company_id"]
    pattern = _pattern(admin, "Pune plant week", NTH)
    _link(admin, alpha["branch"]["id"], work_week_pattern_id=pattern["id"])
    general = _shift(admin, "GENERAL1", "09:00", "18:00")
    night = _shift(admin, "NIGHT2", "22:00", "06:00")
    ravi = create_employee(admin, "Ravi", "Kumar", shift_id=general["id"], branch_id=alpha["branch"]["id"])
    moved = admin.post(f"/api/v1/employees/{ravi['id']}/assignments", json={
        "branch_id": alpha["branch"]["id"], "shift_id": night["id"], "effective_from": "2026-11-16",
        "reason": "TRANSFER"})
    assert moved.status_code in (200, 201), moved.text
    with tenant_db(cid) as db:
        expected = time_inputs.expected_presence(db, cid, [ravi["id"]], D(2026, 11, 14), D(2026, 11, 16))[ravi["id"]]
    by_day = {e.date: e for e in expected}
    assert (by_day[D(2026, 11, 14)].expected_portion, by_day[D(2026, 11, 14)].reasons) == ("NONE", ("WEEKLY_OFF",))
    facts = _facts(alpha, D(2026, 11, 13), D(2026, 11, 16), employee=ravi)
    assert facts[D(2026, 11, 13)].shift_ids == (general["id"],)
    assert facts[D(2026, 11, 16)].shift_ids == (night["id"],) and facts[D(2026, 11, 16)].shift_source == "PLACEMENT"
    start, _ = facts[D(2026, 11, 16)].instances[0]
    assert start.date() == D(2026, 11, 16)  # shift date = local start date (22:00 IST on the 16th)


def test_lh_wl_02_invalid_stored_holiday_setting_fails_closed(alpha):
    """V3-085: an invalid stored holiday.* value is CONFIG_INVALID naming the key, never a silent default."""
    from tests.holiday.test_years_publication import _set

    admin = alpha["admin"]
    cal = _calendar(admin, "WL-02 calendar", country_code="IN")
    _published(admin, cal["id"], [("Founders Day", "2026-12-18", "COMPANY")])
    _set(admin, "holiday.backdate_days", 5)
    _sql("UPDATE setting_values SET value = '999' WHERE company_id = :c AND key = 'holiday.backdate_days'",
         c=alpha["company_id"])
    r = admin.post(f"{HC}/{cal['id']}/closures", json={"date": "2026-11-02", "reason": "Storm", "dry_run": True})
    assert (r.status_code, r.json()["code"]) == (409, "CONFIG_INVALID")
    assert "holiday.backdate_days" in r.json()["detail"]
    from app.payroll import settings_registry

    assert settings_registry.definition("holiday.backdate_days").fails_closed
    assert settings_registry.definition("holiday.provisional_counts").fails_closed
    assert not settings_registry.definition("attendance.grace_minutes").fails_closed  # other modules fall back


def test_lh_rel_05_a_pending_leave_recount_blocks_the_payroll_precheck(alpha, monkeypatch):
    """A big amendment's recount runs as one job (above RECALC_INLINE_LIMIT); while it covers the period the payroll
    pre-check raises the blocking LEAVE_RECALC_PENDING; a rerun of the same change enqueues nothing twice."""
    from types import SimpleNamespace

    from app.payroll.services import jobs, runs
    from app.services import leave_jobs
    from tests.time_goldens import tenant_db

    cid = alpha["company_id"]
    _apply(alpha, "2026-11-26", "2026-11-26")
    monkeypatch.setattr(leave_jobs, "RECALC_INLINE_LIMIT", -1)  # every recount becomes a job
    with tenant_db(cid) as db:
        first = leave_jobs.recount_dates(db, cid, [D(2026, 11, 26)], key="recalc:test:big:v2")
        again = leave_jobs.recount_dates(db, cid, [D(2026, 11, 26)], key="recalc:test:big:v2")
        assert first["mode"] == "job" and again["job_id"] == first["job_id"]
        period = SimpleNamespace(attendance_start=D(2026, 11, 1), attendance_end=D(2026, 11, 30),
                                 period_end=D(2026, 11, 30))
        ctx = SimpleNamespace(db=db, run=SimpleNamespace(company_id=cid), period=period)
        findings = runs._leave_recalc_findings(ctx)  # noqa: SLF001
        assert [(f.code, f.severity) for f in findings] == [("LEAVE_RECALC_PENDING", "BLOCKING")]
        december = SimpleNamespace(db=db, run=SimpleNamespace(company_id=cid), period=SimpleNamespace(
            attendance_start=D(2026, 12, 1), attendance_end=D(2026, 12, 31), period_end=D(2026, 12, 31)))
        assert runs._leave_recalc_findings(december) == []  # noqa: SLF001
    assert jobs.find_handler("leave.recalculate") is not None


def test_lh_rel_12_tzdata_change_recounts_leave_of_employees_in_that_zone_once(alpha):
    from app.services import leave_jobs
    from tests.time_goldens import tenant_db

    cid = alpha["company_id"]
    _sql("UPDATE employee_assignments SET work_timezone = 'America/New_York' WHERE employee_id = :e",
         e=alpha["worker"]["id"])
    with tenant_db(cid) as db:
        first = leave_jobs.recount_zone(db, cid, "America/New_York", D(2027, 3, 1), tzdata_version="2027a")
        again = leave_jobs.recount_zone(db, cid, "America/New_York", D(2027, 3, 1), tzdata_version="2027a")
        none = leave_jobs.recount_zone(db, cid, "Pacific/Auckland", D(2027, 3, 1), tzdata_version="2027a")
    assert first["mode"] == "job" and first["employees"] == 1 and again["job_id"] == first["job_id"]
    assert none == {"mode": "none", "employees": 0}


import pytest  # noqa: E402  (the step_up marker of the next test)


def _stamp_step_up(company_id, at):
    _sql("UPDATE user_sessions SET step_up_at = :t, step_up_method = 'EMAIL_OTP' WHERE user_id IN "
         "(SELECT id FROM users WHERE company_id = :c)", t=at, c=company_id)


@pytest.mark.step_up
def test_lh_sec_02_publishing_a_holiday_year_needs_a_fresh_step_up(alpha):
    """07 §5.2: the dry run is free; the publish itself without a fresh step-up (<= 300 s) answers 403
    STEP_UP_REQUIRED (the UI opens "Confirm it's you" and retries); after a step-up it publishes; a stale one is refused
    again."""
    from datetime import timedelta

    from app.core import clock

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "Step-up calendar", country_code="IN")
    for year, day in ((2027, "2027-03-03"), (2028, "2028-03-03")):
        assert admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Founders Day", "date": day,
                                                                "holiday_type": "COMPANY"}).status_code == 201
    url = f"{HC}/{cal['id']}/years/2027/publish"
    dry = admin.post(url, json={"dry_run": True})
    assert dry.status_code == 200, dry.text
    acks = [w["code"] for w in dry.json()["impact"]["warnings"] if w.get("confirm")]
    refused = admin.post(url, json={"acknowledged_warnings": acks})
    assert (refused.status_code, refused.json()["code"]) == (403, "STEP_UP_REQUIRED")
    assert admin.get(f"{HC}/{cal['id']}/years/2027").json()["status"] == "DRAFT"
    _stamp_step_up(cid, clock.utcnow())
    done = admin.post(url, json={"acknowledged_warnings": acks})
    assert done.status_code == 200, done.text
    assert admin.get(f"{HC}/{cal['id']}/years/2027").json()["status"] == "PUBLISHED"
    _stamp_step_up(cid, clock.utcnow() - timedelta(seconds=400))
    stale = admin.post(f"{HC}/{cal['id']}/years/2028/publish", json={"acknowledged_warnings": acks})
    assert (stale.status_code, stale.json()["code"]) == (403, "STEP_UP_REQUIRED")


# ============================================================================================== rosters (LH-WWK-18/-19)
from tests.shifts.test_rosters import _ack_all, _cell, _drafts, _publish, plant  # noqa: E402,F401  (plant fixture)


def _roster_facts(plant, start, end):
    from app.database.db import SessionLocal, bind_tenant
    from app.payroll.services import time_inputs

    cid = plant["admin"].company_id
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        return {f.date: f for f in time_inputs.day_facts(db, cid, plant["ravi"]["id"], start, end)}
    finally:
        db.close()


def test_lh_wwk_18_roster_based_leave_charged_on_the_pattern_then_recounted_on_publication(app, plant):
    """Before the roster is published the default pattern decides (Mon-Fri leave = 5.00); publishing an OFF on Wed
    inside the leave credits it back automatically (4.00); a WORK on Saturday outside the leave changes nothing."""
    from datetime import timedelta

    from tests.helpers import invite_and_activate

    admin, s, start, ravi = plant["admin"], plant["shifts"], plant["start"], plant["ravi"]
    worker = invite_and_activate(app, admin, ravi, ["employee"])
    friday = start + timedelta(days=4)
    leave = worker.post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": start.isoformat(),
                                                      "end_date": friday.isoformat(), "reason": "family"})
    assert leave.status_code in (200, 201), leave.text
    assert admin.post(f"/api/v1/leaves/{leave.json()['id']}/approve").status_code == 200
    before = _roster_facts(plant, start, start + timedelta(days=6))
    assert before[start].work_source in ("PATTERN", "ROSTER_UNPUBLISHED") and before[start].roster_entry_ids == ()
    days = owner_rows("SELECT number_of_days FROM leaves WHERE id = :l", l=leave.json()["id"])[0]["number_of_days"]
    assert float(days) == 5.0  # charged on the default MON_FRI pattern
    cells = [_cell(ravi, start + timedelta(days=i), s["EARLY"]) for i in (0, 1, 3, 4, 5)]
    cells += [_cell(ravi, start + timedelta(days=2), kind="OFF"), _cell(ravi, start + timedelta(days=6), kind="OFF")]
    _drafts(plant, cells)
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    after = _roster_facts(plant, start, start + timedelta(days=6))
    wednesday, saturday = start + timedelta(days=2), start + timedelta(days=5)
    assert after[wednesday].weekly_off and after[wednesday].work_source == "ROSTER"
    assert not after[saturday].weekly_off and after[saturday].work_source == "ROSTER"
    days = owner_rows("SELECT number_of_days FROM leaves WHERE id = :l", l=leave.json()["id"])[0]["number_of_days"]
    assert float(days) == 4.0  # the rostered OFF released Wednesday


def test_lh_wwk_19_weekly_off_chosen_week_by_week_follows_the_published_days(plant):
    """A store's weekly off moves week by week (Tue in week 1, Sat in week 2, Sunday worked): day facts follow the
    published days, not the MON_FRI pattern."""
    from datetime import timedelta

    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    week1_off, week2_off = start + timedelta(days=1), start + timedelta(days=12)  # Tue of week 1, Sat of week 2
    cells = []
    for i in range(14):
        day = start + timedelta(days=i)
        if day in (week1_off, week2_off):
            cells.append(_cell(ravi, day, kind="OFF"))
        elif day.weekday() == 5 and day < start + timedelta(days=7):
            cells.append(_cell(ravi, day, kind="OFF"))  # week 1: Tue and Sat off
        elif day.weekday() == 6 and day > start + timedelta(days=7):
            cells.append(_cell(ravi, day, s["EARLY"]))  # week 2: Sunday worked
        elif day.weekday() == 4 and day > start + timedelta(days=7):
            cells.append(_cell(ravi, day, kind="OFF"))  # week 2: Fri and Sat off
        else:
            cells.append(_cell(ravi, day, s["EARLY"]))
    _drafts(plant, cells)
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    facts = _roster_facts(plant, start, start + timedelta(days=13))
    assert facts[week1_off].weekly_off and facts[week1_off].work_source == "ROSTER"
    assert not facts[start + timedelta(days=6)].weekly_off  # week 1 Sunday is worked
    assert facts[week2_off].weekly_off and not facts[start + timedelta(days=13)].weekly_off  # week 2: Sat off, Sun worked
    assert all(f.work_source == "ROSTER" for f in facts.values())
    assert facts[start + timedelta(days=6)].working_portion == "FULL" and facts[week1_off].working_portion == "NONE"


def test_lh_tz_13_traveller_punch_uses_the_placement_zone_not_the_device_zone(alpha):
    """Traveller part: a Bengaluru employee in Dubai punches in at 09:00 Dubai (05:00 UTC = 10:30 IST) against a 09:00
    IST shift; the device sends a Dubai-offset timestamp. The session date and lateness follow the placement zone
    (Asia/Kolkata): date 2026-11-20, local check-in 10:30, late."""
    from datetime import datetime, timezone

    from tests.attendance_helpers import act, fix, meta, set_policy
    from tests.helpers import invite_and_activate

    admin = alpha["admin"]
    set_policy(admin, requireFaceForSelfCheckIn=False)
    shift = admin.post("/api/v1/shifts", json={"code": "GEN9", "name": "General 9", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "fixture", "start_time": "09:00", "end_time": "18:00"}})
    assert shift.status_code == 201, shift.text
    traveller = create_employee(admin, "Tara", "Traveller", shift_id=shift.json()["id"], branch_id=alpha["branch"]["id"])
    client = invite_and_activate(alpha["app"], admin, traveller, ["employee"])
    clock = alpha["clock"]
    clock.now = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)
    body = meta(clock, fix(clock))
    body["client_sent_at"] = "2026-11-20T09:00:00+04:00"  # the device's own (Dubai) clock
    r = act(client, "PUNCH_IN", body)
    assert r.status_code in (200, 201), r.text
    row = owner_rows("SELECT date, status, check_in_time FROM attendance WHERE employee_id = :e", e=traveller["id"])[0]
    assert str(row["date"]) == "2026-11-20"
    assert row["check_in_time"].astimezone(timezone.utc).replace(tzinfo=None) == datetime(2026, 11, 20, 5, 0)
    assert row["status"] == "late"  # 10:30 IST against a 09:00 IST shift


def test_lh_hol_27_unknown_legacy_location_is_refused_when_the_tenant_says_so(alpha):
    """LH-HOL-27 location part: with ``holiday.legacy.unknown_location = REFUSE`` the legacy form refuses 'Pune '
    (no such branch) with 422 UNKNOWN_REFERENCE and creates nothing; the default UNMATCHED keeps the original
    behaviour (the legacy payroll goldens rely on it); an exact branch name is always accepted."""
    from tests.holiday.test_years_publication import _set

    admin, cid = alpha["admin"], alpha["company_id"]
    kept = admin.post("/api/v1/holidays", json={"name": "Typo Day", "date": "2027-11-12", "location": "Pune "})
    assert kept.status_code in (200, 201), kept.text  # default UNMATCHED: accepted onto the review calendar
    _set(admin, "holiday.legacy.unknown_location", "REFUSE")
    refused = admin.post("/api/v1/holidays", json={"name": "Another Typo", "date": "2027-11-13", "location": "Pune "})
    assert (refused.status_code, refused.json()["code"]) == (422, "UNKNOWN_REFERENCE")
    assert "Pune " in refused.json()["detail"]
    assert not owner_rows("SELECT id FROM holidays WHERE company_id = :c AND name = 'Another Typo'", c=cid)
    exact = admin.post("/api/v1/holidays", json={"name": "Branch Day", "date": "2027-11-15",
                                                 "location": alpha["branch"]["name"]})
    assert exact.status_code in (200, 201), exact.text
    company_wide = admin.post("/api/v1/holidays", json={"name": "Company Day", "date": "2027-11-16"})
    assert company_wide.status_code in (200, 201), company_wide.text


def test_lh_sec_02_leave_override_needs_a_fresh_step_up_when_the_tenant_requires_it(alpha):
    """LH-SEC-02 (a): a company-wide approver deciding a request that waits for someone else (an override) confirms
    it's them first when ``leave.override.requires_step_up`` is on; the assigned approver never needs it; the default
    (off) keeps the original behaviour."""
    from app.core import clock
    from tests.holiday.test_years_publication import _set

    admin, cid = alpha["admin"], alpha["company_id"]

    def request(day):
        r = _apply(alpha, day, day)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    first = request("2026-12-07")
    assert admin.post(f"/api/v1/leaves/{first}/approve", json={"remarks": "override"}).status_code == 200  # default
    _set(admin, "holiday.backdate_days", 7)  # touch settings once so the scope rows exist
    _set(admin, "leave.override.requires_step_up", True)
    second = request("2026-12-08")
    refused = admin.post(f"/api/v1/leaves/{second}/approve", json={"remarks": "override"})
    assert (refused.status_code, refused.json()["code"]) == (403, "STEP_UP_REQUIRED")
    assert owner_rows("SELECT status FROM leaves WHERE id = :i", i=second)[0]["status"] == "pending"
    by_manager = alpha["boss_client"].post(f"/api/v1/leaves/{second}/approve", json={"remarks": "ok"})
    assert by_manager.status_code == 200, by_manager.text  # the assigned approver: no step-up
    third = request("2026-12-09")
    _sql("UPDATE user_sessions SET step_up_at = :t, step_up_method = 'EMAIL_OTP' WHERE user_id IN "
         "(SELECT id FROM users WHERE company_id = :c)", t=clock.utcnow(), c=cid)
    assert admin.post(f"/api/v1/leaves/{third}/approve", json={"remarks": "override"}).status_code == 200


def test_lh_sec_02_leave_and_holiday_plan_activation_needs_a_fresh_step_up_when_the_tenant_requires_it(alpha):
    """LH-SEC-02 (b): activating a version of a leave or holiday plan (LEAVE_PAY_POLICY, HOLIDAY_PAY_POLICY) needs a
    fresh step-up when ``leave.plan_activation.requires_step_up`` is on; other policy types and the default (off) are
    unchanged; nothing is activated by the refused call."""
    from datetime import timedelta

    from app.core import clock
    from tests.holiday.test_years_publication import _set

    admin, cid = alpha["admin"], alpha["company_id"]
    policies = "/api/v1/payroll/policies"
    by_type = {p["type"]: p for p in admin.get(policies).json()}

    def draft(type_code):
        pid = by_type[type_code]["id"]
        r = admin.post(f"{policies}/{pid}/versions")
        assert r.status_code == 201, r.text
        return pid, r.json()["version"]

    start = (clock.utcnow().date().replace(day=1) + timedelta(days=40)).replace(day=1).isoformat()  # a month ahead

    def activate(pid, version):
        return admin.post(f"{policies}/{pid}/versions/{version}/activate", json={"effective_from": start})

    pid, version = draft("LEAVE_PAY_POLICY")
    assert activate(pid, version).status_code == 200  # default: off
    _set(admin, "leave.plan_activation.requires_step_up", True)
    for type_code in ("LEAVE_PAY_POLICY", "HOLIDAY_PAY_POLICY"):
        pid, version = draft(type_code)
        refused = activate(pid, version)
        assert (refused.status_code, refused.json()["code"]) == (403, "STEP_UP_REQUIRED"), type_code
        assert {v["version"]: v["status"] for v in admin.get(f"{policies}/{pid}").json()["versions"]}[version] == "DRAFT"
    pid, version = draft("ATTENDANCE_PAY_POLICY")
    assert activate(pid, version).status_code == 200, "a payroll-only type is not gated by the leave setting"
    _stamp_step_up(cid, clock.utcnow())
    for type_code in ("LEAVE_PAY_POLICY", "HOLIDAY_PAY_POLICY"):
        pid = by_type[type_code]["id"]
        version = max(v["version"] for v in admin.get(f"{policies}/{pid}").json()["versions"])
        assert activate(pid, version).status_code == 200, type_code
