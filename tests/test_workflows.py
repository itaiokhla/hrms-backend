"""Leave, payroll, attendance, assets, holidays and scoped reporting workflows."""

from datetime import date, datetime, timedelta, timezone

from tests.helpers import create_employee, invite_and_activate, role_id


def _apply(client, start="2099-05-04", end=None, leave_type="casual"):
    return client.post("/api/v1/leaves/apply", json={"leave_type": leave_type, "start_date": start, "end_date": end or start})


# Leave ------------------------------------------------------------------------
def test_leave_chain_manager_then_hr_for_long_requests(org, app):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    hr = invite_and_activate(app, admin, create_employee(admin, "Hana", "HR"), ["hr_admin"])
    leave = _apply(u["e1"], "2099-06-01", "2099-06-03").json()  # 3 days -> HR step applies
    assert [s["approver_type"] for s in leave["steps"]] == ["primary_manager", "role"]
    assert leave["steps"][0]["approver"] == e["m2"]["full_name"]
    # HR cannot jump the queue unless acting as a company-wide override, which is recorded.
    assert u["m1"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).status_code == 403
    step1 = u["m2"].post(f"/api/v1/leaves/{leave['id']}/approve", json={"remarks": "ok"}).json()
    assert step1["status"] == "pending" and step1["steps"][1]["status"] == "pending"
    final = hr.post(f"/api/v1/leaves/{leave['id']}/approve", json={}).json()
    assert final["status"] == "approved"
    balance = u["e1"].get("/api/v1/leaves/my/balance?year=2099").json()["casual"]
    assert balance["used"] == 3 and balance["remaining"] == 7


def test_short_leave_needs_only_manager(org):
    u = org["users"]
    leave = _apply(u["e1"]).json()
    assert len(leave["steps"]) == 1
    assert u["m2"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).json()["status"] == "approved"


def test_no_self_approval_and_rejection(org):
    u = org["users"]
    leave = _apply(u["m2"]).json()  # reports to Manager A1
    assert u["m2"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).status_code == 403
    r = u["m1"].post(f"/api/v1/leaves/{leave['id']}/reject", json={"remarks": "busy week"}).json()
    assert r["status"] == "rejected"


def test_cross_tenant_and_out_of_scope_leave_actions(org):
    u = org["users"]
    leave = _apply(u["e1"]).json()
    assert u["admin_b"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).status_code == 404
    assert u["m3"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).status_code == 404
    assert u["e4"].post(f"/api/v1/leaves/{leave['id']}/cancel").status_code == 404


def test_leave_balance_and_overlap_rules(org):
    u = org["users"]
    assert _apply(u["e1"], "2099-07-01", "2099-07-30").status_code == 400  # exceeds casual quota
    assert _apply(u["e1"], "2099-07-01").status_code == 200
    assert _apply(u["e1"], "2099-07-01").status_code == 409
    assert _apply(u["e1"], "2000-01-01").status_code == 400  # past dates
    assert _apply(u["e1"], leave_type="nonexistent").status_code == 400


def test_delegated_approval_respects_window(org, app):
    u, e = org["users"], org["emp"]
    leave = _apply(u["e1"]).json()
    today = date.today()
    r = u["m2"].post("/api/v1/org/delegations", json={
        "delegate_employee_id": e["m3"]["id"], "starts_on": (today - timedelta(days=10)).isoformat(),
        "ends_on": (today - timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text
    assert u["m3"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).status_code == 404  # expired
    u["m2"].post("/api/v1/org/delegations", json={
        "delegate_employee_id": e["m3"]["id"], "starts_on": today.isoformat(), "ends_on": (today + timedelta(days=2)).isoformat()})
    inbox = u["m3"].get("/api/v1/leaves/approvals").json()
    assert [l["acting_as"] for l in inbox] == ["delegate"]
    assert u["m3"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).json()["status"] == "approved"
    # Employees can only delegate their own approvals.
    r = u["m3"].post("/api/v1/org/delegations", json={
        "delegator_employee_id": e["m1"]["id"], "delegate_employee_id": e["m3"]["id"],
        "starts_on": today.isoformat(), "ends_on": today.isoformat()})
    assert r.status_code == 403


def test_delegation_candidates_cover_chain_and_peers_only(org):
    u, e = org["users"], org["emp"]
    names = {c["full_name"] for c in u["m2"].get("/api/v1/org/delegation-candidates").json()}
    # Manager A2 can delegate up the chain (A1) or to their own reports, never sideways
    # into another branch.
    assert e["m1"]["first_name"] + " Test" in names
    assert e["m3"]["first_name"] + " Test" not in names
    found = u["m2"].get("/api/v1/org/delegation-candidates", params={"q": e["m1"]["first_name"] + " Test"}).json()
    assert [c["id"] for c in found] == [e["m1"]["id"]]  # full-name search
    assert u["e1"].get("/api/v1/org/delegation-candidates").status_code == 403  # employees can't delegate


def test_demoted_manager_cannot_approve_pending_request(org):
    u = org["users"]
    admin = u["admin_a"]
    leave = _apply(u["e1"]).json()
    admin.put(f"/api/v1/users/{u['m2'].user_id}/roles", json={"role_ids": [role_id(admin, "employee")]})
    assert u["m2"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).status_code in (403, 404)
    assert u["m2"].get("/api/v1/leaves/approvals").json() == []


def test_leave_listing_is_scoped(org):
    u = org["users"]
    _apply(u["e1"])
    _apply(u["e4"])
    assert u["m2"].get("/api/v1/leaves").json()["total"] == 1
    assert u["m3"].get("/api/v1/leaves").json()["total"] == 1
    assert u["admin_a"].get("/api/v1/leaves").json()["total"] == 2
    assert u["admin_b"].get("/api/v1/leaves").json()["total"] == 0


# Payroll ----------------------------------------------------------------------
def _payroll_setup(org, app):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    preparer = invite_and_activate(app, admin, create_employee(admin, "Paula", "Prep"), ["payroll_manager"])
    approver = invite_and_activate(app, admin, create_employee(admin, "Andy", "Approve"), ["payroll_manager"])
    admin.post("/api/v1/salary-structures", json={
        "employee_id": e["e1"]["id"], "basic_salary": "30000", "house_rent_allowance": "5000",
        "effective_from": "2024-01-01"})
    return preparer, approver


def test_future_salary_revision_does_not_change_todays_pay(org):
    admin, e1 = org["users"]["admin_a"], org["emp"]["e1"]
    assert admin.post("/api/v1/salary-structures", json={
        "employee_id": e1["id"], "basic_salary": "30000", "effective_from": "2024-01-01"}).status_code == 200
    assert admin.get(f"/api/v1/employees/{e1['id']}").json()["base_salary"] == 30000
    upcoming = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1).isoformat()
    r = admin.post("/api/v1/salary-structures", json={
        "employee_id": e1["id"], "basic_salary": "36000", "effective_from": upcoming})
    assert r.status_code == 200, r.text
    # Both structures are kept; until next month the current one still applies.
    assert len(admin.get(f"/api/v1/salary-structures?employee_id={e1['id']}").json()) == 2
    assert admin.get(f"/api/v1/employees/{e1['id']}").json()["base_salary"] == 30000
    # Editing the employee record can't quietly diverge from the structure payroll uses...
    r = admin.put(f"/api/v1/employees/{e1['id']}", json={"base_salary": "99000"})
    assert r.status_code == 409 and r.json()["code"] == "SALARY_FROM_STRUCTURE"
    # ...while saving the form unchanged (it always sends the field) still works.
    assert admin.put(f"/api/v1/employees/{e1['id']}", json={"base_salary": "30000", "position": "Engineer"}).status_code == 200
    # Without a structure, base salary is edited directly as before.
    e2 = org["emp"]["e2"]
    assert admin.put(f"/api/v1/employees/{e2['id']}", json={"base_salary": "41000"}).status_code == 200
    assert admin.get(f"/api/v1/employees/{e2['id']}").json()["base_salary"] == 41000


def test_payroll_separation_of_duties(org, app):
    u, e = org["users"], org["emp"]
    preparer, approver = _payroll_setup(org, app)
    run = preparer.post(f"/api/v1/payroll/generate?employee_id={e['e1']['id']}&year=2025&month=3")
    assert run.status_code == 200, run.text
    pid = run.json()["id"]
    r = preparer.post(f"/api/v1/payroll/{pid}/approve")
    assert r.status_code == 403  # preparer cannot approve their own run
    assert approver.post(f"/api/v1/payroll/{pid}/approve").json()["status"] == "approved"
    assert preparer.post(f"/api/v1/payroll/{pid}/process").json()["status"] == "processed"


def test_payslip_visibility(org, app):
    u, e = org["users"], org["emp"]
    preparer, approver = _payroll_setup(org, app)
    pid = preparer.post(f"/api/v1/payroll/generate?employee_id={e['e1']['id']}&year=2025&month=4").json()["id"]
    # Drafts are invisible to the employee; managers never see payroll.
    assert u["e1"].get("/api/v1/payroll/my").json() == []
    assert u["e1"].get(f"/api/v1/payroll/{pid}/slip").status_code == 404
    assert u["m2"].get(f"/api/v1/payroll/{pid}/slip").status_code == 404
    approver.post(f"/api/v1/payroll/{pid}/approve")
    assert [p["id"] for p in u["e1"].get("/api/v1/payroll/my").json()] == [pid]
    assert u["e1"].get(f"/api/v1/payroll/{pid}/slip").status_code == 200
    assert u["e1"].get(f"/api/v1/payroll/{pid}/slip.pdf").headers["content-type"] == "application/pdf"
    assert u["e4"].get(f"/api/v1/payroll/{pid}/slip").status_code == 404
    assert u["e1"].get("/api/v1/payroll").status_code == 403


def test_cannot_approve_own_pay(org, app):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    preparer, approver = _payroll_setup(org, app)
    admin.post("/api/v1/salary-structures", json={
        "employee_id": approver.employee["id"], "basic_salary": "40000", "effective_from": "2024-01-01"})
    pid = preparer.post(f"/api/v1/payroll/generate?employee_id={approver.employee['id']}&year=2025&month=5").json()["id"]
    assert approver.post(f"/api/v1/payroll/{pid}/approve").status_code == 403


def test_holidays_reduce_working_days(org, app):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    preparer, _ = _payroll_setup(org, app)
    before = preparer.post(f"/api/v1/payroll/calculate?employee_id={e['e1']['id']}&year=2025&month=8").json()["working_days"]
    admin.post("/api/v1/holidays", json={"name": "Independence Day", "date": "2025-08-15"})
    after = preparer.post(f"/api/v1/payroll/calculate?employee_id={e['e1']['id']}&year=2025&month=8").json()["working_days"]
    assert after == before - 1


# Attendance -------------------------------------------------------------------
def test_self_check_in_cannot_target_other_employees(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    admin.put("/api/v1/settings", json={"requireFaceForSelfCheckIn": False})
    r = u["e1"].post(f"/api/v1/attendance/me/check-in?employee_id={e['e2']['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["employee_id"] == e["e1"]["id"]  # the session decides who checks in
    assert u["e1"].post("/api/v1/attendance/me/check-in").status_code == 409
    # The legacy endpoint that trusted a client-supplied employee id is gone.
    assert u["e1"].post(f"/api/v1/attendance/check-in?employee_id={e['e2']['id']}").status_code in (404, 405)


def test_face_is_required_by_default(org):
    r = org["users"]["e1"].post("/api/v1/attendance/me/check-in")
    assert r.status_code == 400 and r.json()["code"] == "FACE_REQUIRED"


def test_attendance_views_are_scoped(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for key in ("e1", "e4"):
        r = admin.post("/api/v1/attendance/manual", json={
            "employee_id": e[key]["id"], "date": date.today().isoformat(),
            "check_in_time": (now - timedelta(hours=2)).isoformat(), "status": "present"})
        assert r.status_code == 200, r.text
    assert u["m2"].get("/api/v1/attendance/summary/today").json()["present_today"] == 1
    assert u["admin_a"].get("/api/v1/attendance/summary/today").json()["present_today"] == 2
    start = (date.today() - timedelta(days=1)).isoformat()
    end = date.today().isoformat()
    m3_rows = u["m3"].get(f"/api/v1/attendance?start_date={start}&end_date={end}").json()
    assert [r["employee_id"] for r in m3_rows["items"]] == [e["e4"]["id"]]
    # Manager A2 may regularize direct reports but not other branches or themselves.
    assert u["m2"].post("/api/v1/attendance/manual", json={
        "employee_id": e["e4"]["id"], "date": end, "check_in_time": now.isoformat()}).status_code == 404
    own = u["m2"].post("/api/v1/attendance/manual", json={
        "employee_id": e["m2"]["id"], "date": end, "check_in_time": now.isoformat()})
    assert own.status_code == 403 and own.json()["detail"] == "You cannot correct your own attendance."


def test_manual_entry_corrects_the_day_instead_of_duplicating_it(org):
    admin, e1 = org["users"]["admin_a"], org["emp"]["e1"]
    day = date.today().isoformat()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    body = {"employee_id": e1["id"], "date": day, "check_in_time": (now - timedelta(hours=3)).isoformat()}
    first = admin.post("/api/v1/attendance/manual", json=body)
    assert first.status_code == 200, first.text
    second = admin.post("/api/v1/attendance/manual", json={
        **body, "check_out_time": now.isoformat(), "status": "late", "notes": "Badge reader was down"})
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    rows = admin.get(f"/api/v1/attendance?start_date={day}&end_date={day}&employee_id={e1['id']}").json()
    assert rows["total"] == 1 and rows["items"][0]["status"] == "late"
    # The correction is audited together with what it replaced.
    logs = admin.get("/api/v1/audit-logs?module=Attendance").json()["logs"]
    assert any(log["description"].startswith(f"Corrected attendance for {e1['full_name']}") and "was present" in log["description"]
               for log in logs)


def test_database_keeps_one_attendance_record_per_day(org, monkeypatch):
    from app.services import attendance_service

    admin, e1 = org["users"]["admin_a"], org["emp"]["e1"]
    body = {"employee_id": e1["id"], "date": date.today().isoformat(),
            "check_in_time": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    assert admin.post("/api/v1/attendance/manual", json=body).status_code == 200
    # A concurrent request that looked before the first one committed still cannot add a second record.
    monkeypatch.setattr(attendance_service, "_open_record", lambda *args, **kwargs: None)
    r = admin.post("/api/v1/attendance/manual", json=body)
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_CHECKED_IN"


def test_kiosk_requires_company_permission(org):
    files = {"file": ("f.png", b"not an image", "image/png")}
    assert org["users"]["m1"].post("/api/v1/attendance/kiosk/scan", files=files).status_code == 403


# Reports & dashboards ---------------------------------------------------------
def test_reports_and_exports_are_scoped(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for key in ("e1", "e3", "e4"):
        admin.post("/api/v1/attendance/manual", json={
            "employee_id": e[key]["id"], "date": date.today().isoformat(),
            "check_in_time": (now - timedelta(hours=1)).isoformat()})
    start, end = (date.today() - timedelta(days=1)).isoformat(), date.today().isoformat()
    report = u["m2"].get(f"/api/v1/reports/attendance?start_date={start}&end_date={end}").json()
    assert {r["employee_id"] for r in report["records"]} == {e["e1"]["id"]}
    assert u["m1"].get(f"/api/v1/reports/attendance?start_date={start}&end_date={end}").json()["total_records"] == 2
    # Managers can read but not export reports by default.
    assert u["m2"].get(f"/api/v1/reports/attendance/export/excel?start_date={start}&end_date={end}").status_code == 403
    export = admin.get(f"/api/v1/reports/attendance/export/excel?start_date={start}&end_date={end}")
    assert export.status_code == 200
    assert u["admin_b"].get(f"/api/v1/reports/attendance?start_date={start}&end_date={end}").json()["total_records"] == 0


def test_excel_exports_report_real_scores_and_leave_types(org):
    from io import BytesIO

    from openpyxl import load_workbook

    admin, e1 = org["users"]["admin_a"], org["emp"]["e1"]
    today = date.today().isoformat()
    admin.post("/api/v1/attendance/manual", json={
        "employee_id": e1["id"], "date": today,
        "check_in_time": datetime.now(timezone.utc).replace(microsecond=0).isoformat()})
    _apply(org["users"]["e1"], "2099-05-04", leave_type="casual")
    _apply(org["users"]["e1"], "2099-05-11", leave_type="sick")

    sheet = load_workbook(BytesIO(admin.get(
        f"/api/v1/reports/attendance/export/excel?start_date={today}&end_date={today}").content)).worksheets
    details = next(ws for ws in sheet if ws.cell(row=1, column=7).value)
    assert details.cell(row=2, column=7).value == "-"  # manual entry: no face match, not "0.0%"

    book = load_workbook(BytesIO(admin.get("/api/v1/reports/leaves/export/excel?year=2099").content))
    counted = {row[0]: row[1] for ws in book.worksheets for row in ws.iter_rows(values_only=True) if row and row[0]}
    assert counted.get("Total Records") == 2
    assert counted.get("Casual Leave") == 1 and counted.get("Sick Leave") == 1


def test_dashboard_sections_follow_permissions(org):
    u = org["users"]
    employee_view = u["e1"].get("/api/v1/dashboard").json()
    assert "me" in employee_view["sections"] and "attendance" not in employee_view["sections"]
    manager_view = u["m1"].get("/api/v1/dashboard").json()
    assert manager_view["scope"] == "team" and manager_view["headcount"] == 5
    assert "payroll" not in manager_view
    admin_view = u["admin_a"].get("/api/v1/dashboard").json()
    assert admin_view["scope"] == "company" and "payroll" in admin_view


# Assets & holidays -----------------------------------------------------------------
def test_asset_lifecycle_and_visibility(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    asset = admin.post("/api/v1/assets", json={"asset_tag": "LAP-001", "name": "Laptop", "category": "IT"}).json()
    assert admin.post(f"/api/v1/assets/{asset['id']}/assign", json={"employee_id": e["e1"]["id"]}).json()["status"] == "assigned"
    mine = u["e1"].get("/api/v1/assets/mine").json()
    assert [a["asset_tag"] for a in mine] == ["LAP-001"]
    assert u["e1"].post(f"/api/v1/assets/{asset['id']}/acknowledge").status_code == 200
    assert u["e4"].post(f"/api/v1/assets/{asset['id']}/acknowledge").status_code == 404
    assert u["e4"].get(f"/api/v1/assets/{asset['id']}").status_code == 404
    assert u["admin_b"].get(f"/api/v1/assets/{asset['id']}").status_code == 404
    assert u["e1"].post("/api/v1/assets", json={"asset_tag": "X", "name": "X", "category": "X"}).status_code == 403
    # Transfer, then recover.
    admin.post(f"/api/v1/assets/{asset['id']}/assign", json={"employee_id": e["e2"]["id"]})
    assert u["e1"].get("/api/v1/assets/mine").json() == []
    returned = admin.post(f"/api/v1/assets/{asset['id']}/return", json={"condition": "good"}).json()
    assert returned["status"] == "in_stock" and len(returned["history"]) == 2


def test_holiday_calendars_are_company_and_location_specific(org):
    u = org["users"]
    a, b = u["admin_a"], u["admin_b"]
    a.post("/api/v1/holidays", json={"name": "Founders Day", "date": "2099-02-01"})
    a.post("/api/v1/holidays", json={"name": "Pune Festival", "date": "2099-02-02", "location": "Pune"})
    assert b.get("/api/v1/holidays?year=2099").json() == []
    assert len(a.get("/api/v1/holidays?year=2099").json()) == 2
    # Employees without a location only see company-wide holidays in their own calendar.
    assert [h["name"] for h in u["e1"].get("/api/v1/holidays?year=2099&mine=true").json()] == ["Founders Day"]
    assert u["e1"].post("/api/v1/holidays", json={"name": "Nope", "date": "2099-03-01"}).status_code == 403


def test_notifications_are_private(org):
    u = org["users"]
    leave = _apply(u["e1"]).json()
    u["m2"].post(f"/api/v1/leaves/{leave['id']}/approve", json={})
    notes = u["e1"].get("/api/v1/notifications").json()["notifications"]
    assert any("approved" in n["title"] for n in notes)
    note_id = notes[0]["id"]
    assert u["e4"].put(f"/api/v1/notifications/{note_id}/read").status_code == 404
    assert u["admin_b"].put(f"/api/v1/notifications/{note_id}/read").status_code == 404
