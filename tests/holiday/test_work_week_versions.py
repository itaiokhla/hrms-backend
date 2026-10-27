"""Work-week patterns, versions and links through the API (LH-M1 WI-1.13; 04-holiday/03 §5; 11 §6.11).

Cases: LH-WWK-05 (API), 07, 08, 09, 11, 12, 13 (API), 17 (API), 20 (Goal 1 reading: a version transition keeps
past day facts), 24, 29; LH-TZ-08; LH-SEC-01 (another tenant's pattern id answers 404); BUG-13 (legacy writes need
holiday:manage). The company clock is the attendance fake clock (app.core.clock.utcnow).
"""

from datetime import date

import pytest

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.time_goldens import tenant_db

NTH = {"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}]}
MON_SAT = {"weekly_off_days": [6], "nth_weekday_offs": []}
ALT_SAT = {"kind": "CYCLE", "cycle_length_days": 14, "anchor_date": "2027-01-04", "cycle_off_days": [6, 12, 13]}
WW = "/api/v1/work-weeks"


def _facts(company_id: int, employee_id: int, start: date, end: date):
    from app.payroll.services import time_inputs

    with tenant_db(company_id) as db:
        return {f.date: f for f in time_inputs.day_facts(db, company_id, employee_id, start, end)}


@pytest.fixture
def alpha(app, fake_clock):
    fake_clock.at_ist("2027-02-20", "10:00")
    admin = signup_company(app, "Alpha Weeks")
    branch = admin.get("/api/v1/branches").json()
    branch = (branch.get("items") if isinstance(branch, dict) else branch)[0]
    boss = create_employee(admin, "Arjun", "Mehta")
    worker = create_employee(admin, "Priya", "Shah", manager_id=boss["id"], branch_id=branch["id"])
    return {"admin": admin, "branch": branch, "clock": fake_clock, "boss": boss, "worker": worker,
            "company_id": admin.company_id,
            "boss_client": invite_and_activate(app, admin, boss, ["manager"]),
            "worker_client": invite_and_activate(app, admin, worker, ["employee"])}


def _create(admin, name, rules=None, template=None, **extra):
    body = {"name": name, **extra}
    if rules is not None:
        body["rules"] = rules
    if template is not None:
        body["template_code"] = template
    r = admin.post(WW, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _link(admin, branch_id, pattern_id, **extra):
    r = admin.put(f"/api/v1/branches/{branch_id}/calendar-link", json={"work_week_pattern_id": pattern_id, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------------------------- reads
def test_starter_pattern_has_version_one_and_templates_are_listed(alpha):
    listed = alpha["admin"].get(WW).json()
    mon_fri = next(p for p in listed["items"] if p["code"] == "MON_FRI")
    assert mon_fri["current_version"]["version"] == 1
    assert mon_fri["current_version"]["effective_from"] == "2000-01-01"
    assert mon_fri["summary"] == "Sat, Sun off" and listed["can_create"] is True
    templates = {t["code"]: t for t in alpha["admin"].get(f"{WW}/templates").json()["items"]}
    assert len(templates) == 12
    assert templates["MON_SAT_2ND_4TH_SAT_OFF"]["available"] is True
    assert templates["FRI_HALF"]["available"] is False
    assert templates["FRI_HALF"]["unavailable_reason"] == "Available after half-day weekly offs are enabled"
    assert templates["ALT_SAT_OFF"]["rules"]["anchor_date"] == "2027-02-22"  # next Monday after 2027-02-20


def test_lh_wwk_05_pune_factory_schedule_from_template_and_preview(alpha):
    created = _create(alpha["admin"], "Pune Factory Schedule", template="MON_SAT_2ND_4TH_SAT_OFF")
    assert created["code"] == "PUNE_FACTORY_SCHEDULE" and created["rules"] == NTH
    assert created["current_version"]["effective_from"] == "2027-02-20"
    preview = alpha["admin"].post(f"{WW}/preview", json={"rules": NTH, "from": "2027-01-01", "to": "2027-01-31"})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert [int(d["date"][-2:]) for d in body["days"] if d["portion"] == "FULL"] == [3, 9, 10, 17, 23, 24, 31]
    assert body["totals"]["working_days"] == 24
    leap = alpha["admin"].post(f"{WW}/preview", json={"rules": NTH, "from": "2028-02-01", "to": "2028-02-29"}).json()
    assert [d["date"] for d in leap["days"] if d["portion"] == "FULL" and d["weekday"] == 5] == ["2028-02-12",
                                                                                              "2028-02-26"]
    assert len(leap["days"]) == 29
    audit = owner_rows("SELECT action FROM audit_logs WHERE module = 'Holidays' AND entity_id = :i ORDER BY id",
                       i=str(created["id"]))
    assert [a["action"] for a in audit] == ["SAVE", "WORKWEEK_VERSIONED"]
    log = owner_rows("SELECT record_type, date_from FROM source_change_log WHERE record_type = 'work_week_version'")
    assert {"record_type": "work_week_version", "date_from": date(2027, 2, 20)} in log


def test_lh_wwk_13_engine_codes_and_half_day_gate_through_the_api(alpha):
    admin = alpha["admin"]
    for rules, code in [
        ({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [1, 2, 3, 4, -1]}]}, "EVERY_OCCURRENCE"),
        ({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 6, "nth": [2]}]}, "REDUNDANT_RULE"),
        ({"weekly_off_days": [0, 1, 2, 3, 4, 5, 6]}, "NO_WORKING_DAYS"),
        ({"kind": "CYCLE", "cycle_length_days": 14, "cycle_off_days": [6]}, "ANCHOR_REQUIRED"),
        ({"kind": "CYCLE", "cycle_length_days": 60, "anchor_date": "2027-01-04", "cycle_off_days": []}, "INVALID_CYCLE"),
        ({"kind": "WEEKLY", "weekly_off_days": [6], "cycle_length_days": 7}, "KIND_MISMATCH"),
        ({"kind": "WEEKLY", "weekly_off_days": [6], "half_day_offs": [{"weekday": 5, "portion": "SECOND_HALF"}]},
         "INVALID_PORTION"),  # V-9 gate until LH-M5b
    ]:
        r = admin.post(WW, json={"name": f"Bad {code}", "rules": rules})
        assert (r.status_code, r.json().get("code")) == (400, code), r.text
    half = {"kind": "WEEKLY", "weekly_off_days": [6], "half_day_offs": [{"weekday": 5, "portion": "SECOND_HALF"}]}
    preview = admin.post(f"{WW}/preview", json={"rules": half, "from": "2027-01-01", "to": "2027-01-31"}).json()
    assert sum(1 for d in preview["days"] if d["portion"] == "SECOND_HALF") == 5
    assert preview["issues"][0]["code"] == "INVALID_PORTION"
    too_long = admin.post(f"{WW}/preview", json={"rules": NTH, "from": "2027-01-01", "to": "2028-06-01"})
    assert (too_long.status_code, too_long.json()["code"]) == (400, "RANGE_TOO_LONG")


# ---------------------------------------------------------------------------------------------- versions
def test_lh_wwk_20_version_transition_keeps_past_day_facts(alpha):
    """Goal 1 reading of LH-WWK-20: a version from 2027-04-01 changes no date before it. LH-WWK-24: the mirror columns
    follow later through holiday.link_activate, which emits workweek.version.activated; day facts never read them."""
    admin, cid, worker = alpha["admin"], alpha["company_id"], alpha["worker"]["id"]
    pattern = _create(admin, "Pune Factory Schedule", rules=NTH)
    _link(admin, alpha["branch"]["id"], pattern["id"])  # effective today (2027-02-20)
    before = _facts(cid, worker, date(2027, 2, 1), date(2027, 6, 30))
    assert before[date(2027, 2, 6)].weekly_off and before[date(2027, 2, 6)].work_week_id != pattern["id"]  # MON_FRI
    assert before[date(2027, 3, 13)].weekly_off and before[date(2027, 3, 13)].work_week_id == pattern["id"]
    assert not before[date(2027, 3, 20)].weekly_off  # 3rd Saturday
    r = admin.post(f"{WW}/{pattern['id']}/versions", json={"rules": MON_SAT, "effective_from": "2027-04-01"})
    assert r.status_code == 201, r.text
    assert r.json()["version"]["version"] == 2 and r.json()["version"]["state"] == "SCHEDULED"
    after = _facts(cid, worker, date(2027, 2, 1), date(2027, 6, 30))
    for day in (d for d in before if d < date(2027, 4, 1)):
        assert (after[day].weekly_off, after[day].work_week_id) == (before[day].weekly_off, before[day].work_week_id)
    assert after[date(2027, 3, 27)].pattern_version == 1
    assert not after[date(2027, 4, 10)].weekly_off and after[date(2027, 4, 10)].pattern_version == 2
    versions = admin.get(f"{WW}/{pattern['id']}/versions").json()["items"]
    assert [(v["version"], v["effective_from"], v["effective_to"]) for v in versions] == [
        (2, "2027-04-01", None), (1, "2027-02-20", "2027-03-31")]
    assert versions[1]["rules"] == NTH  # history is never rewritten
    detail = admin.get(f"{WW}/{pattern['id']}").json()
    assert detail["rules"] == NTH and detail["next_change"] == {"version": 2, "effective_from": "2027-04-01"}
    # LH-WWK-24: the activation job mirrors the version when it becomes valid and emits the event.
    alpha["clock"].at_ist("2027-04-01", "01:00")
    from app.services import holiday_assignments

    with tenant_db(cid) as db:
        assert holiday_assignments.activate(db, cid, date(2027, 4, 1))["patterns"] == 1
        db.commit()
    assert admin.get(f"{WW}/{pattern['id']}").json()["rules"] == MON_SAT
    assert owner_rows("SELECT count(*) AS n FROM outbox_events WHERE topic = 'workweek.version.activated'")[0]["n"] >= 1


def test_lh_wwk_08_same_effective_from_replaces_the_future_version_and_no_change_is_refused(alpha):
    admin = alpha["admin"]
    pattern = _create(admin, "Office week", rules={"weekly_off_days": [5, 6], "nth_weekday_offs": []})
    assert admin.post(f"{WW}/{pattern['id']}/versions",
                      json={"rules": MON_SAT, "effective_from": "2027-04-01"}).status_code == 201
    alpha["clock"].at_ist("2027-03-10", "10:00")
    r = admin.post(f"{WW}/{pattern['id']}/versions", json={"rules": NTH, "effective_from": "2027-04-01"})
    assert r.status_code == 201, r.text
    versions = admin.get(f"{WW}/{pattern['id']}/versions").json()["items"]
    assert [(v["version"], v["effective_from"]) for v in versions] == [(3, "2027-04-01"), (1, "2027-02-20")]
    audit = owner_rows("SELECT details::text AS d FROM audit_logs WHERE action = 'WORKWEEK_VERSIONED' ORDER BY id DESC "
                       "LIMIT 1")[0]["d"]
    assert '"replaced_versions"' in audit and '"version": 2' in audit
    again = admin.post(f"{WW}/{pattern['id']}/versions", json={"rules": NTH, "effective_from": "2027-04-01"})
    assert (again.status_code, again.json()["code"]) == (409, "NO_CHANGE")


def test_lh_wwk_09_backdating_window_reason_and_publish_permission(app, alpha):
    admin = alpha["admin"]
    pattern = _create(admin, "Plant week", rules=MON_SAT)
    alpha["clock"].at_ist("2027-03-25", "10:00")
    sat_off = {"weekly_off_days": [5, 6], "nth_weekday_offs": []}
    no_reason = admin.post(f"{WW}/{pattern['id']}/versions", json={"rules": sat_off, "effective_from": "2027-03-20"})
    assert (no_reason.status_code, no_reason.json()["code"]) == (400, "REASON_REQUIRED")
    too_old = admin.post(f"{WW}/{pattern['id']}/versions",
                         json={"rules": sat_off, "effective_from": "2027-03-15", "reason": "Plant closed Saturdays"})
    assert (too_old.status_code, too_old.json()["code"]) == (422, "BACKDATE_LIMIT")
    # An administrator with holiday:manage but not holiday:publish cannot backdate.
    role = admin.post("/api/v1/roles", json={"name": "Work calendar editor", "grants": [
        {"permission": "holiday:manage", "scope": "COMPANY"}, {"permission": "holiday:read", "scope": "COMPANY"}]})
    assert role.status_code in (200, 201), role.text
    editor_emp = create_employee(admin, "Sunil", "Rao")
    r = admin.post("/api/v1/users/invite", json={"email": editor_emp["email"], "full_name": "Sunil Rao",
                                                 "employee_id": editor_emp["id"], "role_ids": [role.json()["id"]]})
    from tests.helpers import ApiClient, PASSWORD

    editor = ApiClient(app)
    assert editor.post("/api/v1/auth/invitations/accept", json={
        "token": r.json()["invite_url"].split("token=")[1], "password": PASSWORD}).status_code == 200
    denied = editor.post(f"{WW}/{pattern['id']}/versions",
                         json={"rules": sat_off, "effective_from": "2027-03-20", "reason": "Plant closed Saturdays"})
    assert denied.status_code == 403, denied.text
    ok = admin.post(f"{WW}/{pattern['id']}/versions",
                    json={"rules": sat_off, "effective_from": "2027-03-20", "reason": "Plant closed Saturdays"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["version"]["reason"] == "Plant closed Saturdays"
    future = editor.post(f"{WW}/{pattern['id']}/versions", json={"rules": MON_SAT, "effective_from": "2027-05-01"})
    assert future.status_code == 201, future.text  # not backdated: holiday:manage is enough


def test_lh_wwk_07_parity_flip_recounts_approved_leave(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    pattern = _create(admin, "Alternate Saturdays", rules=ALT_SAT)
    _link(admin, alpha["branch"]["id"], pattern["id"])
    leave = alpha["worker_client"].post("/api/v1/leaves/apply", json={
        "leave_type": "casual", "start_date": "2027-03-08", "end_date": "2027-03-15", "reason": "Trip"})
    assert leave.status_code == 200, leave.text
    approved = alpha["boss_client"].post(f"/api/v1/leaves/{leave.json()['id']}/approve", json={"remarks": "ok"})
    assert approved.status_code == 200, approved.text
    charged = owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave.json()["id"])[0]["number_of_days"]
    assert float(charged) == 6.0  # Sat 03-13 (cycle day 12) and Sun 03-14 off under v1
    preview = admin.post(f"{WW}/preview", json={"pattern_id": pattern["id"], "effective_from": "2027-03-01",
                                                "rules": {**ALT_SAT, "anchor_date": "2027-03-08"}}).json()
    assert preview["impact"]["leave_recount"]["requests"] == 1
    assert preview["impact"]["leave_recount"]["units"] == -1.0  # Sat 03-13 becomes a working day
    assert preview["impact"]["employees_affected"]["count"] == 2  # Arjun and Priya both sit at the branch
    r = admin.post(f"{WW}/{pattern['id']}/versions",
                   json={"rules": {**ALT_SAT, "anchor_date": "2027-03-08"}, "effective_from": "2027-03-01"})
    assert r.status_code == 201, r.text
    assert r.json()["recount"]["mode"] == "inline" and r.json()["recount"]["changed"] >= 1
    charged = owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave.json()["id"])[0]["number_of_days"]
    assert float(charged) == 7.0
    notes = owner_rows("SELECT title, message FROM notifications WHERE title = 'Your weekly offs change'")
    assert notes and "2027-03-01" in notes[0]["message"]


def test_lh_wwk_17_junction_warning_needs_acknowledgement(alpha):
    admin = alpha["admin"]
    entity = admin.get("/api/v1/legal-entities").json()
    entity = (entity.get("items") if isinstance(entity, dict) else entity)[0]
    assert entity["country_code"] == "IN"
    pattern = _create(admin, "Pune plant rota", rules=MON_SAT)
    cycle = {"kind": "CYCLE", "cycle_length_days": 14, "anchor_date": "2027-04-01", "cycle_off_days": [8, 9, 13]}
    r = admin.post(f"{WW}/{pattern['id']}/versions", json={"rules": cycle, "effective_from": "2027-04-01"})
    assert (r.status_code, r.json()["code"]) == (422, "WARNINGS_NOT_ACKNOWLEDGED"), r.text
    runs = [i for i in r.json()["issues"] if i["rule"] == "R-2"]
    assert any((i["start"], i["end"], i["count"]) == ("2027-03-29", "2027-04-08", 11) for i in runs)
    assert all(i["code"] == "WEEKLY_REST_EXCEEDED" and i["legal_entity_id"] == entity["id"] for i in r.json()["issues"])
    ok = admin.post(f"{WW}/{pattern['id']}/versions",
                    json={"rules": cycle, "effective_from": "2027-04-01", "acknowledge_warnings": True})
    assert ok.status_code == 201, ok.text and ok.json()["warnings"]
    audit = owner_rows("SELECT details::text AS d FROM audit_logs WHERE action = 'WORKWEEK_VERSIONED' ORDER BY id DESC "
                       "LIMIT 1")[0]["d"]
    assert '"acknowledged_warnings": ["R-1", "R-2"]' in audit or '"R-2"' in audit


# ---------------------------------------------------------------------------------------------- usage, ownership
def test_lh_wwk_11_deactivating_a_pattern_in_use(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    linked = _create(admin, "Linked week", rules=MON_SAT)
    _link(admin, alpha["branch"]["id"], linked["id"])
    r = admin.patch(f"{WW}/{linked['id']}", json={"status": "INACTIVE"})
    assert (r.status_code, r.json()["code"]) == (409, "PATTERN_IN_USE")
    legacy = admin.patch(f"/api/v1/payroll/org/work-weeks/{linked['id']}", json={"status": "INACTIVE"})
    assert (legacy.status_code, legacy.json()["code"]) == (409, "ORG_DIMENSION_IN_USE")
    crew = _create(admin, "Panama B", rules={**ALT_SAT})
    with tenant_db(cid) as db:  # a future criteria row (the calendar-assignment API arrives with the calendar goal)
        from app.models.pay_org import HolidayCalendarAssignment

        db.add(HolidayCalendarAssignment(company_id=cid, work_week_pattern_id=crew["id"], criteria={"shift_ids": [1]},
                                         priority=10, effective_from=date(2027, 6, 1)))
        db.commit()
    r = admin.patch(f"{WW}/{crew['id']}", json={"status": "INACTIVE"})
    assert (r.status_code, r.json()["code"]) == (409, "PATTERN_IN_USE")
    detail = admin.get(f"{WW}/{crew['id']}").json()
    assert "deactivate" not in detail["allowed_actions"] and detail["in_use"]
    free = _create(admin, "Unused week", rules=MON_SAT)
    assert admin.patch(f"{WW}/{free['id']}", json={"status": "INACTIVE"}).json()["status"] == "INACTIVE"


def test_lh_wwk_12_new_placements_no_longer_copy_the_branch_pattern(alpha):
    rows = owner_rows("SELECT work_week_pattern_id FROM employee_assignments WHERE employee_id = :e",
                      e=alpha["worker"]["id"])
    assert rows and all(r["work_week_pattern_id"] is None for r in rows)
    facts = _facts(alpha["company_id"], alpha["worker"]["id"], date(2027, 3, 1), date(2027, 3, 7))
    branch_pattern = owner_rows("SELECT work_week_pattern_id FROM branches WHERE id = :b",
                                b=alpha["branch"]["id"])[0]["work_week_pattern_id"]
    assert {f.work_week_id for f in facts.values()} == {branch_pattern}


def test_lh_wwk_29_entity_owned_pattern_cannot_be_linked_to_another_entity(alpha):
    admin = alpha["admin"]
    other = admin.post("/api/v1/legal-entities", json={"code": "LE2", "legal_name": "Alpha Gulf FZ-LLC",
                                                      "country_code": "AE", "base_currency": "AED",
                                                      "timezone": "Asia/Dubai"})
    assert other.status_code in (200, 201), other.text
    owned = _create(admin, "Gulf week", rules={"weekly_off_days": [4, 5], "nth_weekday_offs": []},
                    legal_entity_id=other.json()["id"])
    assert owned["legal_entity_id"] == other.json()["id"]
    r = admin.put(f"/api/v1/branches/{alpha['branch']['id']}/calendar-link",
                  json={"work_week_pattern_id": owned["id"]})
    assert (r.status_code, r.json()["code"]) == (422, "PATTERN_ENTITY_MISMATCH")


def test_lh_tz_08_pattern_local_today_follows_users_ahead(alpha):
    admin = alpha["admin"]
    kiritimati = admin.post("/api/v1/branches", json={
        "legal_entity_id": alpha["branch"]["legal_entity_id"], "code": "KIR", "name": "Line Islands Desk",
        "timezone": "Pacific/Kiritimati"})
    assert kiritimati.status_code in (200, 201), kiritimati.text
    pattern = _create(admin, "Island week", rules=MON_SAT)
    _link(admin, kiritimati.json()["id"], pattern["id"])
    alpha["clock"].at_ist("2027-02-28", "20:00")  # 14:30 UTC: still 2027-02-28 in India, 2027-03-01 in Kiritimati
    detail = admin.get(f"{WW}/{pattern['id']}").json()
    assert detail["local_today"] == "2027-03-01"
    r = admin.post(f"{WW}/{pattern['id']}/versions", json={"rules": NTH, "effective_from": "2027-02-28"})
    assert (r.status_code, r.json()["code"]) == (400, "REASON_REQUIRED")  # backdated by the users' clock


def test_legacy_alias_posts_a_version_instead_of_rewriting(alpha):
    admin = alpha["admin"]
    created = admin.post("/api/v1/payroll/org/work-weeks", json={"code": "LEGACY", "name": "Legacy week",
                                                                 "weekly_off_days": [6]})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    versions = admin.get(f"{WW}/{pid}/versions").json()["items"]
    assert [(v["version"], v["effective_from"]) for v in versions] == [(1, "2000-01-01")]
    r = admin.patch(f"/api/v1/payroll/org/work-weeks/{pid}", json={"weekly_off_days": [5, 6]})
    assert r.status_code == 200, r.text
    versions = admin.get(f"{WW}/{pid}/versions").json()["items"]
    assert [(v["version"], v["effective_from"], v["effective_to"]) for v in versions] == [
        (2, "2027-02-20", None), (1, "2000-01-01", "2027-02-19")]
    assert versions[1]["rules"] == MON_SAT


def test_permissions_scope_and_concurrency(app, alpha):
    admin = alpha["admin"]
    pattern = _create(admin, "Scoped week", rules=MON_SAT)
    employee = alpha["worker_client"]
    assert employee.get(WW).status_code == 200  # holiday:read (self-service, company-wide)
    assert employee.post(WW, json={"name": "Nope", "rules": MON_SAT}).status_code == 403
    other = signup_company(app, "Other Tenant")
    assert other.get(f"{WW}/{pattern['id']}").status_code == 404  # LH-SEC-01
    assert other.post(f"{WW}/{pattern['id']}/versions", json={"rules": NTH}).status_code == 404
    stale = admin.patch(f"{WW}/{pattern['id']}", json={"name": "Renamed"}, headers={"If-Match": '"99"'})
    assert (stale.status_code, stale.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    renamed = admin.patch(f"{WW}/{pattern['id']}", json={"name": "Renamed"},
                          headers={"If-Match": str(pattern["row_version"])})
    assert renamed.status_code == 200 and renamed.json()["row_version"] == pattern["row_version"] + 1
    manager = alpha["boss_client"]  # BUG-13: legacy work-week writes need holiday:manage
    assert manager.post("/api/v1/payroll/org/work-weeks", json={"code": "X", "name": "X",
                                                                "weekly_off_days": [6]}).status_code == 403


def test_preview_names_input_locked_payroll_periods_that_get_retro(app, fake_clock):
    """04-holiday/03 §5.3, §5.7: dates in an input-locked period are allowed but reach payroll only through retro."""
    from tests.payroll.test_runs_lifecycle import calendar_for, periods_of
    from tests.payroll.test_wage_inputs import pay_group, place, sql

    today = date.today()
    fake_clock.at_ist(today.isoformat(), "10:00")
    admin = signup_company(app, "Locked Weeks")
    cid = admin.company_id
    branch = admin.get("/api/v1/branches").json()
    branch = (branch.get("items") if isinstance(branch, dict) else branch)[0]
    worker = create_employee(admin, "Ravi", "Kumar", branch_id=branch["id"])
    group = pay_group(cid, branch["legal_entity_id"], "WWLOCK")
    place(worker["id"], branch["legal_entity_id"], group)
    calendar_for(admin, group)
    pattern = _create(admin, "Plant week", rules=NTH)
    _link(admin, branch["id"], pattern["id"])
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    period = next(p for p in periods_of(admin, group) if p["start"] <= nxt.isoformat() <= p["end"])
    sql("UPDATE pay_periods SET status = 'PROCESSING' WHERE id = :p", p=period["id"])
    preview = admin.post(f"{WW}/preview", json={"pattern_id": pattern["id"], "rules": MON_SAT,
                                                "effective_from": nxt.isoformat()})
    assert preview.status_code == 200, preview.text
    locked = preview.json()["impact"]["locked_periods"]
    assert [(p["pay_group_id"], p["state"], p["period_start"]) for p in locked] == [
        (group, "INPUT_LOCKED", period["start"])]
    assert locked[0]["changed_dates"] == 2  # the 2nd and 4th Saturdays of that month become working days
    r = admin.post(f"{WW}/{pattern['id']}/versions", json={"rules": MON_SAT, "effective_from": nxt.isoformat()})
    assert r.status_code == 201, r.text  # allowed; the change log carries it to payroll
    assert owner_rows("SELECT count(*) AS n FROM source_change_log WHERE record_type = 'work_week_version' "
                      "AND company_id = :c AND date_from = :d", c=cid, d=nxt)[0]["n"] == 1
