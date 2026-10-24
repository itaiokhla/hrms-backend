"""Placements carry the payroll dimensions; destination authorization (audit 01 D-8); termination on the
last working day closes every placement (D-4); the last payroll approver is protected on deactivation
(contract §28.9); migration 0013 repairs terminated placements. EC-ORG-06, EC-ORG-07, EC-ORG-11, EC-ORG-22.
"""

import importlib.util
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.helpers import create_employee, invite_and_activate, signup_company

BASE = "/api/v1/payroll/org"
MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0013_org_wiring.py"


def _sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.fetchall() if result.returns_rows else result.rowcount


def _entity(client, code="LE01"):
    return next(e for e in client.get("/api/v1/legal-entities").json() if e["code"] == code)


def _post(client, path, body, status=200):
    r = client.post(path, json=body)
    assert r.status_code == status, r.text
    return r.json()


def _dim(client, path, body):
    return _post(client, f"{BASE}/{path}", body, 201)


def _by_code(client, path, code):
    return next(x for x in client.get(f"{BASE}/{path}").json() if x["code"] == code)


def _history(client, employee_id):
    r = client.get(f"/api/v1/employees/{employee_id}/assignments")
    assert r.status_code == 200, r.text
    return r.json()


def _move(client, employee_id, status=200, **body):
    r = client.post(f"/api/v1/employees/{employee_id}/assignments", json=body)
    assert r.status_code == status, r.text
    return r.json()


@pytest.fixture
def acme(app):
    return signup_company(app, "Acme Corp")


@pytest.fixture
def gulf(acme):
    entity = _post(acme, "/api/v1/legal-entities", {"code": "AE01", "legal_name": "Acme Gulf FZ-LLC", "country_code": "AE",
                                                   "base_currency": "AED", "timezone": "Asia/Dubai"})
    dubai = _post(acme, "/api/v1/branches", {"legal_entity_id": entity["id"], "code": "DXB", "name": "Dubai"})
    abu = _post(acme, "/api/v1/branches", {"legal_entity_id": entity["id"], "code": "AUH", "name": "Abu Dhabi"})
    return {"entity": entity, "dubai": dubai, "abu": abu}


# ---------------------------------------------------------------------------
# Placement dimensions
# ---------------------------------------------------------------------------
def test_hire_places_people_in_the_default_pay_group_permanent_branch_work_week_and_their_shift(acme):
    shift = _post(acme, "/api/v1/shifts", {"name": "Night", "start_time": "22:00", "end_time": "06:00"})
    employee = create_employee(acme, "Hired", shift_id=shift["id"])
    row = _history(acme, employee["id"])[0]
    assert (row["reason"], row["pay_group_code"], row["employment_type_code"], row["work_week_code"]) == (
        "HIRE", "MONTHLY", "PERMANENT", "MON_FRI")
    assert row["shift_id"] == shift["id"] and Decimal(row["fte"]) == 1
    assert (row["grade_id"], row["designation_id"], row["cost_center_id"]) == (None, None, None)


def test_record_hire_and_rehire_validate_explicit_dimensions(app, acme, gulf):
    """The service entry points used by hiring and rehiring take the same dimensions as a transfer."""
    from app.core.errors import AppError
    from app.database.db import SessionLocal, bind_tenant
    from app.models.employee import Employee
    from app.models.org import EmployeeAssignment
    from app.services import org_structure_service

    other = signup_company(app, "Other Co")
    entity = _entity(acme)
    weekly = _dim(acme, "pay-groups", {"legal_entity_id": entity["id"], "code": "WEEKLY", "name": "Weekly",
                                       "pay_frequency": "WEEKLY"})
    gulf_group = _dim(acme, "pay-groups", {"legal_entity_id": gulf["entity"]["id"], "code": "AE-M", "name": "Gulf",
                                           "pay_frequency": "MONTHLY"})
    intern = _dim(acme, "employment-types", {"code": "INTERN", "name": "Intern", "worker_class": "INTERN"})
    foreign_grade = _dim(other, "grades", {"code": "G1", "name": "G1"})
    hired = create_employee(acme, "Direct")
    db = SessionLocal()
    try:
        bind_tenant(db, acme.company_id)
        employee = db.get(Employee, hired["id"])
        db.query(EmployeeAssignment).filter(EmployeeAssignment.employee_id == employee.id).delete()
        db.flush()
        for dims, status in (({"pay_group_id": gulf_group["id"]}, 400), ({"grade_id": foreign_grade["id"]}, 404),
                             ({"fte": "2"}, 400)):
            with pytest.raises(AppError) as err:
                org_structure_service.record_hire(db, employee, None, entity["id"], dimensions=dims)
            assert err.value.status_code == status, dims
        row = org_structure_service.record_hire(db, employee, None, entity["id"], dimensions={
            "pay_group_id": weekly["id"], "employment_type_id": intern["id"], "fte": "0.75"})
        assert (row.pay_group_id, row.employment_type_id, row.fte) == (weekly["id"], intern["id"], Decimal("0.75"))
        row.effective_to = date(2024, 6, 30)
        employee.is_active = False
        db.flush()
        rehired = org_structure_service.record_rehire(db, employee, None, date(2025, 1, 1),
                                                      dimensions={"fte": "0.5"})
        # The rehire continues the last placement's dimensions unless told otherwise.
        assert (rehired.reason, rehired.pay_group_id, rehired.employment_type_id, rehired.fte) == (
            "REHIRE", weekly["id"], intern["id"], Decimal("0.5"))
    finally:
        db.rollback()
        db.close()


def test_ec_org_07_change_placement_accepts_and_validates_every_dimension(app, acme, gulf):
    other = signup_company(app, "Other Co")
    entity = _entity(acme)
    employee = create_employee(acme, "Mover")
    grade = _dim(acme, "grades", {"code": "G3", "name": "Grade 3"})
    designation = _dim(acme, "designations", {"code": "LEAD", "name": "Team lead", "grade_id": grade["id"]})
    center = _dim(acme, "cost-centers", {"code": "CC-IN", "name": "India ops", "legal_entity_id": entity["id"]})
    contractor = _dim(acme, "employment-types", {"code": "CONTRACT", "name": "Contractor", "worker_class": "CONTRACTOR"})
    weekly = _dim(acme, "pay-groups", {"legal_entity_id": entity["id"], "code": "WEEKLY", "name": "Weekly",
                                       "pay_frequency": "WEEKLY"})
    six_days = _dim(acme, "work-weeks", {"code": "MON_SAT", "name": "Mon-Sat", "weekly_off_days": [6]})
    shift = _post(acme, "/api/v1/shifts", {"name": "Early", "start_time": "06:00", "end_time": "14:00"})
    main = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Main")
    body = {"branch_id": main["id"], "effective_from": "2024-06-01", "reason": "PROMOTION", "pay_group_id": weekly["id"],
            "designation_id": designation["id"], "cost_center_id": center["id"], "employment_type_id": contractor["id"],
            "work_week_pattern_id": six_days["id"], "shift_id": shift["id"], "fte": "0.5"}
    row = _move(acme, employee["id"], **body)[0]
    assert (row["pay_group_code"], row["designation_code"], row["grade_code"], row["cost_center_code"],
            row["employment_type_code"], row["work_week_code"], row["shift_id"], Decimal(row["fte"])) == (
        "WEEKLY", "LEAD", "G3", "CC-IN", "CONTRACT", "MON_SAT", shift["id"], Decimal("0.5"))  # grade from designation
    assert acme.get(f"/api/v1/employees/{employee['id']}").json()["shift_id"] == shift["id"]  # legacy column synced
    _move(acme, employee["id"], 400, **{**body, "effective_from": "2024-07-01"})  # ORG_NO_CHANGE

    gulf_group = _dim(acme, "pay-groups", {"legal_entity_id": gulf["entity"]["id"], "code": "AE-M", "name": "Gulf",
                                           "pay_frequency": "MONTHLY"})
    gulf_center = _dim(acme, "cost-centers", {"code": "CC-AE", "name": "Gulf", "legal_entity_id": gulf["entity"]["id"]})
    later = {"branch_id": main["id"], "effective_from": "2024-07-01"}
    for change, status, code in (
        ({"pay_group_id": gulf_group["id"]}, 400, "ORG_PAY_GROUP_MISMATCH"),
        ({"cost_center_id": gulf_center["id"]}, 400, "ORG_COST_CENTER_MISMATCH"),
        ({"fte": "0"}, 400, "ORG_FTE_INVALID"),
        ({"fte": "1.5"}, 400, "ORG_FTE_INVALID"),
        ({"fte": "0.12345"}, 400, "ORG_FTE_INVALID"),
        ({"fte": 0.5}, 422, None),                      # a JSON number is never accepted
        ({"pay_group_id": 999999}, 404, None),
        ({"company_id": other.company_id}, 422, None),  # server-owned
    ):
        r = acme.post(f"/api/v1/employees/{employee['id']}/assignments", json={**later, **change})
        assert r.status_code == status, (change, r.text)
        if code:
            assert r.json()["code"] == code, (change, r.json())
    # EC-ORG-02: another tenant's dimensions are unknown here.
    foreign = {"grade_id": _dim(other, "grades", {"code": "G3", "name": "G3"})["id"],
               "designation_id": _dim(other, "designations", {"code": "LEAD", "name": "Lead"})["id"],
               "cost_center_id": _dim(other, "cost-centers", {"code": "CC-IN", "name": "CC"})["id"],
               "employment_type_id": _by_code(other, "employment-types", "PERMANENT")["id"],
               "work_week_pattern_id": _by_code(other, "work-weeks", "MON_FRI")["id"],
               "pay_group_id": _by_code(other, "pay-groups", "MONTHLY")["id"],
               "shift_id": other.get("/api/v1/shifts").json()[0]["id"]}
    for key, value in foreign.items():
        r = acme.post(f"/api/v1/employees/{employee['id']}/assignments", json={**later, key: value})
        assert r.status_code == 404, (key, r.text)
    spare = _dim(acme, "grades", {"code": "OLD", "name": "Old"})
    acme.patch(f"{BASE}/grades/{spare['id']}", json={"status": "INACTIVE"})
    r = acme.post(f"/api/v1/employees/{employee['id']}/assignments", json={**later, "grade_id": spare["id"]})
    assert r.status_code == 400 and r.json()["code"] == "ORG_DIMENSION_INACTIVE"

    # Fields left out continue; null clears optional ones and picks the default of required ones.
    row = _move(acme, employee["id"], **later, department_id=None, grade_id=None, pay_group_id=None)[0]
    assert (row["pay_group_code"], row["designation_code"], row["grade_code"], row["cost_center_code"],
            row["employment_type_code"], row["shift_id"], Decimal(row["fte"])) == (
        "MONTHLY", "LEAD", None, "CC-IN", "CONTRACT", shift["id"], Decimal("0.5"))
    assert acme.patch(f"{BASE}/pay-groups/{weekly['id']}", json={"status": "INACTIVE"}).status_code == 200  # unused now


def test_ec_org_06_transfer_to_another_entity_takes_its_pay_group_and_branch_work_week(acme, gulf):
    employee = create_employee(acme, "Expat")
    center = _dim(acme, "cost-centers", {"code": "CC-IN", "name": "India", "legal_entity_id": _entity(acme)["id"]})
    shared = _dim(acme, "cost-centers", {"code": "CC-ALL", "name": "Shared"})
    main = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Main")
    _move(acme, employee["id"], branch_id=main["id"], effective_from="2024-03-01", cost_center_id=center["id"])
    row = _move(acme, employee["id"], branch_id=gulf["dubai"]["id"], effective_from="2024-04-01")[0]
    assert (row["legal_entity_code"], row["pay_group_id"], row["cost_center_id"]) == ("AE01", None, None)
    gulf_group = _dim(acme, "pay-groups", {"legal_entity_id": gulf["entity"]["id"], "code": "AE-M", "name": "Gulf",
                                           "pay_frequency": "MONTHLY"})
    sunday = _dim(acme, "work-weeks", {"code": "SUN_THU", "name": "Sun-Thu", "weekly_off_days": [4, 5]})
    acme.put(f"{BASE}/branches/{gulf['abu']['id']}/calendar", json={"work_week_pattern_id": sunday["id"]})
    row = _move(acme, employee["id"], branch_id=gulf["abu"]["id"], effective_from="2024-05-01",
                cost_center_id=shared["id"])[0]
    assert (row["pay_group_id"], row["work_week_code"], row["cost_center_code"]) == (gulf_group["id"], "SUN_THU", "CC-ALL")
    history = _history(acme, employee["id"])
    # EC-ORG-11: the rows never overlap; each closes the day before the next one starts.
    for newer, older in zip(history, history[1:]):
        assert date.fromisoformat(older["effective_to"]) == date.fromisoformat(newer["effective_from"]) - timedelta(days=1)


def test_d8_entity_and_branch_scoped_editors_move_people_only_into_their_targets(app, acme, gulf):
    main = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Main")
    staff = create_employee(acme, "GulfStaff")
    _move(acme, staff["id"], branch_id=gulf["dubai"]["id"], effective_from="2024-02-01")
    india_staff = create_employee(acme, "IndiaStaff")
    role = acme.post("/api/v1/roles", json={"name": "Gulf HR", "grants": [
        {"permission": "employee:read", "scope": "LEGAL_ENTITY"}, {"permission": "employee:update", "scope": "LEGAL_ENTITY"},
        {"permission": "employee:read", "scope": "BRANCH"}, {"permission": "employee:update", "scope": "BRANCH"}]})
    assert role.status_code == 200, role.text
    hr = invite_and_activate(app, acme, create_employee(acme, "GulfHR"), [])
    acme.put(f"/api/v1/users/{hr.user_id}/roles", json={"role_ids": [role.json()["id"]]})
    targets = f"/api/v1/users/{hr.user_id}/scope-targets"
    assert acme.put(targets, json={"assignments": [{"role_id": role.json()["id"],
                                                    "legal_entity_ids": [gulf["entity"]["id"]]}]}).status_code == 200
    _move(hr, staff["id"], branch_id=gulf["abu"]["id"], effective_from="2024-03-01")      # inside the entity
    _move(hr, staff["id"], 404, branch_id=main["id"], effective_from="2024-04-01")         # out of reach (D-8)
    _move(hr, india_staff["id"], 404, branch_id=gulf["dubai"]["id"], effective_from="2024-04-01")
    assert [h["branch"] for h in _history(acme, staff["id"])][:1] == ["Abu Dhabi"]
    # Branch-scoped: only the targeted branch, not the rest of its entity.
    assert acme.put(targets, json={"assignments": [{"role_id": role.json()["id"],
                                                    "branch_ids": [gulf["abu"]["id"]]}]}).status_code == 200
    _move(hr, staff["id"], 404, branch_id=gulf["dubai"]["id"], effective_from="2024-05-01")
    # Staying in the same entity and branch is not a move: a department change is allowed.
    department = acme.post("/api/v1/departments", json={"name": "Sales", "is_active": True})
    assert department.status_code == 200, department.text
    _move(hr, staff["id"], branch_id=gulf["abu"]["id"], department_id=department.json()["id"], effective_from="2024-05-01")
    # Company-wide editors move anyone anywhere.
    _move(acme, staff["id"], branch_id=main["id"], effective_from="2024-06-01")


def test_d8_same_day_location_correction_is_authorized_and_takes_the_new_entitys_pay_group(app, acme, gulf):
    """A hire-day correction on the employee form moves the row in place: it is authorized like a transfer
    (D-8), entity-bound dimensions follow the new entity (no pay group of another entity) and it is audited."""
    gulf_group = _dim(acme, "pay-groups", {"legal_entity_id": gulf["entity"]["id"], "code": "AE-M", "name": "Gulf",
                                           "pay_frequency": "MONTHLY"})
    india_cc = _dim(acme, "cost-centers", {"code": "CC-IN", "name": "India", "legal_entity_id": _entity(acme)["id"]})
    newbie = create_employee(acme, "Newbie", hire_date=date.today().isoformat())
    _sql("UPDATE employee_assignments SET cost_center_id = :cc WHERE employee_id = :e", cc=india_cc["id"], e=newbie["id"])
    role = acme.post("/api/v1/roles", json={"name": "India HR", "grants": [
        {"permission": "employee:read", "scope": "LEGAL_ENTITY"}, {"permission": "employee:update", "scope": "LEGAL_ENTITY"}]})
    hr = invite_and_activate(app, acme, create_employee(acme, "IndiaHR"), [])
    acme.put(f"/api/v1/users/{hr.user_id}/roles", json={"role_ids": [role.json()["id"]]})
    assert acme.put(f"/api/v1/users/{hr.user_id}/scope-targets", json={"assignments": [
        {"role_id": role.json()["id"], "legal_entity_ids": [_entity(acme)["id"]]}]}).status_code == 200
    r = hr.put(f"/api/v1/employees/{newbie['id']}", json={"location": "Dubai"})
    assert r.status_code == 404, r.text  # Dubai is outside this editor's entities
    assert _history(acme, newbie["id"])[0]["branch"] == "Main"
    r = acme.put(f"/api/v1/employees/{newbie['id']}", json={"location": "Dubai"})
    assert r.status_code == 200, r.text
    rows = _history(acme, newbie["id"])
    assert len(rows) == 1 and (rows[0]["branch"], rows[0]["pay_group_id"], rows[0]["cost_center_id"]) == (
        "Dubai", gulf_group["id"], None)
    audit = _sql("SELECT details FROM audit_logs WHERE company_id = :c AND action = 'PLACEMENT_CORRECTED'",
                 c=acme.company_id)
    assert audit[0][0]["before"]["branch_id"] != audit[0][0]["after"]["branch_id"]
    assert audit[0][0]["after"]["pay_group_id"] == gulf_group["id"]


def test_shift_edited_on_the_employee_form_is_kept_by_the_next_placement(acme):
    night = _post(acme, "/api/v1/shifts", {"name": "Night", "start_time": "22:00", "end_time": "06:00"})
    employee = create_employee(acme, "Shifter")
    assert acme.put(f"/api/v1/employees/{employee['id']}", json={"shift_id": night["id"]}).status_code == 200
    main = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Main")
    department = acme.post("/api/v1/departments", json={"name": "Ops", "is_active": True}).json()
    row = _move(acme, employee["id"], branch_id=main["id"], department_id=department["id"],
                effective_from=date.today().isoformat())[0]
    assert row["shift_id"] == night["id"]
    assert acme.get(f"/api/v1/employees/{employee['id']}").json()["shift_id"] == night["id"]


# ---------------------------------------------------------------------------
# Termination (D-4) and rehire (EC-ORG-22)
# ---------------------------------------------------------------------------
def test_d4_termination_closes_the_active_placement_and_removes_future_ones(app, acme, gulf):
    """EC-ORG-22: rehire opens a new placement, same code."""
    employee = create_employee(acme, "Leaver")
    future = (date.today() + timedelta(days=40)).isoformat()
    _move(acme, employee["id"], branch_id=gulf["dubai"]["id"], effective_from=future)
    last_day = date.today() - timedelta(days=3)
    r = acme.post(f"/api/v1/employees/{employee['id']}/termination", json={"last_working_day": last_day.isoformat(),
                                                                           "note": "Resigned"})
    assert r.status_code == 200, r.text
    assert r.json() == {"employee_id": employee["id"], "is_active": False, "termination_date": last_day.isoformat()}
    history = _history(acme, employee["id"])
    assert [(h["reason"], h["effective_to"]) for h in history] == [("HIRE", last_day.isoformat())]
    assert not any(h["is_current"] or h["is_future"] for h in history)
    assert acme.get(f"/api/v1/employees/{employee['id']}").json()["termination_date"] == last_day.isoformat()
    details = _sql("SELECT details FROM audit_logs WHERE company_id = :c AND action = 'DEACTIVATE'", c=acme.company_id)
    assert details[0][0]["last_working_day"] == last_day.isoformat() and len(details[0][0]["removed_assignment_ids"]) == 1
    # EC-ORG-22: rehire opens a new placement instead of reusing a leftover future row.
    assert acme.post(f"/api/v1/employees/{employee['id']}/reactivate").status_code == 200
    history = _history(acme, employee["id"])
    assert [h["reason"] for h in history] == ["REHIRE", "HIRE"]
    assert history[0]["pay_group_code"] == "MONTHLY" and history[0]["employment_type_code"] == "PERMANENT"
    assert acme.get(f"/api/v1/employees/{employee['id']}").json()["employee_code"] == employee["employee_code"]


def test_termination_date_rules_permissions_isolation_and_idempotency(app, acme):
    other = signup_company(app, "Other Co")
    employee = create_employee(acme, "Quitter")
    url = f"/api/v1/employees/{employee['id']}/termination"
    tomorrow = (date.today() + timedelta(days=2)).isoformat()
    r = acme.post(url, json={"last_working_day": tomorrow})
    assert r.status_code == 400 and r.json()["code"] == "TERMINATION_DATE_FUTURE"
    r = acme.post(url, json={"last_working_day": "2023-12-31"})  # hired 2024-01-15
    assert r.status_code == 400 and r.json()["code"] == "TERMINATION_DATE_INVALID"
    assert acme.post(url, json={"last_working_day": "yesterday"}).status_code == 422
    assert acme.post(url, json={"last_working_day": "2024-02-01", "is_active": True}).status_code == 422
    assert other.post(url, json={"last_working_day": "2024-02-01"}).status_code == 404  # another tenant's employee
    staff = invite_and_activate(app, acme, create_employee(acme, "Staff"), ["employee"])
    assert staff.post(url, json={"last_working_day": "2024-02-01"}).status_code == 403
    first = acme.post(url, json={"last_working_day": "2024-02-01"}, headers={"Idempotency-Key": "end-1"})
    again = acme.post(url, json={"last_working_day": "2024-02-01"}, headers={"Idempotency-Key": "end-1"})
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert again.headers.get("Idempotent-Replayed") == "true"
    # The legacy button ends employment today in the entity's timezone.
    other_employee = create_employee(acme, "Button")
    assert acme.post(f"/api/v1/employees/{other_employee['id']}/deactivate").status_code == 200
    ended = acme.get(f"/api/v1/employees/{other_employee['id']}").json()["termination_date"]
    assert abs((date.fromisoformat(ended) - date.today()).days) <= 1


def test_last_payroll_approver_cannot_be_deactivated_or_terminated(app):
    founder = signup_company(app, "Approver Co")  # holds payroll_manager: creates runs, cannot approve
    approver_emp = create_employee(founder, "Approver")
    approver = invite_and_activate(app, founder, approver_emp, ["payroll_approver"])
    r = founder.put(f"/api/v1/users/{approver.user_id}/status", json={"status": "deactivated"})
    assert r.status_code == 409 and r.json()["code"] == "LAST_PAYROLL_APPROVER"
    r = founder.put(f"/api/v1/users/{approver.user_id}/status", json={"status": "suspended"})
    assert r.status_code == 409 and r.json()["code"] == "LAST_PAYROLL_APPROVER"
    r = founder.post(f"/api/v1/employees/{approver_emp['id']}/deactivate")
    assert r.status_code == 409 and r.json()["code"] == "LAST_PAYROLL_APPROVER"
    r = founder.post(f"/api/v1/employees/{approver_emp['id']}/termination", json={"last_working_day": "2024-06-30"})
    assert r.status_code == 409 and r.json()["code"] == "LAST_PAYROLL_APPROVER"
    assert founder.get(f"/api/v1/employees/{approver_emp['id']}").json()["is_active"] is True
    invite_and_activate(app, founder, create_employee(founder, "Approver2"), ["payroll_approver"])
    assert founder.put(f"/api/v1/users/{approver.user_id}/status", json={"status": "suspended"}).status_code == 200


# ---------------------------------------------------------------------------
# Migration 0013 data fix (D-4 for rows written by the previous release)
# ---------------------------------------------------------------------------
def _migration():
    spec = importlib.util.spec_from_file_location("m0013", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0013_repairs_placements_of_people_terminated_by_the_old_release(app, acme, gulf):
    employee = create_employee(acme, "OldLeaver")
    keeper = create_employee(acme, "Stayer")
    future = (date.today() + timedelta(days=30)).isoformat()
    for person in (employee, keeper):
        _move(acme, person["id"], branch_id=gulf["dubai"]["id"], effective_from=future)
    ended = date.today() - timedelta(days=10)
    # What the previous release did: deactivate and close only the active row, leaving the future one.
    _sql("UPDATE employees SET is_active = false, termination_date = :d WHERE id = :e", d=ended, e=employee["id"])
    _sql("UPDATE employee_assignments SET effective_to = :d WHERE employee_id = :e AND reason = 'HIRE'",
         d=ended, e=employee["id"])
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        assert _migration().close_terminated_placements(conn) == {"removed": 1, "closed": 0}
    assert [(h["reason"], h["effective_to"]) for h in _history(acme, employee["id"])] == [("HIRE", ended.isoformat())]
    assert [h["reason"] for h in _history(acme, keeper["id"])] == ["TRANSFER", "HIRE"]  # active people untouched
    # An open row past the termination date is closed on it.
    _sql("UPDATE employee_assignments SET effective_to = NULL WHERE employee_id = :e", e=employee["id"])
    with system_engine.begin() as conn:
        assert _migration().close_terminated_placements(conn) == {"removed": 0, "closed": 1}
        assert _migration().close_terminated_placements(conn) == {"removed": 0, "closed": 0}  # idempotent
