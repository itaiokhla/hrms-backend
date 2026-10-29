"""AS-M4 edge cases beyond the main flows (AS 04-attendance/04; AS 12 areas AS-REG, AS-PAY, AS-RBAC).

* GV-AS-REG-11: an overdue step reminds the approver and, with ``attendance.approval.sla_action = ESCALATE``, the
  approver's manager becomes eligible and decides; nothing is approved by the scan itself (D-AS-72).
* AS-PAY-02 / GV-AS-REG-07 (cutoff variant): under ``ROLLING_AND_CUTOFF`` a SELF request for an INPUT_LOCKED date is
  ``REQUEST_WINDOW_CLOSED`` (reason CUTOFF); ON_BEHALF still files with the ``PERIOD_LOCKED`` warning in the preview; a
  DIRECT edit of a FINALIZED date needs a comment of at least 10 characters.
* GV-AS-PAY-06 / GV-AS-REG-08: an approved past on-duty day with no punch evaluates PRESENT with basis ON_DUTY.
* GV-AS-REG-03: ``ADD_SESSION`` adds session 2 beside session 1 (capture policy allows several sessions).
* AS-RBAC: an employee never decides, never files ON_BEHALF / DIRECT and never sees another employee's request
  (404); a DIRECT ``STATUS_OVERRIDE`` needs ``attendance:override`` (a manager has none).
"""

from datetime import date, timedelta

import pytest

from tests.attendance.capture_v2_helpers import activate_capture, grant, rows, sql, v2_punch
from tests.attendance.test_requests import (  # noqa: F401  (fixtures)
    REQ, activate_request_policy, decide, fake_clock, inbox, missed_out, punch_in, session_of, world,
)

DAY = date(2026, 11, 3)


def set_tenant_setting(company_id, key, value_json):
    sql("DELETE FROM setting_values WHERE company_id = :c AND key = :k AND scope_type = 'TENANT'", c=company_id, k=key)
    sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
        "row_version, source, created_at, updated_at) VALUES (:c, :k, 'TENANT', NULL, CAST(:v AS jsonb), false, "
        "'2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key, v=value_json)


def run_sla_scan(company_id, now):
    from app.database.db import SessionLocal, bind_tenant
    from app.payroll.services import approvals

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        result = approvals.sla_scan(db, company_id, now=now)
        db.commit()
        return result
    finally:
        db.close()


def test_gv_as_reg_11_sla_reminder_then_escalation_to_the_next_manager(world, app):
    from tests.helpers import create_employee, invite_and_activate

    admin, cid = world["admin"], world["cid"]
    # Arjun reports to Neha (a manager) so the escalation has somewhere to go.
    neha_e = create_employee(admin, "Neha", "Joshi")
    neha = invite_and_activate(app, admin, neha_e, ["manager"])
    sql("UPDATE employees SET manager_id = :m WHERE id = :e", m=neha_e["id"], e=world["arjun_id"])
    # The escalation target must cover the subject (spine §7.2): Neha approves for her whole reporting tree.
    grant(cid, "manager", "attendance:approve", "REPORTING_TREE")
    set_tenant_setting(cid, "attendance.approval.sla_action", '"ESCALATE"')
    punch_in(world, world["priya"])
    world["clock"].at_ist("2026-11-04", "09:00")
    filed = world["priya"].post(REQ, json=missed_out()).json()
    step = rows("SELECT * FROM approval_request_steps WHERE request_id = :r", r=filed["approval"]["id"])[0]
    assert step["due_at"] is not None
    assert inbox(neha) == []  # not hers before the SLA breach

    early = run_sla_scan(cid, world["clock"].now + timedelta(hours=1))
    assert early["reminded"] == 0 and early["escalated"] == 0
    late = run_sla_scan(cid, world["clock"].now + timedelta(hours=49))
    assert late["reminded"] == 1 and late["escalated"] == 1
    reminders = rows("SELECT * FROM notifications WHERE user_id = :u AND title LIKE 'Reminder:%'",
                     u=world["arjun"].user_id)
    assert reminders
    step = rows("SELECT * FROM approval_request_steps WHERE request_id = :r", r=filed["approval"]["id"])[0]
    assert step["reminders_sent"] == 1 and step["escalation_level"] == 1 and step["status"] == "pending"
    assert world["priya"].get(f"{REQ}/{filed['id']}").json()["status"] == "PENDING"  # never auto-approved
    # The escalation target decides.
    assert [i["id"] for i in inbox(neha)] == [filed["approval"]["id"]]
    decide(neha, filed["approval"]["id"])
    assert world["priya"].get(f"{REQ}/{filed['id']}").json()["status"] == "APPROVED"
    # A second scan escalates no further (max_escalations 1) and finds nothing overdue.
    assert run_sla_scan(cid, world["clock"].now + timedelta(hours=200))["escalated"] == 0


def test_as_pay_02_cutoff_window_on_behalf_warning_and_finalized_direct_comment(world, monkeypatch):
    from app.payroll.services import calendars

    admin, priya, arjun = world["admin"], world["priya"], world["arjun"]
    activate_request_policy(admin, {"window_mode": "ROLLING_AND_CUTOFF", "backdate_days": 30})
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-04", "10:15")
    state = {"value": "INPUT_LOCKED"}
    monkeypatch.setattr(calendars, "period_state_on", lambda *a, **k: state["value"])
    r = priya.post(REQ, json=missed_out())
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_WINDOW_CLOSED"
    assert r.json()["details"]["reason"] == "CUTOFF"
    body = missed_out(employee_id=world["priya_id"], mode="ON_BEHALF")
    preview = arjun.post(f"{REQ}/preview", json=body).json()
    assert {"code": "PERIOD_LOCKED", "severity": "WARNING"}.items() <= next(
        i for i in preview["issues"] if i["code"] == "PERIOD_LOCKED").items()
    assert preview["payroll_state"]["state"] == "INPUT_LOCKED"
    assert arjun.post(REQ, json=body).status_code == 201

    state["value"] = "FINALIZED"
    direct = missed_out(employee_id=world["nitin_id"], mode="DIRECT")
    punch_in(world, world["nitin"], "2026-11-03", "09:30")
    world["clock"].at_ist("2026-11-04", "10:30")
    r = admin.post(REQ, json=direct)
    assert r.status_code == 422 and r.json()["details"]["missing"] == "COMMENT"
    r = admin.post(REQ, json={**direct, "comment": "Gate register confirms the exit at 18:30"})
    assert r.status_code == 201, r.text


def test_gv_as_pay_06_approved_on_duty_day_is_present_on_duty(world):
    priya, arjun = world["priya"], world["arjun"]
    world["clock"].at_ist("2026-11-06", "10:00")
    r = priya.post("/api/v1/attendance/on-duty", json={"start": "2026-11-04", "end": "2026-11-04",
                                                       "reason": "Client workshop", "reason_code": "ON_CLIENT_SITE"})
    assert r.status_code == 201, r.text
    decide(arjun, inbox(arjun)[0]["id"])
    r = world["admin"].post("/api/v1/attendance/days/recompute",
                            json={"employee_ids": [world["priya_id"]], "start": "2026-11-04", "end": "2026-11-04",
                                  "reason": "on-duty"})
    assert r.status_code == 200, r.text
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = '2026-11-04'",
               e=world["priya_id"])
    assert day and day[0]["day_result"] == "PRESENT" and day[0]["presence_basis"] == "ON_DUTY"
    row = rows("SELECT * FROM on_duty_requests WHERE employee_id = :e", e=world["priya_id"])
    assert row[0]["status"] == "APPROVED" and row[0]["reason_code"] == "ON_CLIENT_SITE"


def test_gv_as_reg_03_add_session_beside_the_first(world):
    priya, arjun = world["priya"], world["arjun"]
    activate_capture(world["admin"], {"multiple_sessions_allowed": True})
    punch_in(world, priya, "2026-11-03", "10:00")
    world["clock"].at_ist("2026-11-03", "18:00")
    assert v2_punch(priya, world["clock"], "OUT").status_code == 200
    world["clock"].at_ist("2026-11-04", "10:00")
    r = priya.post(REQ, json={"shift_date": "2026-11-03", "kind": "ADD_SESSION", "reason_code": "OFFICIAL_WORK",
                              "payload": {"in_at": "2026-11-03T20:00:00+05:30", "out_at": "2026-11-03T22:30:00+05:30",
                                          "work_mode": "REMOTE"}})
    assert r.status_code == 201, r.text
    assert r.json()["minutes"] == 150
    decide(arjun, r.json()["approval"]["id"])
    sessions = rows("SELECT * FROM attendance WHERE employee_id = :e AND date = :d AND voided_at IS NULL "
                    "ORDER BY session_seq", e=world["priya_id"], d=DAY)
    assert [s["session_seq"] for s in sessions] == [1, 2]
    assert sessions[1]["capture_source"] == "REQUEST" and sessions[1]["work_mode"] == "REMOTE"
    # An overlapping second request is refused before anything is written (P-5).
    r = priya.post(REQ, json={"shift_date": "2026-11-03", "kind": "ADD_SESSION", "reason_code": "OFFICIAL_WORK",
                              "payload": {"in_at": "2026-11-03T21:00:00+05:30", "out_at": "2026-11-03T23:00:00+05:30"}})
    assert r.status_code == 409 and r.json()["code"] == "SESSION_OVERLAP"


def test_as_rbac_employee_limits_and_override_needs_its_own_code(world):
    priya, nitin, arjun = world["priya"], world["nitin"], world["arjun"]
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-04", "10:15")
    filed = priya.post(REQ, json=missed_out()).json()
    # A peer neither sees nor decides it.
    assert nitin.get(f"{REQ}/{filed['id']}").status_code == 404
    r = nitin.post(f"/api/v1/approvals/{filed['approval']['id']}/decide", json={"decision": "APPROVE"})
    assert r.status_code in (403, 404)
    # An employee cannot file for others in any mode.
    for mode in ("ON_BEHALF", "DIRECT"):
        r = nitin.post(REQ, json=missed_out(employee_id=world["priya_id"], mode=mode))
        assert r.status_code in (403, 404), (mode, r.text)
    # A manager may correct (regularize) but not mark the day (override).
    r = arjun.post(REQ, json={"shift_date": "2026-11-03", "kind": "STATUS_OVERRIDE", "mode": "DIRECT",
                              "employee_id": world["nitin_id"], "reason_code": "DATA_CORRECTION",
                              "payload": {"first_half": "PRESENT"}})
    assert r.status_code in (403, 404)
    # Lists: the employee sees only own requests, the manager the team's.
    assert [x["id"] for x in nitin.get(REQ).json()["items"]] == []
    assert filed["id"] in [x["id"] for x in arjun.get(REQ).json()["items"]]
    # Muster is HR / supervisor only.
    r = arjun.post("/api/v1/attendance/muster", json={"date": "2026-11-03",
                                                     "rows": [{"employee_id": world["priya_id"], "mark": "PRESENT"}]})
    assert r.status_code == 403


@pytest.mark.parametrize("payload, code", [
    ({"direction": "BREAK_START", "at": "2026-11-03T13:00:00+05:30"}, "INVALID_FIELDS"),
    ({"direction": "OUT", "at": "2026-11-03T09:00:00+05:30"}, "INVALID_RANGE"),
    ({"direction": "OUT", "at": "2026-11-03T18:30:00+05:30", "extra": 1}, "INVALID_FIELDS"),
])
def test_as_reg_payload_validation(world, payload, code):
    priya = world["priya"]
    punch_in(world, priya)
    world["clock"].at_ist("2026-11-04", "10:15")
    r = priya.post(REQ, json={**missed_out(), "payload": payload})
    assert r.status_code == 400 and r.json()["code"] == code, r.text
