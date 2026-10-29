"""AS-M4 attendance requests, routing, on-duty, muster and period close (AS 04-attendance/04; WI-AS-4.8).

Vectors and cases: GV-AS-REG-01 / -02 (missed punch, manager approves, day PRESENT), -05 (excuse keeps the mark),
-06 (limit: SELF refused, ON_BEHALF acknowledged warning, system-caused reasons not counted), -07 (window closed, HR
DIRECT), -08 (on-duty batch skips the weekly off, one approval), -09 (a lead's own request falls back to the manager),
-12 / GV-AS-CAP-03 (muster), -14 (period close), -15 (requester barred → skip level); AS-REG-01 (barring), -04
(per-row muster, no existence leak), AS-RBAC (SELF_EDIT_FORBIDDEN, SELF_DECISION_FORBIDDEN, 404 out of scope),
v1 tenant 404.
"""

import itertools
from datetime import date

import pytest

from tests.attendance.capture_v2_helpers import rows, sql, v2_punch
from tests.attendance_helpers import employee_with_login, fake_clock  # noqa: F401
from tests.helpers import create_employee, invite_and_activate, role_id, signup_company

DAY = date(2026, 11, 3)  # Tuesday
REQ = "/api/v1/attendance/requests"
_names = itertools.count(1)


def enable_v2(company_id):
    updated = sql("UPDATE setting_values SET value = 'true'::jsonb, effective_from = '2026-01-01' WHERE company_id = :c "
                  "AND key = 'attendance.v2.enabled' AND scope_type = 'TENANT' AND status = 'ACTIVE'", c=company_id)
    if updated.rowcount == 0:
        sql("INSERT INTO setting_values (company_id, key, scope_type, value, locked, effective_from, status, "
            "row_version, source, created_at, updated_at) VALUES (:c, 'attendance.v2.enabled', 'TENANT', "
            "'true'::jsonb, false, '2026-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id)


def activate_request_policy(admin, fields):
    n = next(_names)
    r = admin.post("/api/v1/attendance/policies", json={"type": "ATTENDANCE_REQUEST_POLICY", "name": f"Requests {n}",
                                                        "fields": fields})
    assert r.status_code == 201, r.text
    policy = r.json()
    r = admin.put(f"/api/v1/attendance/policies/{policy['id']}/assignments",
                  json={"rules": [{"criteria": {"everyone": True}, "priority": 10 * n}]})
    assert r.status_code == 200, r.text
    r = admin.post(f"/api/v1/attendance/policies/{policy['id']}/versions/1/activate",
                   json={"effective_from": "2026-01-01", "reason": "AS-M4 test"})
    assert r.status_code == 200, r.text


@pytest.fixture
def world(app, fake_clock):
    fake_clock.at_ist("2026-11-01", "09:00")
    admin = signup_company(app, "Requests Co")
    cid = admin.company_id
    enable_v2(cid)
    arjun_e = create_employee(admin, "Arjun", "Mehta")
    arjun = invite_and_activate(app, admin, arjun_e, ["manager"])
    priya = employee_with_login(app, admin, "Priya", manager_id=arjun_e["id"])
    nitin = employee_with_login(app, admin, "Nitin", manager_id=arjun_e["id"])
    activate_request_policy(admin, {"window_mode": "ROLLING"})
    return {"app": app, "admin": admin, "cid": cid, "arjun": arjun, "arjun_id": arjun_e["id"], "priya": priya,
            "priya_id": priya.employee["id"], "nitin": nitin, "nitin_id": nitin.employee["id"], "clock": fake_clock}


def punch_in(world, client, day="2026-11-03", hhmm="09:55"):
    world["clock"].at_ist(day, hhmm)
    assert v2_punch(client, world["clock"]).status_code == 200


def session_of(employee_id, day=DAY):
    found = rows("SELECT * FROM attendance WHERE employee_id = :e AND date = :d AND voided_at IS NULL "
                 "ORDER BY id", e=employee_id, d=day)
    return found[-1] if found else None


def missed_out(at="2026-11-03T18:30:00+05:30", **extra):
    return {"shift_date": "2026-11-03", "kind": "MISSED_PUNCH", "payload": {"direction": "OUT", "at": at},
            "reason_code": "FORGOT_TO_PUNCH", **extra}


def inbox(client, module="attendance"):
    r = client.get("/api/v1/approvals", params={"module": module})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def decide(client, approval_id, decision="APPROVE", status=200, **body):
    r = client.post(f"/api/v1/approvals/{approval_id}/decide", json={"decision": decision, **body})
    assert r.status_code == status, r.text
    return r.json()


def test_gv_as_reg_01_missed_punch_self_request_approved_by_the_manager(world):
    priya, arjun = world["priya"], world["arjun"]
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-04", "10:15")
    r = priya.post(REQ, json=missed_out(comment="Left in a hurry"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "PENDING" and body["mode"] == "SELF" and body["summary"] == "Punch-out 18:30"
    assert body["approval"]["route_key"] == "default" and body["approval"]["due_at"]
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = :d", e=world["priya_id"], d=DAY)
    assert day and "REQUEST_PENDING" in day[0]["exception_codes"]

    # The employee never decides her own request; the manager sees it in the inbox with the subject summary.
    approval_id = body["approval"]["id"]
    refused = priya.post(f"/api/v1/approvals/{approval_id}/decide", json={"decision": "APPROVE"})
    assert refused.status_code in (403, 404)
    items = inbox(arjun)
    assert [i["id"] for i in items] == [approval_id]
    assert items[0]["subject"]["summary"] == "Punch-out 18:30" and items[0]["can_act"]
    assert world["nitin"].get(f"{REQ}/{body['id']}").status_code == 404  # out of scope: 404, never 403

    decide(arjun, approval_id, remarks="ok")
    done = priya.get(f"{REQ}/{body['id']}").json()
    assert done["status"] == "APPROVED" and done["applied_at"]
    session = session_of(world["priya_id"])
    assert session["check_out_time"].isoformat().startswith("2026-11-03T13:00")  # 18:30 IST
    out = rows("SELECT * FROM attendance_punches WHERE employee_id = :e AND direction = 'OUT'", e=world["priya_id"])
    assert out and out[-1]["source"] == "REQUEST" and out[-1]["request_id"] == body["id"]
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = :d", e=world["priya_id"], d=DAY)
    assert day[0]["day_result"] == "PRESENT" and "REQUEST_PENDING" not in day[0]["exception_codes"]
    logged = rows("SELECT * FROM source_change_log WHERE record_type = 'attendance_request' AND record_id = :r",
                  r=body["id"])
    assert len(logged) == 1
    audit = rows("SELECT module, action FROM audit_logs WHERE entity_type = 'approval_request' AND entity_id = :i",
                 i=str(approval_id))
    assert audit and {a["module"] for a in audit} == {"Attendance"}  # BUG-AS-59


def test_v1_tenant_404_and_legacy_policy_disables_self_service(app, fake_clock):
    admin = signup_company(app, "Legacy Co")
    worker = employee_with_login(app, admin, "Ravi")
    assert worker.get(REQ).status_code == 404
    assert worker.post(REQ, json=missed_out()).status_code == 404
    enable_v2(admin.company_id)  # v2 without a request policy: today's behaviour, no self-service kinds
    fake_clock.at_ist("2026-11-04", "10:00")
    r = worker.post(REQ, json=missed_out())
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_KIND_DISABLED"


def test_gv_as_reg_06_limits_self_refused_on_behalf_acknowledged_and_device_failure_not_counted(world):
    admin, priya, arjun = world["admin"], world["priya"], world["arjun"]
    activate_request_policy(admin, {"window_mode": "ROLLING", "backdate_days": 30,
                                    "limits": [{"kinds": ["MISSED_PUNCH", "ADD_SESSION", "CORRECTION"],
                                                "period": "CYCLE", "max_count": 1}]})
    for day in ("2026-11-02", "2026-11-03", "2026-11-04"):
        punch_in(world, priya, day, "09:30")
        world["clock"].at_ist(day, "18:00")
        assert v2_punch(priya, world["clock"], "OUT").status_code == 200
    world["clock"].at_ist("2026-11-05", "10:00")

    def correction(day, **extra):
        return {"shift_date": day, "kind": "CORRECTION", "reason_code": "WRONG_PUNCH",
                "session_id": session_of(world["priya_id"], date.fromisoformat(day))["id"],
                "payload": {"in_at": f"{day}T09:00:00+05:30"}, **extra}

    r = priya.post(REQ, json=correction("2026-11-02"))
    assert r.status_code == 201, r.text
    r = priya.post(REQ, json=correction("2026-11-03"))
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_LIMIT_REACHED"
    details = r.json()["details"]
    assert details["used"] == 1 and details["limit"] == 1 and details["window_from"] == "2026-11-01"
    # A system-caused reason does not consume the quota (R-4).
    r = priya.post(REQ, json=correction("2026-11-03", reason_code="DEVICE_FAILURE"))
    assert r.status_code == 201, r.text
    # ON_BEHALF over the limit: a warning to acknowledge.
    third = correction("2026-11-04", employee_id=world["priya_id"], mode="ON_BEHALF")
    r = arjun.post(REQ, json=third)
    assert r.status_code == 422 and r.json()["details"]["acknowledgeable"]
    r = arjun.post(REQ, json={**third, "acknowledged_warnings": ["REQUEST_LIMIT_REACHED"]})
    assert r.status_code == 201, r.text
    filed = r.json()
    assert filed["issues"][0]["code"] == "REQUEST_LIMIT_REACHED"
    # GV-AS-REG-15 / AS-REG-01: Arjun is barred as requester (cause REQUESTER); with no manager above him the step
    # falls back to the company administrator, who decides.
    r = arjun.post(f"/api/v1/approvals/{filed['approval']['id']}/decide", json={"decision": "APPROVE"})
    assert r.status_code == 403 and r.json()["code"] == "SELF_DECISION_FORBIDDEN"
    assert r.json()["details"]["cause"] == "REQUESTER"
    before = session_of(world["priya_id"], date(2026, 11, 4))
    decide(admin, filed["approval"]["id"])
    assert priya.get(f"{REQ}/{filed['id']}").json()["status"] == "APPROVED"
    # Replace mode: the device session is voided, a REQUEST session with the same seq replaces it.
    sessions = rows("SELECT * FROM attendance WHERE employee_id = :e AND date = '2026-11-04' ORDER BY id",
                    e=world["priya_id"])
    assert [s["void_reason"] for s in sessions] == ["REQUEST", None]
    assert sessions[1]["session_seq"] == before["session_seq"] and sessions[1]["capture_source"] == "REQUEST"
    assert sessions[1]["check_out_time"] == before["check_out_time"]


def test_gv_as_reg_07_window_closed_hr_direct_and_never_on_self(world):
    admin, priya = world["admin"], world["priya"]
    activate_request_policy(admin, {"window_mode": "ROLLING", "backdate_days": 2})
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-09", "10:00")
    r = priya.post(REQ, json=missed_out())
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_WINDOW_CLOSED"
    assert r.json()["details"]["reason"] == "ROLLING" and r.json()["details"]["last_day"] == "2026-11-05"
    # HR's direct edit: applied at once, MANUAL punch, reason required, audit DIRECT_EDIT.
    r = admin.post(REQ, json=missed_out(employee_id=world["priya_id"], mode="DIRECT"))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "APPROVED" and r.json()["approval"] is None
    out = rows("SELECT source FROM attendance_punches WHERE employee_id = :e AND direction = 'OUT'",
               e=world["priya_id"])
    assert out[-1]["source"] == "MANUAL"
    # Never on oneself, checked before any scope lookup.
    me = admin.get("/api/v1/auth/me").json()["employee"]
    if me:
        r = admin.post(REQ, json=missed_out(employee_id=me["id"], mode="DIRECT"))
        assert r.status_code == 403 and r.json()["code"] == "SELF_EDIT_FORBIDDEN"
    r = world["arjun"].post(REQ, json=missed_out(employee_id=world["arjun_id"], mode="ON_BEHALF"))
    assert r.status_code == 403 and r.json()["code"] == "SELF_EDIT_FORBIDDEN"
    # Out of scope: a manager cannot file for someone who does not report to them.
    other = create_employee(admin, "Sameer", "Khan")
    r = world["arjun"].post(REQ, json=missed_out(employee_id=other["id"], mode="ON_BEHALF"))
    assert r.status_code == 404


def test_return_resubmit_and_withdraw(world):
    priya, arjun = world["priya"], world["arjun"]
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-04", "10:15")
    body = priya.post(REQ, json=missed_out()).json()
    r = arjun.post(f"/api/v1/approvals/{body['approval']['id']}/decide", json={"decision": "RETURN"})
    assert r.status_code == 400 and r.json()["code"] == "REMARKS_REQUIRED"
    decide(arjun, body["approval"]["id"], "RETURN", remarks="Which gate did you leave by?")
    returned = priya.get(f"{REQ}/{body['id']}").json()
    assert returned["status"] == "RETURNED" and set(returned["allowed_actions"]) >= {"EDIT", "SUBMIT", "WITHDRAW"}
    r = priya.patch(f"{REQ}/{body['id']}", json={"comment": "Gate B, 18:30", "row_version": returned["row_version"]})
    assert r.status_code == 200, r.text
    r = priya.post(f"{REQ}/{body['id']}/submit", json={})
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["status"] == "PENDING" and again["version"] == 2
    assert again["approval"]["id"] != body["approval"]["id"]
    r = priya.post(f"{REQ}/{body['id']}/withdraw", json={})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert inbox(arjun) == []
    r = priya.post(f"{REQ}/{body['id']}/withdraw", json={})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"
    # One open request per (employee, date, kind, session) (P-10).
    first = priya.post(REQ, json=missed_out())
    assert first.status_code == 201
    r = priya.post(REQ, json=missed_out(at="2026-11-03T18:45:00+05:30"))
    assert r.status_code == 409 and r.json()["code"] == "ATTENDANCE_REQUEST_OPEN"


def test_validation_timezone_future_and_reason(world):
    priya = world["priya"]
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-04", "10:15")
    r = priya.post(REQ, json=missed_out(at="2026-11-03T18:30:00"))
    assert r.status_code == 400 and r.json()["code"] == "TIMEZONE_REQUIRED"
    r = priya.post(REQ, json=missed_out(at="2026-11-05T18:30:00+05:30"))
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_WINDOW_CLOSED"
    r = priya.post(REQ, json=missed_out(reason_code="RAIN_OFF"))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_REASON"
    r = priya.post(REQ, json={**missed_out(), "kind": "STATUS_OVERRIDE", "payload": {"first_half": "PRESENT"}})
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_KIND_DISABLED"
    preview = priya.post(f"{REQ}/preview", json=missed_out()).json()
    assert preview["issues"] == [] and preview["route_preview"]["steps"][0]["approvers"] == ["Arjun Mehta"]
    assert preview["limit_usage"][0]["used"] == 0


def test_gv_as_reg_05_mark_excuse_keeps_the_mark(world):
    priya, arjun = world["priya"], world["arjun"]
    gen = next(x for x in world["admin"].get("/api/v1/shifts").json() if x["code"] == "GEN")
    r = world["admin"].post("/api/v1/shift-assignment-rules", json={
        "name": "Everyone GEN", "shift_id": gen["id"], "criteria": {"everyone": True}, "priority": 500, "reason": "Test",
        "effective_from": "2026-01-01"})
    assert r.status_code == 201, r.text
    punch_in(world, priya, "2026-11-03", "09:40")
    world["clock"].at_ist("2026-11-03", "18:05")
    assert v2_punch(priya, world["clock"], "OUT").status_code == 200
    world["clock"].at_ist("2026-11-04", "10:00")
    r = world["admin"].post("/api/v1/attendance/days/recompute",
                            json={"employee_ids": [world["priya_id"]], "start": "2026-11-03", "end": "2026-11-03",
                                  "reason": "close"})
    assert r.status_code == 200, r.text
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = :d", e=world["priya_id"], d=DAY)
    assert day and day[0]["late_mark"] and day[0]["shift_source"] != "NONE"
    r = priya.post(REQ, json={"shift_date": "2026-11-03", "kind": "MARK_EXCUSE", "payload": {"mark": "LATE"},
                              "reason_code": "TRANSPORT_DELAY"})
    assert r.status_code == 201, r.text
    assert r.json()["payload"]["minutes"] == max(1, day[0]["late_minutes"])
    decide(arjun, r.json()["approval"]["id"])
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = :d", e=world["priya_id"], d=DAY)
    assert day[0]["late_mark"] and day[0]["late_excused"] and day[0]["late_excuse_source"] == "REQUEST"


def test_gv_as_cap_03_muster_per_row_results_and_override(world):
    admin = world["admin"]
    world["clock"].at_ist("2026-11-05", "19:00")
    branch = admin.get("/api/v1/branches").json()[0]
    grid = admin.get("/api/v1/attendance/muster", params={"date": "2026-11-05", "branch_id": branch["id"]})
    assert grid.status_code == 200, grid.text
    r = admin.post("/api/v1/attendance/muster", json={
        "date": "2026-11-05", "offline_batch_id": "batch-1",
        "rows": [{"employee_id": world["priya_id"], "mark": "IN_OUT", "in_time": "08:00", "out_time": "17:00"},
                 {"employee_id": world["nitin_id"], "mark": "IDLE", "reason_code": "RAIN_OFF"},
                 {"employee_id": 999999, "mark": "PRESENT"}]})
    assert r.status_code == 200, r.text
    results = {x["employee_id"]: x for x in r.json()["results"]}
    assert results[world["priya_id"]]["status"] == "APPLIED"
    assert results[world["nitin_id"]]["status"] == "APPLIED"
    assert results[999999] == {"employee_id": 999999, "status": "REFUSED", "code": "NOT_IN_SCOPE",
                               "message": "Not in your crew on this date."}
    session = session_of(world["priya_id"], date(2026, 11, 5))
    assert session["capture_source"] == "SUPERVISOR"
    override = rows("SELECT * FROM attendance_requests WHERE employee_id = :e", e=world["nitin_id"])
    assert override[0]["kind"] == "STATUS_OVERRIDE" and override[0]["mode"] == "DIRECT"
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = '2026-11-05'",
               e=world["nitin_id"])
    assert day and day[0]["override_request_id"] == override[0]["id"]
    replay = admin.post("/api/v1/attendance/muster", json={"date": "2026-11-05", "offline_batch_id": "batch-1",
                                                           "rows": [{"employee_id": world["priya_id"],
                                                                     "mark": "PRESENT"}]})
    assert replay.json()["replayed"] and len(replay.json()["results"]) == 3
    # The employee role has no muster code.
    assert world["priya"].get("/api/v1/attendance/muster", params={"date": "2026-11-05",
                                                                   "branch_id": branch["id"]}).status_code == 403


def test_gv_as_reg_09_team_lead_route_and_lead_own_request(world):
    admin, app = world["admin"], world["app"]
    meghna_e = create_employee(admin, "Meghna", "Rao", manager_id=world["arjun_id"])
    lead_role = admin.post("/api/v1/roles", json={"name": "Tech Lead", "description": "lead", "grants": [
        {"permission": "attendance:approve", "scope": "TEAM"}, {"permission": "attendance:read", "scope": "TEAM"},
        {"permission": "attendance:request", "scope": "SELF"}, {"permission": "attendance:mark", "scope": "SELF"}]})
    assert lead_role.status_code in (200, 201), lead_role.text
    meghna = invite_and_activate(app, admin, meghna_e, ["employee"])
    r = admin.put(f"/api/v1/users/{meghna.user_id}/roles",
                  json={"role_ids": [lead_role.json()["id"], role_id(admin, "employee")]})
    assert r.status_code == 200, r.text
    team = admin.post("/api/v1/teams", json={"code": "PAYMENTS", "name": "Payments", "team_type": "PROJECT",
                                             "effective_from": "2026-01-01"})
    assert team.status_code == 201, team.text
    tid = team.json()["id"]
    for employee, body in ((meghna_e, {"member_role": "LEAD", "is_routing_lead": True}),
                           ({"id": world["priya_id"]}, {"is_primary_team": True})):
        r = admin.post(f"/api/v1/teams/{tid}/members", json={"employee_id": employee["id"],
                                                             "effective_from": "2026-01-01", **body})
        assert r.status_code == 201, r.text
    r = admin.post("/api/v1/approval-routes", json={
        "module": "attendance", "route_key": "TEAM_LEAD_FIRST", "name": "Team lead first", "priority": 50,
        "criteria": {"team_ids": [tid]}, "steps": [{"approver_type": "team_lead"}]})
    assert r.status_code == 201, r.text
    r = admin.post("/api/v1/approval-routes", json={
        "module": "attendance", "route_key": "auto", "name": "x", "priority": 60, "criteria": {},
        "steps": [{"approver_type": "primary_manager"}], "auto_approve": True})
    assert r.status_code == 400 and r.json()["code"] == "ROUTE_INVALID"

    punch_in(world, world["priya"])
    world["clock"].at_ist("2026-11-04", "10:15")
    filed = world["priya"].post(REQ, json=missed_out()).json()
    assert filed["approval"]["route_key"] == "TEAM_LEAD_FIRST"
    assert [i["id"] for i in inbox(meghna)] == [filed["approval"]["id"]]
    assert inbox(world["arjun"]) == []  # not his step
    decide(meghna, filed["approval"]["id"])
    assert world["priya"].get(f"{REQ}/{filed['id']}").json()["status"] == "APPROVED"


def test_gv_as_reg_08_on_duty_batch_skips_the_weekly_off_with_one_approval(world):
    priya, arjun = world["priya"], world["arjun"]
    world["clock"].at_ist("2026-11-10", "09:00")
    plan = priya.post("/api/v1/attendance/on-duty", json={
        "kind": "ON_DUTY", "start": "2026-11-12", "end": "2026-11-15", "reason": "Supplier audit in Chakan",
        "reason_code": "OFFICIAL_WORK", "dry_run": True})
    assert plan.status_code == 201 or plan.status_code == 200, plan.text
    assert [d["date"] for d in plan.json()["dates"]] == ["2026-11-12", "2026-11-13"]
    assert {s["date"] for s in plan.json()["skipped_dates"]} == {"2026-11-14", "2026-11-15"}
    r = priya.post("/api/v1/attendance/on-duty", json={
        "kind": "ON_DUTY", "start": "2026-11-12", "end": "2026-11-15", "reason": "Supplier audit in Chakan",
        "reason_code": "OFFICIAL_WORK"})
    assert r.status_code == 201, r.text
    batch = r.json()
    assert len(batch["rows"]) == 2 and len({x["batch_id"] for x in batch["rows"]}) == 1
    items = inbox(arjun)
    assert len(items) == 1 and items[0]["subject"]["dates"] == ["2026-11-12", "2026-11-13"]
    decide(arjun, items[0]["id"])
    listed = priya.get("/api/v1/attendance/on-duty", params={"mine": "true"}).json()["items"]
    assert {x["status"] for x in listed} == {"APPROVED"}
    # REMOTE_WORK is off by default; overlapping on-duty is refused.
    r = priya.post("/api/v1/attendance/on-duty", json={"kind": "REMOTE_WORK", "start": "2026-11-16",
                                                       "reason": "Home"})
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_KIND_DISABLED"
    r = priya.post("/api/v1/attendance/on-duty", json={"start": "2026-11-12", "reason": "again"})
    assert r.status_code == 409 and r.json()["code"] == "ON_DUTY_OVERLAP"


def test_gv_as_reg_14_period_close_blocks_self_service_and_reopen_needs_step_up(world):
    admin, priya = world["admin"], world["priya"]
    entity = admin.get("/api/v1/legal-entities").json()
    entity_id = (entity["items"] if isinstance(entity, dict) else entity)[0]["id"]
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-04", "10:15")
    created = admin.post("/api/v1/attendance/period-closes", json={
        "legal_entity_id": entity_id, "cycle_start": "2026-11-01", "cycle_end": "2026-11-30"})
    assert created.status_code == 201, created.text
    close_id = created.json()["id"]
    r = admin.post(f"/api/v1/attendance/period-closes/{close_id}/close", json={})
    assert r.status_code == 200 and r.json()["status"] == "CLOSED", r.text
    r = priya.post(REQ, json=missed_out())
    assert r.status_code == 422 and r.json()["code"] == "ATTENDANCE_PERIOD_CLOSED"
    r = admin.post(REQ, json=missed_out(employee_id=world["priya_id"], mode="DIRECT"))
    assert r.status_code == 201, r.text
    r = admin.post(f"/api/v1/attendance/period-closes/{close_id}/reopen", json={"reason": "Late fix"})
    assert r.status_code == 403
    user_id = admin.get("/api/v1/auth/me").json()["user"]["id"]
    sql("UPDATE user_sessions SET step_up_at = :t, step_up_method = 'EMAIL_OTP' WHERE user_id = :u",
        t=world["clock"].now, u=user_id)
    r = admin.post(f"/api/v1/attendance/period-closes/{close_id}/reopen", json={"reason": "Late fix"})
    assert r.status_code == 200 and r.json()["status"] == "REOPENED", r.text
