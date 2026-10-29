"""JC-AS-12 (AS 04-attendance/04 §14.4, AS-PAY-08, GV-AS-PAY-06 partial): attendance requests still pending at the
cutoff are a PENDING_INPUTS finding of kind ATTENDANCE, BLOCKING and waivable under
``payroll.attendance.pending_at_cutoff = EXCEPTION`` (default), a WARNING under ``WARNING``; decided requests are not
listed."""

from datetime import date, timedelta

from tests.payroll.test_run_prechecks import by_code, exceptions, precheck
from tests.payroll.test_runs_lifecycle import new_run, runs_world  # noqa: F401
from tests.payroll.test_wage_inputs import set_setting, sql


def attendance_request(company_id, employee_id, day, status="PENDING"):
    sql("INSERT INTO attendance_requests (company_id, employee_id, shift_date, kind, mode, status, payload, "
        "reason_code, minutes, created_at, updated_at) VALUES (:c, :e, :d, 'MISSED_PUNCH', 'SELF', :s, "
        "CAST(:p AS jsonb), 'FORGOT_TO_PUNCH', 0, now(), now())",
        c=company_id, e=employee_id, d=day, s=status, p='{"direction": "OUT", "at": "2026-01-01T18:00:00+00:00"}')


def attendance_items(client, run_id):
    return [e for e in by_code(exceptions(client, run_id), "PENDING_INPUTS") if e["details"]["kind"] == "ATTENDANCE"]


def test_jc_as_12_pending_attendance_requests_block_by_default_and_warn_when_configured(runs_world):
    w = runs_world
    start = date.fromisoformat(w["period"]["start"])
    attendance_request(w["cid"], w["emp"]["Emma"]["id"], start + timedelta(days=2))
    attendance_request(w["cid"], w["emp"]["Ivan"]["id"], start + timedelta(days=3), status="APPROVED")
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "INPUTS_PENDING"
    found = attendance_items(w["acme"], run["id"])
    assert len(found) == 1 and found[0]["severity"] == "BLOCKING" and found[0]["waivable"]
    assert found[0]["details"]["items"][0]["type"] == "attendance_request"
    assert found[0]["fix_link"] == "/approvals?module=attendance"

    set_setting(w["cid"], "payroll.attendance.pending_at_cutoff", "WARNING")
    precheck(w["acme"], run["id"])
    found = [e for e in attendance_items(w["acme"], run["id"])]
    assert found and all(e["severity"] == "WARNING" for e in found)
