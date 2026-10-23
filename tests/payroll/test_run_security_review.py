"""Adversarial security review of the pay run lifecycle (3.5 §11, 3.9 §5.4; phase 4 review).

Each test pins one defect found by the review (it failed before the fix) together with the positive case:

* EC-SEC-05: run reads (GET runs, GET runs/{id}, the summary's run block, the stale view) carried the stored
  run-wide totals, population counts, exception counts and stale reasons to readers whose employee scope
  does not cover every employee of the run.
* EC-SEC-02: creating a final settlement run for an employee outside the caller's scope answered 409 with the
  employee's employment state instead of the 404 of an unknown id; the pay-date check answered 404 before 403.
* EC-RUN-05: whoever submitted an earlier round of the run (rejected, reworked without recalculation and
  resubmitted by someone else) could approve the very results they had submitted.
* EC-UX-08: findings about an HR_ONLY component carried the component's previous and current amounts to
  viewers from whom its line is hidden.
* EC-CFG-16 / F-14 / F-36: the snapshot builder's hooks were not installed in the API process (see
  tests/payroll_unit/test_run_hooks_wiring.py).
"""

import json

from tests.helpers import create_employee, invite_and_activate
from tests.payroll.test_components import ok
from tests.payroll.test_run_approvals import _preparer_approver, reviewed_run
from tests.payroll.test_run_calculation import (  # noqa: F401  (fixtures)
    calc_world,
    calculated,
    standin,
)
from tests.payroll.test_run_finalize import (  # noqa: F401  (fixtures)
    approve,
    code_of,
    fin_world,
    reject,
    run_state,
    submit,
)
from tests.payroll.test_run_review import add_foreign_row, reader
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    current_period,
    runs_world,
)
from tests.payroll.test_wage_inputs import sql

APPROVALS = f"{PAY}/approvals"


def _foreign_calculated_row(w):
    """Gus (outside LE01 / PG-IN) in the PG-IN run, calculated (gross 500), with an open blocking finding, a
    salary-change stale reason of his own and a change-detection reason naming him and Emma; the run's stored
    totals and population are the whole run's, as the calculation stores them."""
    foreign = add_foreign_row(w)
    gus = w["gus"]["id"]
    sql("UPDATE payroll_run_employees SET status = 'CALCULATED', gross = 500, deductions = 0, reimbursements = 0, "
        "net = 500, employer_cost = 500, calc_hash = repeat('c', 64) WHERE id = :e", e=foreign)
    whole = ok(w["acme"].get(f"{RUNS}/{w['run']}/summary"))
    assert whole["population"]["eligible"] == 5 and whole["totals"]["INR"]["gross"] == "194500.00"
    sql("INSERT INTO payroll_exceptions (company_id, run_id, run_employee_id, employee_id, code, severity, source, "
        "message, details, resolutions, waivable, blocks_finalization, dedupe_key, status, row_version, created_at, "
        "updated_at) VALUES (:c, :r, :re, :e, 'NEGATIVE_NET', 'BLOCKING', 'VALIDATION', 'Gus finding', '{}', "
        "'[\"FIX\"]', false, true, 'TEST:GUS', 'OPEN', 1, now(), now())", c=w["cid"], r=w["run"], re=foreign, e=gus)
    reasons = [
        {"reason": "An approved compensation change affects locked inputs.", "at": "2026-10-22T10:00:00+00:00",
         "source": "compensation", "employee_id": gus},
        {"reason": "Inputs changed after they were locked (attendance) for 2 employee(s).",
         "at": "2026-10-22T11:00:00+00:00", "source": "change_detection", "employees": 2,
         "changes": [{"employee_id": w["emma"], "source": "attendance:1", "type": "attendance", "change": "UPDATED"},
                     {"employee_id": gus, "source": "attendance:2", "type": "attendance", "change": "UPDATED"}]},
    ]
    sql("UPDATE payroll_runs SET totals = CAST(:t AS jsonb), population = CAST(:p AS jsonb), stale = true, "
        "stale_reasons = CAST(:s AS jsonb) WHERE id = :r", t=json.dumps(whole["totals"]),
        p=json.dumps({k: v for k, v in whole["population"].items() if k != "pending"}), s=json.dumps(reasons),
        r=w["run"])
    return foreign, gus


def _named_employees(reasons):
    found = set()
    for reason in reasons:
        if reason.get("employee_id") is not None:
            found.add(reason["employee_id"])
        found.update(reason.get("employee_ids") or [])
        found.update(c.get("employee_id") for c in reason.get("changes") or [])
    return found


# ---------------------------------------------------------------------------
# EC-SEC-05: run-level figures follow the employee scope
# ---------------------------------------------------------------------------
def test_ec_sec_05_run_reads_never_reveal_totals_counts_or_stale_reasons_of_out_of_scope_employees(app, calc_world):
    w = calc_world
    calculated(w)
    foreign, gus = _foreign_calculated_row(w)
    company_view = ok(w["acme"].get(f"{RUNS}/{w['run']}"))
    assert company_view["totals"]["INR"]["gross"] == "194500.00" and company_view["population"]["eligible"] == 5
    assert gus in _named_employees(company_view["stale_reasons"])            # COMPANY scope sees everything
    open_all = company_view["exceptions"]["open"]

    clerk = reader(app, w, "Lena", "LEGAL_ENTITY", [w["le01"]])
    scoped_summary = ok(clerk.get(f"{RUNS}/{w['run']}/summary"))
    expected_totals = scoped_summary["totals"]
    assert expected_totals["INR"]["gross"] == "194000.00" and expected_totals["INR"]["employees"] == 2

    detail = ok(clerk.get(f"{RUNS}/{w['run']}"))
    assert detail["totals"] == expected_totals
    assert detail["population"]["eligible"] == 4 and detail["population"]["included"] == 2
    assert detail["population"]["calculated"] == 2
    assert detail["exceptions"]["open"] == open_all - 1                      # Gus's finding is not counted
    assert gus not in _named_employees(detail["stale_reasons"])
    assert w["emma"] in _named_employees(detail["stale_reasons"])            # in-scope detail is kept
    assert any(r.get("restricted") for r in detail["stale_reasons"])
    assert all("employees" not in r for r in detail["stale_reasons"])

    listed = ok(clerk.get(RUNS))
    item = next(i for i in listed["items"] if i["id"] == w["run"])
    assert item["totals"] == expected_totals and item["population"]["eligible"] == 4
    assert item["exceptions"]["open"] == open_all - 1 and gus not in _named_employees(item["stale_reasons"])

    assert scoped_summary["run"]["totals"] == expected_totals
    assert scoped_summary["run"]["population"]["eligible"] == scoped_summary["population"]["eligible"] == 4

    stale = ok(clerk.get(f"{RUNS}/{w['run']}/stale"))
    assert gus not in _named_employees(stale["reasons"])
    assert foreign not in {e["run_employee_id"] for e in stale["employees"]}
    assert gus in _named_employees(ok(w["acme"].get(f"{RUNS}/{w['run']}/stale"))["reasons"])

    # A pay-group reader covering every employee of a run of its own scope sees the stored figures unchanged.
    assert ok(w["acme"].get(f"{RUNS}/{w['run']}/summary"))["run"]["totals"]["INR"]["gross"] == "194500.00"
    # Other tenants still see nothing of the run.
    beta = w["beta"]["client"]
    assert beta.get(f"{RUNS}/{w['run']}").status_code == 404
    assert w["run"] not in {i["id"] for i in ok(beta.get(RUNS))["items"]}
    assert w["cobalt"]["client"].get(f"{RUNS}/{w['run']}/stale").status_code == 404


# ---------------------------------------------------------------------------
# EC-SEC-02: no existence or state oracle on foreign ids
# ---------------------------------------------------------------------------
def test_ec_sec_02_final_settlement_for_an_out_of_scope_employee_is_404_like_an_unknown_id(runs_world):
    w = runs_world
    cleo = w["cleo"]                                   # run permissions on AE01 only
    period = current_period(w["acme"], w["ae_group"])

    def settle(employee_id):
        return cleo.post(RUNS, json={"run_type": "FINAL_SETTLEMENT", "pay_group_id": w["ae_group"],
                                     "pay_period_id": period["id"], "employee_ids": [employee_id]})

    unknown = settle(99999999)
    assert unknown.status_code == 404, unknown.text
    emma = settle(w["emp"]["Emma"]["id"])              # LE01, still employed
    assert emma.status_code == 404, emma.text
    assert code_of(emma) == code_of(unknown) and emma.json().get("detail") == unknown.json().get("detail")
    sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=period["start"], e=w["emp"]["Nora"]["id"])
    nora = settle(w["emp"]["Nora"]["id"])              # LE01, left
    assert nora.status_code == 404 and nora.json().get("detail") == unknown.json().get("detail"), nora.text
    beta_employee = w["beta"]["employee"]["id"]        # another tenant
    assert settle(beta_employee).status_code == 404
    # In scope the employee's state is reported as before.
    gus = settle(w["gus"]["id"])
    assert gus.status_code == 409 and code_of(gus) == "EMPLOYMENT_NOT_ENDED", gus.text
    assert sql("SELECT count(*) FROM payroll_runs WHERE company_id = :c AND run_type = 'FINAL_SETTLEMENT'",
               c=w["cid"])[0][0] == 0


def test_ec_sec_02_pay_date_check_is_403_without_rights_for_known_and_unknown_periods(app, runs_world):
    w = runs_world
    worker = invite_and_activate(app, w["acme"], create_employee(w["acme"], "Walt"), ["employee"])
    known = worker.get(f"{PAY}/pay-periods/{w['period']['id']}/pay-date-check")
    unknown = worker.get(f"{PAY}/pay-periods/99999999/pay-date-check")
    assert known.status_code == unknown.status_code == 403, (known.text, unknown.text)
    assert ok(w["acme"].get(f"{PAY}/pay-periods/{w['period']['id']}/pay-date-check"))["period_id"] == w["period"]["id"]
    assert w["beta"]["client"].get(f"{PAY}/pay-periods/{w['period']['id']}/pay-date-check").status_code == 404
    assert w["cleo"].get(f"{PAY}/pay-periods/{w['period']['id']}/pay-date-check").status_code == 404  # AE01 only


# ---------------------------------------------------------------------------
# EC-RUN-05: an earlier round's submitter is a preparer of the resubmitted results
# ---------------------------------------------------------------------------
def test_ec_run_05_an_earlier_rounds_submitter_cannot_approve_the_resubmitted_run(app, fin_world):
    w = fin_world
    sam = _preparer_approver(app, w)
    run = reviewed_run(w)                               # the founder calculated it
    submit(sam, run["id"])                              # round 1: Sam submits
    reject(w["approver"], run["id"], reason="Check Emma's bonus")
    ok(w["processor"].post(f"{RUNS}/{run['id']}/rework", json={"note": "Checked, unchanged"}))
    submit(w["acme"], run["id"])                        # round 2: the same results, submitted by the founder
    r = approve(sam, run["id"], expect=403)
    assert code_of(r) == "SEPARATION_OF_DUTIES"
    request_id = sql("SELECT approval_request_id FROM payroll_runs WHERE id = :r", r=run["id"])[0][0]
    context = sql("SELECT context FROM approval_requests WHERE id = :i", i=request_id)[0][0]
    assert sam.user_id in context["maker_user_ids"]
    assert sam.post(f"{APPROVALS}/{request_id}/approve", json={}).status_code == 403   # the generic inbox too
    assert run_state(run["id"])["status"] == "APPROVAL_PENDING"
    approve(w["bob"], run["id"])                        # anyone else may approve
    assert run_state(run["id"])["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# EC-UX-08: HR_ONLY components stay hidden in findings
# ---------------------------------------------------------------------------
def test_ec_ux_08_findings_about_hr_only_components_hide_them_from_other_viewers(app, calc_world):
    w = calc_world
    # HRA (HR_ONLY) 10,000 -> 20,000 (+100%): a COMPONENT_VARIANCE finding about a hidden component.
    w["standin"].config = {w["emma"]: {"hr_only": True, "previous": {
        "gross": "97000.00", "net": "97000.00",
        "components": {"BASIC": "50000.00", "HRA": "10000.00", "FIXED_ALLOWANCE": "27000.00"}}}}
    calculated(w)
    full = ok(w["acme"].get(f"{RUNS}/{w['run']}/employees/{w['emma_row']}"))
    hra = [f for f in full["exceptions"] if f["code"] == "COMPONENT_VARIANCE" and f["details"].get("component") == "HRA"]
    assert hra and hra[0]["details"]["previous"] == "10000.00"            # HR sees the finding in full

    group_reader = reader(app, w, "Gita", "PAY_GROUP", [w["group"]])      # HR_ONLY lines are hidden from her
    detail = ok(group_reader.get(f"{RUNS}/{w['run']}/employees/{w['emma_row']}"))
    assert "HRA" not in {line["component"] for line in detail["lines"]}
    variance_findings = [f for f in detail["exceptions"] if f["code"] == "COMPONENT_VARIANCE"]
    assert variance_findings and all(f["details"] == {"restricted": True} and "HRA" not in f["message"]
                                     for f in variance_findings), variance_findings
    listed = ok(group_reader.get(f"{RUNS}/{w['run']}/exceptions", params={"code": "COMPONENT_VARIANCE"}))["items"]
    assert listed and all(f["details"] == {"restricted": True} for f in listed), listed
    assert "10000" not in json.dumps(listed) and "20000" not in json.dumps(listed)
    entity_reader = reader(app, w, "Ezra", "LEGAL_ENTITY", [w["le01"]])  # entity scope sees HR_ONLY lines
    shown = ok(entity_reader.get(f"{RUNS}/{w['run']}/exceptions", params={"code": "COMPONENT_VARIANCE"}))["items"]
    assert any(f["details"].get("component") == "HRA" for f in shown)
