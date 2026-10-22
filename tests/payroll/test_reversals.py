"""Reversal of finalized runs: request, two approvers, the REVERSAL run of exact negative lines, payment
cancellation or recovery, the payroll.run.reversed event, permissions and three-tenant isolation
(3.5 §4.1 REVERSE, §12).

EC-COR-03, EC-COR-04, EC-COR-16, EC-RUN-17, EC-SEC-01, EC-SEC-05.
"""

from decimal import Decimal

import pytest

from tests.helpers import create_employee
from tests.payroll.test_compensation import login_as
from tests.payroll.test_retro import (  # noqa: F401  (fixtures)
    finalize,
    lines_of,
    paid_regular_run,
    proposal,
    retro_world,
    run_employee_id,
    runs_world,
)
from tests.payroll.test_runs_lifecycle import PAY, RUNS
from tests.payroll.test_wage_inputs import audit_actions, sql

REVERSALS = f"{PAY}/reversals"


def reverse(client, run_id, expect=201, **body):
    r = client.post(f"{RUNS}/{run_id}/reverse", json={"reason": "Paid in the wrong period", **body})
    assert r.status_code == expect, r.text
    return r.json()


def approve(client, reversal_id, expect=200, **body):
    r = client.post(f"{REVERSALS}/{reversal_id}/approve", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def instructions_of(run_id):
    return {row[0]: row[1] for row in sql(
        "SELECT i.id, i.status FROM payment_instructions i JOIN payment_obligations o ON o.id = i.obligation_id "
        "WHERE o.run_id = :r ORDER BY i.id", r=run_id)}


def settle(instruction_id):
    sql("UPDATE payment_instructions SET status = 'SETTLED', submission_channel = 'MANUAL_UPLOAD', "
        "submitted_at = now(), evidence_status = 'VERIFIED', evidence_source = 'STATEMENT', settled_at = now(), "
        "settled_amount = amount WHERE id = :i", i=instruction_id)


@pytest.fixture
def reversal_world(app, retro_world):
    w = retro_world
    w["second"] = login_as(app, w["acme"], create_employee(w["acme"], "Bob", "Checker"), ["payroll_approver"])
    return w


def test_ec_cor_04_reversal_before_payment_cancels_the_unsent_instructions(reversal_world):
    w = reversal_world
    run_id = w["run"]["id"]
    before = instructions_of(run_id)
    assert before and set(before.values()) <= {"NOT_READY", "READY", "ON_HOLD"}  # created by the payments handler
    original_state = sql("SELECT status FROM payroll_runs WHERE id = :r", r=run_id)[0][0]
    created = reverse(w["acme"], run_id)
    assert created["status"] == "PENDING_APPROVAL" and created["reversal_run_id"] is None
    first = approve(w["approver"], created["id"], comment="Checked")
    assert first["status"] == "PENDING_APPROVAL"
    second = approve(w["second"], created["id"])
    assert second["status"] == "APPROVED" and second["reversal_run_id"]
    reversal_run = second["reversal_run_id"]
    detail = w["acme"].get(f"{RUNS}/{reversal_run}").json()
    assert detail["run_type"] == "REVERSAL" and detail["status"] == "REVIEW_REQUIRED"
    assert detail["reverses_run_id"] == run_id
    # Exact negative lines for every reversed employee.
    for employee in (w["emma"], w["ivan"]):
        original = lines_of(run_employee_id(run_id, employee))
        negated = lines_of(run_employee_id(reversal_run, employee))
        assert negated == {code: -amount for code, amount in original.items()}
        figures = sql("SELECT gross, net FROM payroll_run_employees WHERE id = :r",
                      r=run_employee_id(reversal_run, employee))[0]
        orig = sql("SELECT gross, net FROM payroll_run_employees WHERE id = :r",
                   r=run_employee_id(run_id, employee))[0]
        assert (figures[0], figures[1]) == (-orig[0], -orig[1])
    # The original run is never edited: its status is unchanged and the REVERSE move is audited.
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run_id)[0][0] == original_state
    assert "RUN_REVERSED" in audit_actions(w["cid"], "payroll_run", run_id)
    assert set(instructions_of(run_id).values()) == {"ON_HOLD"}  # nobody submits them meanwhile
    # Its own approval and finalization.
    finalize(w, reversal_run)
    assert set(instructions_of(run_id).values()) == {"CANCELLED"}
    data = w["acme"].get(f"{REVERSALS}/{created['id']}").json()
    assert data["status"] == "FINALIZED"
    assert sorted(data["effects"]["instructions_cancelled"]) == sorted(before)
    assert data["effects"]["recoveries"] == []
    assert [a["user_id"] for a in data["approvals"]] == [w["approver"].user_id, w["second"].user_id]
    event = sql("SELECT topic, payload FROM outbox_events WHERE id = :e", e=data["effects"]["outbox_event_id"])[0]
    assert event[0] == "payroll.run.reversed" and event[1]["run_id"] == run_id
    assert sql("SELECT count(*) FROM payment_obligations WHERE run_id = :r", r=reversal_run)[0][0] == 0


def test_ec_cor_03_reversal_of_a_settled_payment_creates_a_recovery_never_a_bank_debit(reversal_world):
    w = reversal_world
    run_id = w["run"]["id"]
    emma_row = w["emma_row"]
    emma_instructions = [i for (i,) in sql(
        "SELECT i.id FROM payment_instructions i JOIN payment_obligations o ON o.id = i.obligation_id "
        "WHERE o.run_employee_id = :r", r=emma_row)]
    for instruction in emma_instructions:
        settle(instruction)
    created = reverse(w["acme"], run_id, run_employee_ids=[emma_row])
    approve(w["approver"], created["id"])
    reversal_run = approve(w["second"], created["id"])["reversal_run_id"]
    assert sql("SELECT count(*) FROM payroll_run_employees WHERE run_id = :r AND status = 'CALCULATED'",
               r=reversal_run)[0][0] == 1  # selected employees only
    finalize(w, reversal_run)
    data = w["acme"].get(f"{REVERSALS}/{created['id']}").json()
    [recovery] = data["effects"]["recoveries"]
    net = sql("SELECT net FROM payroll_run_employees WHERE id = :r", r=emma_row)[0][0]
    assert recovery["run_employee_id"] == emma_row and Decimal(recovery["amount"]) == Decimal(net)
    detail = proposal(w["acme"], recovery["proposal_id"])
    assert detail["trigger"] == "REVERSAL" and detail["status"] == "APPROVED"
    assert [(l["component_code"], l["direction"], Decimal(l["delta"]["amount"])) for l in detail["lines"]] == [
        ("NET_PAY_RECOVERY", "RECOVERY", -Decimal(net))]
    # The settled payments are untouched: no automatic bank debit, no reversal instruction.
    assert {instructions_of(run_id)[i] for i in emma_instructions} == {"SETTLED"}
    assert sql("SELECT count(*) FROM payment_instructions WHERE run_id = :r", r=reversal_run)[0][0] == 0
    # Ivan was not reversed: his payments are untouched.
    ivan_status = {s for (s,) in sql(
        "SELECT i.status FROM payment_instructions i JOIN payment_obligations o ON o.id = i.obligation_id "
        "WHERE o.run_employee_id = :r", r=run_employee_id(run_id, w["ivan"]))}
    assert ivan_status <= {"NOT_READY", "READY"}


def test_two_different_approvers_neither_the_requester(reversal_world):
    w = reversal_world
    created = reverse(w["acme"], w["run"]["id"])
    approve(w["acme"], created["id"], expect=403)  # the requester
    assert approve(w["approver"], created["id"], expect=409, row_version=99)["code"] == "ROW_VERSION_CONFLICT"
    approve(w["approver"], created["id"])
    approve(w["approver"], created["id"], expect=403)  # the same approver twice
    r = w["second"].post(f"{REVERSALS}/{created['id']}/reject", json={"reason": " "})
    assert r.status_code == 422
    r = w["second"].post(f"{REVERSALS}/{created['id']}/reject", json={"reason": "Not a wrong period"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED", r.text
    approve(w["second"], created["id"], expect=409)
    # The database refuses a requester approval even if a service forgot the check.
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError) as info:
        sql("UPDATE payroll_reversals SET first_approver_user_id = requested_by_user_id WHERE id = :i",
            i=created["id"])
    assert info.value.orig.pgcode == "23514"


def test_reversal_request_rules(reversal_world):
    w = reversal_world
    run_id = w["run"]["id"]
    reverse(w["acme"], run_id, expect=422, reason="")
    created = reverse(w["acme"], run_id, run_employee_ids=[w["emma_row"]])
    r = w["acme"].post(f"{RUNS}/{run_id}/reverse", json={"reason": "again"})
    assert r.status_code == 409 and r.json()["code"] == "REVERSAL_PENDING"
    r = w["acme"].post(f"{REVERSALS}/{created['id']}/cancel", json={"reason": "Raised by mistake"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    reverse(w["acme"], run_id, expect=404, run_employee_ids=[999999])
    # A run that is not finalized is reopened or cancelled, never reversed.
    draft = w["acme"].post(RUNS, json={"run_type": "OFF_CYCLE", "pay_group_id": w["group"],
                                       "pay_period_id": w["period"]["id"], "employee_ids": [w["emma"]]}).json()
    r = w["acme"].post(f"{RUNS}/{draft['id']}/reverse", json={"reason": "x"})
    assert r.status_code == 409 and r.json()["code"] == "RUN_NOT_FINALIZED"
    # The processor holds no reverse permission; the approver neither.
    reverse(w["processor"], run_id, expect=403)
    reverse(w["approver"], run_id, expect=403)


def test_an_employee_reversed_once_cannot_be_reversed_again_and_reversal_runs_are_not_reversed(reversal_world):
    w = reversal_world
    run_id = w["run"]["id"]
    created = reverse(w["acme"], run_id, run_employee_ids=[w["emma_row"]])
    approve(w["approver"], created["id"])
    reversal_run = approve(w["second"], created["id"])["reversal_run_id"]
    r = w["acme"].post(f"{RUNS}/{run_id}/reverse", json={"reason": "x", "run_employee_ids": [w["emma_row"]]})
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_REVERSED"
    finalize(w, reversal_run)
    r = w["acme"].post(f"{RUNS}/{reversal_run}/reverse", json={"reason": "x"})
    assert r.status_code == 409 and r.json()["code"] == "REVERSAL_OF_REVERSAL"
    # Ivan can still be reversed separately.
    reverse(w["acme"], run_id, run_employee_ids=[run_employee_id(run_id, w["ivan"])])
    listed = w["acme"].get(REVERSALS, params={"run_id": run_id}).json()
    assert {i["status"] for i in listed["items"]} == {"FINALIZED", "PENDING_APPROVAL"}


def test_ec_sec_01_reversals_across_tenants_and_scopes(reversal_world):
    w = reversal_world
    run_id = w["run"]["id"]
    created = reverse(w["acme"], run_id)
    for other in (w["beta"]["client"], w["cobalt"]["client"]):
        assert other.post(f"{RUNS}/{run_id}/reverse", json={"reason": "probe"}).status_code in (403, 404)
        assert other.get(f"{REVERSALS}/{created['id']}").status_code == 404
        assert other.post(f"{REVERSALS}/{created['id']}/approve", json={}).status_code in (403, 404)
        assert other.post(f"{REVERSALS}/{created['id']}/cancel", json={"reason": "x"}).status_code in (403, 404)
        assert other.get(REVERSALS).json()["total"] == 0
    # The entity clerk sees nothing of LE01.
    assert w["cleo"].get(f"{REVERSALS}/{created['id']}").status_code == 404
    # A foreign tenant's own run cannot be reversed with acme's ids, and acme cannot reach beta's run.
    beta_run = w["beta"]["client"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["beta"]["group"],
                                                    "pay_period_id": w["beta"]["period"]["id"]})
    if beta_run.status_code == 201:
        assert w["acme"].post(f"{RUNS}/{beta_run.json()['id']}/reverse", json={"reason": "x"}).status_code == 404
