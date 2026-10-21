"""Increment cycles: population, methods, preview with totals, approval, apply (3.3 §8.2, 3.8 §5.7).

EC-CMP-11 (applied increments are scheduled), EC-CMP-23 (grade position in the preview).
"""

from datetime import timedelta

import pytest

from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.payroll.test_compensation import (
    COMPENSATION,
    assign_and_approve,
    month_start,
    next_month,
    people,
)
from tests.payroll.test_components import PAY, STRUCTURES, active_version_id, make_structure, ok, setup_catalogue
from tests.payroll.test_settings_engine import sql

CYCLES = f"{PAY}/revision-cycles"


def create_cycle(client, method, effective_from, **extra):
    return client.post(CYCLES, json={"name": extra.pop("name", "FY27 increments"), "method": method,
                                     "effective_from": effective_from.isoformat(), **extra})


def items_by_employee(cycle):
    return {item["employee"]["id"]: item for item in cycle["items"]}


@pytest.fixture
def world(app):
    admin = signup_company(app, "Acme Corp")
    setup_catalogue(admin)
    team = people(app, admin)
    structure = make_structure(admin)
    version_id = active_version_id(structure)
    start = month_start(admin.company_id) - timedelta(days=60)
    current = assign_and_approve(admin, team["approver"], team["worker"]["id"], version_id, "1200000",
                                 start.replace(day=1))
    return {**team, "admin": admin, "structure": structure, "version_id": version_id, "current": current}


def test_percent_cycle_preview_submit_approve_apply(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    effective = next_month(admin.company_id)
    cycle = ok(create_cycle(admin, {"type": "PERCENT", "percent": "10"}, effective), 201)
    assert cycle["status"] == "DRAFT" and cycle["items"] == []
    previewed = ok(admin.post(f"{CYCLES}/{cycle['id']}/preview"))
    assert previewed["status"] == "PREVIEWED"
    items = items_by_employee(previewed)
    proposal = items[worker["id"]]
    assert proposal["status"] == "PROPOSED" and proposal["old_assignment_id"] == world["current"]["id"]
    assert proposal["proposed"]["old_pay_basis_amount"] == "1200000.00"
    assert proposal["proposed"]["pay_basis_amount"] == "1320000.00" and proposal["proposed"]["change_pct"] == "10.00"
    assert proposal["proposed"]["grade_range"] == "NO_RANGE"
    assert items[world["boss"]["id"]]["status"] == "EXCLUDED"  # no salary in force on the effective date
    assert previewed["totals"] == {"INR": {"count": 1, "old": "1200000.00", "new": "1320000.00",
                                           "increase": "120000.00", "change_pct": "10.00"}}
    submitted = ok(admin.post(f"{CYCLES}/{cycle['id']}/submit"))
    assert submitted["status"] == "PENDING_APPROVAL" and submitted["approval_request_id"]
    r = admin.put(f"{CYCLES}/{cycle['id']}", json={"name": "Changed"})
    assert r.status_code == 409 and r.json()["code"] == "CYCLE_LOCKED"
    assert admin.post(f"{CYCLES}/{cycle['id']}/approve", json={}).status_code == 403  # the maker cannot approve
    assert admin.post(f"{CYCLES}/{cycle['id']}/apply").json()["code"] == "CYCLE_NOT_APPROVED"
    approved = ok(approver.post(f"{CYCLES}/{cycle['id']}/approve", json={}))
    assert approved["status"] == "APPROVED"
    applied = ok(admin.post(f"{CYCLES}/{cycle['id']}/apply"))
    assert applied["status"] == "APPLIED" and applied["applied_by_user_id"]
    item = items_by_employee(applied)[worker["id"]]
    assert item["status"] == "APPLIED" and item["new_assignment_id"]
    new = ok(admin.get(f"{COMPENSATION}/{item['new_assignment_id']}"))
    assert new["status"] == "SCHEDULED" and new["reason"] == "REVISION" and new["revision_cycle_id"] == cycle["id"]
    assert new["pay_basis_amount"] == {"amount": "1320000.00", "currency": "INR"}
    assert new["approved_by_user_id"] == approver.user_id and new["created_by_user_id"] != approver.user_id
    old = ok(admin.get(f"{COMPENSATION}/{world['current']['id']}"))
    assert old["effective_to"] == (effective - timedelta(days=1)).isoformat()
    assert admin.post(f"{CYCLES}/{cycle['id']}/apply").json()["code"] == "CYCLE_NOT_APPROVED"
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = "
                                 "'revision_cycle' ORDER BY id", c=admin.company_id)]
    assert actions == ["REVISION_CYCLE_CREATED", "REVISION_CYCLE_PREVIEWED", "REVISION_CYCLE_SUBMITTED",
                       "REVISION_CYCLE_APPROVED", "REVISION_CYCLE_APPLIED"]


def test_nobody_approves_a_cycle_that_raises_their_own_salary(app, world):
    admin, approver = world["admin"], world["approver"]
    second = _second_approver(app, world)
    assign_and_approve(admin, second, approver.employee["id"], world["version_id"], "900000",
                       (month_start(admin.company_id) - timedelta(days=30)).replace(day=1))
    cycle = ok(create_cycle(admin, {"type": "PERCENT", "percent": "8"}, next_month(admin.company_id)), 201)
    ok(admin.post(f"{CYCLES}/{cycle['id']}/preview"))
    ok(admin.post(f"{CYCLES}/{cycle['id']}/submit"))
    r = approver.post(f"{CYCLES}/{cycle['id']}/approve", json={})
    assert r.status_code == 403 and "own salary" in r.json()["detail"]
    assert ok(admin.get(f"{CYCLES}/{cycle['id']}"))["status"] == "PENDING_APPROVAL"  # nothing was decided
    assert ok(second.post(f"{CYCLES}/{cycle['id']}/approve", json={}))["status"] == "APPROVED"


def _second_approver(app, world):
    from app.core.rate_limit import limiter

    limiter.reset()
    world["second"] = invite_and_activate(app, world["admin"], create_employee(world["admin"], "Sam", "Second"),
                                          ["payroll_approver"])
    return world["second"]


def test_reject_and_reopen_rules(world):
    admin, approver = world["admin"], world["approver"]
    cycle = ok(create_cycle(admin, {"type": "PERCENT", "percent": "5"}, next_month(admin.company_id)), 201)
    r = admin.post(f"{CYCLES}/{cycle['id']}/submit")
    assert r.status_code == 409 and r.json()["code"] == "CYCLE_NOT_PREVIEWED"
    previewed = ok(admin.post(f"{CYCLES}/{cycle['id']}/preview"))
    updated = ok(admin.put(f"{CYCLES}/{cycle['id']}", json={"row_version": previewed["row_version"],
                                                            "method": {"type": "PERCENT", "percent": "6"}}))
    assert updated["status"] == "DRAFT" and updated["items"] == []  # a change discards the preview
    r = admin.put(f"{CYCLES}/{cycle['id']}", json={"row_version": previewed["row_version"], "name": "Stale"})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"
    ok(admin.post(f"{CYCLES}/{cycle['id']}/preview"))
    ok(admin.post(f"{CYCLES}/{cycle['id']}/submit"))
    assert approver.post(f"{CYCLES}/{cycle['id']}/reject", json={}).status_code == 400  # a reason is required
    rejected = ok(approver.post(f"{CYCLES}/{cycle['id']}/reject", json={"remarks": "Budget"}))
    assert rejected["status"] == "REJECTED"
    assert admin.post(f"{CYCLES}/{cycle['id']}/apply").json()["code"] == "CYCLE_NOT_APPROVED"
    assert admin.post(f"{CYCLES}/{cycle['id']}/preview").json()["code"] == "CYCLE_LOCKED"


def test_fixed_amount_per_row_and_matrix_methods(world):
    admin, worker = world["admin"], world["worker"]
    cid = admin.company_id
    effective = next_month(cid)
    cycle = ok(create_cycle(admin, {"type": "FIXED_AMOUNT", "amounts": {"INR": "60000"}}, effective), 201)
    proposal = items_by_employee(ok(admin.post(f"{CYCLES}/{cycle['id']}/preview")))[worker["id"]]["proposed"]
    assert proposal["pay_basis_amount"] == "1260000.00" and proposal["change_pct"] == "5.00"

    def remethod(method):
        current = ok(admin.get(f"{CYCLES}/{cycle['id']}"))
        ok(admin.put(f"{CYCLES}/{cycle['id']}", json={"row_version": current["row_version"], "method": method}))
        return items_by_employee(ok(admin.post(f"{CYCLES}/{cycle['id']}/preview")))

    items = remethod({"type": "FIXED_AMOUNT", "amounts": {"AED": "100"}})
    assert items[worker["id"]]["status"] == "EXCLUDED" and "INR" in items[worker["id"]]["note"]
    items = remethod({"type": "PER_ROW", "rows": [{"employee_code": worker["employee_code"],
                                                   "pay_basis_amount": "1500000"}]})
    assert items[worker["id"]]["proposed"]["pay_basis_amount"] == "1500000.00"
    sql("INSERT INTO grades (company_id, code, name, salary_ranges, status, created_at, updated_at) VALUES "
        "(:c, 'G3', 'Grade 3', '[{\"currency\": \"INR\", \"min\": \"1000000.00\", \"max\": \"1400000.00\"}]'::jsonb, "
        "'ACTIVE', now(), now())", c=cid)
    grade = sql("SELECT id FROM grades WHERE company_id = :c AND code = 'G3'", c=cid)[0][0]
    sql("UPDATE employee_assignments SET grade_id = :g WHERE company_id = :c AND employee_id = :e", g=grade, c=cid,
        e=worker["id"])
    items = remethod({"type": "MATRIX", "matrix": {"G3": {"A": "12", "B": "6"}},
                      "ratings": {str(worker["id"]): "A"}, "default_percent": "2"})
    proposed = items[worker["id"]]["proposed"]
    assert proposed["pay_basis_amount"] == "1344000.00" and proposed["grade_range"] == "IN_RANGE"
    assert proposed["position_pct"] == "86.0"
    items = remethod({"type": "MATRIX", "matrix": {"G3": {"A": "12"}}, "ratings": {}, "default_percent": "2"})
    assert items[worker["id"]]["proposed"]["pay_basis_amount"] == "1224000.00"  # unrated: the default percentage


def test_population_filters_and_structure_move(world):
    admin, worker = world["admin"], world["worker"]
    effective = next_month(admin.company_id)
    other = make_structure(admin, code="STAFF_V2")
    cycle = ok(create_cycle(admin, {"type": "PERCENT", "percent": "3",
                                    "structure_version_id": active_version_id(other)}, effective,
                            population={"employee_ids": [worker["id"]]}), 201)
    previewed = ok(admin.post(f"{CYCLES}/{cycle['id']}/preview"))
    assert set(items_by_employee(previewed)) == {worker["id"]}
    proposal = items_by_employee(previewed)[worker["id"]]["proposed"]
    assert proposal["template_version_id"] == active_version_id(other)
    later = ok(create_cycle(admin, {"type": "PERCENT", "percent": "3"}, effective,
                            population={"hire_date_after": "2030-01-01"}), 201)
    assert ok(admin.post(f"{CYCLES}/{later['id']}/preview"))["items"] == []


def test_apply_records_items_that_no_longer_validate(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    cycle = ok(create_cycle(admin, {"type": "PERCENT", "percent": "10"}, next_month(admin.company_id)), 201)
    ok(admin.post(f"{CYCLES}/{cycle['id']}/preview"))
    ok(admin.post(f"{CYCLES}/{cycle['id']}/submit"))
    ok(approver.post(f"{CYCLES}/{cycle['id']}/approve", json={}))
    ok(admin.post(f"{STRUCTURES}/{world['structure']['id']}/archive"))
    applied = ok(admin.post(f"{CYCLES}/{cycle['id']}/apply"))
    item = items_by_employee(applied)[worker["id"]]
    assert applied["status"] == "APPLIED" and item["status"] == "FAILED" and "STRUCTURE_NOT_ACTIVE" in item["note"]
    assert ok(admin.get(f"{COMPENSATION}/{world['current']['id']}"))["effective_to"] is None  # nothing changed


def test_cycle_validation_rejects_bad_methods_and_filters(world):
    admin = world["admin"]
    effective = next_month(admin.company_id)
    assert create_cycle(admin, {"type": "MAGIC"}, effective).json()["code"] == "INVALID_METHOD"
    assert create_cycle(admin, {"type": "PERCENT", "percent": 10}, effective).json()["code"] == "INVALID_DECIMAL"
    assert create_cycle(admin, {"type": "PERCENT", "percent": "1", "extra": 1}, effective).json()["code"] == \
        "INVALID_METHOD"
    r = create_cycle(admin, {"type": "PERCENT", "percent": "1"}, effective, population={"salary_above": 5})
    assert r.json()["code"] == "INVALID_POPULATION"
    r = create_cycle(admin, {"type": "PERCENT", "percent": "1", "structure_version_id": 999999}, effective)
    assert r.json()["code"] == "STRUCTURE_NOT_ACTIVE"
    assert create_cycle(admin, {"type": "PER_ROW", "rows": []}, effective).json()["code"] == "INVALID_METHOD"
    r = admin.post(CYCLES, json={"name": "x", "method": {"type": "PERCENT", "percent": "1"},
                                 "effective_from": effective.isoformat(), "status": "APPROVED"})
    assert r.status_code == 422


def test_authorization_and_isolation_of_cycles(app, world):
    admin, approver = world["admin"], world["approver"]
    cycle = ok(create_cycle(admin, {"type": "PERCENT", "percent": "4"}, next_month(admin.company_id)), 201)
    employee = world["employee_client"]
    assert employee.get(CYCLES).status_code == 403
    assert create_cycle(employee, {"type": "PERCENT", "percent": "4"}, next_month(admin.company_id)).status_code == 403
    assert [c["id"] for c in ok(approver.get(CYCLES))] == [cycle["id"]]  # approvers see cycles to decide them
    assert create_cycle(approver, {"type": "PERCENT", "percent": "4"}, next_month(admin.company_id)).status_code == 403
    assert approver.post(f"{CYCLES}/{cycle['id']}/preview").status_code == 403
    other = signup_company(app, "Beta Industries")
    assert other.get(f"{CYCLES}/{cycle['id']}").status_code == 404
    assert other.post(f"{CYCLES}/{cycle['id']}/preview").status_code == 404
    assert other.put(f"{CYCLES}/{cycle['id']}", json={"name": "x"}).status_code == 404
    assert ok(other.get(CYCLES)) == []
