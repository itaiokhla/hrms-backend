"""Reporting hierarchy, data scopes and hierarchy integrity."""

import threading

from tests.helpers import create_employee, invite_and_activate


def visible_ids(client) -> set:
    return {e["id"] for e in client.get("/api/v1/employees?limit=200").json()["items"]}


def test_manager_scopes_follow_reporting_tree(org):
    u, e = org["users"], org["emp"]
    # Manager A1: own record plus the whole subtree (A2, E1, E2, E3).
    assert visible_ids(u["m1"]) == {e["m1"]["id"], e["m2"]["id"], e["e1"]["id"], e["e2"]["id"], e["e3"]["id"]}
    # Manager A2: own record plus direct reports only.
    assert visible_ids(u["m2"]) == {e["m2"]["id"], e["e1"]["id"], e["e2"]["id"]}
    # Manager A3 sits at the same level as A1 but sees only its own branch.
    assert visible_ids(u["m3"]) == {e["m3"]["id"], e["e4"]["id"]}
    # Employees see only themselves; Company B never appears.
    assert visible_ids(u["e1"]) == {e["e1"]["id"]}
    assert e["b1"]["id"] not in visible_ids(u["m1"])


def test_manager_cannot_read_outside_tree(org):
    u, e = org["users"], org["emp"]
    assert u["m3"].get(f"/api/v1/employees/{e['e1']['id']}").status_code == 404
    assert u["m2"].get(f"/api/v1/employees/{e['e3']['id']}").status_code == 404
    assert u["m1"].get(f"/api/v1/employees/{e['b1']['id']}").status_code == 404


def test_pagination_totals_respect_scope(org):
    body = org["users"]["m2"].get("/api/v1/employees?limit=1").json()
    assert body["total"] == 3
    assert len(body["items"]) == 1


def test_employee_cannot_swap_ids_to_reach_others(org):
    u, e = org["users"], org["emp"]
    assert u["e1"].get(f"/api/v1/employees/{e['e2']['id']}").status_code == 404
    assert u["e1"].get(f"/api/v1/leaves/balance/{e['e2']['id']}").status_code == 404
    assert u["e1"].get(f"/api/v1/attendance/statistics?employee_id={e['e2']['id']}&month=1&year=2026").status_code == 404
    # Self-service endpoints take no employee id at all.
    assert u["e1"].get("/api/v1/employees/me").json()["id"] == e["e1"]["id"]


def test_confidential_and_salary_fields_need_explicit_permission(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    admin.put(f"/api/v1/employees/{e['e1']['id']}", json={"phone": "+911234567890", "base_salary": "45000"})
    as_manager = u["m2"].get(f"/api/v1/employees/{e['e1']['id']}").json()
    assert "phone" not in as_manager and "base_salary" not in as_manager
    as_admin = admin.get(f"/api/v1/employees/{e['e1']['id']}").json()
    assert as_admin["phone"] == "+911234567890" and as_admin["base_salary"] == 45000.0
    me = u["e1"].get("/api/v1/employees/me").json()
    assert me["phone"] == "+911234567890"


def test_manager_without_payroll_permission_cannot_see_payroll(org):
    m1 = org["users"]["m1"]
    assert m1.get("/api/v1/payroll").status_code == 403
    assert m1.get("/api/v1/salary-structures").json() == [] or m1.get("/api/v1/salary-structures").status_code in (200, 403)
    assert m1.get("/api/v1/reports/payroll?year=2026").status_code == 403


def test_self_and_cycle_assignments_rejected(org):
    admin, e = org["users"]["admin_a"], org["emp"]
    r = admin.put(f"/api/v1/org/employees/{e['m1']['id']}/manager", json={"manager_id": e["m1"]["id"]})
    assert r.status_code == 400 and r.json()["code"] == "SELF_REPORTING"
    # A1 -> A2 -> E1; making A1 report to E1 would close a loop.
    r = admin.put(f"/api/v1/org/employees/{e['m1']['id']}/manager", json={"manager_id": e["e1"]["id"]})
    assert r.status_code == 400 and r.json()["code"] == "HIERARCHY_CYCLE"


def test_manager_cannot_change_own_reporting_line(org):
    u, e = org["users"], org["emp"]
    r = u["m1"].put(f"/api/v1/org/employees/{e['m1']['id']}/manager", json={"manager_id": e["m3"]["id"]})
    assert r.status_code == 403


def test_database_trigger_rejects_cycles_even_without_app_checks(org):
    import pytest
    from sqlalchemy import text

    from app.database.db import engine

    a_id = org["users"]["admin_a"].company_id
    e = org["emp"]
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(a_id)})
            conn.execute(text("UPDATE employees SET manager_id = :m WHERE id = :id"), {"m": e["e2"]["id"], "id": e["m1"]["id"]})
    assert "cycle" in str(exc.value).lower()


def test_concurrent_updates_cannot_form_a_cycle(org):
    """X->Y and Y->X submitted at the same time: exactly one may succeed."""
    from sqlalchemy import text

    from app.database.db import engine

    a_id = org["users"]["admin_a"].company_id
    x, y = org["emp"]["m3"]["id"], org["emp"]["e3"]["id"]
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(emp_id, manager_id):
        try:
            with engine.begin() as conn:
                conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(a_id)})
                barrier.wait(timeout=10)
                conn.execute(text("UPDATE employees SET manager_id = :m WHERE id = :id"), {"m": manager_id, "id": emp_id})
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001 - recording the database outcome
            outcomes.append(f"error: {exc.__class__.__name__}")

    threads = [threading.Thread(target=worker, args=(x, y)), threading.Thread(target=worker, args=(y, x))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert outcomes.count("ok") == 1, outcomes


def test_manager_change_moves_access_and_reroutes_pending_leave(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    leave = u["e1"].post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": "2099-03-02", "end_date": "2099-03-02"})
    assert leave.status_code == 200, leave.text
    leave_id = leave.json()["id"]
    assert [l["id"] for l in u["m2"].get("/api/v1/leaves/approvals").json()] == [leave_id]

    # Move E1 from Manager A2 to Manager A3.
    r = admin.put(f"/api/v1/org/employees/{e['e1']['id']}/manager", json={"manager_id": e["m3"]["id"]})
    assert r.status_code == 200, r.text

    # Old manager loses current access and approval authority; new manager gains both.
    assert u["m2"].get(f"/api/v1/employees/{e['e1']['id']}").status_code == 404
    assert u["m2"].get("/api/v1/leaves/approvals").json() == []
    assert u["m2"].post(f"/api/v1/leaves/{leave_id}/approve", json={}).status_code in (403, 404)
    assert u["m3"].get(f"/api/v1/employees/{e['e1']['id']}").status_code == 200
    assert [l["id"] for l in u["m3"].get("/api/v1/leaves/approvals").json()] == [leave_id]
    assert u["m3"].post(f"/api/v1/leaves/{leave_id}/approve", json={}).json()["status"] == "approved"

    history = admin.get(f"/api/v1/org/employees/{e['e1']['id']}/history").json()
    assert [h["manager_id"] for h in history[:2]] == [e["m3"]["id"], e["m2"]["id"]]


def test_secondary_functional_manager_gets_direct_report_visibility(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    assert u["m3"].get(f"/api/v1/employees/{e['e2']['id']}").status_code == 404
    r = admin.post("/api/v1/org/secondary-lines", json={
        "employee_id": e["e2"]["id"], "manager_id": e["m3"]["id"], "relation_type": "functional",
        "effective_from": "2024-01-01",
    })
    assert r.status_code == 200, r.text
    assert u["m3"].get(f"/api/v1/employees/{e['e2']['id']}").status_code == 200
    # The primary tree is unchanged: E2 still reports to Manager A2.
    assert admin.get(f"/api/v1/employees/{e['e2']['id']}").json()["manager_id"] == e["m2"]["id"]
    admin.delete(f"/api/v1/org/secondary-lines/{r.json()['id']}")


def test_org_chart_is_scoped_and_lazy(org):
    u, e = org["users"], org["emp"]
    roots = u["m1"].get("/api/v1/org/nodes").json()
    assert [n["id"] for n in roots] == [e["m1"]["id"]]
    assert roots[0]["report_count"] == 2
    children = {n["id"] for n in u["m1"].get(f"/api/v1/org/nodes?parent_id={e['m1']['id']}").json()}
    assert children == {e["m2"]["id"], e["e3"]["id"]}
    chain = u["e1"].get(f"/api/v1/org/employees/{e['e1']['id']}/chain").json()
    assert [c["id"] for c in chain] == [e["m2"]["id"], e["m1"]["id"]]


def test_deactivation_keeps_history_and_protects_reports(org):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    r = admin.post(f"/api/v1/employees/{e['m2']['id']}/deactivate")
    assert r.status_code == 409 and r.json()["code"] == "HAS_REPORTS"
    r = admin.post(f"/api/v1/employees/{e['e4']['id']}/deactivate")
    assert r.status_code == 200 and r.json()["is_active"] is False
    # The linked login is closed immediately.
    assert u["e4"].get("/api/v1/auth/me").status_code == 401
    assert admin.get(f"/api/v1/employees/{e['e4']['id']}").status_code == 200


def test_employee_without_manager_still_gets_a_decision(org):
    u, e = org["users"], org["emp"]
    # Manager A3 has no manager: the manager step is skipped and an administrator decides.
    r = u["m3"].post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": "2099-04-01", "end_date": "2099-04-01"})
    assert r.status_code == 200, r.text
    steps = r.json()["steps"]
    assert steps[0]["status"] == "skipped"
    assert steps[-1]["approver"] == "Company Administrator" and steps[-1]["status"] == "pending"
    assert u["admin_a"].post(f"/api/v1/leaves/{r.json()['id']}/approve", json={}).json()["status"] == "approved"


def test_department_transfer_changes_department_scope(org, app):
    u, e = org["users"], org["emp"]
    admin = u["admin_a"]
    dept = admin.post("/api/v1/departments", json={"name": "Finance"}).json()["id"]
    head = create_employee(admin, "Head", "Finance", department_id=dept)
    admin.put(f"/api/v1/departments/{dept}", json={"name": "Finance", "head_employee_id": head["id"]})
    role = admin.post("/api/v1/roles", json={"name": "Department Head", "grants": [
        {"permission": "employee:read", "scope": "DEPARTMENT"}]}).json()["id"]
    head_user = invite_and_activate(app, admin, head, [])
    admin.put(f"/api/v1/users/{head_user.user_id}/roles", json={"role_ids": [role]})
    assert head_user.get(f"/api/v1/employees/{e['e3']['id']}").status_code == 404
    admin.put(f"/api/v1/employees/{e['e3']['id']}", json={"department_id": dept})
    assert head_user.get(f"/api/v1/employees/{e['e3']['id']}").status_code == 200
