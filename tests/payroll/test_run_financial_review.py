"""Adversarial review of the pay-run lifecycle through the financial-correctness lens (3.5 §5.8, §6, §9, §10).

EC-RUN-09 (a finalization changes what other locked runs froze: they become stale), EC-RUN-19 / EC-RUN-24 (run
totals = Σ employee totals = Σ lines, per currency; a FULL hold is never paid in the run, so it is never in its
totals), EC-VAR-01 / EC-VAR-08 (exactly-once consumption through the real chain), EC-STA-02 / I-15 (no pack:
an approved attestation), EC-SEC-01 (stale marks never reach another tenant or another legal entity).

Every expected amount is worked out by hand in the test (never taken from the engine).
"""

from decimal import Decimal

from tests.helpers import create_employee
from tests.payroll.test_compensation import login_as
from tests.payroll.test_components import ok
from tests.payroll.test_run_calculation import (  # noqa: F401  (calc_world, standin are fixtures)
    calc_world,
    calculated,
    findings,
    rows_of,
    run_state,
    standin,
)
from tests.payroll.test_run_finalize import (  # noqa: F401  (fin_world is a fixture)
    ATTESTATIONS,
    _claim,
    _wage_input,
    approve,
    calculate as standin_calculate,
    code_of,
    fin_world,
    finalize,
    submit,
    ytd,
)
from tests.payroll.test_runs_lifecycle import RUNS, new_run, run_employees, runs_world  # noqa: F401
from tests.payroll.test_wage_inputs import set_setting, sql

D = Decimal


def _stale(run_id):
    stale, reasons = sql("SELECT stale, stale_reasons FROM payroll_runs WHERE id = :r", r=run_id)[0]
    return stale, reasons


def _hold(client, run_id, row_id, hold_type="FULL", expect=200):
    r = client.post(f"{RUNS}/{run_id}/employees/{row_id}/hold",
                    json={"hold_type": hold_type, "reason_code": "DISPUTE", "note": "Under review"})
    assert r.status_code == expect, r.text
    return r.json()


def _release(client, run_id, row_id, expect=200):
    r = client.post(f"{RUNS}/{run_id}/employees/{row_id}/release", json={"note": "Resolved"})
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------------------
# A FULL hold is calculated but never paid in the run: it is never in the run's totals (3.5 §9, EC-RUN-24)
# ---------------------------------------------------------------------------
def test_ec_run_24_full_hold_is_kept_out_of_the_run_totals_the_approvers_approve(calc_world):
    """3.3 §11 Example A, CTC 12,00,000 each (by hand): BASIC 50,000.00 + HRA 20,000.00 + FIXED_ALLOWANCE
    27,000.00 (1,00,000 − 50,000 − 20,000 − 3,000 employer PF inside the CTC) = gross = net = 97,000.00.

    Ivan is on a FULL hold before calculation: calculated (his figures exist) but not finalized in this run, so
    the run totals hold Emma alone: 97,000.00, one employee. Releasing him before submission brings him back
    (1,94,000.00, two employees); holding him again after calculation removes him again."""
    w = calc_world
    acme, run = w["acme"], w["run"]
    _hold(acme, run, w["ivan_row"])
    calculated(w)
    rows = rows_of(run)
    assert rows[w["ivan"]][1] == "ON_HOLD" and rows[w["ivan"]][4] == D("97000")      # calculated, on hold
    one = {"gross": "97000.00", "deductions": "0.00", "reimbursements": "0.00", "net": "97000.00",
           "employer_cost": "97000.00", "employees": 1}
    assert run_state(run)[2] == {"INR": one}
    assert ok(acme.get(f"{RUNS}/{run}/summary"))["totals"] == {"INR": one}
    assert not [f for f in findings(run) if f[0] == "RUN_TOTALS_MISMATCH"]       # lines reconcile (EC-RUN-24)

    version = run_state(run)[1]
    _release(acme, run, w["ivan_row"])
    two = {"gross": "194000.00", "deductions": "0.00", "reimbursements": "0.00", "net": "194000.00",
           "employer_cost": "194000.00", "employees": 2}
    status, after, totals = run_state(run)[:3]
    assert status == "REVIEW_REQUIRED" and totals == {"INR": two} and after > version   # totals changed: new version
    assert ok(acme.get(f"{RUNS}/{run}/summary"))["totals"] == {"INR": two}

    _hold(acme, run, w["ivan_row"])
    assert run_state(run)[2] == {"INR": one}
    assert ok(acme.get(f"{RUNS}/{run}/summary"))["totals"] == {"INR": one}


def test_full_hold_cannot_be_released_into_a_submitted_or_approved_run(app, calc_world):
    """The approvers approve the run without the held employee: releasing a FULL hold after submission would
    pay an amount nobody approved. 409 RUN_BUSY (reject or reopen first); the totals stay as approved."""
    w = calc_world
    acme, run = w["acme"], w["run"]
    _hold(acme, run, w["ivan_row"])
    calculated(w)
    submit(acme, run)
    assert run_state(run)[0] == "APPROVAL_PENDING"
    assert code_of(_release(acme, run, w["ivan_row"], expect=409)) == "RUN_BUSY"
    approve(w["approver"], run)
    assert run_state(run)[0] == "APPROVED"
    assert code_of(_release(acme, run, w["ivan_row"], expect=409)) == "RUN_BUSY"
    assert rows_of(run)[w["ivan"]][1] == "ON_HOLD"
    assert run_state(run)[2]["INR"]["net"] == "97000.00" and run_state(run)[2]["INR"]["employees"] == 1


# ---------------------------------------------------------------------------
# EC-RUN-09: finalizing a run makes the other locked runs of its employees stale
# ---------------------------------------------------------------------------
def _approved(w, run_id):
    standin_calculate(w["acme"], run_id)
    submit(w["acme"], run_id)
    approve(w["approver"], run_id)
    assert run_state(run_id)[0] == "APPROVED"


def test_ec_run_09_finalizing_a_run_makes_other_locked_runs_of_its_employees_stale(fin_world):
    """The regular run (Emma, Ivan) and an off-cycle run for Emma are both locked and approved. Finalizing the
    off-cycle run changes Emma's year-to-date figures (and previous period, pack state, loan balances) that the
    regular run froze, so the regular run is stale: finalizing it from the out-of-date snapshot is refused
    (409 RUN_STALE). An off-cycle run of Ivan only, another tenant and another legal entity are untouched; the
    YTD of the finalized run is written once."""
    w = fin_world
    acme, cid = w["acme"], w["cid"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    regular = new_run(acme, w["group"], w["period"]["id"])
    bonus = new_run(acme, w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[emma])
    other = new_run(acme, w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[ivan])
    for run in (regular, bonus, other):
        _approved(w, run["id"])
    beta = w["beta"]
    beta_run = new_run(beta["client"], beta["group"], beta["period"]["id"])
    sql("UPDATE payroll_runs SET status = 'REVIEW_REQUIRED' WHERE id = :r", r=beta_run["id"])

    body = finalize(acme, bonus["id"], key="bonus-1").json()
    assert body["status"] == "FINALIZED"
    stale, reasons = _stale(regular["id"])
    assert stale is True
    reason = reasons[-1]
    assert reason["source"] == "finalization" and reason["finalized_run_id"] == bonus["id"]
    assert reason["employee_ids"] == [emma]
    assert code_of(finalize(acme, regular["id"], key="regular-1", expect=409)) == "RUN_STALE"
    assert run_state(regular["id"])[0] == "APPROVED"
    assert _stale(other["id"])[0] is False                     # Ivan's off-cycle run froze nothing of Emma's
    assert _stale(beta_run["id"])[0] is False                  # EC-SEC-01: never another tenant
    assert _stale(bonus["id"])[0] is False                     # the finalized run itself is not marked
    # Emma's YTD holds the off-cycle run once (its net by hand: the PLAIN fixture snapshot of the stand-in).
    bonus_net = sql("SELECT net FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e",
                    r=bonus["id"], e=emma)[0][0]
    assert ytd(cid, emma, "TOTAL:NET") == bonus_net
    again = finalize(acme, bonus["id"], key="bonus-1")
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert ytd(cid, emma, "TOTAL:NET") == bonus_net
    assert len(_stale(regular["id"])[1]) == len(reasons)       # a replay marks nothing again


# ---------------------------------------------------------------------------
# Golden run through the real chain, every figure by hand (EC-RUN-24, EC-VAR-01, EC-STA-02 / I-15)
# ---------------------------------------------------------------------------
def test_golden_real_chain_bonus_and_claim_are_paid_once_and_totals_reconcile_by_hand(app, fin_world):
    """Real snapshot builder, real calculation job, real finalization. LE01 has no country pack (GB): an
    approved maker-checker attestation lets the run finalize without statutory lines (I-15).

    By hand (3.3 §11 Example A, CTC 12,00,000, no employer contribution inside the CTC without a pack):
        Emma: BASIC 50,000.00 + HRA 20,000.00 + FIXED_ALLOWANCE 30,000.00 + BONUS (approved wage input)
              5,000.00 = gross 1,05,000.00; REIMBURSEMENT (approved travel claim) 300.00; deductions 0;
              net = 1,05,000.00 + 300.00 − 0 = 1,05,300.00; employer cost = gross = 1,05,000.00.
        Ivan: gross = net = employer cost = 1,00,000.00.
        Run (INR): gross 2,05,000.00, reimbursements 300.00, net 2,05,300.00, employer cost 2,05,000.00, 2 employees.
    After finalization the bonus is CONSUMED by Emma's run employee, the claim PAID, each line source consumed
    once, YTD written once per bucket; an off-cycle run for Emma afterwards pays neither again, and its salary
    for the period (already paid by the regular run) is stopped by ALREADY_PAID_IN_PERIOD."""
    from tests.payroll.test_run_calculation import calculate as calculate_job
    from tests.payroll.test_run_calculation import lines_of, no_pack_country, precheck_ready

    w = fin_world
    acme, cid = w["acme"], w["cid"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    no_pack_country(w)
    set_setting(cid, "payroll.attendance.enabled", False)
    bonus = _wage_input(w, emma, "5000.00")
    claim = _claim(w, emma, "300.00")
    attestation = ok(acme.post(ATTESTATIONS, json={"legal_entity_id": w["le01"], "effective_from": "2020-01-01",
                                                   "reason": "Statutory filings by our external provider"}), 201)
    checker = login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))

    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    calculate_job(acme, run["id"])
    assert run_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])
    rows = rows_of(run["id"])
    emma_row, ivan_row = rows[emma][0], rows[ivan][0]
    assert {k: v for k, v in lines_of(emma_row).items()} == {
        "BASIC": (D("50000.00"), "EARNING"), "HRA": (D("20000.00"), "EARNING"),
        "FIXED_ALLOWANCE": (D("30000.00"), "EARNING"), "BONUS": (D("5000.00"), "EARNING"),
        "REIMBURSEMENT": (D("300.00"), "REIMBURSEMENT")}
    assert rows[emma][1:5] == ("CALCULATED", D("105000"), D("0"), D("105300"))
    assert rows[emma][5] == D("105000")
    assert rows[ivan][1:6] == ("CALCULATED", D("100000"), D("0"), D("100000"), D("100000"))
    assert run_state(run["id"])[2] == {"INR": {"gross": "205000.00", "deductions": "0.00", "reimbursements": "300.00",
                                              "net": "205300.00", "employer_cost": "205000.00", "employees": 2}}
    # Σ lines = Σ employee totals (EC-RUN-24), computed from the stored lines independently of the service.
    by_kind = dict(sql("SELECT kind, sum(amount) FROM payroll_lines WHERE run_id = :r GROUP BY kind", r=run["id"]))
    assert by_kind == {"EARNING": D("205000"), "REIMBURSEMENT": D("300")}
    assert by_kind["EARNING"] + by_kind["REIMBURSEMENT"] == D("205300")
    assert sql("SELECT status, reserved_by_run_employee_id FROM wage_inputs WHERE id = :i", i=bonus)[0] == (
        "APPROVED", emma_row)                                   # reserved at lock, consumed only at finalization

    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    body = finalize(acme, run["id"], key="golden-1").json()
    assert body["status"] == "FINALIZED"
    assert sql("SELECT status, consumed_by_run_employee_id FROM wage_inputs WHERE id = :i", i=bonus)[0] == (
        "CONSUMED", emma_row)
    assert sql("SELECT status FROM reimbursement_claims WHERE id = :c", c=claim)[0][0] == "PAID"
    assert sorted(sql("SELECT source_type, source_id, consumed FROM payroll_line_sources WHERE run_id = :r",
                      r=run["id"])) == sorted([("CLAIM", claim, True), ("WAGE_INPUT", bonus, True)])
    expected_ytd = {"BASIC": D("50000"), "HRA": D("20000"), "FIXED_ALLOWANCE": D("30000"), "BONUS": D("5000"),
                    "REIMBURSEMENT": D("300"), "TOTAL:GROSS": D("105000"), "TOTAL:REIMBURSEMENTS": D("300"),
                    "TOTAL:NET": D("105300"), "TOTAL:EMPLOYER_COST": D("105000")}
    assert dict(sql("SELECT bucket, amount FROM ytd_balances WHERE employee_id = :e", e=emma)) == expected_ytd
    assert ytd(cid, ivan, "TOTAL:NET") == D("100000")
    assert sql("SELECT count(*) FROM ytd_balances WHERE employee_id = :e", e=w["emp"]["Nora"]["id"])[0][0] == 0
    assert sql("SELECT count(*) FROM statutory_liabilities WHERE run_id = :r", r=run["id"])[0][0] == 0

    # A retry replays; an off-cycle run for Emma afterwards pays neither the bonus nor the claim again.
    assert finalize(acme, run["id"], key="golden-1").headers.get("Idempotent-Replayed") == "true"
    assert dict(sql("SELECT bucket, amount FROM ytd_balances WHERE employee_id = :e", e=emma)) == expected_ytd
    off = new_run(acme, w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[emma])
    precheck_ready(acme, off["id"])
    calculate_job(acme, off["id"])
    off_row = run_employees(off["id"])[emma][2]
    codes = set(lines_of(off_row))
    assert "BONUS" not in codes and "REIMBURSEMENT" not in codes, codes
    # Without a component selection the off-cycle run pays the whole period again: the regular run already
    # paid Emma for it, so the salary would be paid twice. A blocking (waivable) finding stops it.
    assert run_state(off["id"])[0] == "VALIDATION_FAILED"
    paid = sql("SELECT severity, waivable, details->'runs' FROM payroll_exceptions WHERE run_id = :r "
               "AND code = 'ALREADY_PAID_IN_PERIOD' AND status = 'OPEN'", r=off["id"])
    assert paid == [("BLOCKING", True, [run["number"]])]
    assert sql("SELECT status, consumed_by_run_employee_id FROM wage_inputs WHERE id = :i", i=bonus)[0] == (
        "CONSUMED", emma_row)


# ---------------------------------------------------------------------------
# EC-RUN-24: reconciliation per employee; a superseded mismatch never blocks a corrected run
# ---------------------------------------------------------------------------
def test_ec_run_24_line_errors_that_cancel_out_are_found_per_employee_and_cleared_by_recalculating(
        calc_world, monkeypatch):
    """A defect writes Emma's BASIC line 1.00 too high and Ivan's 1.00 too low: the run's per-currency sums
    still agree (1,94,000.00 both ways), but each employee's lines no longer add up to the stored figures, so
    the run fails validation with a never-waivable RUN_TOTALS_MISMATCH naming the employees. Once the defect is
    gone, recalculating supersedes the finding and the run reaches review."""
    from app.models.pay_runs import PayrollLine
    from app.payroll.services import calculation

    w = calc_world
    original = calculation._write_lines
    delta = {w["emma"]: D("1.00"), w["ivan"]: D("-1.00")}

    def defective(db, run, row, result, catalogue):
        written = original(db, run, row, result, catalogue)
        db.query(PayrollLine).filter(PayrollLine.run_employee_id == row.id, PayrollLine.component_code == "BASIC") \
            .update({PayrollLine.amount: PayrollLine.amount + delta[row.employee_id]}, synchronize_session=False)
        return written

    monkeypatch.setattr(calculation, "_write_lines", defective)
    calculated(w, expect_status="VALIDATION_FAILED")
    mismatch = sql("SELECT severity, waivable, blocks_finalization, message FROM payroll_exceptions "
                   "WHERE run_id = :r AND code = 'RUN_TOTALS_MISMATCH' AND status = 'OPEN'", r=w["run"])
    assert len(mismatch) == 1 and mismatch[0][:3] == ("BLOCKING", False, True)
    assert f"#{w['emma_row']} gross" in mismatch[0][3] and f"#{w['ivan_row']} gross" in mismatch[0][3]
    assert sql("SELECT sum(amount) FROM payroll_lines WHERE run_id = :r", r=w["run"])[0][0] == D("194000")

    monkeypatch.setattr(calculation, "_write_lines", original)
    calculated(w)                                                # RECALCULATE -> REVIEW_REQUIRED
    assert sql("SELECT status FROM payroll_exceptions WHERE run_id = :r AND code = 'RUN_TOTALS_MISMATCH'",
               r=w["run"]) == [("FIXED",)]
    assert run_state(w["run"])[2]["INR"]["net"] == "194000.00"


def test_ec_run_09_recalculating_the_changed_employees_clears_a_change_detection_stale_mark(calc_world, monkeypatch):
    """Change detection marks the run stale for Emma's changed sources; recalculating Emma alone (the employees
    the reason names) clears it, as the processor is told. Ivan's recalculation alone does not."""
    from app.payroll.services import change_detection
    from tests.payroll.test_runs_lifecycle import principal_of

    w = calc_world
    calculated(w)
    monkeypatch.setattr(change_detection, "detect_changes", lambda db, run, full=False, sourced_only=False: {
        w["emma"]: [{"source": "wage_input:1", "type": "wage_input", "change": "UPDATED"}]})
    db, principal = principal_of(w["acme"])
    try:
        from app.payroll.services import runs

        assert change_detection.match_run(db, runs.load_run(db, w["cid"], w["run"]))["marked"] is True
        db.commit()
    finally:
        db.close()
    stale, reasons = _stale(w["run"])
    assert stale is True and reasons[-1]["employee_ids"] == [w["emma"]]
    calculated(w, employee_ids=[w["ivan"]])
    assert _stale(w["run"])[0] is True
    calculated(w, employee_ids=[w["emma"]])
    assert _stale(w["run"]) == (False, [])


# ---------------------------------------------------------------------------
# EC-RUN-09 / EC-VAR-06: a loan changed after lock is never recovered at the frozen instalment
# ---------------------------------------------------------------------------
def test_ec_run_09_ec_var_06_loan_paused_after_lock_blocks_finalization_until_recalculated(app, fin_world):
    """Emma's loan of 60,000.00 over 6 instalments at the loan policy's interest (the EMI test_loans works out,
    10,352.90): the snapshot freezes that instalment, so her net is 1,00,000.00 − 10,352.90 = 89,647.10. The loan is
    paused (approved) after the run was approved. Finalizing from the frozen snapshot would recover a paused loan:
    409 RUN_STALE (the full change-detection gate, p4-followups item 2; the run is marked stale) and nothing is
    recovered. Reopened and Emma recalculated, the paused loan recovers nothing (net 1,00,000.00) and the run
    finalizes with no instalment."""
    from tests.payroll.test_loans import STAFF
    from tests.payroll.test_run_calculation import calculate as calculate_job
    from tests.payroll.test_run_calculation import lines_of, no_pack_country, precheck_ready
    from tests.payroll.test_run_finalize import _loan

    w = fin_world
    acme, cid = w["acme"], w["cid"]
    emma = w["emp"]["Emma"]["id"]
    no_pack_country(w)
    set_setting(cid, "payroll.attendance.enabled", False)
    loan = _loan(app, w, emma)
    attestation = ok(acme.post(ATTESTATIONS, json={"legal_entity_id": w["le01"], "effective_from": "2020-01-01",
                                                   "reason": "Statutory filings by our external provider"}), 201)
    checker = login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))

    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    calculate_job(acme, run["id"])
    assert run_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])
    emma_row = rows_of(run["id"])[emma][0]
    assert lines_of(emma_row)["LOAN_RECOVERY"] == (D("10352.90"), "DEDUCTION")
    assert rows_of(run["id"])[emma][4] == D("100000") - D("10352.90") == D("89647.10")

    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    r = w["processor"].post(f"{STAFF}/{loan}/pause", json={"reason": "Medical leave"})
    assert r.status_code == 202, r.text
    assert acme.post(f"{STAFF}/{loan}/approve", json={}).status_code == 200
    assert sql("SELECT status FROM loans WHERE id = :l", l=loan)[0][0] == "PAUSED"

    assert code_of(finalize(acme, run["id"], key="loan-1", expect=409)) == "RUN_STALE"
    assert sql("SELECT stale FROM payroll_runs WHERE id = :r", r=run["id"])[0][0] is True
    assert run_state(run["id"])[0] == "APPROVED"
    assert sql("SELECT count(*) FROM loan_transactions WHERE loan_id = :l AND kind = 'INSTALMENT'", l=loan)[0][0] == 0
    assert sql("SELECT count(*) FROM ytd_balances WHERE employee_id = :e", e=emma)[0][0] == 0

    ok(acme.post(f"{RUNS}/{run['id']}/reopen", json={"reason": "Loan paused after lock",
                                                       "target": "REVIEW_REQUIRED"}))
    calculate_job(acme, run["id"], employee_ids=[emma])
    assert run_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])
    assert "LOAN_RECOVERY" not in lines_of(emma_row) and rows_of(run["id"])[emma][4] == D("100000")
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    assert finalize(acme, run["id"], key="loan-2").json()["status"] == "FINALIZED"
    assert sql("SELECT count(*) FROM loan_transactions WHERE loan_id = :l AND kind = 'INSTALMENT'", l=loan)[0][0] == 0
    assert ytd(cid, emma, "TOTAL:NET") == D("100000")
