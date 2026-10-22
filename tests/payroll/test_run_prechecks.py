"""Run pre-checks and exceptions (3.5 §5.2), the statutory attestation gate (decision I-15) and the
finalization blockers the finalize service reads.

EC-RUN-11, EC-RUN-12, EC-RUN-20, EC-CFG-07, EC-CFG-09, EC-STA-02, EC-ORG-23, EC-CAL-13.
"""

from datetime import date, timedelta

import pytest

from tests.helpers import create_employee
from tests.payroll.test_compensation import assign_and_approve, login_as
from tests.payroll.test_components import ok
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    current_period,
    new_run,
    periods_of,
    principal_of,
    run_employees,
    runs_world,
)
from tests.payroll.test_wage_inputs import CHECK, audit_actions, expect_error, set_setting, sql, tenant

ATTESTATIONS = f"{PAY}/statutory-attestations"


def precheck(client, run_id, expect=200):
    r = client.post(f"{RUNS}/{run_id}/precheck")
    assert r.status_code == expect, r.text
    return r.json()


def exceptions(client, run_id, **params):
    return ok(client.get(f"{RUNS}/{run_id}/exceptions", params=params))["items"]


def open_blocking(items):
    """(code, message) of the OPEN blocking exceptions: the assertion message when a run is not READY."""
    return [(e["code"], e["message"]) for e in items if e["severity"] == "BLOCKING" and e["status"] == "OPEN"]


def by_code(items, code, status="OPEN"):
    return [e for e in items if e["code"] == code and (status is None or e["status"] == status)]


def blockers(client, run_id):
    from app.payroll.services import runs as service

    db, _ = principal_of(client)
    try:
        return {b["code"] for b in service.finalization_blockers(db, service.load_run(db, client.company_id, run_id))}
    finally:
        db.close()


def pending_leave(company_id, employee_id, day):
    sql("INSERT INTO leaves (company_id, employee_id, leave_type, start_date, end_date, number_of_days, start_portion, "
        "end_portion, status, created_at, updated_at) VALUES (:c, :e, 'CL', :d, :d, 1, 'FULL', 'FULL', 'pending', "
        "now(), now())", c=company_id, e=employee_id, d=day)


# ---------------------------------------------------------------------------
# A clean run, population findings (EC-RUN-11, EC-RUN-12, EC-RUN-20)
# ---------------------------------------------------------------------------
def test_precheck_of_a_clean_run_is_ready_with_listed_exclusions(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    result = precheck(w["acme"], run["id"])
    items = exceptions(w["acme"], run["id"])
    assert result["status"] == "READY", open_blocking(items)
    assert result["blocking"] == 0
    # Settings that never change an amount (payments, documents ...) warn instead of blocking the calculation.
    from app.payroll.services import runs as service

    after = [e for e in by_code(items, "CONFIG_INVALID") if e["details"].get("section") in
             service.AFTER_CALCULATION_SECTIONS]
    assert all(e["severity"] == "WARNING" for e in after)
    no_salary = by_code(items, "NO_SALARY")
    assert {e["employee_id"] for e in no_salary} == {w["emp"]["Nora"]["id"], w["emp"]["Pia"]["id"]}
    assert all(e["severity"] == "BLOCKING" and not e["waivable"] for e in no_salary)  # blocking for that employee
    assert not by_code(items, "STATUTORY_NOT_VERIFIED", None)  # IN fixture pack in the test environment
    assert "PRECHECK_RUN" in audit_actions(w["cid"], "payroll_run", run["id"])
    again = precheck(w["acme"], run["id"], expect=409)
    assert again["code"] == "INVALID_TRANSITION"  # READY is not re-checked; nothing changed meanwhile


def test_ec_run_12_employee_terminated_before_the_period_blocks_until_removed(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    start = date.fromisoformat(w["period"]["start"])
    sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=start - timedelta(days=3),
        e=w["emp"]["Ivan"]["id"])
    result = precheck(w["acme"], run["id"])
    assert result["status"] == "INPUTS_PENDING"
    finding = by_code(exceptions(w["acme"], run["id"]), "INELIGIBLE_INCLUDED")
    assert [e["employee_id"] for e in finding] == [w["emp"]["Ivan"]["id"]] and not finding[0]["waivable"]
    r = w["acme"].post(f"{PAY}/exceptions/{finding[0]['id']}/waive", json={"reason": "ignore"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_WAIVABLE", r.text
    fixed = ok(w["acme"].post(f"{PAY}/exceptions/{finding[0]['id']}/fix", json={"note": "Left last month"}))
    assert fixed["status"] == "FIXED"
    assert run_employees(run["id"])[w["emp"]["Ivan"]["id"]][:2] == ("EXCLUDED", "INELIGIBLE")
    assert precheck(w["acme"], run["id"])["status"] == "READY"
    population = ok(w["acme"].get(f"{RUNS}/{run['id']}/population"))
    assert population["eligible"] == population["included"] + population["excluded"]  # EC-RUN-11
    assert "EXCEPTION_FIXED" in audit_actions(w["cid"], "payroll_exception", finding[0]["id"])


def test_ec_run_20_pay_group_without_eligible_employees_cannot_proceed(runs_world):
    beta = runs_world["beta"]
    run = new_run(beta["client"], beta["group"], beta["period"]["id"])
    assert run["population"]["included"] == 0
    assert precheck(beta["client"], run["id"])["status"] == "INPUTS_PENDING"
    assert by_code(exceptions(beta["client"], run["id"]), "EMPTY_POPULATION")


def test_new_salary_re_includes_an_excluded_employee_at_precheck(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assign_and_approve(w["acme"], w["approver"], w["emp"]["Nora"]["id"], w["version"], "700000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    precheck(w["acme"], run["id"])
    assert run_employees(run["id"])[w["emp"]["Nora"]["id"]][0] == "PENDING"
    nora = [e for e in exceptions(w["acme"], run["id"], code="NO_SALARY") if e["employee_id"] == w["emp"]["Nora"]["id"]]
    assert nora == []


# ---------------------------------------------------------------------------
# Configuration (EC-CFG-07, EC-CFG-09, I-13, I-14)
# ---------------------------------------------------------------------------
def test_ec_cfg_09_invalid_stored_setting_is_a_blocking_config_invalid(runs_world):
    w = runs_world
    set_setting(w["cid"], "payroll.lop.proration_basis", "NONSENSE")
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "INPUTS_PENDING"
    found = by_code(exceptions(w["acme"], run["id"]), "CONFIG_INVALID")
    assert any(e["details"].get("key") == "payroll.lop.proration_basis" for e in found), found
    set_setting(w["cid"], "payroll.lop.proration_basis", "CALENDAR_DAYS")
    assert precheck(w["acme"], run["id"])["status"] == "READY"
    closed = by_code(exceptions(w["acme"], run["id"]), "CONFIG_INVALID", status="FIXED")
    assert closed and closed[0]["resolution_note"]


def test_ec_cfg_07_switch_on_without_matching_policy_and_starter_decision(runs_world):
    w = runs_world
    sql("DELETE FROM policy_assignments WHERE company_id = :c AND policy_id IN (SELECT id FROM policies WHERE "
        "company_id = :c AND type = 'LEAVE_PAY_POLICY')", c=w["cid"])
    set_setting(w["cid"], "payroll.overtime.enabled", True)  # STARTER_V1 leaves the overtime policy a draft
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "INPUTS_PENDING"
    items = exceptions(w["acme"], run["id"])
    leave = [e for e in by_code(items, "CONFIG_INVALID") if e["details"].get("policy_type") == "LEAVE_PAY_POLICY"]
    assert leave and {p["employee_id"] for p in leave[0]["details"]["employees"]} >= {w["emp"]["Emma"]["id"]}
    decision = [e for e in by_code(items, "CONFIG_DECISION_REQUIRED")
                if e["details"].get("policy_type") == "OVERTIME_POLICY"]
    assert decision and decision[0]["details"]["switches"] == ["payroll.overtime.enabled"]


def test_i14_legacy_tenant_needs_the_review_acknowledgment(runs_world):
    w = runs_world
    sql("UPDATE companies SET payroll_profile_version = 'LEGACY_V1' WHERE id = :c", c=w["cid"])
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "INPUTS_PENDING"
    legacy = by_code(exceptions(w["acme"], run["id"]), "LEGACY_REVIEW_PENDING")
    assert legacy and legacy[0]["severity"] == "BLOCKING" and not legacy[0]["waivable"]
    r = w["acme"].post(f"{PAY}/exceptions/{legacy[0]['id']}/waive", json={"reason": "later"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_WAIVABLE"
    set_setting(w["cid"], "payroll.migration.legacy_review_acknowledged", "2026-01-01")
    assert precheck(w["acme"], run["id"])["status"] == "READY"


# ---------------------------------------------------------------------------
# Statutory gate (EC-STA-02, I-15) and unconfirmed country (EC-ORG-23)
# ---------------------------------------------------------------------------
def test_ec_sta_02_no_verified_pack_blocks_finalization_until_an_approved_attestation(app, runs_world):
    w = runs_world
    acme = w["acme"]
    ae_period = current_period(acme, w["ae_group"])
    run = new_run(acme, w["ae_group"], ae_period["id"])
    precheck(acme, run["id"])
    gate = by_code(exceptions(acme, run["id"]), "STATUTORY_NOT_VERIFIED")
    assert gate and gate[0]["severity"] == "WARNING" and gate[0]["blocks_finalization"]
    assert "compliance not verified" in gate[0]["message"]
    assert "STATUTORY_NOT_VERIFIED" in blockers(acme, run["id"])

    body = {"legal_entity_id": w["ae"], "effective_from": "2020-01-01", "reason": "Handled by our UAE provider",
            "schemes": ["gpssa"]}
    attestation = ok(acme.post(ATTESTATIONS, json=body), 201)
    assert attestation["status"] == "PENDING" and attestation["schemes"] == ["GPSSA"]
    r = acme.post(ATTESTATIONS, json=body)
    assert r.status_code == 409 and r.json()["code"] == "ATTESTATION_OVERLAP"
    r = acme.post(f"{ATTESTATIONS}/{attestation['id']}/approve")
    assert r.status_code == 403, r.text  # maker-checker: not the attester
    r = w["approver"].post(f"{ATTESTATIONS}/{attestation['id']}/approve")
    assert r.status_code in (403, 404), r.text  # the run approver holds no statutory permission
    assert "STATUTORY_NOT_VERIFIED" in blockers(acme, run["id"])
    checker = login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"])
    approved = ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    assert approved["status"] == "ACTIVE" and approved["approved_by_user_id"] != approved["attested_by_user_id"]
    assert "STATUTORY_NOT_VERIFIED" not in blockers(acme, run["id"])
    with tenant(w["cid"]) as conn:
        expect_error(conn, CHECK, "UPDATE statutory_attestations SET approved_by_user_id = attested_by_user_id "
                                  "WHERE id = :a", a=attestation["id"])
    revoked = ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/revoke", json={"reason": "Provider changed"}))
    assert revoked["status"] == "REVOKED"
    assert "STATUTORY_NOT_VERIFIED" in blockers(acme, run["id"])  # read live by the finalize gate
    actions = audit_actions(w["cid"], "statutory_attestation", attestation["id"])
    assert actions == ["STATUTORY_ATTESTATION_CREATED", "STATUTORY_ATTESTATION_APPROVED", "STATUTORY_ATTESTATION_REVOKED"]
    listed = ok(acme.get(ATTESTATIONS, params={"legal_entity_id": w["ae"]}))
    assert listed["total"] == 1


def test_attestation_scope_and_validation(runs_world):
    w = runs_world
    r = w["beta"]["client"].post(ATTESTATIONS, json={"legal_entity_id": w["ae"], "effective_from": "2026-01-01",
                                                     "reason": "x"})
    assert r.status_code == 404, r.text  # another tenant's entity
    r = w["processor"].post(ATTESTATIONS, json={"legal_entity_id": w["ae"], "effective_from": "2026-01-01",
                                                "reason": "x"})
    assert r.status_code == 403, r.text
    r = w["acme"].post(ATTESTATIONS, json={"legal_entity_id": w["ae"], "effective_from": "2026-02-01",
                                           "effective_to": "2026-01-01", "reason": "x"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_RANGE"
    r = w["acme"].post(ATTESTATIONS, json={"legal_entity_id": w["ae"], "effective_from": "2026-01-01",
                                           "reason": "x", "status": "ACTIVE"})
    assert r.status_code == 422  # server-owned status


def test_ec_org_23_unconfirmed_country_blocks_finalization_until_confirmed(runs_world):
    w = runs_world
    sql("UPDATE legal_entities SET country_confirmed = false WHERE id = :e", e=w["le01"])
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "READY"  # never blocks calculation
    found = by_code(exceptions(w["acme"], run["id"]), "UNCONFIRMED_COUNTRY")
    assert found and found[0]["blocks_finalization"]
    assert "UNCONFIRMED_COUNTRY" in blockers(w["acme"], run["id"])
    sql("UPDATE legal_entities SET country_confirmed = true WHERE id = :e", e=w["le01"])
    assert "UNCONFIRMED_COUNTRY" not in blockers(w["acme"], run["id"])


# ---------------------------------------------------------------------------
# Pending inputs, waive and defer (EC-CAL-13), FX
# ---------------------------------------------------------------------------
def test_pending_leave_blocks_then_waive_with_reason_and_rerun_keeps_the_waiver(runs_world):
    w = runs_world
    pending_leave(w["cid"], w["emp"]["Emma"]["id"], date.fromisoformat(w["period"]["start"]) + timedelta(days=2))
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "INPUTS_PENDING"
    leave = [e for e in by_code(exceptions(w["acme"], run["id"]), "PENDING_INPUTS") if e["details"]["kind"] == "LEAVE"]
    assert leave and leave[0]["severity"] == "BLOCKING" and leave[0]["waivable"]
    r = w["acme"].post(f"{PAY}/exceptions/{leave[0]['id']}/waive", json={"reason": " "})
    assert r.status_code == 422
    waived = ok(w["acme"].post(f"{PAY}/exceptions/{leave[0]['id']}/waive",
                               json={"reason": "Leave decided after payroll; corrected next month"}))
    assert waived["status"] == "WAIVED"
    r = w["acme"].post(f"{PAY}/exceptions/{leave[0]['id']}/waive", json={"reason": "again"})
    assert r.status_code == 409 and r.json()["code"] == "EXCEPTION_RESOLVED"
    assert precheck(w["acme"], run["id"])["status"] == "READY"
    assert len([e for e in exceptions(w["acme"], run["id"], code="PENDING_INPUTS") if e["status"] == "OPEN"]) == 0
    assert "EXCEPTION_WAIVED" in audit_actions(w["cid"], "payroll_exception", leave[0]["id"])


def test_defer_pending_wage_input_to_the_next_period_and_refuse_where_not_allowed(runs_world):
    w = runs_world
    component = sql("SELECT id FROM pay_components WHERE company_id = :c ORDER BY id LIMIT 1", c=w["cid"])[0][0]
    sql("INSERT INTO wage_inputs (company_id, employee_id, legal_entity_id, pay_group_id, component_id, component_code, "
        "amount, currency, target_period_start, target_period_end, status, row_version, created_at, updated_at) VALUES "
        "(:c, :e, :le, :g, :comp, 'BONUS', 500, 'INR', :s, :t, 'PENDING_APPROVAL', 1, now(), now())",
        c=w["cid"], e=w["emp"]["Ivan"]["id"], le=w["le01"], g=w["group"], comp=component, s=w["period"]["start"],
        t=w["period"]["end"])
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    precheck(w["acme"], run["id"])
    items = exceptions(w["acme"], run["id"])
    wage = [e for e in by_code(items, "PENDING_INPUTS") if e["details"]["kind"] == "WAGE_INPUT"]
    assert wage and wage[0]["severity"] == "WARNING"
    deferred = ok(w["acme"].post(f"{PAY}/exceptions/{wage[0]['id']}/defer",
                                 json={"to": "NEXT_PERIOD", "reason": "Approve next month"}))
    assert deferred["status"] == "DEFERRED" and deferred["deferred_to"] == "NEXT_PERIOD"
    next_period = next(p for p in periods_of(w["acme"], w["group"]) if p["start"] > w["period"]["end"])
    assert sql("SELECT deferred_to_period_id FROM payroll_exceptions WHERE id = :x", x=wage[0]["id"])[0][0] == \
        next_period["id"]
    no_salary = by_code(items, "NO_SALARY")[0]
    r = w["acme"].post(f"{PAY}/exceptions/{no_salary['id']}/defer", json={"to": "RETRO", "reason": "x"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_DEFERRABLE"
    r = w["acme"].post(f"{PAY}/exceptions/{no_salary['id']}/defer", json={"to": "NEVER", "reason": "x"})
    assert r.status_code == 422
    assert "EXCEPTION_DEFERRED" in audit_actions(w["cid"], "payroll_exception", wage[0]["id"])


def test_nobody_waives_an_exception_about_their_own_pay(app, runs_world):
    from tests.helpers import login

    w = runs_world
    assign_and_approve(w["acme"], w["approver"], w["emp"]["Pia"]["id"], w["version"], "900000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    pending_leave(w["cid"], w["emp"]["Pia"]["id"], date.fromisoformat(w["period"]["start"]) + timedelta(days=1))
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    precheck(w["acme"], run["id"])
    own = [e for e in by_code(exceptions(w["acme"], run["id"]), "PENDING_INPUTS")
           if e["employee_id"] == w["emp"]["Pia"]["id"]]
    pia = login(app, w["processor"].email)
    r = pia.post(f"{PAY}/exceptions/{own[0]['id']}/waive", json={"reason": "my own leave"})
    assert r.status_code == 403, r.text


def test_fx_missing_blocks_an_employee_paid_in_another_currency(runs_world):
    w = runs_world
    sql("UPDATE compensation_assignments SET currency = 'USD' WHERE employee_id = :e", e=w["emp"]["Emma"]["id"])
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "INPUTS_PENDING"
    found = by_code(exceptions(w["acme"], run["id"]), "FX_MISSING")
    assert found and found[0]["employee_id"] == w["emp"]["Emma"]["id"] and found[0]["details"]["base"] == "USD"


def test_missing_bank_is_a_warning_per_included_employee_and_never_blocks(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    assert precheck(w["acme"], run["id"])["status"] == "READY"  # warnings never block calculation
    missing = by_code(exceptions(w["acme"], run["id"]), "MISSING_BANK")
    assert {e["employee_id"] for e in missing} == {w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]}
    assert all(e["severity"] == "WARNING" and not e["blocks_finalization"] for e in missing)
    # Excluded employees (no salary) are not paid by this run, so no bank warning for them.
    assert not {e["employee_id"] for e in missing} & {w["emp"]["Nora"]["id"], w["emp"]["Pia"]["id"]}


# ---------------------------------------------------------------------------
# Permissions and states
# ---------------------------------------------------------------------------
def test_precheck_and_resolution_permissions_and_scope(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    r = w["approver"].post(f"{RUNS}/{run['id']}/precheck")
    assert r.status_code == 403, r.text
    r = w["cleo"].post(f"{RUNS}/{run['id']}/precheck")
    assert r.status_code == 404, r.text
    r = w["beta"]["client"].post(f"{RUNS}/{run['id']}/precheck")
    assert r.status_code == 404, r.text
    precheck(w["acme"], run["id"])
    no_salary = by_code(exceptions(w["acme"], run["id"]), "NO_SALARY")[0]
    for client, status in ((w["approver"], 403), (w["cleo"], 404), (w["beta"]["client"], 404)):
        r = client.post(f"{PAY}/exceptions/{no_salary['id']}/waive", json={"reason": "x"})
        assert r.status_code == status, (status, r.text)
    r = w["cleo"].get(f"{RUNS}/{run['id']}/exceptions")
    assert r.status_code == 404
    ok(w["approver"].get(f"{RUNS}/{run['id']}/exceptions"))  # payroll:run:read is enough to read
    r = w["acme"].post(f"{RUNS}/{run['id']}/precheck", json={"row_version": 1})
    assert r.status_code == 409, r.text


def test_precheck_while_cancelled_or_finalized_is_refused(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    ok(w["acme"].post(f"{RUNS}/{run['id']}/cancel", json={"reason": "test"}))
    r = w["acme"].post(f"{RUNS}/{run['id']}/precheck")
    assert r.status_code == 409 and r.json()["code"] == "RUN_CANCELLED", r.text


@pytest.mark.parametrize("severity", ["BLOCKING", "WARNING"])
def test_exception_list_filters(runs_world, severity):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    precheck(w["acme"], run["id"])
    items = exceptions(w["acme"], run["id"], severity=severity)
    assert all(e["severity"] == severity for e in items)
