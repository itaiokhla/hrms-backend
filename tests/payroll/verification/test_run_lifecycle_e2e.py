"""Run lifecycle through the HTTP API: positives and negatives (Goal 2 §11; 3.5 §4-§5, §12-§13, §16).

Draft -> precheck -> calculate (real snapshot builder + engine) -> review -> submit -> independent approval ->
finalize (step-up, typed label, Idempotency-Key, I-15 attestation) -> obligations, payslips, YTD -> close, and
the negatives: duplicate regular run, missing salary (excluded, never zero pay), self-approval under separation
of duties, approving one's own pay, finalize without approval / without step-up / when stale, modifying a
finalized run (409, no reopening outside the correction workflow), retry after a timeout with the same key,
concurrent approvals, a settings change after the inputs were locked (stale), a permission revoked mid-process
(403 at action time), retro arrears, reversal, off-cycle bonus, closing with open payment obligations.

Amounts are hand-computed in the comments (Decimal). Shared helpers (attest, submit_and_approve, finalize_run)
are used by test_reconciliation.py.
"""

import threading
from decimal import Decimal

import pytest

from tests.helpers import create_employee
from tests.payroll.test_compensation import login_as
from tests.payroll.test_components import ok
from tests.payroll.test_runs_lifecycle import PAY, RUNS
from tests.payroll.test_wage_inputs import audit_actions, place, sql
from tests.payroll.verification.test_golden_calculations import (
    ATTESTATIONS,
    COMPENSATION,
    calculated_run,
    figures,
    gold_group,
    gold_world,
    hire,
    lines,
    lop,
    open_findings,
    period_of,
    wage_input,
)
from tests.payroll.verification.test_reconciliation import assert_finalized_reconciles, assert_run_reconciles

D = Decimal
ARREARS = f"{PAY}/arrears-proposals"
REVERSALS = f"{PAY}/reversals"
OFF_CYCLE = f"{PAY}/off-cycle-runs"
SETTINGS = f"{PAY}/settings/values"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def code_of(response):
    return response.json().get("code")


def state(run_id):
    status, version, label, stale = sql("SELECT status, row_version, label, stale FROM payroll_runs WHERE id = :r",
                                        r=run_id)[0]
    return {"status": status, "row_version": version, "label": label, "stale": stale}


def step_up(client):
    """A fresh step-up on the client's sessions (the e-mail OTP flow has its own tests)."""
    sql("UPDATE user_sessions SET step_up_at = now(), step_up_method = 'EMAIL_OTP' WHERE user_id = :u "
        "AND revoked_at IS NULL", u=client.user_id)


def clear_step_up(client):
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=client.user_id)


def attest(w):
    """I-15: an ACTIVE attestation for LE01 (no verified pack), made by the founder and approved by Chen."""
    made = ok(w["acme"].post(ATTESTATIONS, json={"legal_entity_id": w["le01"], "effective_from": "2020-01-01",
                                                 "reason": "Statutory filings are handled by our provider"}), 201)
    ok(w["checker"].post(f"{ATTESTATIONS}/{made['id']}/approve"))
    return made["id"]


def acknowledge_variance(client, run_id):
    for item in ok(client.get(f"{RUNS}/{run_id}/variance", params={"only_flagged": True}))["items"]:
        if item["pending"]:
            ok(client.post(f"{RUNS}/{run_id}/variance/{item['run_employee_id']}/acknowledge",
                           json={"comment": "Expected: verified by hand"}))


def submit(client, run_id, expect=200):
    r = client.post(f"{RUNS}/{run_id}/submit", json={})
    assert r.status_code == expect, r.text
    return r


def approve(client, run_id, expect=200):
    r = client.post(f"{RUNS}/{run_id}/approve", json={"row_version": state(run_id)["row_version"]})
    assert r.status_code == expect, r.text
    return r


def submit_and_approve(w, run_id, submitter=None, approver=None):
    submitter = submitter or w["acme"]
    acknowledge_variance(submitter, run_id)
    submit(submitter, run_id)
    approve(approver or w["approver"], run_id)
    assert state(run_id)["status"] == "APPROVED"


def finalize(client, run_id, key, expect=200, label=None):
    r = client.post(f"{RUNS}/{run_id}/finalize", headers={"Idempotency-Key": key} if key else {},
                    json={"confirm_label": label or state(run_id)["label"]})
    assert r.status_code == expect, r.text
    return r


def finalize_run(w, run_id, key):
    step_up(w["acme"])
    body = finalize(w["acme"], run_id, key).json()
    assert body["status"] == "FINALIZED"
    return body


def setting_token(client, key):
    """The concurrency token of the TENANT value of `key` (0 when nothing is saved at that scope)."""
    data = ok(client.get(SETTINGS))
    items = data["items"] if isinstance(data, dict) else data
    item = next((i for i in items if i["key"] == key), None)
    return (item or {}).get("row_version") or 0


def ytd(company_id, employee_id, bucket):
    rows = sql("SELECT sum(amount) FROM ytd_balances WHERE company_id = :c AND employee_id = :e AND bucket = :b",
               c=company_id, e=employee_id, b=bucket)
    return rows[0][0]


def give_salary(w, employee_id, group, amount, start="2026-01-01", structure="flat"):
    place(employee_id, w["le01"], group["id"])
    body = {"template_version_id": w[structure], "effective_from": start, "reason": "HIRE",
            "pay_basis_amount": {"amount": amount, "currency": group["currency"]}}
    proposed = ok(w["acme"].post(f"{PAY}/employees/{employee_id}/compensation", json=body), 201)
    return proposed["id"]


@pytest.fixture
def world(app):
    return gold_world(app, "Cycle Corp")


# ---------------------------------------------------------------------------
# Positive chain + immutability, duplicates, retries, close (EC-RUN-01, -04, -11, -17, EC-CMP-14)
# ---------------------------------------------------------------------------
def test_regular_run_lifecycle_finalize_retry_immutability_duplicate_and_close(world):
    """January 2026, FLAT: Emma annual 3,60,000 -> 30,000.00 (full month); Nora has no salary."""
    w = world
    acme, cid = w["acme"], w["cid"]
    group = gold_group(w, "PG-LC")
    emma = hire(w, "Emma", group, "360000")
    nora = create_employee(acme, "Nora")
    place(nora["id"], w["le01"], group["id"])
    attest(w)

    run_id = calculated_run(w, group, "2026-01-01")
    # EC-RUN-01: one live regular run per pay group and period.
    dup = acme.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": group["id"],
                                "pay_period_id": period_of(group, "2026-01-01")["id"]})
    assert dup.status_code == 409 and code_of(dup) == "DUPLICATE_RUN", dup.text
    # EC-CMP-14: no salary -> EXCLUDED with a reason, never a zero-pay line.
    population = ok(acme.get(f"{RUNS}/{run_id}"))["population"]
    assert population["eligible"] == population["included"] + population["excluded"]
    assert population["excluded_by_reason"] == {"NO_SALARY": 1}
    assert figures(run_id)[nora["id"]]["status"] == "EXCLUDED" and lines(run_id, nora["id"]) == ({}, {})
    assert lines(run_id, emma)[0] == {"BASIC": D("30000.00")}
    assert_run_reconciles(run_id)

    # Finalize without approval: refused.
    step_up(acme)
    r = finalize(acme, run_id, "early", expect=409)
    assert code_of(r) in ("INVALID_TRANSITION", "RUN_NOT_APPROVED"), r.text
    submit_and_approve(w, run_id)
    # Without step-up: 403; with a wrong label: 400; without a key: 400.
    clear_step_up(acme)
    assert code_of(finalize(acme, run_id, "no-step-up", expect=403)) == "STEP_UP_REQUIRED"
    step_up(acme)
    assert code_of(finalize(acme, run_id, "wrong-label", expect=400, label="Another run")) == "CONFIRMATION_MISMATCH"
    assert code_of(finalize(acme, run_id, None, expect=400)) == "IDEMPOTENCY_KEY_REQUIRED"
    first = finalize(acme, run_id, "jan-final")
    body = first.json()
    assert body["status"] == "FINALIZED"
    # EC-RUN-04: a retry after a timeout with the same key replays; nothing happens twice.
    again = finalize(acme, run_id, "jan-final")
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json() == body
    other = finalize(acme, run_id, "jan-other", expect=409)
    assert code_of(other) == "RUN_FINALIZED"
    assert audit_actions(cid, "payroll_run", run_id).count("RUN_FINALIZED") == 1
    assert sql("SELECT status FROM pay_periods WHERE id = :p", p=period_of(group, "2026-01-01")["id"])[0][0] == "CLOSED"
    assert ytd(cid, emma, "TOTAL:NET") == D("30000.00")
    assert_finalized_reconciles(run_id)

    # EC-RUN-17: a finalized run never changes; history is reopened only through the correction workflow.
    r = acme.post(f"{RUNS}/{run_id}/calculate", headers={"Idempotency-Key": "late-recalc"})
    assert r.status_code == 409, r.text
    assert acme.post(f"{RUNS}/{run_id}/precheck").status_code == 409
    r = acme.post(f"{RUNS}/{run_id}/cancel", json={"reason": "Mistake"})
    assert r.status_code == 409, r.text
    r = acme.post(f"{RUNS}/{run_id}/reopen", json={"reason": "Change a number", "target": "DRAFT"})
    assert r.status_code == 409, r.text
    assert code_of(r) in ("RUN_FINALIZED", "INVALID_TRANSITION"), r.text
    with pytest.raises(Exception):
        sql("UPDATE payroll_lines SET amount = amount + 1 WHERE run_id = :r", r=run_id)
    with pytest.raises(Exception):
        sql("UPDATE payroll_run_employees SET net = net + 1 WHERE run_id = :r AND employee_id = :e", r=run_id, e=emma)
    assert lines(run_id, emma)[0] == {"BASIC": D("30000.00")}
    # Closing with an open payment obligation is refused (Emma's 30,000.00 is not paid yet).
    r = acme.post(f"{RUNS}/{run_id}/close", json={})
    assert r.status_code == 409 and code_of(r) == "PAYMENTS_OPEN", r.text


# ---------------------------------------------------------------------------
# Second period, retro arrears, YTD, register (EC-CMP-12, EC-COR-01, EC-STA-22)
# ---------------------------------------------------------------------------
def test_backdated_revision_pays_arrears_in_the_next_run_and_ytd_adds_two_finalized_periods(world):
    """January 2026 finalized at 30,000.00 (annual 3,60,000). In February the salary is revised to 4,20,000 a year
    from 1 January (backdated): January arrears = (4,20,000 - 3,60,000) / 12 = 5,000.00, paid by the February
    regular run (payroll.arrears.payment_mode = NEXT_REGULAR_RUN). February (28 days, CALENDAR_DAYS) has 2 LOP
    days: BASIC = 35,000 x 26 / 28 = 32,500.00; gross = 32,500.00 + 5,000.00 = 37,500.00.
    YTD after the two finalized periods: TOTAL:GROSS = 30,000.00 + 37,500.00 = 67,500.00."""
    w = world
    acme, cid = w["acme"], w["cid"]
    group = gold_group(w, "PG-RETRO")
    emma = hire(w, "Emma", group, "360000")
    attest(w)
    jan = calculated_run(w, group, "2026-01-01")
    submit_and_approve(w, jan)
    finalize_run(w, jan, "retro-jan")
    jan_row = figures(jan)[emma]["row"]

    revision = give_salary(w, emma, group, "420000", start="2026-01-01")
    assert ok(w["approver"].post(f"{COMPENSATION}/{revision}/approve", json={}))["status"] == "ACTIVE"
    detected = ok(acme.post(f"{ARREARS}/detect", json={"employee_id": emma, "trigger": "COMPENSATION",
                                                         "from_date": "2026-01-01",
                                                         "reason": "Backdated revision"}))
    [item] = detected["items"]
    assert item["run_employee_id"] == jan_row
    proposal = ok(acme.get(f"{ARREARS}/{item['proposal_id']}"))
    assert {l["component_code"]: D(l["delta"]["amount"]) for l in proposal["lines"]} == {
        "BASIC": (D("420000") - D("360000")) / 12}
    assert ok(w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}))["status"] == "APPROVED"
    # The finalized January run is untouched.
    assert lines(jan, emma)[0] == {"BASIC": D("30000.00")}

    for day in ("2026-02-03", "2026-02-04"):
        lop(w, emma, day)
    feb = calculated_run(w, group, "2026-02-01")
    basic = (D("420000") / 12 * 26 / 28).quantize(D("0.01"))
    assert basic == D("32500.00")
    amounts, _ = lines(feb, emma)
    assert amounts == {"BASIC": basic, "ARREARS": D("5000.00")}, amounts
    submit_and_approve(w, feb)
    finalize_run(w, feb, "retro-feb")
    assert_finalized_reconciles(feb)
    assert ok(acme.get(f"{ARREARS}/{item['proposal_id']}"))["status"] == "SETTLED"
    assert ytd(cid, emma, "TOTAL:GROSS") == D("30000.00") + D("37500.00") == D("67500.00")
    assert ytd(cid, emma, "TOTAL:NET") == D("67500.00")
    # The payroll register for January-February reconciles to the finalized lines.
    data = ok(acme.get(f"{PAY}/reports/payroll_register/preview",
                       params={"period_from": "2026-01-01", "period_to": "2026-02-28", "pay_group_id": group["id"]}))
    assert data["totals"] == {"INR": {"gross": "67500.00", "deductions": "0.00", "reimbursements": "0.00",
                                      "net": "67500.00", "employer_cost": "67500.00"}}, data["totals"]


# ---------------------------------------------------------------------------
# Reversal and off-cycle (EC-COR-04, EC-COR-06, EC-VAR-01)
# ---------------------------------------------------------------------------
def test_reversal_negates_exactly_and_off_cycle_bonus_pays_only_the_selected_component(app, world):
    """March 2026, FLAT 3,60,000 -> 30,000.00 for Emma and Ivan. The finalized run is reversed for Emma only (two
    approvers, neither the requester): the REVERSAL run carries exactly -30,000.00, so Emma's March nets add up to
    0.00 and her unpaid instruction is cancelled. An off-cycle BONUS run for April pays Ivan's approved
    7,500.00 bonus and nothing else; the regular April run then pays BASIC 30,000.00 only (the input is
    reserved by the off-cycle run, never paid twice)."""
    w = world
    acme = w["acme"]
    bob = login_as(app, acme, create_employee(acme, "Bob", "Second"), ["payroll_approver"])
    group = gold_group(w, "PG-REV")
    emma = hire(w, "Emma", group, "360000")
    ivan = hire(w, "Ivan", group, "360000")
    attest(w)
    march = calculated_run(w, group, "2026-03-01")
    submit_and_approve(w, march)
    finalize_run(w, march, "rev-mar")
    emma_row = figures(march)[emma]["row"]

    created = ok(acme.post(f"{RUNS}/{march}/reverse", json={"reason": "Paid in the wrong entity",
                                                            "run_employee_ids": [emma_row]}), 201)
    assert acme.post(f"{REVERSALS}/{created['id']}/approve", json={}).status_code == 403  # the requester
    ok(w["approver"].post(f"{REVERSALS}/{created['id']}/approve", json={}))
    second = ok(bob.post(f"{REVERSALS}/{created['id']}/approve", json={}))
    reversal = second["reversal_run_id"]
    assert reversal and ok(acme.get(f"{RUNS}/{reversal}"))["run_type"] == "REVERSAL"
    assert lines(reversal, emma)[0] == {"BASIC": D("-30000.00")}
    assert ivan not in {e for e, f in figures(reversal).items() if f["status"] == "CALCULATED"}
    submit_and_approve(w, reversal)
    finalize_run(w, reversal, "rev-final")
    net_after = sum(D(n) for (n,) in sql(
        "SELECT e.net FROM payroll_run_employees e JOIN payroll_runs r ON r.id = e.run_id WHERE e.employee_id = :e "
        "AND r.pay_period_id = :p AND e.status = 'CALCULATED'", e=emma, p=period_of(group, "2026-03-01")["id"]))
    assert net_after == D("0.00")
    assert sql("SELECT count(*) FROM payment_obligations WHERE run_id = :r", r=reversal)[0][0] == 0
    # Reversals keep reconciliation (Goal 2 §9): the reversal run's own identities hold (net -30,000.00 =
    # earnings -30,000.00), no obligation for a negative net, liabilities = Σ statutory lines (none here).
    assert_run_reconciles(reversal)
    assert_finalized_reconciles(reversal, expect_payslips=False)
    assert_finalized_reconciles(march)
    statuses = {s for (s,) in sql("SELECT i.status FROM payment_instructions i JOIN payment_obligations o "
                                  "ON o.id = i.obligation_id WHERE o.run_employee_id = :r", r=emma_row)}
    assert statuses <= {"CANCELLED"}, statuses
    # Reversal runs are not reversed again, and Emma is not reversed twice.
    r = acme.post(f"{RUNS}/{march}/reverse", json={"reason": "Again", "run_employee_ids": [emma_row]})
    assert r.status_code == 409, r.text

    april = period_of(group, "2026-04-01")
    wage_input(w, ivan, "BONUS", "7500.00", april)
    bonus_run = ok(acme.post(OFF_CYCLE, json={"kind": "BONUS", "reason": "Quarterly bonus", "pay_group_id": group["id"],
                                              "pay_period_id": april["id"], "employee_ids": [ivan],
                                              "component_codes": ["BONUS"]}), 201)
    assert ok(acme.post(f"{RUNS}/{bonus_run['id']}/precheck"))["status"] == "READY"
    assert acme.post(f"{RUNS}/{bonus_run['id']}/calculate", headers={"Idempotency-Key": "bonus-1"}).status_code == 202
    assert state(bonus_run["id"])["status"] == "REVIEW_REQUIRED"
    assert lines(bonus_run["id"], ivan)[0] == {"BONUS": D("7500.00")}
    assert set(figures(bonus_run["id"])) == {ivan}
    regular = calculated_run(w, group, "2026-04-01")
    assert lines(regular, ivan)[0] == {"BASIC": D("30000.00")}
    assert lines(regular, emma)[0] == {"BASIC": D("30000.00")}
    for run_id in (march, reversal, bonus_run["id"], regular):
        assert_run_reconciles(run_id)


# ---------------------------------------------------------------------------
# Separation of duties, own pay, permission revoked mid-process, concurrent approvals (EC-RUN-05/06/08/21)
# ---------------------------------------------------------------------------
def test_separation_of_duties_own_pay_revoked_permission_and_concurrent_approvals(app, world):
    w = world
    acme = w["acme"]
    sam = login_as(app, acme, create_employee(acme, "Sam", "Both"), ["payroll_processor", "payroll_approver"])
    bob = login_as(app, acme, create_employee(acme, "Bob", "Second"), ["payroll_approver"])
    group = gold_group(w, "PG-SOD")
    hire(w, "Emma", group, "360000")
    ava_employee = w["approver"].employee["id"]
    revision = give_salary(w, ava_employee, group, "480000")
    assert ok(bob.post(f"{COMPENSATION}/{revision}/approve", json={}))["status"] == "ACTIVE"

    run_id = calculated_run(w, group, "2026-05-01", client=sam)
    acknowledge_variance(sam, run_id)
    submit(sam, run_id)
    # Sam calculated and submitted the run: Sam may not approve it although Sam holds the approve right.
    r = approve(sam, run_id, expect=403)
    assert code_of(r) == "SEPARATION_OF_DUTIES"
    # Ava is paid by this run (EC-RUN-06): her approval moves the run on, but her own pay line needs another
    # approver, and finalization waits for it.
    ava_row = figures(run_id)[ava_employee]["row"]
    body = approve(w["approver"], run_id).json()
    assert state(run_id)["status"] == "APPROVED" and body["self_line_pending"] == [ava_row], body
    r = w["approver"].post(f"{RUNS}/{run_id}/employees/{ava_row}/approve-self-line", json={})
    assert r.status_code == 403 and code_of(r) == "SELF_PAY_APPROVAL", r.text
    attest(w)
    step_up(acme)
    r = finalize(acme, run_id, "own-pay-1", expect=409)
    assert code_of(r) == "SELF_LINE_APPROVAL_PENDING", r.text
    # Bob loses the approver role mid-process: the right is checked when he acts (403), not when he was invited.
    ok(acme.put(f"/api/v1/users/{bob.user_id}/roles", json={"role_ids": []}))
    r = bob.post(f"{RUNS}/{run_id}/employees/{ava_row}/approve-self-line", json={})
    assert r.status_code in (401, 403), r.text
    cleo = login_as(app, acme, create_employee(acme, "Cleo", "Third"), ["payroll_approver"])
    ok(cleo.post(f"{RUNS}/{run_id}/employees/{ava_row}/approve-self-line", json={}))
    assert finalize(acme, run_id, "own-pay-2").json()["status"] == "FINALIZED"
    assert_finalized_reconciles(run_id)

    # Concurrent approvals by two different approvers: exactly one decision wins (EC-RUN-08).
    group2 = gold_group(w, "PG-RACE")
    hire(w, "Ivan", group2, "360000")
    race = calculated_run(w, group2, "2026-05-01")
    acknowledge_variance(acme, race)
    submit(acme, race)
    version = state(race)["row_version"]
    barrier = threading.Barrier(2)
    results = []

    def attempt(client):
        barrier.wait()
        response = client.post(f"{RUNS}/{race}/approve", json={"row_version": version})
        results.append((response.status_code, response.json().get("code")))

    threads = [threading.Thread(target=attempt, args=(c,)) for c in (w["approver"], cleo)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s for s, _ in results) == [200, 409], results
    assert state(race)["status"] == "APPROVED"
    decisions = sql("SELECT count(*) FROM payroll_approvals WHERE run_id = :r AND decision = 'APPROVED' "
                    "AND run_employee_id IS NULL", r=race)
    assert decisions[0][0] == 1


# ---------------------------------------------------------------------------
# Stale after a settings change; calculate retries; finalize guards (EC-RUN-02, EC-RUN-09, EC-CFG-14)
# ---------------------------------------------------------------------------
def test_settings_change_after_lock_makes_the_run_stale_and_calculate_retries_replay(world):
    """October 2026 (the current period, so a setting changed today applies to it)."""
    w = world
    acme = w["acme"]
    group = gold_group(w, "PG-STALE")
    emma = hire(w, "Emma", group, "360000")
    attest(w)
    period = period_of(group, "2026-10-01")
    run = ok(acme.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": group["id"],
                                   "pay_period_id": period["id"]}), 201)
    run_id = run["id"]
    assert ok(acme.post(f"{RUNS}/{run_id}/precheck"))["status"] == "READY"
    first = acme.post(f"{RUNS}/{run_id}/calculate", headers={"Idempotency-Key": "calc-once"})
    assert first.status_code == 202, first.text
    # EC-RUN-02: the same key replays the same job; nothing is calculated twice.
    again = acme.post(f"{RUNS}/{run_id}/calculate", headers={"Idempotency-Key": "calc-once"})
    assert again.status_code == 202 and again.json() == first.json()
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert sql("SELECT count(*) FROM jobs WHERE type = 'payroll.calculate_run' AND parent_job_id IS NULL "
               "AND (payload->>'run_id')::int = :r", r=run_id)[0][0] == 1
    assert state(run_id)["status"] == "REVIEW_REQUIRED" and lines(run_id, emma)[0] == {"BASIC": D("30000.00")}

    # A payroll setting changed through the settings API after the inputs were locked marks the run stale.
    change = {"key": "attendance.grace_minutes", "value": 10, "reason": "Golden test change",
              "row_version": setting_token(acme, "attendance.grace_minutes")}
    result = acme.put(SETTINGS, json={"changes": [change]})
    assert result.status_code == 200, result.text
    assert state(run_id)["stale"] is True
    acknowledge_variance(acme, run_id)
    r = submit(acme, run_id, expect=409)
    assert code_of(r) == "RUN_STALE"
    # Recalculating clears it; then approval and finalization go through.
    assert acme.post(f"{RUNS}/{run_id}/calculate", headers={"Idempotency-Key": "calc-after-change"}).status_code == 202
    assert state(run_id)["stale"] is False and state(run_id)["status"] == "REVIEW_REQUIRED"
    submit_and_approve(w, run_id)
    # A change after approval: finalize refuses a stale run.
    change = {"key": "attendance.grace_minutes", "value": 12, "reason": "Second change",
              "row_version": setting_token(acme, "attendance.grace_minutes")}
    r = acme.put(SETTINGS, json={"changes": [change]})
    assert r.status_code == 200 and r.json()["changes"][0]["status"] == "ACTIVE", r.text
    step_up(acme)
    r = finalize(acme, run_id, "stale-final", expect=409)
    assert code_of(r) == "RUN_STALE"
    assert state(run_id)["status"] == "APPROVED"
    assert sql("SELECT count(*) FROM payment_obligations WHERE run_id = :r", r=run_id)[0][0] == 0


def test_source_record_change_after_calculation_is_caught_at_submit_and_paid_after_recalculation(world):
    """Change detection (phase-4 follow-up): June 2026, FLAT 3,60,000 -> BASIC 30,000.00. After the run is
    calculated, a BONUS of 1,000.00 is approved for June. Submitting the calculated figures would approve pay
    that ignores an approved input: 409 RUN_STALE. After recalculation: 30,000.00 + 1,000.00 = 31,000.00, and the
    run goes through approval and finalization with the bonus paid exactly once."""
    w = world
    acme = w["acme"]
    group = gold_group(w, "PG-SRC")
    emma = hire(w, "Emma", group, "360000")
    attest(w)
    run_id = calculated_run(w, group, "2026-06-01")
    assert lines(run_id, emma)[0] == {"BASIC": D("30000.00")}
    wage_input(w, emma, "BONUS", "1000.00", period_of(group, "2026-06-01"))
    acknowledge_variance(acme, run_id)
    r = submit(acme, run_id, expect=409)
    assert code_of(r) == "RUN_STALE", r.text
    assert state(run_id)["status"] != "APPROVAL_PENDING"
    assert acme.post(f"{RUNS}/{run_id}/calculate", headers={"Idempotency-Key": "src-recalc"}).status_code == 202
    assert lines(run_id, emma)[0] == {"BASIC": D("30000.00"), "BONUS": D("1000.00")}
    assert figures(run_id)[emma]["net"] == D("30000.00") + D("1000.00") == D("31000.00")
    submit_and_approve(w, run_id)
    finalize_run(w, run_id, "src-final")
    assert_finalized_reconciles(run_id)
    assert sql("SELECT amount FROM payment_obligations WHERE run_id = :r", r=run_id) == [(D("31000.00"),)]
    assert sql("SELECT status FROM wage_inputs WHERE employee_id = :e", e=emma) == [("CONSUMED",)]
