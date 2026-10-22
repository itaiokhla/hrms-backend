"""Off-cycle runs: bonus and correction runs over selected employees and components referencing a regular
period for YTD, payment-only reissue runs referencing the original lines, loan disbursement runs, permissions
and three-tenant isolation (3.5 §13; 3.7 §10).

EC-COR-06, EC-COR-16, EC-PAY-04, EC-SEC-01, EC-SEC-05.
"""

from decimal import Decimal

import pytest

from tests.payroll.test_retro import (  # noqa: F401  (fixtures)
    finalize,
    next_period,
    retro_world,
    run_employee_id,
    runs_world,
)
from tests.payroll.test_runs_lifecycle import PAY, RUNS, session_for
from tests.payroll.test_wage_inputs import audit_actions, sql

OFF_CYCLE = f"{PAY}/off-cycle-runs"


def create(client, expect=201, **body):
    r = client.post(OFF_CYCLE, json={"reason": "Diwali bonus", **body})
    assert r.status_code == expect, r.text
    return r.json()


def selection(run_id):
    return sql("SELECT selection FROM payroll_runs WHERE id = :r", r=run_id)[0][0]


def instructions_of(run_id):
    return [i for (i,) in sql("SELECT i.id FROM payment_instructions i JOIN payment_obligations o "
                              "ON o.id = i.obligation_id WHERE o.run_id = :r ORDER BY i.id", r=run_id)]


def fail(instruction_id):
    sql("UPDATE payment_instructions SET status = 'FAILED', submission_channel = 'MANUAL_UPLOAD', "
        "submitted_at = now(), failure_code = 'AC01', failure_message = 'Account closed' WHERE id = :i",
        i=instruction_id)


# ---------------------------------------------------------------------------
# Bonus and correction runs (EC-COR-06)
# ---------------------------------------------------------------------------
def test_ec_cor_06_bonus_run_over_selected_employees_and_components_references_a_period_for_ytd(runs_world):
    from app.payroll.services import off_cycle, runs

    w = runs_world
    emma = w["emp"]["Emma"]["id"]
    created = create(w["acme"], kind="BONUS", pay_group_id=w["group"], pay_period_id=w["period"]["id"],
                     employee_ids=[emma], component_codes=["bonus"])
    assert created["run_type"] == "OFF_CYCLE" and created["status"] == "DRAFT" and created["kind"] == "BONUS"
    data = selection(created["id"])["off_cycle"]
    assert data == {"kind": "BONUS", "payment_only": False, "components": ["BONUS"],
                    "ytd_period_id": w["period"]["id"], "tax_year": data["tax_year"], "reason": "Diwali bonus"}
    population = {e: s for e, s in sql("SELECT employee_id, status FROM payroll_run_employees WHERE run_id = :r",
                                       r=created["id"])}
    assert population == {emma: "PENDING"}  # selected employees only
    db = session_for(w["cid"])
    try:
        run = runs.load_run(db, w["cid"], created["id"])
        assert off_cycle.selected_components(run) == ("BONUS",)
    finally:
        db.close()
    assert "OFF_CYCLE_RUN_CREATED" in audit_actions(w["cid"], "payroll_run", created["id"])
    listed = w["acme"].get(OFF_CYCLE, params={"kind": "BONUS"}).json()
    assert [i["id"] for i in listed["items"]] == [created["id"]]
    # Several off-cycle runs may exist for the same period.
    create(w["acme"], kind="CORRECTION", pay_group_id=w["group"], pay_period_id=w["period"]["id"],
           employee_ids=[w["emp"]["Ivan"]["id"]], component_codes=["BASIC"], reason="Missed joiner")


def test_bonus_run_validation(runs_world):
    w = runs_world
    base = {"kind": "BONUS", "pay_group_id": w["group"], "pay_period_id": w["period"]["id"]}
    emma = w["emp"]["Emma"]["id"]
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [emma]})
    assert r.status_code == 400 and r.json()["code"] == "COMPONENTS_REQUIRED"
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": "x", "component_codes": ["BONUS"]})
    assert r.status_code == 400 and r.json()["code"] == "EMPLOYEES_REQUIRED"
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [emma], "component_codes": ["NOPE"]})
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_COMPONENT"
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [emma],
                                        "component_codes": ["LOAN_RECOVERY"]})
    assert r.status_code == 400 and r.json()["code"] == "COMPONENT_NOT_ALLOWED"  # a bonus pays earnings only
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": " ", "employee_ids": [emma], "component_codes": ["BONUS"]})
    assert r.status_code == 422
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [w["gus"]["id"]],
                                        "component_codes": ["BONUS"]})
    assert r.status_code == 409 and r.json()["code"] == "EMPLOYEE_NOT_IN_PAY_GROUP"
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [emma], "component_codes": ["BONUS"],
                                        "loan_ids": [1]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_SELECTION"
    r = w["acme"].post(OFF_CYCLE, json={**base, "kind": "PAYOUT", "reason": "x"})
    assert r.status_code == 422
    r = w["acme"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [emma], "component_codes": ["BONUS"],
                                        "status": "FINALIZED"})
    assert r.status_code == 422  # server-owned fields are refused
    # The entity clerk (AE01 only) cannot reach LE01's pay group; the approver cannot create runs.
    r = w["cleo"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [emma], "component_codes": ["BONUS"]})
    assert r.status_code == 404
    r = w["approver"].post(OFF_CYCLE, json={**base, "reason": "x", "employee_ids": [emma],
                                            "component_codes": ["BONUS"]})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Payment-only reissue runs (EC-PAY-04)
# ---------------------------------------------------------------------------
def test_payment_only_reissue_run_references_the_original_lines_without_new_earnings(retro_world):
    w = retro_world
    run_id = w["run"]["id"]
    emma_instruction = next(i for i in instructions_of(run_id)
                            if sql("SELECT employee_id FROM payment_instructions WHERE id = :i", i=i)[0][0] == w["emma"])
    r = w["acme"].post(OFF_CYCLE, json={"kind": "REISSUE", "reason": "Account closed", "original_run_id": run_id,
                                        "instruction_ids": [emma_instruction]})
    assert r.status_code == 409 and r.json()["code"] == "NOT_REISSUABLE"  # not failed yet
    fail(emma_instruction)
    created = create(w["acme"], kind="REISSUE", reason="Account closed", original_run_id=run_id,
                     instruction_ids=[emma_instruction])
    assert created["status"] == "REVIEW_REQUIRED"
    data = selection(created["id"])["off_cycle"]
    assert data["payment_only"] is True and data["original_run_id"] == run_id
    assert [i["instruction_id"] for i in data["instructions"]] == [emma_instruction]
    row = run_employee_id(created["id"], w["emma"])
    figures = sql("SELECT gross, deductions, net FROM payroll_run_employees WHERE id = :r", r=row)[0]
    assert all(Decimal(f) == 0 for f in figures)  # no new earnings
    [line] = sql("SELECT component_code, kind, amount, period_ref FROM payroll_lines WHERE run_employee_id = :r",
                 r=row)
    assert line[0] == "PAYMENT_REISSUE" and line[1] == "INFORMATIONAL"
    assert line[3]["original_run_id"] == run_id and line[3]["instruction_id"] == emma_instruction
    # The same payment cannot be in two reissue runs.
    r = w["acme"].post(OFF_CYCLE, json={"kind": "REISSUE", "reason": "again", "original_run_id": run_id,
                                        "instruction_ids": [emma_instruction]})
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_IN_REISSUE_RUN"
    # Finalized: no payee account is usable in this tenant, so the reissue is reported, never guessed.
    finalize(w, created["id"])
    effects = [d for (d,) in sql("SELECT details FROM audit_logs WHERE entity_type = 'payroll_run' AND "
                                 "entity_id = :e AND action = 'OFF_CYCLE_RUN_EFFECTS'", e=str(created["id"]))]
    assert effects and effects[0]["skipped"][0]["reason"] == "BANK_ACCOUNT_NOT_USABLE"
    assert sql("SELECT status FROM payment_instructions WHERE id = :i", i=emma_instruction)[0][0] == "FAILED"
    assert sql("SELECT count(*) FROM payment_obligations WHERE run_id = :r", r=created["id"])[0][0] == 0


def test_reissue_run_validation(retro_world):
    w = retro_world
    run_id = w["run"]["id"]
    r = w["acme"].post(OFF_CYCLE, json={"kind": "REISSUE", "reason": "x", "original_run_id": run_id})
    assert r.status_code == 400 and r.json()["code"] == "INSTRUCTIONS_REQUIRED"
    r = w["acme"].post(OFF_CYCLE, json={"kind": "REISSUE", "reason": "x", "original_run_id": run_id,
                                        "instruction_ids": [999999]})
    assert r.status_code == 404
    r = w["acme"].post(OFF_CYCLE, json={"kind": "REISSUE", "reason": "x", "original_run_id": run_id,
                                        "instruction_ids": instructions_of(run_id)[:1],
                                        "component_codes": ["BONUS"]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_SELECTION"
    draft = w["acme"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                                       "pay_period_id": next_period(w)["id"]}).json()
    r = w["acme"].post(OFF_CYCLE, json={"kind": "REISSUE", "reason": "x", "original_run_id": draft["id"],
                                        "instruction_ids": [1]})
    assert r.status_code == 409 and r.json()["code"] == "RUN_NOT_FINALIZED"


def test_loan_disbursement_run_validation(runs_world):
    w = runs_world
    base = {"kind": "LOAN_DISBURSEMENT", "reason": "Approved advance", "pay_group_id": w["group"],
            "pay_period_id": w["period"]["id"]}
    r = w["acme"].post(OFF_CYCLE, json=base)
    assert r.status_code == 400 and r.json()["code"] == "LOANS_REQUIRED"
    r = w["acme"].post(OFF_CYCLE, json={**base, "loan_ids": [999999]})
    assert r.status_code == 404
    r = w["acme"].post(OFF_CYCLE, json={**base, "loan_ids": [1], "employee_ids": [w["emp"]["Emma"]["id"]]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_SELECTION"


# ---------------------------------------------------------------------------
# Isolation (EC-SEC-01)
# ---------------------------------------------------------------------------
def test_ec_sec_01_off_cycle_runs_across_tenants(retro_world):
    w = retro_world
    emma = w["emma"]
    created = create(w["acme"], kind="BONUS", pay_group_id=w["group"], pay_period_id=w["period"]["id"],
                     employee_ids=[emma], component_codes=["BONUS"])
    for tenant in (w["beta"], w["cobalt"]):
        other = tenant["client"]
        assert other.get(OFF_CYCLE).json()["total"] == 0
        # Foreign pay group / employees / runs / instructions are unknown (404).
        r = other.post(OFF_CYCLE, json={"kind": "BONUS", "reason": "x", "pay_group_id": w["group"],
                                        "pay_period_id": w["period"]["id"], "employee_ids": [emma],
                                        "component_codes": ["BONUS"]})
        assert r.status_code == 404
        r = other.post(OFF_CYCLE, json={"kind": "BONUS", "reason": "x", "pay_group_id": tenant["group"],
                                        "pay_period_id": w["period"]["id"], "employee_ids": [emma],
                                        "component_codes": ["BONUS"]})
        assert r.status_code in (400, 404)
        r = other.post(OFF_CYCLE, json={"kind": "REISSUE", "reason": "x", "original_run_id": w["run"]["id"],
                                        "instruction_ids": instructions_of(w["run"]["id"])})
        assert r.status_code == 404
        assert other.get(f"{RUNS}/{created['id']}").status_code == 404
