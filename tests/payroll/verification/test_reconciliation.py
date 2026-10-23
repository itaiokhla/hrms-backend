"""Reconciliation invariants of calculated and finalized runs (Goal 2 §9; 3.5 §17; contract §2 invariants).

The invariants are checked independently of the product's own check (``calculation._reconcile_lines``): the test
reads the stored rows and lines with SQL and recomputes every identity with ``Decimal``:

* per employee: cash earnings - employee deductions + cash reimbursements + explicit rounding = net;
  gross = Σ EARNING lines; employer cost = gross + employer contributions; employer contributions and benefits
  in kind never reach the cash net; every line is in the employee's (= the run's) currency and has at most the
  currency's minor units (no cross-currency line, no extra precision);
* per run: Σ employee figures = run totals PER CURRENCY (never added across currencies); excluded employees
  (NO_SALARY) have no lines and no figures - never a silent zero pay;
* after finalization: payment obligations = the finalized positive nets, payment allocations (instructions) <=
  obligations, statutory liabilities = Σ statutory lines (employee portion vs employer portion kept apart),
  payslip totals = the finalized figures, the payroll register and the overview reconcile to the same lines.

``assert_run_reconciles`` and ``assert_finalized_reconciles`` are used by test_golden_calculations.py and
test_run_lifecycle_e2e.py for every run they calculate or finalize.
"""

from collections import defaultdict
from decimal import Decimal

import pytest

from tests.helpers import create_employee
from tests.payroll.test_wage_inputs import place, sql

D = Decimal
ZERO = D(0)
# ISO 4217 minor units of the currencies used by the verification suites (written out, not read from the product).
MINOR_UNITS = {"INR": 2, "USD": 2, "EUR": 2, "AED": 2, "GBP": 2, "JPY": 0, "KWD": 3}
# Run statuses the reports treat as final (reports.FINAL_STATUSES, the definition of "finalized pay").
FINAL_RUN_STATUSES = ("FINALIZED", "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")
CASH_NET_KINDS = {"EARNING": 1, "DEDUCTION": -1, "REIMBURSEMENT": 1, "ROUNDING": 1}


def _scale_ok(amount, currency):
    quantum = D(1).scaleb(-MINOR_UNITS[currency])
    return D(amount) == D(amount).quantize(quantum)


def employee_rows(run_id):
    return sql("SELECT id, employee_id, status, hold_type, calc_hash, currency, gross, deductions, reimbursements, "
               "net, employer_cost, exclusion_reason FROM payroll_run_employees WHERE run_id = :r ORDER BY id",
               r=run_id)


def in_totals(row):
    """The documented rule (3.5 §9, run totals = Σ finalized employees): calculated with a result; a FULL hold
    is ON_HOLD and stays out."""
    return row[2] == "CALCULATED" and row[4] is not None


def assert_run_reconciles(run_id):
    run_currency, totals = sql("SELECT currency, totals FROM payroll_runs WHERE id = :r", r=run_id)[0]
    rows = employee_rows(run_id)
    by_kind = defaultdict(lambda: ZERO)
    line_count = defaultdict(int)
    for row_id, kind, currency, amount in sql(
            "SELECT run_employee_id, kind, currency, amount FROM payroll_lines WHERE run_id = :r", r=run_id):
        by_kind[(row_id, kind, currency)] += D(amount)
        line_count[row_id] += 1
        assert _scale_ok(amount, currency), (run_id, row_id, kind, amount, currency)
    sums = defaultdict(lambda: defaultdict(lambda: ZERO))
    employees = defaultdict(int)
    for row in rows:
        row_id, employee_id, status, hold, calc_hash, currency, gross, deductions, reimbursements, net, cost, why = row
        if status == "EXCLUDED":
            # Never a silent zero pay: an excluded employee has a reason, no lines and no figures.
            assert why and line_count[row_id] == 0 and gross is None and net is None, row
            continue
        if not in_totals(row):
            continue
        # No line in another currency than the employee's, and the employee is paid in the run currency.
        foreign = {c for (r, _, c) in by_kind if r == row_id and c != currency}
        assert not foreign and currency == run_currency, (row, foreign)

        def total(*kinds):
            return sum((by_kind[(row_id, k, currency)] for k in kinds), ZERO)

        earnings, employee_deductions = total("EARNING"), total("DEDUCTION")
        cash_reimbursements, rounding = total("REIMBURSEMENT"), total("ROUNDING")
        employer = total("EMPLOYER_CONTRIBUTION")
        # Cash earnings - employee deductions + cash reimbursements + explicit rounding = net pay.
        assert D(net) == earnings - employee_deductions + cash_reimbursements + rounding, (row, earnings,
                                                                                            employee_deductions)
        assert D(gross) == earnings and D(deductions) == employee_deductions, row
        assert D(reimbursements) == cash_reimbursements, row
        # Employer cost = gross + employer contributions; employer contributions are never in the cash net.
        assert D(cost) == earnings + employer, (row, employer)
        for name, value in (("gross", gross), ("deductions", deductions), ("reimbursements", reimbursements),
                            ("net", net), ("employer_cost", cost)):
            sums[currency][name] += D(value)
        employees[currency] += 1
    # Σ employees = run totals, per currency, never across currencies.
    assert set(totals or {}) == set(sums), (totals, dict(sums))
    for currency, figures in sums.items():
        stored = totals[currency]
        for name, value in figures.items():
            assert D(stored[name]) == value, (run_id, currency, name, stored[name], value)
        assert stored["employees"] == employees[currency]
    return {c: dict(v) for c, v in sums.items()}


def assert_finalized_reconciles(run_id, *, expect_payslips=True):
    """Obligations, allocations, liabilities and payslips of a FINALIZED (or later) run."""
    status, currency = sql("SELECT status, currency FROM payroll_runs WHERE id = :r", r=run_id)[0]
    assert status in FINAL_RUN_STATUSES, status
    rows = [r for r in employee_rows(run_id) if in_totals(r)]
    # Payment obligations = the finalized positive nets (one per employee, same currency).
    owed = {e: (D(a), c) for e, a, c in sql(
        "SELECT employee_id, amount, currency FROM payment_obligations WHERE run_id = :r", r=run_id)}
    expected = {r[1]: (D(r[9]), r[5]) for r in rows if D(r[9]) > 0}
    assert owed == expected, (owed, expected)
    # Allocations (instructions not cancelled) never exceed their obligation.
    for obligation_id, amount, allocated in sql(
            "SELECT o.id, o.amount, coalesce(sum(i.amount) FILTER (WHERE i.status NOT IN ('CANCELLED', 'REISSUED')), 0) "
            "FROM payment_obligations o LEFT JOIN payment_instructions i ON i.obligation_id = o.id "
            "WHERE o.run_id = :r GROUP BY o.id, o.amount", r=run_id):
        assert D(allocated) <= D(amount), (obligation_id, allocated, amount)
    # Statutory liabilities: employee portions = Σ statutory deductions, employer portions = Σ employer lines.
    liabilities = sql("SELECT coalesce(sum(employee_amount), 0), coalesce(sum(employer_amount), 0) "
                      "FROM statutory_liabilities WHERE run_id = :r", r=run_id)[0]
    statutory = dict(sql("SELECT kind, sum(l.amount) FROM payroll_lines l JOIN payroll_run_employees e "
                         "ON e.id = l.run_employee_id WHERE l.run_id = :r AND l.statutory AND e.status = 'CALCULATED' "
                         "GROUP BY kind", r=run_id))
    assert D(liabilities[0]) == D(statutory.get("DEDUCTION") or 0), (liabilities, statutory)
    assert D(liabilities[1]) == D(statutory.get("EMPLOYER_CONTRIBUTION") or 0), (liabilities, statutory)
    if expect_payslips:
        payslips = {rid: content for rid, content in sql(
            "SELECT run_employee_id, content FROM documents WHERE run_id = :r AND doc_type = 'PAYSLIP_V2'", r=run_id)}
        minor = MINOR_UNITS[currency]
        for row in rows:
            content = payslips.get(row[0])
            assert content is not None, ("no payslip", row)
            for name, index in (("gross", 6), ("deductions", 7), ("reimbursements", 8), ("net", 9)):
                assert content["totals"][name] == f"{D(row[index]):.{minor}f}", (name, content["totals"], row)


def assert_summary_matches(client, run_id):
    """GET /runs/{id}/summary (what reviewers and approvers see) = the totals recomputed from the stored rows, and
    its statutory liabilities keep the employee portion (withholding) apart from the employer portion: Σ employee
    portions = Σ statutory DEDUCTION lines, Σ employer portions = Σ statutory EMPLOYER_CONTRIBUTION lines."""
    sums = assert_run_reconciles(run_id)
    response = client.get(f"/api/v1/payroll/runs/{run_id}/summary")
    assert response.status_code == 200, response.text
    summary = response.json()
    assert set(summary["totals"]) == set(sums), (summary["totals"], sums)
    for currency, figures in sums.items():
        for name, value in figures.items():
            assert D(summary["totals"][currency][name]) == value, (currency, name, summary["totals"][currency])
    statutory = defaultdict(lambda: ZERO)
    for kind, currency, amount in sql(
            "SELECT l.kind, l.currency, l.amount FROM payroll_lines l JOIN payroll_run_employees e "
            "ON e.id = l.run_employee_id WHERE l.run_id = :r AND l.statutory AND e.status = 'CALCULATED'", r=run_id):
        statutory[(kind, currency)] += D(amount)
    portions = defaultdict(lambda: ZERO)
    for item in summary["liabilities"]:
        portions[("DEDUCTION", item["currency"])] += D(item["employee_amount"])
        portions[("EMPLOYER_CONTRIBUTION", item["currency"])] += D(item["employer_amount"])
    for key in set(statutory) | set(portions):
        assert portions[key] == statutory[key], (key, dict(portions), dict(statutory))
    return summary




def assert_reports_reconcile(client, period_from, period_to, **filters):
    """The gross-to-net report (component totals) and the payroll register reconcile to the finalized lines and
    rows recomputed here with SQL, per currency: Σ lines per (currency, kind, component) = the component row;
    the TOTAL rows and the register totals = Σ finalized employee figures. Returns the per-currency sums."""
    params = {"period_from": period_from, "period_to": period_to, **filters}
    where = ("r.status = ANY(:final) AND e.status = 'CALCULATED' AND p.period_end >= :f AND p.period_start <= :t "
             "AND r.company_id = :c" + (" AND r.pay_group_id = :g" if "pay_group_id" in filters else ""))
    binds = {"final": list(FINAL_RUN_STATUSES), "f": period_from, "t": period_to, "c": client.company_id}
    if "pay_group_id" in filters:
        binds["g"] = filters["pay_group_id"]
    component_sums = defaultdict(lambda: ZERO)
    for currency, kind, code, amount in sql(
            "SELECT l.currency, l.kind, l.component_code, l.amount FROM payroll_lines l "
            "JOIN payroll_run_employees e ON e.id = l.run_employee_id JOIN payroll_runs r ON r.id = e.run_id "
            f"JOIN pay_periods p ON p.id = r.pay_period_id WHERE {where}", **binds):
        component_sums[(currency, kind, code)] += D(amount)
    figure_sums = defaultdict(lambda: defaultdict(lambda: ZERO))
    for currency, gross, deductions, reimbursements, net, cost in sql(
            "SELECT e.currency, e.gross, e.deductions, e.reimbursements, e.net, e.employer_cost "
            "FROM payroll_run_employees e JOIN payroll_runs r ON r.id = e.run_id "
            f"JOIN pay_periods p ON p.id = r.pay_period_id WHERE {where}", **binds):
        for name, value in (("gross", gross), ("deductions", deductions), ("reimbursements", reimbursements),
                            ("net", net), ("employer_cost", cost)):
            figure_sums[currency][name] += D(value)

    report = client.get("/api/v1/payroll/reports/gross_to_net/preview", params=params)
    assert report.status_code == 200, report.text
    data = report.json()
    assert data["truncated"] is False, "the reconciliation needs every row"
    shown, totals = {}, defaultdict(dict)
    for row in data["rows"]:
        money = row["amount"]
        amount = D(money["amount"] if isinstance(money, dict) else money)
        if isinstance(money, dict):
            assert money["currency"] == row["currency"], row          # never a cross-currency amount
        if row["section"] == "TOTAL":
            totals[row["currency"]][row["component_code"].lower()] = amount
        else:
            shown[(row["currency"], row["section"], row["component_code"])] = amount
    assert shown == dict(component_sums), (shown, dict(component_sums))
    assert set(totals) == set(figure_sums), (dict(totals), dict(figure_sums))
    for currency, figures in figure_sums.items():
        assert totals[currency] == dict(figures), (currency, totals[currency], dict(figures))
    register = client.get("/api/v1/payroll/reports/payroll_register/preview", params=params)
    assert register.status_code == 200, register.text
    register_totals = register.json()["totals"]
    assert set(register_totals) == set(figure_sums), (register_totals, dict(figure_sums))
    for currency, figures in figure_sums.items():
        for name, value in figures.items():
            assert D(register_totals[currency][name]) == value, (currency, name, register_totals[currency])
    return {c: dict(v) for c, v in figure_sums.items()}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.fixture
def world(app):
    from tests.payroll.verification.test_golden_calculations import gold_world

    return gold_world(app, "Recon Corp")


def test_multi_currency_finalized_runs_reconcile_to_obligations_payslips_register_and_overview(world):
    """Two pay groups paid in INR and KWD for September 2026 are calculated, approved and finalized.

    INR (Example A): Emma 1,00,000.00 + BONUS 2,500.50 -> 1,02,500.50; Ivan 1,00,000.00; Nora has no salary
      (EXCLUDED, NO_SALARY, no lines). INR totals: gross = net = 2,02,500.50 for 2 employees.
    KWD (FLAT, annual 10,001): Kam 10,001 / 12 = 833.41666.. -> 833.417.
    The register and the overview show one total per currency; nothing adds INR and KWD together.
    """
    from tests.payroll.test_payment_obligations import NUMBERS, add_bank_account
    from tests.payroll.verification.test_golden_calculations import calculated_run, gold_group, hire, wage_input
    from tests.payroll.verification.test_run_lifecycle_e2e import attest, finalize_run, submit_and_approve

    w = world
    inr = gold_group(w, "PG-RC-IN")
    kwd = gold_group(w, "PG-RC-KW", currency="KWD")
    emma = hire(w, "Emma", inr, "1200000", structure="staff")
    ivan = hire(w, "Ivan", inr, "1200000", structure="staff")
    nora = create_employee(w["acme"], "Nora")
    place(nora["id"], w["le01"], inr["id"])
    kam = hire(w, "Kam", kwd, "10001")
    wage_input(w, emma, "BONUS", "2500.50", inr["periods"]["2026-09-01"])
    add_bank_account(w["cid"], emma, NUMBERS["Emma"], holder="Emma Test")
    attest(w)

    runs = {}
    for group in (inr, kwd):
        run_id = calculated_run(w, group, "2026-09-01")
        submit_and_approve(w, run_id)
        finalize_run(w, run_id, key=f"recon-{run_id}")
        runs[group["currency"]] = run_id
    inr_sums = assert_run_reconciles(runs["INR"])
    kwd_sums = assert_run_reconciles(runs["KWD"])
    assert inr_sums["INR"]["net"] == D("100000.00") + D("2500.50") + D("100000.00") == D("202500.50")
    assert kwd_sums == {"KWD": {"gross": D("833.417"), "deductions": D("0"), "reimbursements": D("0"),
                                "net": D("833.417"), "employer_cost": D("833.417")}}
    excluded = sql("SELECT status, exclusion_reason FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e",
                   r=runs["INR"], e=nora["id"])
    assert excluded == [("EXCLUDED", "NO_SALARY")]
    for run_id in runs.values():
        assert_finalized_reconciles(run_id)
    # Obligations: Nora (no salary) owes nothing; nobody is owed a zero.
    assert sql("SELECT count(*) FROM payment_obligations WHERE employee_id = :e", e=nora["id"])[0][0] == 0

    # The summary each approver saw = the stored rows (no statutory lines without a pack: no liabilities).
    for run_id in runs.values():
        assert assert_summary_matches(w["acme"], run_id)["liabilities"] == []
    # Component totals (gross-to-net report) = Σ lines; register totals = Σ finalized rows, per currency.
    report_sums = assert_reports_reconcile(w["acme"], "2026-09-01", "2026-09-30")
    assert report_sums["INR"]["gross"] == D("202500.50") and report_sums["KWD"]["net"] == D("833.417")
    # The payroll register reconciles to the finalized lines, per currency.
    register = w["acme"].get("/api/v1/payroll/reports/payroll_register/preview",
                             params={"period_from": "2026-09-01", "period_to": "2026-09-30"})
    assert register.status_code == 200, register.text
    data = register.json()
    assert data["totals"]["INR"]["net"] == "202500.50" and data["totals"]["INR"]["gross"] == "202500.50"
    assert data["totals"]["KWD"]["net"] == "833.417"
    assert set(data["totals"]) == {"INR", "KWD"} and data["row_count"] == 3
    # The overview shows the same money per currency and stage, never one mixed figure.
    overview = w["acme"].get("/api/v1/payroll/overview", params={"period_from": "2026-09-01",
                                                                 "period_to": "2026-09-30"})
    assert overview.status_code == 200, overview.text
    money = {(m["currency"], m["stage"]): m for m in overview.json()["money"]}
    finals = {cur: m for (cur, stage), m in money.items() if stage in ("Final", "FINAL", "Finalized", "FINALIZED")}
    assert set(finals) == {"INR", "KWD"}, money
    assert finals["INR"]["net"] == "202500.50" and finals["KWD"]["net"] == "833.417"


def test_excluded_and_failed_employees_stay_out_of_totals_and_obligations(world):
    """An employee whose salary starts after the period is EXCLUDED (no lines, no figures, no obligation) and a
    FULL-held employee is calculated but kept out of the totals the approvers approve (3.5 §9, F-55).
    September 2026, FLAT: Asha 30,000.00 is counted; Hold 30,000.00 is held; Late starts in October."""
    from tests.payroll.verification.test_golden_calculations import calculated_run, gold_group, hire

    w = world
    group = gold_group(w, "PG-RC-EX")
    asha = hire(w, "Asha", group, "360000")
    held = hire(w, "Hold", group, "360000")
    late = hire(w, "Late", group, "360000", start="2026-10-01")
    run_id = calculated_run(w, group, "2026-09-01")
    rows = {r[1]: r for r in employee_rows(run_id)}
    assert rows[late][2] == "EXCLUDED" and rows[late][11] == "NO_SALARY"
    r = w["acme"].post(f"/api/v1/payroll/runs/{run_id}/employees/{rows[held][0]}/hold",
                       json={"hold_type": "FULL", "reason_code": "INVESTIGATION", "note": "Pending review"})
    assert r.status_code == 200, r.text
    totals = sql("SELECT totals FROM payroll_runs WHERE id = :r", r=run_id)[0][0]
    # Only Asha is in the totals: 30,000.00 (the held employee's 30,000.00 is not approved with the run).
    assert totals == {"INR": {"gross": "30000.00", "deductions": "0.00", "reimbursements": "0.00",
                              "net": "30000.00", "employer_cost": "30000.00", "employees": 1}}, totals
    assert_run_reconciles(run_id)
    assert asha in rows


def test_payment_allocations_settlement_and_close_reconcile_to_the_finalized_nets(app, world):
    """July 2026 (31 days), FLAT: Emma annual 3,60,000 -> 30,000.00; Ivan the same + an approved BONUS of
    1,234.56 -> 31,234.56. After finalization: obligations = {Emma 30,000.00, Ivan 31,234.56} (= the nets);
    a bank-file batch prepared by Pete, approved by Alma and released by Fran (three different people) allocates
    exactly the obligations (never more); a sandbox bank statement (fictitious account numbers, no real money)
    debiting the two instructions settles them, the run is RECONCILED and then CLOSED with no open item.
    Σ settled = 30,000.00 + 31,234.56 = 61,234.56 = run net."""
    from tests.payroll.test_payment_obligations import NUMBERS, add_bank_account, add_funding_account, submitted_batch
    from tests.payroll.test_reconciliation import csv_statement, upload
    from tests.payroll.verification.test_golden_calculations import calculated_run, gold_group, hire, wage_input
    from tests.payroll.test_compensation import login_as
    from tests.payroll.verification.test_run_lifecycle_e2e import attest, finalize_run, state, submit_and_approve

    w = world
    cid, acme = w["cid"], w["acme"]
    group = gold_group(w, "PG-RC-PAY")
    emma = hire(w, "Emma", group, "360000")
    ivan = hire(w, "Ivan", group, "360000")
    wage_input(w, ivan, "BONUS", "1234.56", group["periods"]["2026-07-01"])
    for name, employee in (("Emma", emma), ("Ivan", ivan)):
        add_bank_account(cid, employee, NUMBERS[name], holder=f"{name} Test")
    attest(w)
    run_id = calculated_run(w, group, "2026-07-01")
    submit_and_approve(w, run_id)
    finalize_run(w, run_id, key=f"pay-{run_id}")
    nets = {emma: D("30000.00"), ivan: D("30000.00") + D("1234.56")}
    assert nets[ivan] == D("31234.56")
    assert {e: f for e, f in sql("SELECT employee_id, net FROM payroll_run_employees WHERE run_id = :r",
                                 r=run_id)} == nets
    assert_finalized_reconciles(run_id)
    # Closing before anything is paid is refused.
    r = acme.post(f"/api/v1/payroll/runs/{run_id}/close", json={})
    assert r.status_code == 409 and r.json()["code"] == "PAYMENTS_OPEN", r.text

    pete = login_as(app, acme, create_employee(acme, "Pete", "Preparer"), ["finance_payments"])
    fran = login_as(app, acme, create_employee(acme, "Fran", "Finance"), ["finance_payments"])
    alma = login_as(app, acme, create_employee(acme, "Alma", "Approver"), ["finance_payments"])
    funding = add_funding_account(cid, w["le01"], [fran.user_id])
    submitted_batch({"pete": pete, "fran": fran, "alma": alma}, run_id)
    assert state(run_id)["status"] == "SUBMITTED"
    # Allocations = obligations (one live instruction each, same amount and currency): never more.
    allocated = {e: (a, c) for e, a, c in sql(
        "SELECT o.employee_id, sum(i.amount), min(i.currency) FROM payment_instructions i JOIN payment_obligations o "
        "ON o.id = i.obligation_id WHERE o.run_id = :r AND i.status NOT IN ('CANCELLED', 'REISSUED') "
        "GROUP BY o.employee_id", r=run_id)}
    assert allocated == {e: (n, "INR") for e, n in nets.items()}, allocated
    assert_finalized_reconciles(run_id)

    from datetime import date

    statement = [(date.today().isoformat(), f"-{D(amount):.2f}", reference, "") for reference, amount in sql(
        "SELECT reference, amount FROM payment_instructions WHERE run_id = :r AND status NOT IN "
        "('CANCELLED', 'REISSUED') ORDER BY id", r=run_id)]
    assert sum(-D(a) for _, a, _, _ in statement) == D("61234.56")
    result = upload(fran, funding, csv_statement(*statement))
    assert result["import"]["counts"] == {"MATCHED": 2}, result
    assert state(run_id)["status"] == "RECONCILED"
    assert {s for (s,) in sql("SELECT status FROM payment_obligations WHERE run_id = :r", r=run_id)} == {"SETTLED"}
    closed = acme.post(f"/api/v1/payroll/runs/{run_id}/close", json={"note": "All salaries settled"})
    assert closed.status_code == 200, closed.text
    assert state(run_id)["status"] == "CLOSED"
    assert_finalized_reconciles(run_id)
    report_sums = assert_reports_reconcile(acme, "2026-07-01", "2026-07-31", pay_group_id=group["id"])
    assert report_sums["INR"]["net"] == D("61234.56")
