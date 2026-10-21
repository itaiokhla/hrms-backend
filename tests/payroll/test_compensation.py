"""Compensation assignments: assign, revise, approve, schedule, visibility, grade range, bulk CSV (3.3 §7-§8.1).

EC-CMP-04, EC-CMP-07, EC-CMP-10, EC-CMP-11, EC-CMP-12, EC-CMP-13, EC-CMP-14, EC-CMP-16, EC-CMP-23.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.payroll.test_components import (
    EXAMPLE_A,
    PAY,
    SALARY_TAGS,
    active_version_id,
    add_component,
    make_structure,
    ok,
    setup_catalogue,
)
from tests.payroll.test_settings_engine import change, put, session, sql, today_of, token

COMPENSATION = f"{PAY}/compensation"
GRADE_SETTING = "payroll.compensation.enforce_grade_range"


# --------------------------------------------------------------------------- shared helpers
def employee_url(employee_id):
    return f"{PAY}/employees/{employee_id}/compensation"


def login_as(app, admin, employee, roles):
    """A signed-in user of the admin's company for `employee` with `roles`."""
    from app.core.rate_limit import limiter

    limiter.reset()
    client = invite_and_activate(app, admin, employee, roles)
    client.company_id = admin.company_id
    return client


def people(app, admin):
    """An approver (payroll_approver), a worker with a self-service login and the worker's manager."""
    approver = login_as(app, admin, create_employee(admin, "Ava", "Approver"), ["payroll_approver"])
    boss = create_employee(admin, "Mona", "Manager")
    worker = create_employee(admin, "Wendy", "Worker", manager_id=boss["id"])
    employee_client = login_as(app, admin, worker, ["employee"])
    manager_client = login_as(app, admin, boss, ["manager"])
    return {"approver": approver, "worker": worker, "boss": boss, "employee_client": employee_client,
            "manager_client": manager_client}


def month_start(company_id):
    return today_of(company_id).replace(day=1)


def next_month(company_id):
    start = month_start(company_id)
    return (start + timedelta(days=32)).replace(day=1)


def assign(admin, employee_id, version_id, amount, effective_from=None, currency="INR", **extra):
    body = {"template_version_id": version_id, "pay_basis_amount": {"amount": amount, "currency": currency},
            "effective_from": (effective_from or month_start(admin.company_id)).isoformat(),
            "reason": extra.pop("reason", "HIRE"), **extra}
    return admin.post(employee_url(employee_id), json=body)


def approve(approver, assignment_id):
    return approver.post(f"{COMPENSATION}/{assignment_id}/approve", json={})


def assign_and_approve(admin, approver, employee_id, version_id, amount, effective_from=None, **extra):
    proposed = ok(assign(admin, employee_id, version_id, amount, effective_from, **extra), 201)
    assert proposed["status"] == "PENDING_APPROVAL", proposed
    return ok(approve(approver, proposed["id"]))


def base_salary(employee_id):
    return sql("SELECT base_salary FROM employees WHERE id = :e", e=employee_id)[0][0]


@pytest.fixture
def acme(app):
    client = signup_company(app, "Acme Corp")
    setup_catalogue(client)
    return client


@pytest.fixture
def world(app, acme):
    team = people(app, acme)
    structure = make_structure(acme)
    return {**team, "admin": acme, "structure": structure, "version_id": active_version_id(structure)}


# --------------------------------------------------------------------------- assign and approve
def test_assign_needs_approval_then_activates_and_syncs_base_salary(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    proposed = ok(assign(admin, worker["id"], world["version_id"], "1200000.00", note="Offer letter"), 201)
    assert proposed["status"] == "PENDING_APPROVAL" and proposed["pay_basis_amount"] == {"amount": "1200000.00",
                                                                                        "currency": "INR"}
    assert proposed["reason"] == "HIRE" and proposed["note"] == "Offer letter"
    listed = ok(admin.get(COMPENSATION, params={"filter": "pending"}))
    assert [i["compensation"]["id"] for i in listed["items"]] == [proposed["id"]] and listed["counts"]["pending"] == 1
    # the maker has no approve permission; the approver approves
    assert admin.post(f"{COMPENSATION}/{proposed['id']}/approve", json={}).status_code == 403
    approved = ok(approve(world["approver"], proposed["id"]))
    assert approved["status"] == "ACTIVE" and approved["approved_by_user_id"] == approver.user_id
    assert base_salary(worker["id"]) == Decimal("50000.00")  # BASIC = 50% of the monthly CTC (backward compatibility)
    detail = ok(admin.get(employee_url(worker["id"])))
    assert detail["current"]["id"] == proposed["id"] and detail["without_salary"] is False
    lines = {line["component"]: line for line in detail["breakdown"]["lines"]}
    assert lines["BASIC"]["monthly"]["amount"] == "50000.00" and lines["HRA"]["monthly"]["amount"] == "20000.00"
    assert lines["FIXED_ALLOWANCE"]["annual"]["amount"] == "360000.00"
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = "
                                 "'compensation_assignment' ORDER BY id", c=admin.company_id)]
    assert actions == ["COMPENSATION_DRAFTED", "COMPENSATION_SUBMITTED", "COMPENSATION_APPROVED"]
    assert approve(approver, proposed["id"]).json()["code"] == "NOT_PENDING"


def test_maker_cannot_approve_and_nobody_approves_their_own_salary(app, world):
    admin = world["admin"]
    both = login_as(app, admin, create_employee(admin, "Bo", "Both"), ["payroll_processor", "payroll_approver"])
    proposed = ok(assign(both, world["worker"]["id"], world["version_id"], "900000"), 201)
    r = both.post(f"{COMPENSATION}/{proposed['id']}/approve", json={})
    assert r.status_code == 403  # the maker cannot approve their own change
    own = ok(assign(admin, world["approver"].employee["id"], world["version_id"], "800000"), 201)
    r = approve(world["approver"], own["id"])
    assert r.status_code == 403  # nobody approves their own salary
    assert ok(both.post(f"{COMPENSATION}/{own['id']}/approve", json={}))["status"] == "ACTIVE"
    r = world["approver"].post(f"{COMPENSATION}/{proposed['id']}/reject", json={})
    assert r.status_code == 400 and r.json()["code"] == "REMARKS_REQUIRED"
    rejected = ok(world["approver"].post(f"{COMPENSATION}/{proposed['id']}/reject", json={"remarks": "Too high"}))
    assert rejected["status"] == "REJECTED"
    r = world["approver"].post(f"{COMPENSATION}/{proposed['id']}/reject", json={"remarks": "again"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_PENDING"


def test_draft_then_submit_and_pending_duplicate_is_409(world):
    admin, worker = world["admin"], world["worker"]
    draft = ok(assign(admin, worker["id"], world["version_id"], "1000000", submit=False), 201)
    assert draft["status"] == "DRAFT"
    assert ok(admin.get(COMPENSATION, params={"filter": "draft"}))["counts"]["draft"] == 1
    submitted = ok(admin.post(f"{COMPENSATION}/{draft['id']}/submit"))
    assert submitted["status"] == "PENDING_APPROVAL" and submitted["approval_request_id"]
    assert admin.post(f"{COMPENSATION}/{draft['id']}/submit").json()["code"] == "NOT_DRAFT"
    r = assign(admin, worker["id"], world["version_id"], "1100000")
    assert r.status_code == 409 and r.json()["code"] == "COMPENSATION_PENDING"


# --------------------------------------------------------------------------- dates
def test_ec_cmp_11_future_dated_revision_is_scheduled_and_earlier_periods_unaffected(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    cid = admin.company_id
    first = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000")
    start = next_month(cid)
    second = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1500000", start,
                                reason="REVISION")
    assert second["status"] == "SCHEDULED" and second["previous_assignment_id"] == first["id"]
    old = ok(admin.get(f"{COMPENSATION}/{first['id']}"))
    assert old["status"] == "ACTIVE" and old["effective_to"] == (start - timedelta(days=1)).isoformat()
    detail = ok(admin.get(employee_url(worker["id"])))
    assert detail["current"]["id"] == first["id"] and [s["id"] for s in detail["scheduled"]] == [second["id"]]
    assert base_salary(worker["id"]) == Decimal("50000.00")  # the scheduled change does not apply yet
    history = {h["id"]: h for h in detail["history"]}
    assert history[second["id"]]["change"]["change_pct"] == "25.00"
    counts = ok(admin.get(COMPENSATION, params={"filter": "scheduled"}))
    assert counts["counts"]["scheduled"] == 1 and counts["items"][0]["compensation"]["id"] == second["id"]


def test_scheduled_assignment_activates_when_its_date_comes(world):
    from app.payroll.services import compensation as comp_service

    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    cid = admin.company_id
    assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000")
    second = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1800000", next_month(cid),
                                reason="PROMOTION")
    sql("UPDATE compensation_assignments SET effective_to = :d WHERE id = :i AND company_id = :c",
        d=today_of(cid) - timedelta(days=1), i=second["previous_assignment_id"], c=cid)
    sql("UPDATE compensation_assignments SET effective_from = :d WHERE id = :i AND company_id = :c",
        d=today_of(cid), i=second["id"], c=cid)
    db = session(cid)
    try:
        assert comp_service.activate_due(db, cid) == 1
        db.commit()
    finally:
        db.close()
    assert sql("SELECT status FROM compensation_assignments WHERE id = :i", i=second["id"])[0][0] == "ACTIVE"
    assert base_salary(worker["id"]) == Decimal("75000.00")


def test_revision_closes_the_previous_assignment_the_day_before(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    cid = admin.company_id
    start = month_start(cid) - timedelta(days=62)
    first = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000", start)
    revision_day = month_start(cid)
    second = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1320000", revision_day,
                                reason="REVISION")
    assert second["status"] == "ACTIVE" and second["previous_assignment_id"] == first["id"]
    assert ok(admin.get(f"{COMPENSATION}/{first['id']}"))["effective_to"] == (
        revision_day - timedelta(days=1)).isoformat()
    assert base_salary(worker["id"]) == Decimal("55000.00")
    # a correction starting on the same day replaces it (SUPERSEDED)
    third = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1440000", revision_day,
                               reason="CORRECTION")
    assert ok(admin.get(f"{COMPENSATION}/{second['id']}"))["status"] == "SUPERSEDED"
    assert third["status"] == "ACTIVE"


def test_ec_cmp_16_mid_period_revision_is_recorded_for_a_split_period(world):
    from app.payroll.services import compensation as comp_service

    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    cid = admin.company_id
    start = month_start(cid) - timedelta(days=40)
    assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000", start.replace(day=1))
    middle = month_start(cid).replace(day=16)
    revised = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1440000", middle,
                                 reason="REVISION")
    assert revised["split_period"] is True
    period_start = month_start(cid)
    period_end = next_month(cid) - timedelta(days=1)
    db = session(cid)
    try:
        segments = comp_service.segments(db, cid, worker["id"], period_start, period_end)
    finally:
        db.close()
    assert [(s["start"], s["end"]) for s in segments] == [(period_start, middle - timedelta(days=1)),
                                                          (middle, period_end)]
    assert Decimal(segments[0]["compensation"]["pay_basis_amount"]) == Decimal("1200000")
    assert Decimal(segments[1]["compensation"]["pay_basis_amount"]) == Decimal("1440000")
    total_days = sum((s["end"] - s["start"]).days + 1 for s in segments)
    assert total_days == (period_end - period_start).days + 1  # segments add up to one full period


def test_ec_cmp_12_ec_cmp_13_backdated_revision_calls_the_arrears_and_stale_hooks(world, monkeypatch):
    from app.payroll.services import compensation as comp_service

    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    cid = admin.company_id
    calls = {}
    monkeypatch.setattr(comp_service, "finalized_periods",
                        lambda db, company_id, employee_id, start: [{"run_id": 7, "start": start, "end": start}])

    def proposals(db, principal, row, periods):
        calls["arrears"] = (row.id, len(periods))
        return [{"period": "P"}]

    def stale(db, company_id, employee_id, start):
        calls["stale"] = employee_id
        return 1

    monkeypatch.setattr(comp_service, "arrears_proposals", proposals)
    monkeypatch.setattr(comp_service, "mark_runs_stale", stale)
    start = month_start(cid) - timedelta(days=95)
    approved = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000", start.replace(day=1))
    assert calls == {"arrears": (approved["id"], 1), "stale": worker["id"]}
    details = sql("SELECT details FROM audit_logs WHERE company_id = :c AND action = 'COMPENSATION_APPROVED'",
                  c=cid)[0][0]
    assert details["backdated"] is True and details["arrears_proposals"] == 1 and details["stale_runs"] == 1


def test_arrears_hook_is_a_documented_no_op_until_runs_exist(world):
    from app.payroll.services import compensation as comp_service

    assert comp_service.finalized_periods(None, 1, 1, month_start(world["admin"].company_id)) == []
    assert comp_service.arrears_proposals(None, None, None, []) == []
    assert comp_service.mark_runs_stale(None, 1, 1, month_start(world["admin"].company_id)) == 0


def test_ec_cmp_10_overlapping_in_force_assignments_are_refused_by_the_database(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    first = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000")
    with pytest.raises(Exception, match="cannot overlap"):
        sql("INSERT INTO compensation_assignments (company_id, employee_id, assignment_id, template_version_id, "
            "pay_basis_amount, currency, effective_from, reason, status, created_at, updated_at) "
            "SELECT company_id, employee_id, assignment_id, template_version_id, 1, currency, effective_from, "
            "'CORRECTION', 'ACTIVE', now(), now() FROM compensation_assignments WHERE id = :i", i=first["id"])
    # a DRAFT row may overlap (it is not in force)
    sql("INSERT INTO compensation_assignments (company_id, employee_id, assignment_id, template_version_id, "
        "pay_basis_amount, currency, effective_from, reason, status, created_at, updated_at) "
        "SELECT company_id, employee_id, assignment_id, template_version_id, 1, currency, effective_from, "
        "'CORRECTION', 'DRAFT', now(), now() FROM compensation_assignments WHERE id = :i", i=first["id"])
    with pytest.raises(Exception, match="placement does not belong"):
        sql("UPDATE compensation_assignments SET employee_id = :e WHERE id = :i",
            e=world["boss"]["id"], i=first["id"])


def test_ec_cmp_10_service_maps_the_overlap_trigger_to_409(world, monkeypatch):
    """The API answers 409 when the trigger refuses an overlap the service did not resolve."""
    from app.core.errors import AppError
    from app.models.compensation import CompensationAssignment
    from app.payroll.services import compensation as comp_service

    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    cid = admin.company_id
    first = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000")
    db = session(cid)
    try:
        row = db.get(CompensationAssignment, first["id"])
        clone = CompensationAssignment(
            company_id=cid, employee_id=row.employee_id, assignment_id=row.assignment_id,
            template_version_id=row.template_version_id, pay_basis_amount=1, currency=row.currency,
            effective_from=row.effective_from, reason="CORRECTION", status="DRAFT", row_version=1)
        db.add(clone)
        db.flush()
        clone.status = "ACTIVE"
        with pytest.raises(AppError) as error:
            comp_service._flush(db)
        assert error.value.status_code == 409 and error.value.code == "COMPENSATION_OVERLAP"
    finally:
        db.rollback()
        db.close()


# --------------------------------------------------------------------------- validation
def test_ec_cmp_04_negative_balancing_is_rejected_with_the_minimum_ctc(acme, world):
    lines = [{"component": "BASIC", "method": "FIXED", "value": "20000"},
             {"component": "HRA", "method": "PERCENT_OF", "value": "40", "params": {"bases": ["BASIC"]}},
             {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}]
    structure = make_structure(acme, code="FLOORED", lines=lines, test_range={"min": "400000", "max": "900000"})
    r = assign(acme, world["worker"]["id"], active_version_id(structure), "300000")
    assert r.status_code == 400 and r.json()["code"] == "BALANCING_NEGATIVE"
    minimum = Decimal(r.json()["detail"]["minimum_pay_basis"]["amount"])
    assert Decimal("335999") <= minimum <= Decimal("336000")  # (20,000 + 8,000) x 12, to the minor unit
    assert assign(acme, world["worker"]["id"], active_version_id(structure), "336000").status_code == 201


def test_overrides_only_on_overridable_lines_within_bounds(acme, world):
    lines = [*EXAMPLE_A, {"component": "SPECIAL", "method": "FIXED", "value": "1000", "overridable": True,
                          "override_min": "500", "override_max": "5000"}]
    ok(add_component(acme, "SPECIAL", tags=SALARY_TAGS), 201)
    structure = make_structure(acme, code="WITH_SPECIAL", lines=lines)
    version_id, worker = active_version_id(structure), world["worker"]["id"]
    r = assign(acme, worker, version_id, "1200000", overrides={"BASIC": {"amount": "1"}})
    assert r.status_code == 400 and r.json()["code"] == "NOT_OVERRIDABLE"
    r = assign(acme, worker, version_id, "1200000", overrides={"SPECIAL": {"amount": "9000"}})
    assert r.status_code == 400 and r.json()["code"] == "OVERRIDE_OUT_OF_BOUNDS"
    r = assign(acme, worker, version_id, "1200000", overrides={"SPECIAL": {"percent": "10"}})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_OVERRIDE"
    r = assign(acme, worker, version_id, "1200000", overrides={"SPECIAL": {"amount": 2000}})
    assert r.status_code == 422  # money is a decimal string, never a JSON number
    r = assign(acme, worker, version_id, "1200000", overrides={"NOPE": {"amount": "1"}})
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_OVERRIDE"
    created = ok(assign(acme, worker, version_id, "1200000", overrides={"SPECIAL": {"amount": "2500"}}), 201)
    approved = ok(approve(world["approver"], created["id"]))
    assert ok(acme.get(f"{COMPENSATION}/{approved['id']}"))["overrides"] == {"SPECIAL": {"amount": "2500"}}
    lines = {line["component"]: line for line in ok(acme.get(employee_url(worker)))["breakdown"]["lines"]}
    assert lines["SPECIAL"]["monthly"]["amount"] == "2500.00"
    assert lines["FIXED_ALLOWANCE"]["monthly"]["amount"] == "27500.00"  # balancing absorbs the override


def test_request_validation_rejects_server_fields_bad_reasons_and_unpublished_structures(acme, world):
    worker = world["worker"]["id"]
    r = assign(acme, worker, world["version_id"], "1200000", status="ACTIVE")
    assert r.status_code == 422  # status is server-owned
    r = assign(acme, worker, world["version_id"], "1200000", reason="MIGRATED")
    assert r.status_code == 422  # MIGRATED is reserved for the legacy migration
    r = acme.post(employee_url(worker), json={"template_version_id": world["version_id"],
                                              "pay_basis_amount": {"amount": 1200000, "currency": "INR"},
                                              "effective_from": month_start(acme.company_id).isoformat(),
                                              "reason": "HIRE"})
    assert r.status_code == 422
    draft = make_structure(acme, code="DRAFTY", publish=False)
    r = assign(acme, worker, draft["versions"][0]["id"], "1200000")
    assert r.status_code == 409 and r.json()["code"] == "STRUCTURE_NOT_ACTIVE"
    r = assign(acme, worker, world["version_id"], "1200000", effective_from=month_start(acme.company_id).replace(
        year=2020))
    assert r.status_code == 400 and r.json()["code"] == "NO_PLACEMENT"
    assert assign(acme, 999999, world["version_id"], "1200000").status_code == 404
    assert assign(acme, worker, 999999, "1200000").status_code == 404


def test_ec_cmp_07_contract_currency_needs_an_fx_rule(acme, world):
    worker = world["worker"]["id"]
    r = assign(acme, worker, world["version_id"], "15000", currency="USD")
    assert r.status_code == 400 and r.json()["code"] == "FX_RULE_REQUIRED"
    r = assign(acme, worker, world["version_id"], "15000", currency="USD", fx_rule={"source": "MAGIC"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_FX_RULE"
    r = assign(acme, worker, world["version_id"], "1200000", fx_rule={"source": "PERIOD_SNAPSHOT"})
    assert r.status_code == 400 and r.json()["code"] == "FX_RULE_NOT_NEEDED"
    created = ok(assign(acme, worker, world["version_id"], "15000", currency="USD",
                        fx_rule={"source": "FIXED_RATE", "rate": "83.25"}), 201)
    assert created["currency"] == "USD" and created["fx_rule"] == {"source": "FIXED_RATE", "from": "USD", "to": "INR",
                                                                   "rate": "83.25"}


def test_ec_cmp_23_grade_range_warns_or_blocks_when_enforced(acme, world):
    cid = acme.company_id
    worker = world["worker"]["id"]
    sql("INSERT INTO grades (company_id, code, name, salary_ranges, status, created_at, updated_at) VALUES "
        "(:c, 'G3', 'Grade 3', '[{\"currency\": \"INR\", \"min\": \"600000.00\", \"max\": \"1000000.00\"}]'::jsonb, "
        "'ACTIVE', now(), now())", c=cid)
    grade = sql("SELECT id FROM grades WHERE company_id = :c AND code = 'G3'", c=cid)[0][0]
    sql("UPDATE employee_assignments SET grade_id = :g WHERE company_id = :c AND employee_id = :e", g=grade, c=cid,
        e=worker)
    created = ok(assign(acme, worker, world["version_id"], "1200000", submit=False), 201)
    assert created["grade_range"]["status"] == "ABOVE" and created["grade_range"]["grade"]["code"] == "G3"
    assert [w["code"] for w in created["warnings"]] == ["OUT_OF_GRADE_RANGE"]
    inside = ok(acme.post(f"{employee_url(worker)}/preview", json={
        "pay_basis_amount": {"amount": "800000", "currency": "INR"},
        "effective_from": month_start(cid).isoformat(), "template_version_id": world["version_id"]}))
    assert inside["grade_range"]["status"] == "IN_RANGE" and inside["grade_range"]["position_pct"] == "50.0"
    ok(put(acme, change(GRADE_SETTING, True, row_version=token(acme, GRADE_SETTING))))
    sql("DELETE FROM compensation_assignments WHERE id = :i", i=created["id"])
    r = assign(acme, worker, world["version_id"], "1200000")
    assert r.status_code == 400 and r.json()["code"] == "OUT_OF_GRADE_RANGE"
    assert assign(acme, worker, world["version_id"], "900000").status_code == 201


def test_preview_change_shows_before_and_after(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000")
    preview = ok(admin.post(f"{employee_url(worker['id'])}/preview", json={
        "pay_basis_amount": {"amount": "1320000", "currency": "INR"},
        "effective_from": next_month(admin.company_id).isoformat()}))
    lines = {line["component"]: line for line in preview["lines"]}
    assert lines["BASIC"]["before_monthly"]["amount"] == "50000.00"
    assert lines["BASIC"]["after_monthly"]["amount"] == "55000.00" and lines["BASIC"]["change_pct"] == "10.00"
    assert preview["pay_basis"]["change_pct"] == "10.00" and preview["structure_after"]["code"] == "STAFF_CTC"
    assert world["employee_client"].post(f"{employee_url(worker['id'])}/preview", json={
        "pay_basis_amount": {"amount": "1", "currency": "INR"},
        "effective_from": next_month(admin.company_id).isoformat()}).status_code == 403


# --------------------------------------------------------------------------- lists and visibility
def test_ec_cmp_14_without_salary_list_and_kpi(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    without = ok(admin.get(COMPENSATION, params={"filter": "without"}))
    ids = {item["employee"]["id"] for item in without["items"]}
    assert worker["id"] in ids and world["boss"]["id"] in ids
    assert all(item["reason"] == "NO_SALARY" for item in without["items"])
    before = without["counts"]["without"]
    assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000")
    after = ok(admin.get(COMPENSATION, params={"filter": "without"}))
    assert after["counts"]["without"] == before - 1 and worker["id"] not in {i["employee"]["id"] for i in after["items"]}
    with_salary = ok(admin.get(COMPENSATION, params={"filter": "with", "q": "wendy"}))
    assert [i["employee"]["id"] for i in with_salary["items"]] == [worker["id"]] and with_salary["counts"]["with"] == 1
    assert ok(admin.get(employee_url(world["boss"]["id"])))["without_salary"] is True
    assert admin.get(COMPENSATION, params={"filter": "nope"}).status_code == 422


def test_visibility_self_sees_own_only_and_managers_see_nothing(world):
    admin, approver, worker = world["admin"], world["approver"], world["worker"]
    approved = assign_and_approve(admin, approver, worker["id"], world["version_id"], "1200000")
    own = ok(world["employee_client"].get(employee_url(worker["id"])))
    assert own["self_service"] is True and own["current"]["id"] == approved["id"] and "grade_range" not in own
    assert world["employee_client"].get(employee_url(world["boss"]["id"])).status_code == 404
    # the list is scope-filtered: a SELF holder sees only their own row, never colleagues without salary
    listed = ok(world["employee_client"].get(COMPENSATION))
    assert [i["employee"]["id"] for i in listed["items"]] == [worker["id"]] and listed["counts"]["with"] == 1
    assert ok(world["employee_client"].get(COMPENSATION, params={"filter": "without"}))["items"] == []
    assert ok(world["employee_client"].get(f"{COMPENSATION}/{approved['id']}"))["employee_id"] == worker["id"]
    boss_salary = assign_and_approve(admin, approver, world["boss"]["id"], world["version_id"], "2400000")
    assert world["employee_client"].get(f"{COMPENSATION}/{boss_salary['id']}").status_code == 404
    # the reporting manager sees nothing of the report's salary unless granted (contract §25)
    assert world["manager_client"].get(employee_url(worker["id"])).status_code == 404
    assert world["manager_client"].get(f"{COMPENSATION}/{approved['id']}").status_code == 404


def test_hr_only_components_are_hidden_from_self_service(acme, world):
    ok(add_component(acme, "RETENTION", tags=SALARY_TAGS, visibility="HR_ONLY"), 201)
    structure = make_structure(acme, code="HIDDEN", lines=[*EXAMPLE_A, {"component": "RETENTION", "value": "3000"}])
    worker = world["worker"]["id"]
    assign_and_approve(acme, world["approver"], worker, active_version_id(structure), "1200000")
    hr = {line["component"] for line in ok(acme.get(employee_url(worker)))["breakdown"]["lines"]}
    own = {line["component"] for line in ok(world["employee_client"].get(employee_url(worker)))["breakdown"]["lines"]}
    assert "RETENTION" in hr and "RETENTION" not in own and "BASIC" in own


def test_cross_tenant_compensation_is_not_found(app, world):
    other = signup_company(app, "Beta Industries")
    setup_catalogue(other)
    approved = assign_and_approve(world["admin"], world["approver"], world["worker"]["id"], world["version_id"],
                                  "1200000")
    assert other.get(f"{COMPENSATION}/{approved['id']}").status_code == 404
    assert other.get(employee_url(world["worker"]["id"])).status_code == 404
    assert other.post(f"{COMPENSATION}/{approved['id']}/approve", json={}).status_code in (403, 404)
    r = assign(other, world["worker"]["id"], world["version_id"], "1200000")
    assert r.status_code == 404
    assert world["worker"]["id"] not in {i["employee"]["id"] for i in ok(other.get(
        COMPENSATION, params={"filter": "without"}))["items"]}


def test_processor_without_approve_and_employee_without_edit(world):
    worker = world["worker"]["id"]
    r = assign(world["employee_client"], worker, world["version_id"], "1200000")
    assert r.status_code == 403
    r = assign(world["approver"], worker, world["version_id"], "1200000")
    assert r.status_code == 403  # approvers cannot propose


# --------------------------------------------------------------------------- bulk CSV
def test_bulk_csv_dry_run_reports_per_row_errors_and_commit_runs_as_a_job(world):
    admin, approver, worker, boss = world["admin"], world["approver"], world["worker"], world["boss"]
    start = month_start(admin.company_id).isoformat()
    csv = ("employee_code,structure_code,pay_basis_amount,effective_from,note\r\n"
           f"{worker['employee_code']},STAFF_CTC,1200000,{start},Annual review\r\n"
           f"{boss['employee_code']},STAFF_CTC,=1+1,{start},\r\n"
           f"NOPE,STAFF_CTC,1000000,{start},\r\n"
           f"{boss['employee_code']},MISSING,1000000,{start},\r\n"
           f"{boss['employee_code']},STAFF_CTC,1000000,{start},\"=HYPERLINK(\"\"http://evil\"\")\"\r\n"
           f"{boss['employee_code']},STAFF_CTC,1000000,not-a-date,\r\n")
    result = ok(admin.post(f"{COMPENSATION}/import", json={"mode": "dry_run", "csv": csv, "file_name": "a.csv"}))
    rows = {row["row"]: row for row in result["rows"]}
    assert result["summary"] == {"rows": 6, "valid": 1, "invalid": 5}
    assert rows[2]["ok"] is True
    assert any("treated as text" in e for e in rows[3]["errors"]) and rows[3]["values"]["pay_basis_amount"] == "'=1+1"
    assert any("unknown employee" in e for e in rows[4]["errors"])
    assert any("structure_code" in e for e in rows[5]["errors"])
    assert any("treated as text" in e for e in rows[6]["errors"]) and rows[6]["values"]["note"].startswith("'=")
    assert any("effective_from" in e for e in rows[7]["errors"])
    queued = ok(admin.post(f"{COMPENSATION}/import", json={"mode": "commit", "import_id": result["import_id"]}), 202)
    assert queued["status"] == "QUEUED" and queued["job"]["id"]
    report = ok(admin.get(f"{COMPENSATION}/imports/{result['import_id']}"))
    assert report["status"] == "COMMITTED" and report["result"]["created_count"] == 1
    pending = ok(admin.get(COMPENSATION, params={"filter": "pending"}))["items"]
    assert [p["employee"]["id"] for p in pending] == [worker["id"]]
    r = admin.post(f"{COMPENSATION}/import", json={"mode": "commit", "import_id": result["import_id"]})
    assert r.status_code == 409 and r.json()["code"] == "IMPORT_NOT_VALIDATED"
    assert approver.get(f"{COMPENSATION}/imports/{result['import_id']}").status_code == 403


def test_bulk_csv_rejects_bad_headers_and_other_tenants_imports(app, world):
    admin = world["admin"]
    r = admin.post(f"{COMPENSATION}/import", json={"mode": "dry_run", "csv": "employee_code,amount\r\nE1,1\r\n"})
    assert r.status_code == 400 and r.json()["code"] == "IMPORT_COLUMNS"
    r = admin.post(f"{COMPENSATION}/import", json={"mode": "dry_run", "csv": "employee_code,structure_code,"
                                                   "pay_basis_amount,effective_from,=cmd\r\n"})
    assert r.status_code == 400 and "'=cmd" in r.json()["detail"]
    template = admin.get(f"{COMPENSATION}/import-template")
    assert template.status_code == 200 and template.text.startswith("employee_code,structure_code")
    result = ok(admin.post(f"{COMPENSATION}/import", json={"mode": "dry_run", "csv": "employee_code,structure_code,"
                                                           "pay_basis_amount,effective_from\r\n"}))
    other = signup_company(app, "Gamma Co")
    assert other.get(f"{COMPENSATION}/imports/{result['import_id']}").status_code == 404
    r = other.post(f"{COMPENSATION}/import", json={"mode": "commit", "import_id": result["import_id"]})
    assert r.status_code == 404
