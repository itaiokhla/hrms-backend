"""AS-M8 overtime proposals (G-18) and AS-M9 live board and registers (G-20).

Overtime (04-attendance/05): a FINAL STANDARD day above the daily threshold becomes a PENDING proposal in payroll's
``overtime_entries`` (GV-AS-OT-01 shape); a rerun changes nothing; a correction edits a PENDING proposal in place
and proposes a successor version for an APPROVED one (GV-AS-OT-06, AS-OT-05); "Approve for payroll" and "Convert to
comp-off" go through payroll's decide path; a worked hour is paid or comp-off, never both (§10 E2); an enforcing
VERIFIED pack refuses approval without OVERTIME consent until one is recorded (AS-OT-09, GV-AS-OT-13); the approval
context shows the attendance candidate and the quarter usage.

Live board (10-analytics §3.5): tiles and rows from one scoped population (count = list, D-1), the identity D-2,
status classes, filters, 403 for a SELF-only reader.

Registers (§6): Form 25 marks and totals, Form D hours, CSV / XLSX / PDF with the seal, ``attendance:export`` only.
"""

from datetime import date, datetime, time, timedelta

import pytest

from tests.attendance.capture_v2_helpers import rows, sql, v2_punch
from tests.attendance.test_requests import enable_v2
from tests.attendance_helpers import IST, employee_with_login, fake_clock  # noqa: F401
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.payroll.test_overtime_entries import ot_policy

DAY = date(2026, 11, 3)  # Tuesday
OT = "/api/v1/attendance/overtime"


def set_setting(company_id, key, value_sql):
    updated = sql("UPDATE setting_values SET value = " + value_sql + " WHERE company_id = :c AND key = :k "
                  "AND scope_type = 'TENANT' AND status = 'ACTIVE'", c=company_id, k=key)
    if updated.rowcount == 0:
        sql("INSERT INTO setting_values (company_id, key, scope_type, value, locked, effective_from, status, "
            "row_version, source, created_at, updated_at) VALUES (:c, :k, 'TENANT', " + value_sql + ", false, "
            "'2024-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key)


def activate_rules(admin):
    r = admin.post("/api/v1/attendance/policies", json={"type": "ATTENDANCE_RULES_POLICY", "name": "Staff rules",
                                                        "fields": {"threshold_mode": "ABSOLUTE",
                                                                   "full_day_min_minutes": "450",
                                                                   "half_day_min_minutes": "240"}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert admin.put(f"/api/v1/attendance/policies/{pid}/assignments",
                     json={"rules": [{"criteria": {"everyone": True}, "priority": 10}]}).status_code == 200
    r = admin.post(f"/api/v1/attendance/policies/{pid}/versions/1/activate",
                   json={"effective_from": "2026-01-01", "reason": "AS-M8 test"})
    assert r.status_code == 200, r.text


def session(company_id, employee_id, day, start, end):
    """A closed session (local times IST); returns its id."""
    sql("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, occupied_until, date, "
        "is_manual, source, capture_source, status, created_at, updated_at) VALUES (:c, :e, :i, :o, :o, :d, true, "
        "'manual', 'MANUAL', 'present', now(), now())", c=company_id, e=employee_id, d=day,
        i=IST.localize(datetime.combine(day, start)), o=IST.localize(datetime.combine(day, end)))
    return rows("SELECT id FROM attendance WHERE employee_id = :e AND date = :d", e=employee_id, d=day)[0]["id"]


def recompute(admin, employee_id, day=DAY):
    r = admin.post("/api/v1/attendance/days/recompute",
                   json={"employee_ids": [employee_id], "start": day.isoformat(), "end": day.isoformat(),
                         "reason": "AS-M8 test"})
    assert r.status_code == 200, r.text


def run_proposals(client, first=date(2026, 11, 2), last=date(2026, 11, 8)):
    r = client.post(f"{OT}/proposals/run", json={"from": first.isoformat(), "to": last.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def entries(employee_id):
    return rows("SELECT * FROM overtime_entries WHERE employee_id = :e ORDER BY id", e=employee_id)


@pytest.fixture
def crew(app, fake_clock):
    fake_clock.at_ist("2026-11-05", "10:00")  # 3 Nov is past its close point
    admin = signup_company(app, "Overtime Plant Co")
    cid = admin.company_id
    enable_v2(cid)
    set_setting(cid, "payroll.overtime.enabled", "'true'::jsonb")
    activate_rules(admin)
    ot_policy(admin, source="ATTENDANCE_WITH_APPROVAL")  # daily threshold 8 h, min 30, comp-off allowed
    mona = create_employee(admin, "Mona", "Supervisor")
    ravi = create_employee(admin, "Ravi", "Kumar", manager_id=mona["id"], employee_code="0417")
    return {"app": app, "admin": admin, "cid": cid, "mona": invite_and_activate(app, admin, mona, ["manager"]),
            "ravi_id": ravi["id"], "ravi": ravi, "clock": fake_clock}


def test_gv_as_ot_01_final_day_becomes_a_pending_proposal_and_reruns_change_nothing(crew):
    admin, cid, ravi = crew["admin"], crew["cid"], crew["ravi_id"]
    sid = session(cid, ravi, DAY, time(9, 0), time(19, 30))  # 630 min worked, threshold 480
    recompute(admin, ravi)
    [day] = rows("SELECT * FROM attendance_days WHERE employee_id = :e", e=ravi)
    assert day["evaluation_state"] == "FINAL" and day["rules_mode"] == "STANDARD"
    assert day["ot_candidate_minutes"] == 150
    employee = signup_company(crew["app"], "Other Co")  # no overtime:approve in this tenant's scope
    assert employee.post(f"{OT}/proposals/run", json={"from": "2026-11-02", "to": "2026-11-08"}).json()["created"] == 0
    result = run_proposals(crew["mona"])  # the manager runs it for her reporting tree
    assert result["created"] == 1
    [entry] = entries(ravi)
    assert (entry["minutes_requested"], entry["minutes_computed"], entry["status"], entry["source"]) == (
        150, 150, "PENDING_APPROVAL", "ATTENDANCE_WITH_APPROVAL")
    assert entry["bucket"] == "WORKDAY" and entry["requested_by_user_id"] is None and "day version v" in entry["reason"]
    again = run_proposals(admin)
    assert again["created"] == again["updated"] == again["successors"] == 0 and len(entries(ravi)) == 1

    # GV-AS-OT-06 (a): the day changes while the proposal is pending -> edited in place, approval restarted.
    sql("UPDATE attendance SET check_out_time = :o, occupied_until = :o WHERE id = :s", s=sid,
        o=IST.localize(datetime.combine(DAY, time(19, 0))))
    recompute(admin, ravi)
    assert run_proposals(admin)["updated"] == 1
    [entry] = entries(ravi)
    assert entry["minutes_requested"] == 120 and entry["status"] == "PENDING_APPROVAL" and entry["row_version"] > 1

    # Approval context: the attendance candidate and the quarter usage.
    ctx = admin.get(f"{OT}/{entry['id']}/context").json()
    assert ctx["origin"] == "ATTENDANCE" and ctx["attendance_day"]["ot_candidate_minutes"] == 120
    assert ctx["usage"]["quarter"]["from"] == "2026-10-01" and ctx["consent"]["required"] is False

    # One-click "Approve for payroll", then (b) an approved proposal is never changed: a successor version.
    r = crew["mona"].post(f"{OT}/{entry['id']}/convert", json={"outcome": "PAY"})
    assert r.status_code == 200, r.text
    assert rows("SELECT status, minutes_approved, comp_off FROM overtime_entries WHERE id = :i", i=entry["id"]) == [
        {"status": "APPROVED", "minutes_approved": 120, "comp_off": False}]
    sql("UPDATE attendance SET check_out_time = :o, occupied_until = :o WHERE id = :s", s=sid,
        o=IST.localize(datetime.combine(DAY, time(18, 0))))
    recompute(admin, ravi)
    assert run_proposals(admin)["successors"] == 1
    first, successor = entries(ravi)
    assert first["status"] == "APPROVED" and first["minutes_approved"] == 120
    assert successor["supersedes_id"] == first["id"] and successor["minutes_requested"] == 60
    assert successor["status"] == "PENDING_APPROVAL" and successor["version"] == 2


def test_comp_off_conversion_and_exclusivity(crew):
    admin, cid, ravi = crew["admin"], crew["cid"], crew["ravi_id"]
    session(cid, ravi, DAY, time(9, 0), time(19, 0))
    recompute(admin, ravi)
    run_proposals(admin)
    [entry] = entries(ravi)
    r = crew["mona"].post(f"{OT}/{entry['id']}/convert", json={"outcome": "COMP_OFF", "minutes": 90})
    assert r.status_code == 200, r.text
    assert rows("SELECT status, minutes_approved, comp_off FROM overtime_entries WHERE id = :i", i=entry["id"]) == [
        {"status": "APPROVED", "minutes_approved": 90, "comp_off": True}]
    # §10 E2: the same date cannot also be claimed as paid overtime.
    paid = crew["mona"].post("/api/v1/overtime/entries", json={
        "employee_id": ravi, "date": DAY.isoformat(), "minutes": 30, "reason": "Extra pay"})
    assert paid.status_code == 409 and paid.json()["code"] == "COMP_OFF_SOURCE_TAKEN"
    assert crew["mona"].post(f"{OT}/{entry['id']}/convert", json={"outcome": "PAY"}).status_code == 409


def test_as_ot_09_enforcing_pack_requires_consent_before_approval(crew, monkeypatch):
    from app.services import pack_resolver

    real = pack_resolver.resolve

    def resolve(db, company_id, legal_entity_id, field, on):
        if field in ("overtime.requires_consent", "overtime.consent_enforce"):
            return pack_resolver.PackValue(field=field, value=True, source="PACK", pack_code="IN",
                                           pack_version="WT-IN-TEST", verified=True)
        return real(db, company_id, legal_entity_id, field, on)

    monkeypatch.setattr(pack_resolver, "resolve", resolve)
    admin, cid, ravi = crew["admin"], crew["cid"], crew["ravi_id"]
    session(cid, ravi, DAY, time(9, 0), time(19, 0))
    recompute(admin, ravi)
    result = run_proposals(admin)
    assert result["created"] == 1 and result["consent_missing"] == 1
    [entry] = entries(ravi)
    ctx = admin.get(f"{OT}/{entry['id']}/context").json()
    assert ctx["consent"] == {"required": True, "on_file": False, "enforced": True, "pack_version": "WT-IN-TEST"}
    refused = crew["mona"].post(f"{OT}/{entry['id']}/convert", json={"outcome": "PAY"})
    assert refused.status_code == 422 and refused.json()["code"] == "OVERTIME_CONSENT_REQUIRED"
    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_consents

    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        hr_user = rows("SELECT id FROM users WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0]["id"]
        attendance_consents.record(db, cid, ravi, "OVERTIME", "GRANTED", channel="HR_RECORDED", on=DAY,
                                   scope="PER_INSTANCE", subject_ref={"overtime_entry_id": entry["id"]},
                                   recorded_by_user_id=hr_user)
        db.commit()
    finally:
        db.close()
    assert crew["mona"].post(f"{OT}/{entry['id']}/convert", json={"outcome": "PAY"}).status_code == 200


def test_queue_lists_pending_with_the_candidate_and_respects_scope(crew, app):
    admin, cid, ravi = crew["admin"], crew["cid"], crew["ravi_id"]
    session(cid, ravi, DAY, time(9, 0), time(19, 0))
    recompute(admin, ravi)
    run_proposals(admin)
    queue = crew["mona"].get(f"{OT}/queue").json()
    assert queue["total"] == 1 and queue["items"][0]["attendance"]["ot_candidate_minutes"] == 120
    other = signup_company(app, "Spy Co")
    [entry] = entries(ravi)
    assert other.get(f"{OT}/{entry['id']}/context").status_code == 404


# ------------------------------------------------------------------------------------------------ live board
@pytest.fixture
def floor(app, fake_clock):
    fake_clock.at_ist("2026-11-03", "11:00")
    admin = signup_company(app, "Live Board Co")
    enable_v2(admin.company_id)
    asha = employee_with_login(app, admin, "Asha")
    bala = employee_with_login(app, admin, "Bala")
    chen = create_employee(admin, "Chen", "Li")
    return {"admin": admin, "cid": admin.company_id, "asha": asha, "bala": bala, "chen_id": chen["id"],
            "clock": fake_clock}


def test_live_board_count_equals_list_identity_and_filters(floor):
    admin, clock = floor["admin"], floor["clock"]
    clock.at_ist("2026-11-03", "09:05")
    assert v2_punch(floor["asha"], clock).status_code == 200
    clock.at_ist("2026-11-03", "09:10")
    assert v2_punch(floor["bala"], clock).status_code == 200
    clock.at_ist("2026-11-03", "12:00")
    assert v2_punch(floor["bala"], clock, "OUT").status_code == 200
    clock.at_ist("2026-11-03", "12:30")
    board = admin.get("/api/v1/attendance/live-board").json()
    assert board["live"] is True and board["consistency"]["expected_equals_present_plus_on_duty_plus_absent"]
    tiles = {t["key"]: t["value"] for t in board["tiles"]}
    assert tiles["present_people"] == "2"
    statuses = {r["name"].split()[0]: r["status"] for r in board["items"]}
    assert statuses["Asha"] == "WORKING" and statuses["Bala"] == "COMPLETED"
    assert board["total"] == sum(board["counts"].values()) == len(board["items"])
    office = admin.get("/api/v1/attendance/live-board", params={"quick": "in_office"}).json()
    assert [r["name"].split()[0] for r in office["items"]] == ["Asha"]
    found = admin.get("/api/v1/attendance/live-board", params={"q": "bala"}).json()
    assert found["total"] == 1 and found["items"][0]["status"] == "COMPLETED"
    assert admin.get("/api/v1/attendance/live-board", params={"status": "NOPE"}).status_code == 400
    assert floor["asha"].get("/api/v1/attendance/live-board").status_code == 403  # SELF only


# ------------------------------------------------------------------------------------------------ registers
def test_form_25_and_form_d_marks_hours_exports_and_permission(crew):
    admin, cid, ravi = crew["admin"], crew["cid"], crew["ravi_id"]
    session(cid, ravi, DAY, time(9, 0), time(19, 0))
    recompute(admin, ravi)
    entity = rows("SELECT legal_entity_id FROM employee_assignments WHERE employee_id = :e LIMIT 1", e=ravi) or \
        rows("SELECT id AS legal_entity_id FROM legal_entities WHERE company_id = :c LIMIT 1", c=cid)
    params = {"from": "2026-11-01", "to": "2026-11-05", "legal_entity_id": entity[0]["legal_entity_id"]}
    catalogue = admin.get("/api/v1/attendance/registers").json()
    assert {i["code"] for i in catalogue["items"]} == {"FORM_25", "FORM_D"}
    form = admin.get("/api/v1/attendance/registers/form-25", params=params).json()
    row = next(r for r in form["rows"] if r["employee_id"] == ravi)
    assert form["dates"][2] == "2026-11-03" and row["marks"][2] == "P"
    assert row["marks"][0] == "W" or row["marks"][0] in ("…", "H")  # Sunday 1 Nov
    assert row["totals"]["present_units"] == "1" and row["totals"]["hours_worked"] == "10.00"
    assert len(form["seal"]["sha256"]) == 64 and "electronic register" in form["seal"]["statement"]
    form_d = admin.get("/api/v1/attendance/registers/form-d", params=params).json()
    row_d = next(r for r in form_d["rows"] if r["employee_id"] == ravi)
    assert row_d["hours"][2] == "10.00" and row_d["wages"] == "Held in payroll register"
    for fmt, magic in (("CSV", b"\xef\xbb\xbf"), ("XLSX", b"PK"), ("PDF", b"%PDF")):
        r = admin.get("/api/v1/attendance/registers/form-25", params={**params, "format": fmt})
        assert r.status_code == 200, r.text
        assert r.content.startswith(magic) and len(r.headers["X-Register-Seal"]) == 64
        assert "attachment" in r.headers["content-disposition"]
    csv_text = admin.get("/api/v1/attendance/registers/form-25", params={**params, "format": "CSV"}).text
    assert "Seal SHA-256" in csv_text and "Ravi Kumar" in csv_text
    audits = rows("SELECT details FROM audit_logs WHERE action = 'REPORT_EXPORTED' AND company_id = :c", c=cid)
    assert len(audits) >= 4 and all(a["details"]["seal_sha256"] for a in audits)
    # A manager without attendance:export: no catalogue entries and 403 on the register.
    assert crew["mona"].get("/api/v1/attendance/registers").json()["items"] == []
    assert crew["mona"].get("/api/v1/attendance/registers/form-25", params=params).status_code == 403
    too_long = admin.get("/api/v1/attendance/registers/form-25",
                         params={**params, "from": "2026-01-01", "to": "2026-11-05"})
    assert too_long.status_code == 422 and too_long.json()["code"] == "REPORT_RANGE_TOO_LARGE"
