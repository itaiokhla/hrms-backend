"""Incentive plans end to end (3.1 §5.9, decision I-22) and employee benefits (decision I-21, EC-CMP-24).

Through the real APIs on the settings world (conftest.py: Verity Alpha, GBP, BASIC = 1,000.00 x the period's
calendar days; Verity Beta is the bystander tenant). Expected amounts are written out by hand.

* Measures: the API rules of an achievement measure (switch, plan, component, value), the CSV import column, and
  tenant isolation (another tenant can neither create nor read them).
* Exactly once: a measure added after the run locked makes the run stale at the submit gate; finalization
  consumes every paid measure once (payroll_line_sources + CONSUMED), a second consumption is refused, and the
  consumed measure never reaches another snapshot.
* Payout window: a QUARTERLY plan pays only in the period that ends the quarter; in other periods the measures stay
  approved and unreserved for the payout run.
* Benefits (I-21): no benefit switch or policy exists in the design; a BENEFIT_IN_KIND component and an
  EMPLOYER_CONTRIBUTION of category BENEFIT stay out of cash gross and net, appear on the payslip and count in the
  CTC breakdown, and the employer contribution counts in employer cost.
"""

from decimal import Decimal

from tests.helpers import create_employee
from tests.payroll.test_compensation import assign_and_approve
from tests.payroll.test_components import add_component, make_structure, active_version_id
from tests.payroll.test_settings_engine import sql
from tests.payroll.verification.conftest import (
    PAY,
    RUNS,
    activate_policy,
    calculate,
    create_run,
    employee_lines,
    employee_rows,
    idem,
    monthly_basic,
    ok,
    place,
    precheck,
    put_setting,
    resolve,
    run_detail,
)
from tests.payroll.verification.test_settings_onoff import (
    REASON,
    _finalized,
    _payslip,
    amount,
    describe,
    fresh_run,
    lines_of,
    measure,
    switch,
)

D = Decimal
KEY = "payroll.incentives.enabled"


def gbp(value):
    return {"amount": value, "currency": "GBP"}


def plan_on(w, formula="inc.measure * 10", **fields):
    activate_policy(w["admin"], "INCENTIVE_PLAN", {"formula": formula, **fields})
    switch(w, KEY, True)


def finalize_existing(w, run_id, entity_code):
    """Submit, approve and finalize a calculated run (attestation as in the harness's ``_finalized``)."""
    from tests.payroll.test_run_finalize import ATTESTATIONS, approve, finalize, step_up, submit
    from tests.payroll.verification.conftest import second_admin

    if entity_code not in w.setdefault("attested", set()):
        attestation = ok(w["admin"].post(ATTESTATIONS, json={
            "legal_entity_id": w["entities"][entity_code], "effective_from": "2020-01-01",
            "reason": "Statutory filings handled outside this system"}), 201)
        ok(second_admin(w).post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
        w["attested"].add(entity_code)
    submit(w["admin"], run_id)
    approve(w["approver"], run_id)
    step_up(w["admin"])
    finalize(w["admin"], run_id, key=f"fin-{run_id}")


# ---------------------------------------------------------------------------
# Measures: API rules, CSV import, tenant isolation
# ---------------------------------------------------------------------------
def test_measure_rules_import_and_tenant_isolation(settings_world):
    """Measures (I-22): switch and plan gates, value rules, CSV import (EC-VAR-16) and cross-tenant refusal
    (another tenant's employee and input are 404)."""
    w, beta = settings_world["alpha"], settings_world["beta"]
    # ON without an active plan (the starter plan is a draft): refused, never assumed.
    switch(w, KEY, True)
    assert measure(w, "Ann", "10", expect=409)["code"] == "INCENTIVE_PLAN_MISSING"
    activate_policy(w["admin"], "INCENTIVE_PLAN", {"formula": "inc.measure * 10"})
    assert measure(w, "Ann", "10", component="BONUS", expect=400)["code"] == "INCENTIVE_COMPONENT_MISMATCH"
    assert measure(w, "Ann", "0", expect=400)["code"] == "MEASURE_NOT_POSITIVE"
    assert measure(w, "Ann", "1000000", expect=400)["code"] == "MEASURE_TOO_LARGE"
    body = {"employee_id": w["emp"]["Ann"], "component_code": "INCENTIVE", "period_start": w["start"].isoformat(),
            "period_end": w["end"].isoformat(), "measure": "10"}
    for extra in ({"amount": gbp("10.00")}, {"units": "3"}, {"rate": "2"}, {"recurrence": {"until": "2099-01-01"}}):
        r = w["admin"].post(f"{PAY}/wage-inputs", json={**body, **extra}, headers=idem())
        assert r.status_code == 422, (extra, r.text)
    # Manual wage inputs OFF does not govern measures (their own switch does).
    switch(w, "payroll.wage_inputs.enabled", False)
    item = measure(w, "Ann", "12.5", approve=False)
    detail = ok(w["admin"].get(f"{PAY}/wage-inputs/{item['id']}"))
    assert (detail["source"], D(detail["units"]), detail["amount"]) == ("INCENTIVE_PLAN", D("12.5"), None), detail
    # CSV import: the measure column creates an INCENTIVE_PLAN input; a measure row with an amount is refused.
    code = w["employees"]["Ben"]["employee_code"]
    s, e = w["start"].isoformat(), w["end"].isoformat()
    header = "employee_code,component_code,amount,units,rate,currency,period_start,period_end,external_ref,reason,measure"
    bad = f"{header}\n{code},INCENTIVE,5.00,,,,{s},{e},M-1,,40\n".encode()
    r = w["admin"].post(f"{PAY}/wage-inputs/import", files={"file": ("m.csv", bad, "text/csv")}, headers=idem())
    assert r.status_code == 201 and r.json()["status"] == "INVALID", r.text
    assert "no amount, units or rate" in " ".join(r.json()["rows"][0]["errors"])
    good = f"{header}\n{code},INCENTIVE,,,,,{s},{e},M-1,Imported achievement,40\n".encode()
    r = w["admin"].post(f"{PAY}/wage-inputs/import", files={"file": ("m.csv", good, "text/csv")}, headers=idem())
    assert r.status_code == 201 and r.json()["status"] == "VALIDATED", r.text
    committed = ok(w["admin"].post(f"{PAY}/wage-input-imports/{r.json()['id']}/commit", headers=idem()), 202)
    assert ok(w["admin"].get(f"/api/v1/jobs/{committed['job_id']}"))["status"] == "SUCCEEDED"
    listed = ok(w["admin"].get(f"{PAY}/wage-inputs", params={"employee_id": w["emp"]["Ben"]}))["items"]
    assert [(i["source"], D(i["units"]), i["status"], i["external_ref"]) for i in listed] == [
        ("INCENTIVE_PLAN", D("40"), "PENDING_APPROVAL", "M-1")]
    # Tenant isolation: Beta can neither enter a measure for Alpha's employee nor read Alpha's measure.
    r = beta["admin"].post(f"{PAY}/wage-inputs", json=body, headers=idem())
    assert r.status_code == 404, r.text                            # Alpha's employee does not exist for Beta
    switch(beta, KEY, True)                                         # even with Beta's own switch ON
    assert beta["admin"].post(f"{PAY}/wage-inputs", json=body, headers=idem()).status_code == 404
    switch(beta, KEY, False)
    assert beta["admin"].get(f"{PAY}/wage-inputs/{item['id']}").status_code == 404
    assert ok(beta["admin"].get(f"{PAY}/wage-inputs"))["total"] == 0
    assert resolve(beta["admin"], KEY, employee_id=beta["emp"]["Zed"], on=beta["end"])["value"] is False
    assert sql("SELECT count(*) FROM wage_inputs WHERE company_id = :c", c=beta["cid"])[0][0] == 0


# ---------------------------------------------------------------------------
# Change detection and exactly-once consumption (EC-VAR-01)
# ---------------------------------------------------------------------------
def test_late_measure_marks_the_run_stale_and_finalization_consumes_each_measure_once(settings_world):
    """EC-VAR-01 and EC-RUN-09 for incentive measures: a change after lock is caught at submit, and a finalized run
    consumes each paid measure exactly once."""
    from app.core.errors import AppError
    from app.database.db import SessionLocal, bind_tenant
    from app.payroll.services import wage_inputs

    w = settings_world["alpha"]
    plan_on(w, "inc.measure * 10")
    first = measure(w, "Ann", "12")
    month = monthly_basic(w["period"])
    run_id = fresh_run(w, "PG-A")
    assert amount(w, run_id, "Ann", "INCENTIVE") == D("120.00")          # 12 x 10
    # A measure approved after the inputs were locked: the submit gate finds it and refuses (409 RUN_STALE).
    late = measure(w, "Ann", "3")
    r = w["admin"].post(f"{RUNS}/{run_id}/submit", json={})
    assert r.status_code == 409 and r.json()["code"] == "RUN_STALE", r.text
    calculate(w["admin"], run_id)
    assert amount(w, run_id, "Ann", "INCENTIVE") == D("150.00")          # (12 + 3) x 10
    finalize_existing(w, run_id, "VA1")
    row = employee_rows(w["admin"], run_id)[w["emp"]["Ann"]]["id"]
    states = dict(sql("SELECT id, status FROM wage_inputs WHERE id IN (:a, :b)", a=first["id"], b=late["id"]))
    assert states == {first["id"]: "CONSUMED", late["id"]: "CONSUMED"}
    consumed_by = {r[0] for r in sql("SELECT consumed_by_run_employee_id FROM wage_inputs WHERE id IN (:a, :b)",
                                     a=first["id"], b=late["id"])}
    assert consumed_by == {row}
    sources = sql("SELECT s.source_id, s.consumed FROM payroll_line_sources s JOIN payroll_lines l ON l.id = s.line_id "
                  "WHERE l.run_employee_id = :r AND l.component_code = 'INCENTIVE' ORDER BY s.source_id", r=row)
    assert [(str(i), c) for i, c in sources] == [(str(first["id"]), True), (str(late["id"]), True)], sources
    lines, detail = lines_of(w, run_id, "Ann")
    assert lines == {"BASIC": month, "INCENTIVE": D("150.00")} and D(detail["totals"]["net"]["amount"]) == month + 150
    # Exactly once: another run employee cannot consume it, releasing leaves it consumed, and no later snapshot
    # sees it (approved_inputs is what the snapshot builder reads).
    db = SessionLocal()
    try:
        bind_tenant(db, w["cid"])
        try:
            wage_inputs.consume_inputs(db, w["cid"], row + 100000, [first["id"]])
            raise AssertionError("a consumed measure was consumed again")
        except AppError as error:
            assert error.code == "INPUT_ALREADY_CONSUMED"
        db.rollback()
        assert wage_inputs.release_inputs(db, w["cid"], [row]) == 0
        assert wage_inputs.approved_inputs(db, w["cid"], w["emp"]["Ann"], w["start"], w["end"]) == []
    finally:
        db.rollback()
        db.close()
    assert sql("SELECT status FROM wage_inputs WHERE id = :a", a=first["id"])[0][0] == "CONSUMED"


def test_quarterly_plan_pays_only_in_the_period_that_ends_the_quarter(settings_world):
    """payout_frequency QUARTERLY (I-22): measures wait, approved and unreserved, until the quarter's last period."""
    w = settings_world["alpha"]
    plan_on(w, "inc.measure * 10", payout_frequency="QUARTERLY")
    item = measure(w, "Ben", "7")
    run_id = fresh_run(w, "PG-A")
    snapshot = sql("SELECT s.content FROM payroll_input_snapshots s JOIN payroll_run_employees e ON "
                   "e.snapshot_id = s.id WHERE e.run_id = :r AND e.employee_id = :e", r=run_id, e=w["emp"]["Ben"])[0][0]
    # The pay calendar declares no tax year, so quarters are calendar quarters (Jan-Mar, Apr-Jun, ...).
    if w["end"].month in (3, 6, 9, 12):
        assert amount(w, run_id, "Ben", "INCENTIVE") == D("70.00")       # 7 x 10, paid in the quarter's last period
        assert snapshot["incentive"]["window_end"] == w["end"].isoformat()
    else:
        assert amount(w, run_id, "Ben", "INCENTIVE") is None, describe(w, run_id, "Ben")
        assert "incentive" not in snapshot and snapshot["wage_inputs"] == []
        status, reserved = sql("SELECT status, reserved_by_run_employee_id FROM wage_inputs WHERE id = :i",
                               i=item["id"])[0]
        assert (status, reserved) == ("APPROVED", None)                  # waits, unreserved, for the payout run


# ---------------------------------------------------------------------------
# Employee benefits (decision I-21, EC-CMP-24, EC-VAR-13 proration is per component)
# ---------------------------------------------------------------------------
def test_benefits_stay_out_of_cash_net_and_appear_on_payslip_and_ctc(settings_world):
    """EC-CMP-24 (decision I-21): a taxable benefit in kind is never paid in cash; an employer-paid benefit is
    employer cost only; both are listed in the CTC breakdown and neither changes gross, net or the payment."""
    w = settings_world["alpha"]
    admin = w["admin"]
    ok(add_component(admin, "CAR_BENEFIT", kind="BENEFIT_IN_KIND", category="BENEFIT", name="Company car"), 201)
    ok(add_component(admin, "HEALTH_COVER_ER", kind="EMPLOYER_CONTRIBUTION", category="BENEFIT",
                     name="Health cover (employer)"), 201)
    structure = make_structure(admin, code="WITH_BENEFITS", type_="GROSS_BASED", lines=[
        {"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly * 100%"},
        {"component": "CAR_BENEFIT", "method": "FIXED", "value": "2500"},
        {"component": "HEALTH_COVER_ER", "method": "FIXED", "value": "1200"}],
        test_range={"min": "1000", "max": "100000"})
    eve = create_employee(admin, "Eve", "Benefit", manager_id=w["manager_id"], hire_date="2024-01-15")
    place(admin, eve["id"], w["entities"]["VA2"], w["branches"]["EDI"], w["groups"]["PG-C"])
    w["emp"]["Eve"], w["employees"]["Eve"] = eve["id"], eve
    month = monthly_basic(w["period"])
    assign_and_approve(admin, w["approver"], eve["id"], active_version_id(structure), str(month),
                       effective_from=w["start"], currency="GBP")
    # CTC breakdown (3.3 §6.4): both benefits are listed with their own kind; neither is cash gross or net; the
    # employer contribution counts in the CTC total (3.5 §6: employer cost = gross + employer contributions).
    breakdown = ok(admin.get(f"{PAY}/employees/{eve['id']}/compensation"))["breakdown"]
    rows = {row["component"]: row for row in breakdown["lines"]}
    assert (rows["CAR_BENEFIT"]["kind"], rows["CAR_BENEFIT"]["monthly"]["amount"]) == ("BENEFIT_IN_KIND", "2500.00")
    assert (rows["HEALTH_COVER_ER"]["kind"], rows["HEALTH_COVER_ER"]["monthly"]["amount"]) == (
        "EMPLOYER_CONTRIBUTION", "1200.00")
    totals = {k: D(v["monthly"]["amount"]) for k, v in breakdown["totals"].items()}
    assert totals["gross"] == month and totals["net_estimate"] == month             # no benefit is cash
    assert totals["employer_contributions"] == D("1200.00") and totals["ctc"] >= month + D("1200.00")
    # The payslip shows the employer contribution in its own section when the template option is ON (06 §3 #7).
    switch(w, "payslip.show_employer_contributions", True)
    run_id = _finalized(w, "PG-C", "VA2")
    lines, detail = employee_lines(admin, run_id, eve["id"])
    kinds = {line["component"]: line["kind"] for line in detail["lines"]}
    # Run: BASIC month, CAR_BENEFIT 2,500.00 (BENEFIT_IN_KIND), HEALTH_COVER_ER 1,200.00 (EMPLOYER_CONTRIBUTION).
    assert lines == {"BASIC": month, "CAR_BENEFIT": D("2500.00"), "HEALTH_COVER_ER": D("1200.00")}, lines
    assert (kinds["CAR_BENEFIT"], kinds["HEALTH_COVER_ER"]) == ("BENEFIT_IN_KIND", "EMPLOYER_CONTRIBUTION")
    figures = {k: D(v["amount"]) for k, v in detail["totals"].items()}
    # Cash: gross = net = month (EC-CMP-24: a benefit in kind is never paid in cash; employer contributions never
    # change net). Employer cost = month + 1,200.00.
    assert (figures["gross"], figures["net"], figures["deductions"]) == (month, month, D("0.00"))
    assert figures["employer_cost"] == month + D("1200.00")
    obligation = sql("SELECT sum(o.amount) FROM payment_obligations o JOIN payroll_run_employees e "
                     "ON e.id = o.run_employee_id WHERE o.run_id = :r AND e.employee_id = :e", r=run_id, e=eve["id"])
    assert D(obligation[0][0]) == month                                       # only the cash net is paid out
    content = _payslip(w, run_id, "Eve")["content"]
    assert [i["code"] for i in content["earnings"]] == ["BASIC"]
    assert [(i["code"], i["amount"]) for i in content["employer_contributions"]] == [("HEALTH_COVER_ER", "1200.00")]
    assert content["employer_note"].startswith("Paid by your employer")
    assert (D(content["totals"]["gross"]), D(content["totals"]["net"])) == (month, month)
    sections = ("earnings", "deductions", "reimbursements", "employer_contributions")
    assert all(i["code"] != "CAR_BENEFIT" for name in sections for i in content[name])   # never shown as cash
    # No benefit switch or policy exists to turn ON or OFF (decision I-21).
    from app.payroll.policy_types import POLICY_TYPES
    from app.payroll.settings_registry import REGISTRY
    assert not [k for k in REGISTRY if "benefit" in k.lower()]
    assert not [k for k in POLICY_TYPES if "BENEFIT" in k]


# ---------------------------------------------------------------------------
# Clawback window (3.1 §5.9 clawback_window_days, EC-VAR-12)
# ---------------------------------------------------------------------------
def test_incentive_clawback_only_within_the_window_and_up_to_the_payout(settings_world):
    """EC-VAR-12 for incentives: a negative input claws back only within clawback_window_days of a finalized payout
    and never more than was paid."""
    from tests.payroll.verification.conftest import periods_of

    w = settings_world["alpha"]
    ok(add_component(w["admin"], "SALES_INCENTIVE", category="VARIABLE", method="INPUT", tags=["PART_OF_GROSS"],
                     params={"allow_negative_input": True}), 201)
    fields = {"formula": "inc.measure * 10", "component": "SALES_INCENTIVE"}
    plan_on(w, **fields)
    measure(w, "Ann", "20", component="SALES_INCENTIVE")
    run_id = _finalized(w, "PG-A", "VA1")
    assert amount(w, run_id, "Ann", "SALES_INCENTIVE") == D("200.00")       # 20 x 10, paid on the period's pay date
    nxt = next(p for p in periods_of(w["admin"], w["groups"]["PG-A"]) if p["start"] > w["period"]["end"])

    def clawback(value, expect):
        r = w["admin"].post(f"{PAY}/wage-inputs", json={
            "employee_id": w["emp"]["Ann"], "component_code": "SALES_INCENTIVE", "period_start": nxt["start"],
            "period_end": nxt["end"], "amount": gbp(value), "reason": "Order cancelled"}, headers=idem())
        assert r.status_code == expect, r.text
        return r.json()

    assert clawback("-50.00", 409)["code"] == "CLAWBACK_NOT_ALLOWED"         # clawback_window_days 0 = never
    activate_policy(w["admin"], "INCENTIVE_PLAN", {**fields, "clawback_window_days": 60})
    # Window: 60 days before the next period's end covers this period's pay date; at most 200.00 in total.
    assert clawback("-250.00", 409)["code"] == "CLAWBACK_EXCEEDS_PAYOUT"
    assert clawback("-150.00", 201)["items"][0]["status"] == "PENDING_APPROVAL"   # a negative input is approved
    assert clawback("-60.00", 409)["code"] == "CLAWBACK_EXCEEDS_PAYOUT"     # 150.00 + 60.00 > 200.00
    assert clawback("-50.00", 201)["items"][0]["status"] == "PENDING_APPROVAL"   # 150.00 + 50.00 = 200.00
    activate_policy(w["admin"], "INCENTIVE_PLAN", {**fields, "clawback_window_days": 1})
    assert clawback("-1.00", 409)["code"] == "CLAWBACK_WINDOW_CLOSED"        # the pay date is outside 1 day
