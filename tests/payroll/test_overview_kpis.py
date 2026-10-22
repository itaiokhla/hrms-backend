"""Payroll overview KPIs (3.8 §5.1): one scoped population query family for counts and drill-downs.

EC-UX-03 (a KPI count equals its drill-down list: same query, same scope), EC-UX-07 (pack-specific
statutory KPIs only for entities whose pack defines them; labels from the pack), contract §23.2 (paid only
with settlement evidence), EC-MON-04 (money per currency, labelled Draft / Approved / Final), EC-SEC-05.
"""

from datetime import date

import pytest

from tests.helpers import signup_company
from tests.payroll.test_reports import build_world, ok, other_tenant, with_roles
from tests.payroll.test_wage_inputs import session, sql

PAY = "/api/v1/payroll"
OVERVIEW = f"{PAY}/overview"
SEP = {"period_from": "2026-09-01", "period_to": "2026-09-30"}
KPIS = ("eligible", "with_salary", "without_salary", "processed", "paid", "on_hold")


@pytest.fixture
def kpi_world(app):
    return build_world(app, salaries=True)


def overview(client, **params):
    return ok(client.get(OVERVIEW, params={**SEP, **params}))


def drill(client, kpi, **params):
    return ok(client.get(f"{OVERVIEW}/{kpi}", params={**SEP, **params}))


def assert_drilldowns_match(client, data, **params):
    """EC-UX-03: every count KPI equals the length of its drill-down list."""
    for kpi in KPIS:
        listed = drill(client, kpi, **params)
        assert listed["total"] == data["kpis"][kpi] == len(listed["items"]), kpi


def names(w, items):
    by_id = {e["id"]: n for n, e in w["emp"].items()}
    return sorted(by_id[i["employee_id"]] for i in items)


def test_ec_ux_03_counts_equal_drill_downs_and_with_plus_without_is_eligible(kpi_world):
    w = kpi_world
    data = overview(w["acme"])
    assert data["kpis"] == {"eligible": 6, "with_salary": 2, "without_salary": 4, "processed": 4, "paid": 0,
                            "on_hold": 0}
    assert data["consistency"] == {"eligible_equals_with_plus_without": True}
    assert_drilldowns_match(w["acme"], data)
    assert names(w, drill(w["acme"], "with_salary")["items"]) == ["Emma", "Ivan"]
    assert names(w, drill(w["acme"], "processed")["items"]) == ["Emma", "Ivan", "Zack", "Zoe"]
    assert names(w, drill(w["acme"], "without_salary")["items"]) == ["Gus", "Nora", "Zack", "Zoe"]
    page = drill(w["acme"], "eligible", skip=4, limit=10)
    assert page["total"] == 6 and len(page["items"]) == 2
    # The same holds under every filter.
    for params, eligible in (({"legal_entity_id": w["le01"]}, 3), ({"currency": "EUR"}, 2),
                             ({"pay_group_id": w["groups"]["AE"]}, 1), ({"branch_id": w["zz_branch"]}, 2)):
        filtered = overview(w["acme"], **params)
        assert filtered["kpis"]["eligible"] == eligible, params
        assert filtered["kpis"]["with_salary"] + filtered["kpis"]["without_salary"] == eligible
        assert_drilldowns_match(w["acme"], filtered, **params)
    assert w["acme"].get(f"{OVERVIEW}/bogus", params=SEP).status_code == 422


def test_money_is_per_currency_and_labelled_by_run_stage(kpi_world):
    w = kpi_world
    data = overview(w["acme"])
    assert data["money"] == [
        {"currency": "AED", "stage": "Approved", "gross": "10000.00", "net": "9900.00", "employer_cost": "10000.00",
         "employees": 1, "runs": 1},
        {"currency": "EUR", "stage": "Final", "gross": "7000.00", "net": "6150.00", "employer_cost": "7490.00",
         "employees": 2, "runs": 1},
        {"currency": "INR", "stage": "Final", "gross": "92000.00", "net": "83000.00", "employer_cost": "95500.00",
         "employees": 2, "runs": 1},
    ]
    assert [m["currency"] for m in overview(w["acme"], status="FINAL")["money"]] == ["EUR", "INR"]
    assert [m["stage"] for m in overview(w["acme"], status="APPROVED")["money"]] == ["Approved"]
    assert [m["currency"] for m in overview(w["acme"], branch_id=w["zz_branch"])["money"]] == ["EUR"]
    # Never converted 1:1: without rates the converted total is unavailable and says why.
    converted = overview(w["acme"], convert_to="INR")["converted"]
    assert converted["available"] is False and {i["currency"] for i in converted["issues"]} == {"AED", "EUR"}
    assert w["acme"].get(OVERVIEW, params={**SEP, "status": "NOPE"}).json()["code"] == "INVALID_STATUS"
    assert w["acme"].get(OVERVIEW, params={"period_from": "2026-09-30", "period_to": "2026-09-01"}).json()[
        "code"] == "INVALID_PERIOD_RANGE"


def test_ec_ux_07_statutory_totals_only_for_entities_whose_pack_defines_them(kpi_world):
    w = kpi_world
    statutory = {s["legal_entity_id"]: s for s in overview(w["acme"], status="ALL")["statutory"]}
    # The UAE run carries a line coded like an Indian PF component: its entity's pack is a draft, so no
    # statutory KPI is shown for it at all.
    assert set(statutory) == {w["le01"], w["zz01"]}
    zz = statutory[w["zz01"]]
    assert zz["pack_version"] == "ZZ-2026.1" and zz["watermark"] is None
    assert [(i["code"], i["label"], i["amount"], i["currency"]) for i in zz["items"]] == [
        ("ZZ_PENSION_EE", "Pension (employee)", "350.00", "EUR"),
        ("ZZ_PENSION_ER", "Pension (employer)", "490.00", "EUR"),
        ("ZZ_WHT", "Wage withholding", "500.00", "EUR")]
    india = statutory[w["le01"]]
    assert india["watermark"] == "FIXTURE — not law"
    assert {(i["code"], i["label"], i["amount"]) for i in india["items"]} == {
        ("IN_EPF_EE", "Provident fund (employee)", "3600.00"), ("IN_EPF_ER", "Provident fund (employer)", "3500.00")}
    assert overview(w["acme"], legal_entity_id=w["ae01"], status="ALL")["statutory"] == []


def test_paid_needs_verified_settlement_evidence_and_holds_are_counted(kpi_world):
    from app.core import clock
    from app.models.payment_flows import PaymentInstruction, PaymentObligation

    w = kpi_world
    cid = w["cid"]
    run = w["runs"]["IN_SEP"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    with session(cid) as (db, _):
        instructions = {}
        for employee, status in ((emma, "SUBMITTED"), (ivan, "ON_HOLD")):
            obligation = PaymentObligation(
                company_id=cid, source_type="RUN_EMPLOYEE", payee_type="EMPLOYEE", employee_id=employee,
                legal_entity_id=w["le01"], run_id=run["id"], run_employee_id=run["members"][employee],
                pay_group_id=w["groups"]["IN"], pay_date=date(2026, 9, 30), amount=1, currency="INR", status="OPEN",
                row_version=1)
            db.add(obligation)
            db.flush()
            row = PaymentInstruction(
                company_id=cid, obligation_id=obligation.id, employee_id=employee, legal_entity_id=w["le01"],
                run_id=run["id"], sequence=1, idempotency_key=f"test-{employee}", beneficiary_snapshot={}, amount=1,
                currency="INR", status=status, evidence_status="UNVERIFIED", row_version=1,
                submission_channel="MANUAL_UPLOAD" if status == "SUBMITTED" else None,
                submitted_at=clock.utcnow() if status == "SUBMITTED" else None)
            db.add(row)
            db.flush()
            instructions[employee] = row.id
        db.commit()
    data = overview(w["acme"])
    # A file submitted for manual payment is not "paid" (unverified, I-17).
    assert data["kpis"]["paid"] == 0 and data["kpis"]["on_hold"] == 1
    assert names(w, drill(w["acme"], "on_hold")["items"]) == ["Ivan"]
    sql("UPDATE payment_instructions SET status = 'SETTLED', evidence_status = 'VERIFIED', "
        "evidence_source = 'STATEMENT', settled_at = now() WHERE id = :i", i=instructions[emma])
    data = overview(w["acme"])
    assert data["kpis"]["paid"] == 1 and names(w, drill(w["acme"], "paid")["items"]) == ["Emma"]
    # A run-level payment hold counts as on hold too.
    sql("UPDATE payroll_run_employees SET hold_type = 'PAYMENT', hold = '{\"reason_code\": \"OTHER\"}'::jsonb "
        "WHERE id = :r", r=w["runs"]["ZZ_SEP"]["members"][w["emp"]["Zoe"]["id"]])
    data = overview(w["acme"])
    assert data["kpis"]["on_hold"] == 2
    assert_drilldowns_match(w["acme"], data)


def test_deadlines_exceptions_and_scope(app, kpi_world):
    w = kpi_world
    for run, code, severity, status in ((w["runs"]["AE_SEP"]["id"], "MISSING_BANK", "BLOCKING", "OPEN"),
                                        (w["runs"]["ZZ_SEP"]["id"], "NET_DROP", "WARNING", "OPEN"),
                                        (w["runs"]["IN_SEP"]["id"], "OLD", "WARNING", "FIXED")):
        sql("INSERT INTO payroll_exceptions (company_id, run_id, code, severity, message, dedupe_key, status, "
            "resolved_at, created_at, updated_at) VALUES (:c, :r, :code, :s, 'x', :code, :st, "
            "CASE WHEN :st = 'OPEN' THEN NULL ELSE now() END, now(), now())",
            c=w["cid"], r=run, code=code, s=severity, st=status)
    data = overview(w["acme"])
    assert data["exceptions"] == {"open": 2, "blocking": 1, "by_code": {"MISSING_BANK": 1, "NET_DROP": 1}}
    deadlines = {d["pay_group"]: d for d in data["deadlines"]}
    assert set(deadlines) == {"PG-IN", "PG-ZZ", "PG-AE"}
    assert (deadlines["PG-IN"]["input_cutoff"], deadlines["PG-IN"]["approval_deadline"],
            deadlines["PG-IN"]["pay_date"]) == ("2026-09-25", "2026-09-28", "2026-09-30")
    assert deadlines["PG-AE"]["run"]["stage"] == "Approved" and deadlines["PG-ZZ"]["run"]["stage"] == "Final"
    october = overview(w["acme"], period_from="2026-10-01", period_to="2026-10-31")
    assert {d["pay_group"] for d in october["deadlines"]} == {"PG-IN", "PG-ZZ", "PG-AE"}
    assert all(d["run"] is None for d in october["deadlines"]) and october["kpis"]["processed"] == 0
    assert overview(w["acme"])["definitions"]["paid"].startswith("Every payment instruction settled")

    cleo = w["cleo"]  # run read and reports at ZZ01 only
    scoped = overview(cleo)
    assert scoped["kpis"] == {"eligible": 2, "with_salary": 0, "without_salary": 2, "processed": 2, "paid": 0,
                              "on_hold": 0}
    assert [m["currency"] for m in scoped["money"]] == ["EUR"]
    assert [s["legal_entity_id"] for s in scoped["statutory"]] == [w["zz01"]]
    assert scoped["exceptions"]["by_code"] == {"NET_DROP": 1}
    assert [d["pay_group"] for d in scoped["deadlines"]] == ["PG-ZZ"]
    assert_drilldowns_match(cleo, scoped)
    assert overview(cleo, legal_entity_id=w["le01"])["kpis"]["eligible"] == 0
    employee = with_roles(app, w["acme"], "Eve", ["employee"])
    assert employee.get(OVERVIEW, params=SEP).status_code == 403
    assert employee.get(f"{OVERVIEW}/eligible", params=SEP).status_code == 403
    default = ok(w["acme"].get(OVERVIEW))
    assert default["filters"]["period_from"].endswith("-01")


def test_ec_sec_01_three_tenants_and_empty_states(app, kpi_world):
    w = kpi_world
    beta = other_tenant(app, "Beta Industries")
    data = overview(beta["client"])
    assert data["kpis"] == {"eligible": 1, "with_salary": 0, "without_salary": 1, "processed": 1, "paid": 0,
                            "on_hold": 0}
    assert data["empty_state"] == "NO_SALARIES" and data["processing_disabled_reason"]
    assert [m["gross"] for m in data["money"]] == ["7777.00"]
    assert drill(beta["client"], "eligible")["items"][0]["employee_id"] == beta["employee"]["id"]
    # Another tenant's ids as filters reveal nothing.
    assert overview(beta["client"], legal_entity_id=w["le01"])["kpis"]["eligible"] == 0
    assert overview(beta["client"], pay_group_id=w["groups"]["ZZ"])["money"] == []
    fresh = signup_company(app, "Cobalt Labs")
    empty = overview(fresh)
    assert empty["kpis"]["eligible"] == 0 and empty["empty_state"] == "NO_EMPLOYEES"
    assert empty["money"] == [] and empty["statutory"] == [] and empty["deadlines"] == []
    assert overview(w["acme"])["kpis"]["eligible"] == 6 and overview(w["acme"])["empty_state"] is None
