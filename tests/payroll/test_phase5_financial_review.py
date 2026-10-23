"""Phase-5 adversarial review (documents and corrections, financial lens): defects found and their regressions.

* EC-DOC-12 / EC-DOC-11 with a partial reversal (3.5 §12 "selected employees"): reversing one employee of a
  run must not block the publication of the other employees' payslips (the guard RUN_NOT_REVERSED is per
  reversed employee, not per run).
* A FULL hold (status ON_HOLD) keeps the employee out of finalization (runs.py, finalization.py): its
  calculated figures are never finalized pay. They must not reach the payslip YTD column, the finalized
  payroll register PDF, the FINAL payroll register report or a statutory report.
* An independent, hand-computed reconciliation: the gross-to-net and payroll-register reports equal the
  finalized lines per currency, including a REVERSAL run (exact negatives) and an off-cycle run.
"""

from datetime import date
from decimal import Decimal

from tests.payroll.test_payslips_v2 import (  # noqa: F401  (docs_world is a fixture)
    PAY,
    add_instruction,
    content_of,
    docs_world,
    emma_run,
    finalized_run,
    generate,
    payslip_of,
)
from tests.payroll.test_reports import (  # noqa: F401  (rc_world is a fixture)
    SEP,
    SEP_FILTER,
    line,
    member,
    ok,
    preview,
    rc_world,
    seed_run,
)
from tests.payroll.test_wage_inputs import session, set_setting, sql


def doc_status(document_id):
    return sql("SELECT status FROM documents WHERE id = :d", d=document_id)[0][0]


def full_hold(run_employee_id):
    """A FULL hold placed before finalization (finalized results are immutable: database guard)."""
    sql("UPDATE payroll_run_employees SET status = 'ON_HOLD', hold_type = 'FULL', "
        "hold = '{\"type\": \"FULL\", \"reason_code\": \"OTHER\", \"note\": \"held\"}'::jsonb WHERE id = :r",
        r=run_employee_id)


def finalize(run_id):
    sql("UPDATE payroll_runs SET status = 'FINALIZED', finalized_at = now() WHERE id = :r", r=run_id)


# ---------------------------------------------------------------------------
# Partial reversal and publication (EC-DOC-11, EC-DOC-12)
# ---------------------------------------------------------------------------
def test_ec_doc_12_partial_reversal_never_blocks_publication_of_the_other_employees(docs_world):
    from app.payroll.services import publication

    w = docs_world
    cid = w["cid"]
    set_setting(cid, "payslip.publish_trigger", "ON_SETTLED")
    emma_id, ivan_id = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    run_id, rows = emma_run(w)
    generate(cid, run_id)
    emma_doc, ivan_doc = payslip_of(rows[emma_id])[0], payslip_of(rows[ivan_id])[0]
    assert doc_status(emma_doc) == doc_status(ivan_doc) == "GENERATED"  # nothing settled yet
    # Emma alone is reversed (a selective reversal: the finalized REVERSAL run carries her only).
    reversal_id, _ = finalized_run(cid, w["le01"], w["group"], date(2026, 9, 1), [emma_id], run_type="REVERSAL",
                                   reverses=run_id)
    generate(cid, reversal_id)
    with session(cid) as (db, _):
        from app.models.pay_runs import PayrollRun

        run = db.get(PayrollRun, run_id)
        assert publication.reversed_run_employee_ids(db, run) == {rows[emma_id]}
        assert publication.run_is_reversed(db, run) is False  # only partly reversed
    # Ivan's pay settles with verified evidence: his payslip is published at once; Emma's never is.
    add_instruction(cid, run_id, rows[ivan_id], ivan_id, w["le01"], w["group"], status="SETTLED", channel="API",
                    evidence="VERIFIED")
    with session(cid) as (db, _):
        result = publication.on_payment_progress(db, cid, run_id)
        db.commit()
    assert result["published"] == 1 and result["waiting"] == 0
    assert doc_status(ivan_doc) == "PUBLISHED" and doc_status(emma_doc) == "GENERATED"
    # Manual publication: refused for the reversed employee only.
    r = w["acme"].post(f"{PAY}/payslips/{emma_doc}/publish", json={"reason": "Paid by cheque"})
    assert r.status_code == 409 and r.json()["code"] == "RUN_REVERSED"
    assert doc_status(emma_doc) == "GENERATED"


def test_ec_doc_11_publish_run_with_reason_skips_reversed_employees_only(docs_world):
    w = docs_world
    cid = w["cid"]
    set_setting(cid, "payslip.publish_trigger", "ON_SETTLED")
    emma_id, ivan_id = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    run_id, rows = emma_run(w)
    generate(cid, run_id)
    reversal_id, _ = finalized_run(cid, w["le01"], w["group"], date(2026, 9, 1), [emma_id], run_type="REVERSAL",
                                   reverses=run_id)
    generate(cid, reversal_id)
    r = w["acme"].post(f"{PAY}/runs/{run_id}/publish", json={"reason": "Paid by cheque after the bank outage"})
    assert r.status_code == 200, r.text
    assert r.json()["published_manually"] == 1
    assert doc_status(payslip_of(rows[ivan_id])[0]) == "PUBLISHED"
    assert doc_status(payslip_of(rows[emma_id])[0]) == "GENERATED"
    # A fully reversed run still refuses (existing guard).
    reversal2, _ = finalized_run(cid, w["le01"], w["group"], date(2026, 9, 1), [ivan_id], run_type="REVERSAL",
                                 reverses=run_id)
    generate(cid, reversal2)
    r = w["acme"].post(f"{PAY}/runs/{run_id}/publish", json={"reason": "again"})
    assert r.status_code == 409 and r.json()["code"] == "RUN_REVERSED"


# ---------------------------------------------------------------------------
# FULL holds are never finalized pay
# ---------------------------------------------------------------------------
def test_payslip_ytd_never_counts_a_full_hold_that_was_not_finalized(docs_world):
    w = docs_world
    cid = w["cid"]
    emma_id, ivan_id = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    august, aug_rows = emma_run(w, start=date(2026, 8, 1), status="APPROVED")
    full_hold(aug_rows[emma_id])  # Emma's August pay was held in full: no payslip, no payment, no YTD
    finalize(august)
    generate(cid, august)
    assert payslip_of(aug_rows[emma_id]) is None
    september, rows = emma_run(w, start=date(2026, 9, 1))
    generate(cid, september)
    emma = next(i for i in content_of(payslip_of(rows[emma_id])[0])["earnings"] if i["code"] == "BASIC")
    ivan = next(i for i in content_of(payslip_of(rows[ivan_id])[0])["earnings"] if i["code"] == "BASIC")
    assert emma["ytd"] == "50000.00"  # September only
    assert ivan["ytd"] == "100000.00"  # August + September


def test_finalized_register_pdf_totals_only_finalized_employees(docs_world):
    from app.models.pay_runs import PayrollRun
    from app.payroll.services import registers

    w = docs_world
    cid = w["cid"]
    ivan_id = w["emp"]["Ivan"]["id"]
    run_id, rows = emma_run(w, status="APPROVED")
    full_hold(rows[ivan_id])
    # A draft register lists the held employee, marked, but never counts him in the totals.
    with session(cid) as (db, _):
        draft = registers.register_content(db, db.get(PayrollRun, run_id), draft=True)
    tables = {t["title"]: t for t in draft["tables"]}
    assert len(tables["Employees"]["rows"]) == 2
    assert any("on hold" in r[1] for r in tables["Employees"]["rows"])
    assert tables["Totals per currency"]["rows"][0][4] == "68700.00"
    assert any("full hold" in note for note in draft["notes"])
    finalize(run_id)
    with session(cid) as (db, _):
        content = registers.register_content(db, db.get(PayrollRun, run_id), draft=False)
    tables = {t["title"]: t for t in content["tables"]}
    assert [r[0] for r in tables["Employees"]["rows"]] == [w["emp"]["Emma"]["employee_code"]]
    # Emma alone: 50,000 + 20,000 - 1,800 + 500 = 68,700 net.
    assert tables["Totals per currency"]["rows"] == [["INR", "70000.00", "1800.00", "500.00", "68700.00",
                                                      "71800.00"]]
    components = {(r[0], r[2]): r[3] for r in tables["Components"]["rows"]}
    assert components[("BASIC", "INR")] == "50000.00" and components[("PF_EMPLOYEE", "INR")] == "1800.00"


# ---------------------------------------------------------------------------
# Independent hand-computed reconciliation of reports to finalized lines per currency
# ---------------------------------------------------------------------------
def _correction_runs(w):
    """Ivan's September pay reversed (exact negatives) and a EUR off-cycle bonus run where Zack is fully held."""
    emp = w["emp"]
    seed_run(w["cid"], entity=w["le01"], group=w["groups"]["IN"], period=w["periods"]["IN"][SEP], currency="INR",
             run_type="REVERSAL", reverses=w["runs"]["IN_SEP"]["id"], members=[
                 member(emp["Ivan"], "-40000.00", "-4000.00", "-36000.00", "-41500.00", lines=[
                     line("BASIC", amount="-40000.00", category="BASE"),
                     line("IN_EPF_EE", "DEDUCTION", "-1800.00", statutory=True, fixture=True),
                     line("OTHER_DEDUCTION", "DEDUCTION", "-2200.00"),
                     line("IN_EPF_ER", "EMPLOYER_CONTRIBUTION", "-1500.00", statutory=True, fixture=True)])])
    seed_run(w["cid"], entity=w["zz01"], group=w["groups"]["ZZ"], period=w["periods"]["ZZ"][SEP], currency="EUR",
             run_type="OFF_CYCLE", members=[
                 member(emp["Zoe"], "1000.00", "150.00", "850.00", "1070.00", lines=[
                     line("BONUS", amount="1000.00", category="BONUS"),
                     line("ZZ_PENSION_EE", "DEDUCTION", "50.00", statutory=True),
                     line("ZZ_WHT", "DEDUCTION", "100.00", statutory=True),
                     line("ZZ_PENSION_ER", "EMPLOYER_CONTRIBUTION", "70.00", statutory=True)]),
                 member(emp["Zack"], "500.00", "75.00", "425.00", "535.00", status="ON_HOLD", hold_type="FULL",
                        lines=[line("BONUS", amount="500.00", category="BONUS"),
                               line("ZZ_PENSION_EE", "DEDUCTION", "25.00", statutory=True),
                               line("ZZ_WHT", "DEDUCTION", "50.00", statutory=True),
                               line("ZZ_PENSION_ER", "EMPLOYER_CONTRIBUTION", "35.00", statutory=True)])])


def test_reports_reconcile_to_finalized_lines_per_currency_hand_computed(rc_world):
    w = rc_world
    _correction_runs(w)
    # Hand-computed from the seeded finalized lines (September; the APPROVED AED run is not final):
    # INR: Emma 50,000 BASIC + 2,000 OT; Ivan 40,000 BASIC reversed by -40,000.
    # EUR: Zoe 4,000 + Zack 3,000 BASIC; Zoe's 1,000 bonus; Zack's held 500 bonus is never finalized.
    expected_components = {
        ("INR", "EARNING", "BASIC"): "50000.00", ("INR", "EARNING", "OT_WORKDAY"): "2000.00",
        ("INR", "DEDUCTION", "IN_EPF_EE"): "1800.00", ("INR", "DEDUCTION", "OTHER_DEDUCTION"): "3200.00",
        ("INR", "EMPLOYER_CONTRIBUTION", "IN_EPF_ER"): "2000.00",
        ("EUR", "EARNING", "BASIC"): "7000.00", ("EUR", "EARNING", "BONUS"): "1000.00",
        ("EUR", "DEDUCTION", "ZZ_PENSION_EE"): "400.00", ("EUR", "DEDUCTION", "ZZ_WHT"): "600.00",
        ("EUR", "EMPLOYER_CONTRIBUTION", "ZZ_PENSION_ER"): "560.00",
    }
    expected_totals = {
        "INR": {"gross": "52000.00", "deductions": "5000.00", "reimbursements": "0.00", "net": "47000.00",
                "employer_cost": "54000.00"},
        "EUR": {"gross": "8000.00", "deductions": "1000.00", "reimbursements": "0.00", "net": "7000.00",
                "employer_cost": "8560.00"},
    }
    g2n = preview(w["acme"], "gross_to_net", **SEP_FILTER)["rows"]
    components = {(r["currency"], r["section"], r["component_code"]): r["amount"]["amount"]
                  for r in g2n if r["section"] != "TOTAL"}
    assert components == expected_components
    totals = {(r["currency"], r["component_code"]): r["amount"]["amount"] for r in g2n if r["section"] == "TOTAL"}
    for currency, figures in expected_totals.items():
        for key, amount in figures.items():
            assert totals[(currency, key.upper())] == amount, (currency, key)
        # Gross-to-net arithmetic per currency, from the component rows alone.
        earn = sum(Decimal(v) for (c, k, _), v in components.items() if c == currency and k == "EARNING")
        ded = sum(Decimal(v) for (c, k, _), v in components.items() if c == currency and k == "DEDUCTION")
        er = sum(Decimal(v) for (c, k, _), v in components.items() if c == currency and k == "EMPLOYER_CONTRIBUTION")
        assert Decimal(figures["gross"]) == earn and Decimal(figures["deductions"]) == ded
        assert Decimal(figures["net"]) == earn - ded and Decimal(figures["employer_cost"]) == earn + er
    # The FINAL payroll register reconciles to the same figures (the fully held employee is not final pay).
    register = preview(w["acme"], "payroll_register", **SEP_FILTER)
    assert register["totals"] == expected_totals
    assert all(r["status"] == "CALCULATED" for r in register["rows"])
    # The held employee stays visible where holds belong (exclusions report).
    exclusions = preview(w["acme"], "exclusions", **SEP_FILTER)["rows"]
    assert (w["emp"]["Zack"]["employee_code"], "ON_HOLD") in {(r["employee_code"], r["status"]) for r in exclusions}
    # Independent check straight from the database: finalized (CALCULATED) lines of final runs, per currency.
    rows = sql("SELECT l.currency, l.kind, l.component_code, sum(l.amount) FROM payroll_lines l "
               "JOIN payroll_run_employees e ON e.id = l.run_employee_id JOIN payroll_runs r ON r.id = l.run_id "
               "JOIN pay_periods p ON p.id = r.pay_period_id WHERE r.company_id = :c AND e.status = 'CALCULATED' "
               "AND r.status IN ('FINALIZED','PAYMENT_READY','SUBMITTED','PARTIALLY_PAID','PAID','RECONCILED','CLOSED') "
               "AND p.period_start = '2026-09-01' GROUP BY 1, 2, 3", c=w["cid"])
    assert {(c, k, code): f"{Decimal(total):.2f}" for c, k, code, total in rows} == expected_components


def test_statutory_report_never_includes_a_full_hold(rc_world):
    w = rc_world
    _correction_runs(w)
    acme = w["acme"]
    created = ok(acme.post("/api/v1/payroll/statutory-reports", json={
        "legal_entity_id": w["zz01"], "report_code": "ZZ_PENSION_RETURN", "period_start": "2026-09-01"}), 201)
    ok(acme.post(f"/api/v1/payroll/statutory-reports/{created['id']}/generate", json={}))
    report = ok(acme.get(f"/api/v1/payroll/statutory-reports/{created['id']}"))
    rows = {r["employee_code"]: r["amounts"] for r in report["content"]["rows"]}
    # Zoe: 200 + 50 EE, 280 + 70 ER; Zack: his regular September only (the held bonus run row is not final).
    assert rows[w["emp"]["Zoe"]["employee_code"]] == {"ZZ_PENSION_EE": "250.00", "ZZ_PENSION_ER": "350.00"}
    assert rows[w["emp"]["Zack"]["employee_code"]] == {"ZZ_PENSION_EE": "150.00", "ZZ_PENSION_ER": "210.00"}
    assert report["totals"]["components"] == {"ZZ_PENSION_EE": {"EUR": "400.00"}, "ZZ_PENSION_ER": {"EUR": "560.00"}}


def test_arrears_report_signs_recoveries_and_never_lists_a_reversal_as_arrears(rc_world):
    """EC-COR-01/02: an arrears earning is ARREARS (+), a recovery deduction is RECOVERY (−); the exact negatives
    of a REVERSAL run (their period_ref points at the reversed lines) are not arrears of an earlier period."""
    w = rc_world
    _correction_runs(w)  # Ivan's September reversed: four negated lines with a period_ref
    aug_run = w["runs"]["IN_AUG"]["id"]
    ref = {"run_id": aug_run, "start": "2026-08-01", "end": "2026-08-31"}
    seed_run(w["cid"], entity=w["le01"], group=w["groups"]["IN"], period=w["periods"]["IN"][SEP], currency="INR",
             run_type="RETRO_ADJUSTMENT", members=[
                 member(w["emp"]["Emma"], "1000.00", "300.00", "700.00", "1000.00", lines=[
                     line("ARREARS", amount="1000.00", category="RETRO_ADJUSTMENT", period_ref=ref),
                     line("ARREARS_RECOVERY", "DEDUCTION", "300.00", category="RECOVERY", period_ref=ref)])])
    data = preview(w["acme"], "arrears", **SEP_FILTER)
    rows = [(r["component_code"], r["direction"], r["amount"]["amount"]) for r in data["rows"]]
    assert sorted(rows) == [("ARREARS", "ARREARS", "1000.00"), ("ARREARS_RECOVERY", "RECOVERY", "-300.00")]
    assert data["totals"] == {"INR": {"amount": "700.00"}}  # the net change the employee received
    # The reversal is shown where it belongs: the corrected view, against the reversed period and run.
    corrected = [r for r in preview(w["acme"], "corrected_view", **SEP_FILTER)["rows"]
                 if r["correction_type"] == "REVERSAL"]
    assert len(corrected) == 4
    assert {r["original_period"] for r in corrected} == {"2026-09-01 – 2026-09-30"}
    basic = next(r for r in corrected if r["component_code"] == "BASIC")
    assert basic["delta"]["amount"] == "-40000.00" and basic["corrected_amount"]["amount"] == "0.00"


def test_full_and_final_statement_items_add_up_to_the_net_settlement(docs_world):
    """EC-COR-07..14 statement: items are in the payee's perspective (recoveries negative) and add up to the
    net settlement; the employer's contributions are shown apart, never as a settlement item."""
    from app.models.pay_runs import PayrollRun
    from app.payroll.services import registers

    w = docs_world
    emma_id = w["emp"]["Emma"]["id"]
    settlement, rows = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 10, 1), [emma_id],
                                     run_type="FINAL_SETTLEMENT")
    with session(w["cid"]) as (db, _):
        [doc] = registers.final_settlement_documents(db, None, db.get(PayrollRun, settlement))
        content = doc.content
        db.commit()
    items, employer = content["tables"]
    assert items["title"] == "Settlement items"
    amounts = {r[0]: r[3] for r in items["rows"]}
    # BASIC 50,000 + HRA 20,000 − PF 1,800 + travel 500 = 68,700 (the HR-only accrual is never shown).
    assert amounts == {"BASIC": "50000.00", "HRA": "20000.00", "PF_EMPLOYEE": "-1800.00", "TRAVEL": "500.00"}
    assert sum(Decimal(v) for v in amounts.values()) == Decimal(items["total_row"][-1]) == Decimal("68700.00")
    assert employer["rows"] == [["EMPLOYER_PF", "1800.00"]] and "not part of" in employer["title"]
