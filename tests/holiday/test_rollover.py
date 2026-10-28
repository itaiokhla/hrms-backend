"""Rollover, reminders and emergency / backdated closures (LH-M1 Goal 5; 04-holiday/02 §6.3, §8, §10; 01 §5.8).

LH-REC-11, LH-REC-14 (idempotent regeneration), LH-REC-29 (DAILY check against ``generate_on``), LH-REC-30 (order,
failed calendar, calendars without rules), the Next year overview, provisional and unpublished-year reminders,
LH-HOL-24 (backdated closure: BACKDATED_CLOSURE audit, leave recount, payroll flags / retro) and the closure limits.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.holiday.test_day_facts_v2 import (  # noqa: F401  (alpha is a fixture)
    HC, _apply, _approve, _calendar, _facts, _link, _published, _sql, alpha,
)

D = date


@contextmanager
def tenant(company_id):
    """A tenant session that commits (jobs queued inside run inline after the commit)."""
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _rule(admin, calendar_id, name, kind="FIXED_DATE", params=None, **extra):
    r = admin.post(f"{HC}/{calendar_id}/rules", json={"name": name, "kind": kind, "params": params or {},
                                                      "holiday_type": "PUBLIC", **extra})
    assert r.status_code == 201, r.text
    body = r.json()
    return body.get("rule", body)


def _national(alpha, name="IN National"):
    admin = alpha["admin"]
    cal = _calendar(admin, name, country_code="IN")
    _rule(admin, cal["id"], "Republic Day", params={"month": 1, "day": 26})
    _rule(admin, cal["id"], "Independence Day", params={"month": 8, "day": 15})
    _rule(admin, cal["id"], "Gandhi Jayanti", params={"month": 10, "day": 2})
    return cal


def _rows(cid, calendar_id, year):
    return owner_rows("SELECT id, name, date, status, source_ref FROM holidays WHERE company_id = :c AND "
                      "calendar_id = :k AND date BETWEEN :a AND :b ORDER BY date, id", c=cid, k=calendar_id,
                      a=f"{year}-01-01", b=f"{year}-12-31")


def _year(cid, calendar_id, year):
    rows = owner_rows("SELECT status, version, generation_status, generation_error, generation_summary FROM "
                      "holiday_calendar_years WHERE company_id = :c AND calendar_id = :k AND year = :y",
                      c=cid, k=calendar_id, y=year)
    return rows[0] if rows else None


# ============================================================================================== rollover runs
def test_lh_rec_11_14_rollover_is_idempotent_and_keeps_overrides_and_skips(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _national(alpha)
    with tenant(cid) as db:
        first = holiday_rollover.run_rollover(db, cid, 2027)
    line = next(r for r in first["calendars"] if r["calendar_id"] == cal["id"])
    assert line["result"] == "GENERATED" and line["counts"]["inserted"] == 3
    assert line["summary"]["holidays"] == 3 and line["summary"]["weekend"] >= 1  # 15 Aug 2027 is a Sunday
    before = _rows(cid, cal["id"], 2027)
    # LH-REC-14: second and third runs change nothing
    for _ in range(2):
        with tenant(cid) as db:
            again = holiday_rollover.run_rollover(db, cid, 2027)
        counts = next(r for r in again["calendars"] if r["calendar_id"] == cal["id"])["counts"]
        assert (counts["inserted"], counts["updated"], counts["deleted"]) == (0, 0, 0)
    assert _rows(cid, cal["id"], 2027) == before
    # LH-REC-11: an overridden row and a skipped row survive regeneration with their notes
    republic = next(r for r in before if r["name"] == "Republic Day")
    gandhi = next(r for r in before if r["name"] == "Gandhi Jayanti")
    assert admin.patch(f"{HC}/occurrences/{republic['id']}", json={"date": "2027-01-25"}).status_code == 200
    assert admin.patch(f"{HC}/occurrences/{gandhi['id']}", json={"action": "SKIP"}).status_code == 200
    with tenant(cid) as db:
        third = holiday_rollover.generate(db, cid, cal["id"], 2027)
    assert (third["counts"]["kept_overridden"], third["counts"]["kept_skipped"]) == (1, 1)
    assert any("you changed it to 2027-01-25" in n["message"] for n in third["notes"])
    after = {r["name"]: r for r in _rows(cid, cal["id"], 2027)}
    assert str(after["Republic Day"]["date"]) == "2027-01-25" and after["Gandhi Jayanti"]["status"] == "CANCELLED"
    assert len(after) == 3


def test_lh_rec_30_order_failure_and_calendars_without_rules(alpha, monkeypatch):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    state = _calendar(admin, "IN Karnataka", country_code="IN")  # created first, includes the national one
    national = _national(alpha)
    _rule(admin, state["id"], "Kannada Rajyotsava", params={"month": 11, "day": 1})
    assert admin.put(f"{HC}/{state['id']}/includes", json={"includes": [
        {"included_calendar_id": national["id"], "priority": 1}]}).status_code == 200
    legacy = _calendar(admin, "Branch legacy", country_code="IN")
    real = holiday_rollover.generate

    def flaky(db, company_id, calendar_id, year, actor=None):
        if calendar_id == state["id"]:
            raise RuntimeError("synthetic failure")
        return real(db, company_id, calendar_id, year, actor)

    monkeypatch.setattr(holiday_rollover, "generate", flaky)
    with tenant(cid) as db:
        result = holiday_rollover.run_rollover(db, cid, 2027)
    order = [r["calendar_id"] for r in result["calendars"]]
    assert order.index(national["id"]) < order.index(state["id"])  # included calendars first
    by = {r["calendar_id"]: r for r in result["calendars"]}
    assert by[state["id"]]["result"] == "FAILED" and by[national["id"]]["result"] == "GENERATED"
    assert by[legacy["id"]]["result"] == "NO_RULES"
    assert _year(cid, state["id"], 2027)["generation_status"] == "FAILED"
    assert _year(cid, national["id"], 2027)["status"] == "DRAFT"  # nothing is published automatically
    overview = admin.get(f"{HC}/rollover", params={"year": 2027}).json()
    items = {i["calendar_id"]: i for i in overview["items"]}
    assert items[state["id"]]["year_status"] == "FAILED" and "synthetic failure" in items[state["id"]]["error"]
    assert items[legacy["id"]]["year_status"] == "NO_RULES"
    assert items[national["id"]]["year_status"] == "DRAFT" and items[national["id"]]["counts"]["holidays"] == 3
    # Retry = "Generate drafts now": only the failed (and missing) calendars are generated.
    monkeypatch.setattr(holiday_rollover, "generate", real)
    retry = admin.post(f"{HC}/rollover/generate", json={"year": 2027})
    assert retry.status_code == 200, retry.text
    results = {r["calendar_id"]: r["result"] for r in retry.json()["calendars"]}
    assert results[state["id"]] == "GENERATED" and results[national["id"]] == "EXISTS"
    assert _year(cid, state["id"], 2027)["generation_status"] == "OK"


def test_next_year_overview_shapes_states_and_permissions(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _national(alpha)
    empty = admin.get(f"{HC}/rollover").json()  # default year = local year + 1
    assert empty["year"] == 2027 and empty["generate_on"] == "2026-10-01" and not empty["before_generate_on"]
    line = next(i for i in empty["items"] if i["calendar_id"] == cal["id"])
    assert line["year_status"] == "NOT_GENERATED" and line["rules"] == 3
    assert admin.post(f"{HC}/rollover/generate", json={"year": 2027}).status_code == 200
    ready = admin.get(f"{HC}/rollover", params={"year": 2027}).json()
    line = next(i for i in ready["items"] if i["calendar_id"] == cal["id"])
    assert line["year_status"] == "DRAFT" and line["last_generated_at"] and line["can_publish"]
    assert ready["generated_on"] and ready["published"] == 0 and ready["total"] >= 1
    publish = admin.post(f"{HC}/{cal['id']}/years/2027/publish", json={"dry_run": True}).json()
    codes = [w["code"] for w in publish["impact"]["warnings"] if w.get("confirm")]
    assert admin.post(f"{HC}/{cal['id']}/years/2027/publish", json={"acknowledged_warnings": codes}).status_code == 200
    done = admin.get(f"{HC}/rollover", params={"year": 2027}).json()
    assert next(i for i in done["items"] if i["calendar_id"] == cal["id"])["year_status"] == "PUBLISHED"
    assert done["published"] >= 1
    assert alpha["worker_client"].get(f"{HC}/rollover").status_code == 403
    assert admin.post(f"{HC}/rollover/generate", json={"year": 2025}).json()["code"] == "HOLIDAY_IN_PAST"
    assert admin.post(f"{HC}/rollover/generate", json={"year": 2030}).json()["code"] == "FUTURE_LIMIT"


def test_lh_rec_29_daily_check_runs_after_generate_on_once_and_notifies_publishers(alpha):
    """DAILY scheduled row: the first tick on/after generate_on enqueues ``holiday.rollover:{Y+1}`` (a late deploy or
    a moved date self-heals); later ticks find the key and run nothing twice."""
    from app.core import scheduler
    from app.database.db import system_engine
    from app.services import holiday_rollover, scheduler_fanout  # noqa: F401  (register the handlers)

    cid = alpha["company_id"]
    cal = _national(alpha)
    with system_engine.begin() as conn:
        conn.execute(text("UPDATE scheduled_jobs SET enabled = (job_type = 'holiday.rollover'), next_run_at = :t"),
                     {"t": datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)})
    with tenant(cid) as db:
        assert holiday_rollover.due_year(db, cid, D(2026, 9, 30)) is None
        assert holiday_rollover.due_year(db, cid, D(2026, 10, 1)) == 2027
    from app.database.db import SessionLocal

    for day in (2, 3):
        db = SessionLocal()
        try:
            scheduler.tick(db, datetime(2026, 11, day, 2, 0, tzinfo=timezone.utc))
            db.commit()
        finally:
            db.close()
    runs = owner_rows("SELECT idempotency_key, status FROM jobs WHERE company_id = :c AND type = 'holiday.rollover' "
                      "ORDER BY id", c=cid)
    assert [r["idempotency_key"] for r in runs].count("holiday.rollover:2027") == 1
    assert all(r["status"] == "SUCCEEDED" for r in runs)
    assert _year(cid, cal["id"], 2027)["status"] == "DRAFT"
    notes = owner_rows("SELECT title, message, action_url FROM notifications WHERE company_id = :c AND "
                       "title = '2027 holiday drafts are ready'", c=cid)
    assert notes and notes[0]["action_url"] == "/holidays/rollover?year=2027"
    assert "3 holiday(s)" in notes[0]["message"] and "Review before 1 Jan 2027" in notes[0]["message"]


def test_generate_on_setting_moves_the_due_date(alpha):
    from app.services import holiday_rollover

    """LH-REC-29: each tenant has its own ``holiday.rollover.generate_on`` (tenant B: 11-15); before that local
    date the daily check enqueues nothing, on it the run for next year is due."""
    from tests.holiday.test_years_publication import _set

    admin, cid = alpha["admin"], alpha["company_id"]
    _set(admin, "holiday.rollover.generate_on", "11-15")
    with tenant(cid) as db:
        assert holiday_rollover.generate_on(db, cid, 2026) == D(2026, 11, 15)
        assert holiday_rollover.due_year(db, cid, D(2026, 11, 2)) is None
        assert holiday_rollover.due_year(db, cid, D(2026, 11, 15)) == 2027
        assert holiday_rollover.schedule_tenant(db, cid, D(2026, 11, 14)) == {"due": False,
                                                                             "generate_on": "2026-11-15"}
    overview = admin.get(f"{HC}/rollover").json()
    assert overview["generate_on"] == "2026-11-15" and overview["before_generate_on"] is True


# ============================================================================================== reminders
def test_provisional_and_unpublished_reminders_are_sent_once(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "AE National", country_code="AE")
    r = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Eid al-Fitr", "date": "2027-03-09",
                                                          "holiday_type": "PUBLIC", "status": "PROVISIONAL"})
    assert r.status_code in (200, 201), r.text
    with tenant(cid) as db:
        assert holiday_rollover.provisional_reminders(db, cid, D(2027, 2, 23)) >= 1  # 14 days before
    with tenant(cid) as db:
        assert holiday_rollover.provisional_reminders(db, cid, D(2027, 2, 23)) == 0  # never twice
        assert holiday_rollover.provisional_reminders(db, cid, D(2027, 2, 24)) == 0  # 13 days: not a reminder day
    note = owner_rows("SELECT title, action_url FROM notifications WHERE company_id = :c AND title LIKE 'Confirm Eid%'",
                      c=cid)[0]
    assert note["title"] == "Confirm Eid al-Fitr (14 days)" and "mode=confirm" in note["action_url"]
    _national(alpha, "IN Rules")
    with tenant(cid) as db:
        assert holiday_rollover.unpublished_reminders(db, cid, D(2026, 11, 30)) == 0  # not a reminder date
        assert holiday_rollover.unpublished_reminders(db, cid, D(2026, 12, 1)) >= 1
    with tenant(cid) as db:
        assert holiday_rollover.unpublished_reminders(db, cid, D(2026, 12, 1)) == 0
    titles = [r["title"] for r in owner_rows("SELECT title FROM notifications WHERE company_id = :c AND title LIKE "
                                             "'%2027 is not published%'", c=cid)]
    assert titles and all("2026-12-01" in t for t in titles)


# ============================================================================================== closures
def _office(alpha):
    cal = _calendar(alpha["admin"], "Bengaluru Office", country_code="IN")
    _published(alpha["admin"], cal["id"], [("Founders Day", "2026-12-18", "COMPANY")])
    _link(alpha["admin"], alpha["branch"]["id"], holiday_calendar_id=cal["id"])
    return cal


def test_lh_hol_24_backdated_closure_recounts_leave_and_logs_backdated_closure(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _office(alpha)
    leave = _apply(alpha, "2026-11-03", "2026-11-04")
    assert leave.status_code == 200, leave.text
    assert _approve(alpha, leave.json()["id"]) == 2.0
    alpha["clock"].at_ist("2026-11-05", "09:00")
    body = {"date": "2026-11-04", "name": "Flood closure", "reason": "Flooding in the office area"}
    preview = admin.post(f"{HC}/{cal['id']}/closures", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    closure = preview.json()["closure"]
    assert closure["totals"]["employees"] >= 2 and closure["totals"]["approved_leave"] == 1
    assert closure["totals"]["scheduled"] >= 2 and not closure["requires_typed_confirm"]
    assert preview.json()["hint"].startswith("Not paid?")
    assert not owner_rows("SELECT id FROM holidays WHERE calendar_id = :k AND name = 'Flood closure'", k=cal["id"])
    done = admin.post(f"{HC}/{cal['id']}/closures", json=body)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "AMENDED" and done.json()["year"]["version"] == 2
    row = owner_rows("SELECT category, holiday_type, status, date FROM holidays WHERE calendar_id = :k AND "
                     "name = 'Flood closure'", k=cal["id"])[0]
    assert (row["category"], row["holiday_type"], row["status"], str(row["date"])) == (
        "EMERGENCY", "COMPANY", "CONFIRMED", "2026-11-04")
    audit = owner_rows("SELECT action FROM audit_logs WHERE company_id = :c AND action = 'BACKDATED_CLOSURE'", c=cid)
    assert len(audit) == 1
    assert float(owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave.json()["id"])[0][
        "number_of_days"]) == 1.0  # the closure date is credited back
    fact = _facts(alpha, D(2026, 11, 4), D(2026, 11, 4))[D(2026, 11, 4)]
    assert fact.holiday.name == "Flood closure" and fact.working_portion == "NONE"
    days = {d["date"]: d for d in alpha["worker_client"].get("/api/v1/me/calendar", params={
        "from": "2026-11-04", "to": "2026-11-04"}).json()["days"]}
    assert days["2026-11-04"]["holiday"]["category"] == "EMERGENCY"


def test_lh_hol_02_closure_inside_and_outside_the_backdate_window_and_validation(alpha):
    admin = alpha["admin"]
    cal = _office(alpha)
    alpha["clock"].at_ist("2026-11-20", "09:00")
    url = f"{HC}/{cal['id']}/closures"
    late = admin.post(url, json={"date": "2026-11-11", "reason": "Power cut", "dry_run": True})
    assert (late.status_code, late.json()["code"]) == (422, "BACKDATE_LIMIT")
    future = admin.post(url, json={"date": "2026-11-21", "reason": "x", "dry_run": True})
    assert (future.status_code, future.json()["code"]) == (400, "NOT_BACKDATED")
    blank = admin.post(url, json={"date": "2026-11-19", "reason": "   ", "dry_run": True})
    assert blank.status_code == 422 and blank.json()["code"] in ("REASON_REQUIRED", "VALIDATION_ERROR")
    draft = _calendar(admin, "Draft only", country_code="IN")
    unpublished = admin.post(f"{HC}/{draft['id']}/closures", json={"date": "2026-11-19", "reason": "Storm",
                                                                    "dry_run": True})
    assert unpublished.json()["code"] in ("YEAR_NOT_PUBLISHED", "HOLIDAY_IN_PAST")
    worker = alpha["worker_client"].post(url, json={"date": "2026-11-19", "reason": "Storm"})
    assert worker.status_code == 403
    ok = admin.post(url, json={"date": "2026-11-20", "name": "Strike closure", "reason": "City-wide strike",
                               "portion": "FULL"})
    assert ok.status_code == 200, ok.text
    again = admin.post(url, json={"date": "2026-11-20", "name": "Strike closure", "reason": "City-wide strike"})
    assert again.status_code == 409


def test_closure_in_a_finalized_period_needs_the_typed_date_and_queues_retro(alpha, monkeypatch):
    from app.services import holiday_closures

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _office(alpha)
    alpha["clock"].at_ist("2026-11-06", "09:00")
    real = holiday_closures.closure_impact

    def finalized(db, company_id, calendar, day):
        out = real(db, company_id, calendar, day)
        for row in out["employees"]:
            row["period_state"] = "FINALIZED"
        out["totals"]["finalized"] = out["totals"]["employees"]
        out["requires_typed_confirm"] = True
        return out

    monkeypatch.setattr(holiday_closures, "closure_impact", finalized)
    from app.payroll.services import calendars as pay_calendars

    monkeypatch.setattr(pay_calendars, "period_state_on", lambda db, c, e, d, now=None: "FINALIZED")
    body = {"date": "2026-11-02", "name": "Power failure", "reason": "Grid failure"}
    preview = admin.post(f"{HC}/{cal['id']}/closures", json={**body, "dry_run": True}).json()
    assert preview["closure"]["requires_typed_confirm"] and preview["closure"]["totals"]["finalized"] >= 1
    refused = admin.post(f"{HC}/{cal['id']}/closures", json=body)
    assert (refused.status_code, refused.json()["code"]) == (422, "CONFIRM_DATE_REQUIRED")
    done = admin.post(f"{HC}/{cal['id']}/closures", json={**body, "confirm_date": "2026-11-02"})
    assert done.status_code == 200, done.text
    payroll = done.json()["effects"]["payroll"]
    assert payroll["finalized"]["2026-11-02"] and payroll["retro_job_id"]
    job = owner_rows("SELECT type, status FROM jobs WHERE id = :i", i=payroll["retro_job_id"])[0]
    assert job["type"] == "holiday.closure_retro" and job["status"] in ("SUCCEEDED", "QUEUED", "PENDING")
    flagged = owner_rows("SELECT action FROM audit_logs WHERE company_id = :c AND action = 'CLOSURE_PAYROLL_FLAGGED'",
                         c=cid)
    assert len(flagged) == 1


# ============================================================================================== LH-REC-12, -13, -15
def _publish_year(admin, calendar_id, year):
    url = f"{HC}/{calendar_id}/years/{year}/publish"
    dry = admin.post(url, json={"dry_run": True}).json()
    codes = [w["code"] for w in dry["impact"]["warnings"] if w.get("confirm")]
    r = admin.post(url, json={"acknowledged_warnings": codes})
    assert r.status_code == 200, r.text


def test_lh_rec_12_rule_deactivated_after_its_year_was_published(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "IN Deactivate", country_code="IN")
    rule = _rule(admin, cal["id"], "Founders Day", params={"month": 3, "day": 10})
    other = _rule(admin, cal["id"], "Harvest Day", params={"month": 4, "day": 14})
    for year in (2027, 2028):
        with tenant(cid) as db:
            holiday_rollover.run_rollover(db, cid, year, notify=False)
    _publish_year(admin, cal["id"], 2027)
    r = admin.post(f"{HC}/rules/{rule['id']}/deactivate", json={"reason": "No longer observed"})
    assert r.status_code == 200, r.text
    assert 2027 in r.json()["published_years_affected"]
    names_2028 = [row["name"] for row in _rows(cid, cal["id"], 2028)]
    assert "Founders Day" not in names_2028 and "Harvest Day" in names_2028  # the DRAFT row is gone
    published = [row for row in _rows(cid, cal["id"], 2027) if row["name"] == "Founders Day"]
    assert published and published[0]["status"] == "CONFIRMED"  # the published occurrence still counts
    assert other["id"]


def test_lh_rec_13_weekend_change_regenerates_drafts_and_lists_published_moves(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "Gulf calendar", country_code="AE")
    assert admin.patch(f"{HC}/{cal['id']}", json={"weekend_days": [6]}).status_code == 200  # Sunday only
    _rule(admin, cal["id"], "Christmas", params={"month": 12, "day": 25}, observed_rule="NEAREST_WORKDAY")
    with tenant(cid) as db:
        holiday_rollover.run_rollover(db, cid, 2027, notify=False)
        holiday_rollover.run_rollover(db, cid, 2028, notify=False)
    _publish_year(admin, cal["id"], 2028)
    before = [(r["name"], str(r["date"])) for r in _rows(cid, cal["id"], 2027) if r["status"] != "CANCELLED"]
    assert ("Christmas", "2027-12-25") in before and len(before) == 1  # Saturday is a working day: not moved
    changed = admin.patch(f"{HC}/{cal['id']}", json={"weekend_days": [4, 5]})  # Friday-Saturday weekend
    assert changed.status_code == 200, changed.text
    report = changed.json()["weekend_change"]
    assert report["weekend"] == [4, 5]
    assert [r["year"] for r in report["regenerated"]] == [2027]
    after = {(r["name"], str(r["date"]), r["status"]) for r in _rows(cid, cal["id"], 2027)}
    observed = [r for r in owner_rows("SELECT date, observance FROM holidays WHERE calendar_id = :k AND "
                                      "observance = 'OBSERVED'", k=cal["id"])]
    # Saturday is now a weekend day: the rule observes Christmas on the nearest working day (Fri is off too).
    assert any(str(o["date"]) in ("2027-12-23", "2027-12-26") for o in observed), (after, observed)
    # 2028 is published: Christmas 2028 is a Monday (no move) — nothing to review, nothing written there.
    assert all(item["year"] != 2027 for item in report["published_review"])


def test_lh_rec_15_cross_year_observed_row_is_offered_not_written(alpha):
    from app.services import holiday_rollover

    admin, cid = alpha["admin"], alpha["company_id"]
    cal = _calendar(admin, "US Company 2028", country_code="US")
    assert admin.patch(f"{HC}/{cal['id']}", json={"weekend_days": [5, 6]}).status_code == 200
    _published(admin, cal["id"], [("Thanksgiving", "2027-11-25", "PUBLIC")], year=2027)
    _rule(admin, cal["id"], "New Year's Day", params={"month": 1, "day": 1}, observed_rule="NEAREST_WORKDAY")
    with tenant(cid) as db:
        result = holiday_rollover.generate(db, cid, cal["id"], 2028)
    cross = result["cross_year_items"]
    assert len(cross) == 1 and cross[0]["date"] == "2027-12-31" and cross[0]["observance"] == "OBSERVED"
    rows_2028 = [(r["name"], str(r["date"])) for r in _rows(cid, cal["id"], 2028)]
    assert rows_2028 == [("New Year's Day", "2028-01-01")]  # the ORIGINAL only
    assert not [r for r in _rows(cid, cal["id"], 2027) if r["name"] == "New Year's Day"]  # nothing written into 2027
    overview = admin.get(f"{HC}/rollover", params={"year": 2028}).json()
    assert next(i for i in overview["items"] if i["calendar_id"] == cal["id"])["year_status"] == "DRAFT"
