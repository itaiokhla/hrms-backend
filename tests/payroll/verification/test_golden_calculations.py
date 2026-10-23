"""Golden calculations through the real APIs (Goal 2 §9; 3.3 §11; 3.4 §5.2, §17.1; 3.5 §17).

Every amount asserted here is worked out BY HAND in the test with ``Decimal`` arithmetic and the reasoning in a
comment. Nothing is produced by calling the engine, the snapshot builder or a service: the product computes, the
test compares. Each scenario goes through the real chain - salary structure (POST /salary-structures + publish),
compensation assignment and approval, manual LOP inputs approved through the approvals engine, wage inputs,
claims, loans, overtime proposals from attendance, FX rates - then POST /runs -> /precheck -> /calculate (the real
snapshot builder and engine, jobs inline) -> /summary, /employees, lines.

The fixed facts of 2026 used below (checked in the comments): 1 Feb 2026 is a Sunday (28 days, 4 Sundays,
24 Mon-Sat working days); 1 Sep 2026 is a Tuesday (30 days, 4 Sundays, 26 Mon-Sat working days); 7 Sep 2026 is
a Monday. The tenant's work week is changed to Monday-Saturday through PATCH /org/work-weeks (3.4 §5.2 table).
The legal entity is moved to GB (no country pack: no statutory lines, I-15 attestation at finalization) except in
the IN fixture-pack scenario. Settings that change per scenario are written at PAY_GROUP scope so each pay group
is an independent fixture (and the inheritance PAY_GROUP -> TENANT is exercised).

The world helpers here are shared with test_reconciliation.py and test_run_lifecycle_e2e.py.
"""

import json
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.helpers import create_employee, signup_company
from tests.payroll.test_compensation import login_as
from tests.payroll.test_components import active_version_id, make_structure, ok, setup_catalogue
from tests.payroll.test_runs_lifecycle import PAY, RUNS, calendar_for, periods_of
from tests.payroll.test_wage_inputs import pay_group, place, set_setting, sql

D = Decimal
COMPENSATION = f"{PAY}/compensation"
WAGE_INPUTS = f"{PAY}/wage-inputs"
APPROVALS = f"{PAY}/approvals"
CLAIMS = "/api/v1/payroll/reimbursement-claims"
LOANS = "/api/v1/payroll/loans"
POLICIES = f"{PAY}/policies"
ATTESTATIONS = f"{PAY}/statutory-attestations"
FLAT = [{"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly * 100%"}]


def q(value, places="0.01"):
    """Round half up to the currency's minor unit (payroll.rounding.line_mode = HALF_UP, the default)."""
    return D(value).quantize(D(places), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# World helpers (shared)
# ---------------------------------------------------------------------------
def set_scoped(company_id, key, value, scope_type="PAY_GROUP", scope_id=None):
    """A setting value at a non-tenant scope (PAY_GROUP / LEGAL_ENTITY / EMPLOYEE) from 2000-01-01."""
    sql("DELETE FROM setting_values WHERE company_id = :c AND key = :k AND scope_type = :t AND scope_id = :i",
        c=company_id, k=key, t=scope_type, i=scope_id)
    sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
        "row_version, source, created_at, updated_at) VALUES (:c, :k, :t, :i, CAST(:v AS jsonb), false, "
        "'2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key, t=scope_type, i=scope_id,
        v=json.dumps(value))


def gold_world(app, name="Gold Corp", country="GB"):
    """A tenant with the seeded catalogue (+ HRA, 3.3 §11 Example A), two published structures (FLAT: BASIC =
    100% of the monthly CTC; STAFF_CTC: Example A), a Mon-Sat work week, attendance OFF at tenant level, the
    founder (payroll manager), approver Ava, processor Pia, HR admin Hana and checker Chen (payroll manager)."""
    acme = signup_company(app, name)
    cid = acme.company_id
    setup_catalogue(acme)
    # The tenant is created today (Oct 2026) but pays periods from January 2026: its component versions start on
    # the signup day, so they are moved back like the shared `component()` helper of the wage-input tests does.
    sql("UPDATE pay_component_versions SET effective_from = '2020-01-01' WHERE company_id = :c", c=cid)
    flat = active_version_id(make_structure(acme, code="FLAT", lines=FLAT, test_range={"min": "100000",
                                                                                         "max": "50000000"}))
    staff = active_version_id(make_structure(acme))
    le01 = sql("SELECT id FROM legal_entities WHERE company_id = :c AND code = 'LE01'", c=cid)[0][0]
    sql("UPDATE legal_entities SET country_code = :cc, country_confirmed = true WHERE id = :e", cc=country, e=le01)
    set_setting(cid, "payroll.attendance.enabled", False)
    weeks = ok(acme.get(f"{PAY}/org/work-weeks"))
    week = next(w for w in weeks if w["code"] == "MON_FRI")
    assert ok(acme.patch(f"{PAY}/org/work-weeks/{week['id']}", json={"weekly_off_days": [6]}))["weekly_off_days"] == [6]
    users = {
        "approver": login_as(app, acme, create_employee(acme, "Ava", "Approver"), ["payroll_approver"]),
        "processor": login_as(app, acme, create_employee(acme, "Pia", "Processor"), ["payroll_processor"]),
        "hr": login_as(app, acme, create_employee(acme, "Hana", "Hr"), ["hr_admin"]),
        "checker": login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"]),
    }
    acme.user_id = acme.me()["user"]["id"]
    return {"acme": acme, "cid": cid, "le01": le01, "flat": flat, "staff": staff, "app": app, **users}


def gold_group(w, code, currency="INR", settings=None, entity=None):
    """A monthly pay group with a calendar from 1 Jan 2026 and PAY_GROUP-scoped settings; {"id", "periods"}."""
    group = pay_group(w["cid"], entity or w["le01"], code, currency=currency)
    calendar_for(w["acme"], group, effective_from="2026-01-01")
    for key, value in (settings or {}).items():
        set_scoped(w["cid"], key, value, "PAY_GROUP", group)
    periods = {p["start"]: p for p in periods_of(w["acme"], group)}
    return {"id": group, "periods": periods, "currency": currency, "code": code}


def period_of(group, start):
    return group["periods"][start]


def hire(w, first, group, amount, *, hired="2024-01-15", start="2026-01-01", structure="flat", currency=None,
         fx_rule=None, entity=None):
    """An employee placed in the pay group with an approved annual CTC from `start` (maker founder, checker Ava)."""
    employee = create_employee(w["acme"], first, hire_date=hired)
    place(employee["id"], entity or w["le01"], group["id"])
    body = {"template_version_id": w[structure], "effective_from": start, "reason": "HIRE",
            "pay_basis_amount": {"amount": amount, "currency": currency or group["currency"]}}
    if fx_rule is not None:
        body["fx_rule"] = fx_rule
    proposed = ok(w["acme"].post(f"{PAY}/employees/{employee['id']}/compensation", json=body), 201)
    assert ok(w["approver"].post(f"{COMPENSATION}/{proposed['id']}/approve", json={}))["status"] == "ACTIVE"
    return employee["id"]


def terminate(w, employee_id, last_day):
    ok(w["acme"].post(f"/api/v1/employees/{employee_id}/termination", json={"last_working_day": last_day}))


def lop(w, employee_id, day, portion="FULL"):
    """A manual LOP input (maker founder) approved by HR through the approvals engine."""
    created = ok(w["acme"].post(f"{PAY}/lop-inputs", json={"employee_id": employee_id, "date": day,
                                                           "portion": portion, "reason": "Unauthorised absence"}), 201)
    assert ok(w["hr"].post(f"{APPROVALS}/{created['approval_request_id']}/approve", json={}))["status"] == "APPROVED"
    return created["id"]


def wage_input(w, employee_id, code, amount, period, currency="INR"):
    """A wage input filed by processor Pia and approved by the founder (maker != checker)."""
    body = {"employee_id": employee_id, "component_code": code, "period_start": period["start"],
            "period_end": period["end"], "amount": {"amount": amount, "currency": currency}}
    [item] = ok(w["processor"].post(WAGE_INPUTS, json=body), 201)["items"]
    if item["status"] != "APPROVED":
        assert ok(w["acme"].post(f"{WAGE_INPUTS}/{item['id']}/approve", json={}))["status"] == "APPROVED"
    return item["id"]


def activate_policy(w, type_code, fields):
    """A new ACTIVE version of the tenant's policy of the type, for everyone, effective 1 Jan 2024."""
    client = w["acme"]
    policy = next(p for p in ok(client.get(POLICIES, params={"type": type_code})))
    pid = policy["id"]
    ok(client.put(f"{POLICIES}/{pid}/assignments",
                  json={"rules": [{"criteria": {"everyone": True}, "priority": 0, "effective_from": "2024-01-01"}]}))
    if policy["active_version"] is None:
        ok(client.put(f"{POLICIES}/{pid}/versions/1", json={"fields": fields}))
        version = 1
    else:
        version = ok(client.post(f"{POLICIES}/{pid}/versions", json={"fields": fields}), 201)["version"]
    ok(client.post(f"{POLICIES}/{pid}/versions/{version}/activate",
                   json={"effective_from": "2024-01-01", "reason": "Golden test policy"}))


def open_findings(client, run_id, severity=None):
    params = {"status": "OPEN"}
    if severity:
        params["severity"] = severity
    return ok(client.get(f"{RUNS}/{run_id}/exceptions", params=params))["items"]


def calculated_run(w, group, start, expect_status="REVIEW_REQUIRED", client=None):
    """create -> precheck (READY) -> calculate (one job, inline) -> the run's status; returns the run id."""
    client = client or w["acme"]
    period = period_of(group, start)
    run = ok(client.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": group["id"],
                                     "pay_period_id": period["id"]}), 201)
    pre = ok(client.post(f"{RUNS}/{run['id']}/precheck"))
    assert pre["status"] == "READY", [(f["code"], f["message"]) for f in open_findings(client, run["id"], "BLOCKING")]
    calc = client.post(f"{RUNS}/{run['id']}/calculate", headers={"Idempotency-Key": f"gold-{run['id']}"})
    assert calc.status_code == 202, calc.text
    status = sql("SELECT status FROM payroll_runs WHERE id = :r", r=run["id"])[0][0]
    assert status == expect_status, (status, [(f["code"], f["employee_id"], f["message"])
                                              for f in open_findings(client, run["id"], "BLOCKING")])
    return run["id"]


def figures(run_id):
    """{employee_id: {gross, deductions, reimbursements, net, employer_cost, status, currency}} as stored."""
    rows = sql("SELECT employee_id, gross, deductions, reimbursements, net, employer_cost, status, currency, id "
               "FROM payroll_run_employees WHERE run_id = :r", r=run_id)
    return {e: {"gross": g, "deductions": d, "reimbursements": rb, "net": n, "employer_cost": c, "status": s,
                "currency": cur, "row": rid} for e, g, d, rb, n, c, s, cur, rid in rows}


def lines(run_id, employee_id):
    """{component_code: Σ amount} of one employee's lines, and {code: kind}."""
    result, kinds = {}, {}
    for code, kind, amount in sql(
            "SELECT l.component_code, l.kind, l.amount FROM payroll_lines l JOIN payroll_run_employees e "
            "ON e.id = l.run_employee_id WHERE l.run_id = :r AND e.employee_id = :e", r=run_id, e=employee_id):
        result[code] = result.get(code, D(0)) + D(amount)
        kinds[code] = kind
    return result, kinds


def run_totals(run_id):
    return sql("SELECT totals FROM payroll_runs WHERE id = :r", r=run_id)[0][0]


def reconcile(run_id):
    from tests.payroll.verification.test_reconciliation import assert_run_reconciles

    assert_run_reconciles(run_id)


# ---------------------------------------------------------------------------
# 3.4 §5.2: the February table for the four proration bases, a mid-month joiner and leaver
# ---------------------------------------------------------------------------
@pytest.fixture
def world(app):
    return gold_world(app)


BASES = ("CALENDAR_DAYS", "WORKING_DAYS", "FIXED_30", "FIXED_26")


def test_february_2026_table_four_bases_full_month_with_lop_joiner_and_leaver(world):
    """BASIC R = 30,000.00 (FLAT structure, annual CTC 3,60,000 / 12). February 2026: 28 days, starts on a
    Sunday, Mon-Sat week -> 24 working days, 4 Sundays (1, 8, 15, 22 Feb). No holidays.

    * Emma: employed all month, 2 LOP working days (Tue 3 and Wed 4 Feb, approved manual LOP).
    * Jay: joined Monday 16 Feb, no LOP: 13 calendar days (16-28), 12 working days (16-21, 23-28).
    * Leo: last working day Tuesday 10 Feb: 10 calendar days (1-10), working days 2-7 (6) + 9, 10 (2) = 8;
      days without weekly offs = 8 (1 and 8 Feb are Sundays).
    """
    w = world
    R = D("30000.00")
    expected = {
        # basis: (Emma, Jay, Leo) - 3.4 §5.1 formulas, the first two columns are the doc's golden vectors.
        "CALENDAR_DAYS": (q(R * 26 / 28),          # (28 employment days - 2 LOP) / 28 = 27,857.142.. -> 27,857.14
                          q(R * 13 / 28),          # 13 / 28 = 13,928.571.. -> 13,928.57
                          q(R * 10 / 28)),         # 10 / 28 = 10,714.285.. -> 10,714.29
        "WORKING_DAYS": (q(R * 22 / 24),           # (24 working days - 2 LOP) / 24 = 27,500.00
                         q(R * 12 / 24),           # 12 / 24 = 15,000.00
                         q(R * 8 / 24)),           # 8 / 24 = 10,000.00
        "FIXED_30": (q(R * (30 - 2) / 30),         # employed all month: N - min(lop, N) = 28 -> 28,000.00
                     q(R * 13 / 30),               # joined: min(30, 13 calendar days - 0) = 13 -> 13,000.00
                     q(R * 10 / 30)),              # left: min(30, 10 calendar days) = 10 -> 10,000.00
        "FIXED_26": (q(R * (26 - 2) / 26),         # 24 / 26 = 27,692.307.. -> 27,692.31
                     q(R * 12 / 26),               # joined: days excluding weekly offs = 12 -> 13,846.153.. -> 13,846.15
                     q(R * 8 / 26)),               # left: 8 days excluding weekly offs -> 9,230.769.. -> 9,230.77
    }
    # The doc's own numbers (3.4 §5.2), so a wrong hand formula above cannot hide a product error.
    assert [v[:2] for v in expected.values()] == [
        (D("27857.14"), D("13928.57")), (D("27500.00"), D("15000.00")), (D("28000.00"), D("13000.00")),
        (D("27692.31"), D("13846.15"))]
    for basis in BASES:
        group = gold_group(w, f"PG-{basis[:6]}-{basis[-2:]}", settings={"payroll.lop.proration_basis": basis})
        emma = hire(w, f"Emma{basis[:3]}", group, "360000")
        jay = hire(w, f"Jay{basis[:3]}", group, "360000", hired="2026-02-16", start="2026-02-16")
        leo = hire(w, f"Leo{basis[:3]}", group, "360000")
        terminate(w, leo, "2026-02-10")
        for day in ("2026-02-03", "2026-02-04"):
            lop(w, emma, day)
        run_id = calculated_run(w, group, "2026-02-01")
        stored = figures(run_id)
        for employee, value in zip((emma, jay, leo), expected[basis]):
            amounts, _ = lines(run_id, employee)
            assert amounts == {"BASIC": value}, (basis, employee, amounts, value)
            assert stored[employee]["gross"] == stored[employee]["net"] == stored[employee]["employer_cost"] == value
            assert stored[employee]["deductions"] == D("0")
        # Units on the BASIC line are the paid units U of the formula (R x U / N).
        units = {e: u for e, u in sql("SELECT e.employee_id, l.units FROM payroll_lines l JOIN payroll_run_employees e "
                                      "ON e.id = l.run_employee_id WHERE l.run_id = :r", r=run_id)}
        expected_units = {"CALENDAR_DAYS": (26, 13, 10), "WORKING_DAYS": (22, 12, 8), "FIXED_30": (28, 13, 10),
                          "FIXED_26": (24, 12, 8)}[basis]
        assert [units[e] for e in (emma, jay, leo)] == [D(u) for u in expected_units], (basis, units)
        total = sum(expected[basis], D(0))
        assert D(run_totals(run_id)["INR"]["net"]) == total and run_totals(run_id)["INR"]["employees"] == 3
        summary = ok(w["acme"].get(f"{RUNS}/{run_id}/summary"))
        assert summary["settings"]["payroll.lop.proration_basis"] == basis  # the pay-group override is in force
        assert summary["reconciles"] is True
        reconcile(run_id)


def test_full_period_example_a_ctc_split_and_half_day_lop_counted_once(world):
    """3.3 §11 Example A (STAFF_CTC): annual CTC 12,00,000 -> monthly 1,00,000; BASIC = 50% = 50,000.00,
    HRA = 40% of BASIC = 20,000.00, FIXED_ALLOWANCE = balancing = 1,00,000 - 50,000 - 20,000 = 30,000.00.
    September 2026, CALENDAR_DAYS (30 days).

    * Asha: full period -> 50,000 + 20,000 + 30,000 = 1,00,000.00.
    * Lara: LOP Thu 10 Sep (FULL) + Fri 11 Sep first half -> 1.5 LOP days; U = 30 - 1.5 = 28.5.
      BASIC 50,000 x 28.5 / 30 = 47,500.00; HRA 20,000 x 28.5 / 30 = 19,000.00; FA 30,000 x 28.5 / 30 = 28,500.00;
      gross 95,000.00. A second LOP input for the same half-day slot is refused, so the half day is deducted
      once (3.4 §4.1 anti-double-counting).
    """
    w = world
    group = gold_group(w, "PG-CTC")
    asha = hire(w, "Asha", group, "1200000", structure="staff")
    lara = hire(w, "Lara", group, "1200000", structure="staff")
    lop(w, lara, "2026-09-10")
    lop(w, lara, "2026-09-11", "FIRST_HALF")
    again = w["acme"].post(f"{PAY}/lop-inputs", json={"employee_id": lara, "date": "2026-09-11",
                                                      "portion": "FIRST_HALF", "reason": "Duplicate"})
    assert again.status_code in (400, 409), again.text
    full = w["acme"].post(f"{PAY}/lop-inputs", json={"employee_id": lara, "date": "2026-09-10", "portion": "FULL",
                                                     "reason": "Duplicate"})
    assert full.status_code in (400, 409), full.text
    run_id = calculated_run(w, group, "2026-09-01")
    amounts, kinds = lines(run_id, asha)
    assert amounts == {"BASIC": D("50000.00"), "HRA": D("20000.00"), "FIXED_ALLOWANCE": D("30000.00")}
    assert set(kinds.values()) == {"EARNING"}
    u = D("30") - D("1.5")
    amounts, _ = lines(run_id, lara)
    assert amounts == {"BASIC": q(D("50000") * u / 30), "HRA": q(D("20000") * u / 30),
                       "FIXED_ALLOWANCE": q(D("30000") * u / 30)}
    assert sum(amounts.values()) == D("95000.00")
    stored = figures(run_id)
    assert stored[asha]["net"] == D("100000.00") and stored[lara]["net"] == D("95000.00")
    assert D(run_totals(run_id)["INR"]["gross"]) == D("195000.00")
    reconcile(run_id)


def test_unpaid_leave_and_half_days_are_deducted_once_with_manual_lop(app, world):
    """Approved UNPAID leave from the leave module plus a manual LOP half day, CALENDAR_DAYS, FLAT 30,000.00.

    Leave cannot start in the past, so the scenario uses the pay period AFTER the current one (N calendar days,
    computed from the date). Ulla (manager Max approves her leave) takes unpaid leave on Wednesday W (FULL) and on
    Thursday W+1 (FIRST_HALF); HR approves a manual LOP for W+1 SECOND_HALF. A manual LOP on W (already unpaid
    leave) is refused (LOP_SLOT_TAKEN): the same slot is never deducted twice (3.4 §4.1).
      LOP days = 1 (W) + 0.5 (W+1 AM leave) + 0.5 (W+1 PM manual LOP) = 2
      BASIC = 30,000.00 x (N - 2) / N   (e.g. N = 30 -> 28,000.00; N = 31 -> 28,064.516.. -> 28,064.52)
    """
    import calendar as _calendar

    from tests.payroll.verification.conftest import today_of

    w = world
    group = gold_group(w, "PG-LEAVE", settings={"payroll.leave.enabled": True,
                                                "payroll.lop.proration_basis": "CALENDAR_DAYS"})
    today = today_of(w["acme"])
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    n = _calendar.monthrange(nxt.year, nxt.month)[1]
    wednesday = nxt + timedelta(days=7 + (2 - nxt.weekday()) % 7)        # the second-week Wednesday
    thursday = wednesday + timedelta(days=1)
    assert wednesday.weekday() == 2 and thursday.month == nxt.month
    boss = create_employee(w["acme"], "Max", "Manager")
    manager = login_as(app, w["acme"], boss, ["manager"])
    ulla = create_employee(w["acme"], "Ulla", "Unpaid", manager_id=boss["id"])
    place(ulla["id"], w["le01"], group["id"])
    body = {"template_version_id": w["flat"], "effective_from": "2026-01-01", "reason": "HIRE",
            "pay_basis_amount": {"amount": "360000", "currency": "INR"}}
    proposed = ok(w["acme"].post(f"{PAY}/employees/{ulla['id']}/compensation", json=body), 201)
    assert ok(w["approver"].post(f"{COMPENSATION}/{proposed['id']}/approve", json={}))["status"] == "ACTIVE"
    worker = login_as(app, w["acme"], ulla, ["employee"])
    for start, portion in ((wednesday, {}), (thursday, {"end_portion": "FIRST_HALF"})):
        r = worker.post("/api/v1/leaves/apply", json={"leave_type": "unpaid", "start_date": start.isoformat(),
                                                      "end_date": start.isoformat(), **portion})
        assert r.status_code == 200, r.text
        decided = manager.post(f"/api/v1/leaves/{r.json()['id']}/approve", json={})
        assert decided.status_code == 200 and decided.json()["status"] == "approved", decided.text
    for day, portion in ((wednesday, "FULL"), (thursday, "FIRST_HALF")):
        r = w["acme"].post(f"{PAY}/lop-inputs", json={"employee_id": ulla["id"], "date": day.isoformat(),
                                                      "portion": portion, "reason": "Duplicate of the leave"})
        assert r.status_code == 409 and r.json()["code"] == "LOP_SLOT_TAKEN", r.text
    lop(w, ulla["id"], thursday.isoformat(), "SECOND_HALF")
    run_id = calculated_run(w, group, nxt.isoformat())
    expected = q(D("30000.00") * (n - 2) / n)
    amounts, _ = lines(run_id, ulla["id"])
    assert amounts == {"BASIC": expected}, (amounts, expected, n)
    [(units,)] = sql("SELECT l.units FROM payroll_lines l JOIN payroll_run_employees e ON e.id = l.run_employee_id "
                     "WHERE l.run_id = :r AND e.employee_id = :e", r=run_id, e=ulla["id"])
    assert D(units) == D(n) - 2, units
    assert figures(run_id)[ulla["id"]]["net"] == expected
    reconcile(run_id)


def test_daily_rated_worker_is_paid_for_paid_working_days_only(world):
    """A DAILY structure pays the pay-basis rate per paid day (att.paid_days). September 2026, Mon-Sat week:
    26 working days, 4 Sundays. Weekly offs are paid for salaried employees only (3.4 §4.2 rule 5), so a daily
    worker employed all month with one LOP day (Wed 16 Sep) has 26 - 1 = 25 paid days:
    1,200.00 x 25 = 30,000.00."""
    w = world
    structure = ok(w["acme"].post(f"{PAY}/salary-structures", json={
        "code": "DAILY", "name": "Daily rate", "type": "DAILY",
        "lines": [{"component": "BASIC", "method": "RATE_X_UNITS"}], "test_range": {"min": "100", "max": "10000"}}), 201)
    w["daily"] = active_version_id(ok(w["acme"].post(f"{PAY}/salary-structures/{structure['id']}/publish",
                                                    json={"version": 1})))
    group = gold_group(w, "PG-DAY")
    dina = hire(w, "Dina", group, "1200", structure="daily")
    lop(w, dina, "2026-09-16")
    run_id = calculated_run(w, group, "2026-09-01")
    amounts, _ = lines(run_id, dina)
    assert amounts == {"BASIC": D("1200.00") * 25}, amounts
    units = sql("SELECT units, rate FROM payroll_lines WHERE run_id = :r AND component_code = 'BASIC'", r=run_id)
    assert units == [(D("25"), D("1200"))]
    reconcile(run_id)


# ---------------------------------------------------------------------------
# Variable pay, deductions, reimbursements, net rounding (one Example A run)
# ---------------------------------------------------------------------------
LOAN_POLICY = {"max_amount_formula": "3 * MONTHLY_GROSS", "max_tenure_months": 12, "interest_rate_percent": "0",
               "interest_method": "FLAT", "max_instalment_percent_of_net": "50", "min_service_months": 6,
               "max_active_loans": 1}
ADVANCE_POLICY = {"max_percent_of_monthly_net": "50", "recover_in_runs": 1, "max_per_year": 1}
CLAIM_POLICY = {"claim_types": [{"code": "TRAVEL", "name": "Travel", "taxability": "EXEMPT"}],
                "submission_window_days": 60, "duplicate_receipt_check": True}


def _disburse(company_id, loan_id, paid_on):
    """Disbursement is a payment-batch step (3.4 §15); recorded through the loans service as its tests do."""
    from app.payroll.services import loans
    from tests.payroll.test_wage_inputs import session

    with session(company_id) as (db, _):
        loan = loans.record_disbursement(db, None, company_id, loan_id, paid_on=paid_on, reference=f"PAY-{loan_id}")
        db.commit()
        return loan.status


def test_bonus_commission_loan_advance_protected_net_reimbursement_and_net_rounding(world):
    """September 2026, Example A (gross 1,00,000.00 a month), CALENDAR_DAYS, protected net 60% of gross,
    net pay rounded to a multiple of 10 (HALF_UP) at PAY_GROUP scope.

    Emma: BONUS 5,000.00 + COMMISSION 2,345.67 (approved wage inputs); loan 60,000.00 over 6 instalments at 0%
    FLAT -> instalment 60,000 / 6 = 10,000.00; approved TRAVEL claim 300.00 paid WITH_PAYROLL.
      gross = 1,00,000 + 5,000 + 2,345.67 = 1,07,345.67 (the claim is not a wage)
      net before rounding = 1,07,345.67 - 10,000.00 + 300.00 = 97,645.67
      protected floor = 60% x 1,07,345.67 = 64,407.402 -> the instalment fits (97,645.67 > floor)
      rounded to 10 HALF_UP: 97,645.67 -> 97,650.00, NET_ROUNDING = +4.33
      employer cost = gross = 1,07,345.67 (no employer contributions without a country pack)
    Ivan: salary advance 50,000.00 recovered in one run; floor = 60% x 1,00,000 = 60,000.00, so only
      1,00,000 - 60,000 = 40,000.00 is recovered now (EC-VAR-05), 10,000.00 is carried; net = 60,000.00.
    """
    from app.payroll.services import loans
    from tests.payroll.test_loans import pay_figures

    w = world
    cid = w["cid"]
    group = gold_group(w, "PG-VAR", settings={"payroll.rounding.net_pay_increment": "10"})
    emma = hire(w, "Emma", group, "1200000", structure="staff")
    ivan = hire(w, "Ivan", group, "1200000", structure="staff")
    period = period_of(group, "2026-09-01")
    wage_input(w, emma, "BONUS", "5000.00", period)
    wage_input(w, emma, "COMMISSION", "2345.67", period)

    for key in ("payroll.loans.enabled", "payroll.advances.enabled", "payroll.reimbursements.enabled"):
        set_setting(cid, key, True)
    set_setting(cid, "payroll.deductions.min_net_pay_percent", "60")
    activate_policy(w, "LOAN_POLICY", LOAN_POLICY)
    activate_policy(w, "ADVANCE_POLICY", ADVANCE_POLICY)
    activate_policy(w, "REIMBURSEMENT_POLICY", CLAIM_POLICY)
    # The application checks read the employee's monthly pay figures (gross 1,00,000, net 1,00,000); the hook is
    # replaced as in the loans tests so the eligibility check does not depend on finalized history.
    previous = loans.set_pay_figures_provider(lambda db, c, e, on, currency: pay_figures(D("100000"), D("100000")))
    try:
        loan = ok(w["processor"].post(LOANS, json={"employee_id": emma, "kind": "LOAN", "tenure": 6,
                                                   "amount": {"amount": "60000.00", "currency": "INR"}}), 201)
        advance = ok(w["processor"].post(LOANS, json={"employee_id": ivan, "kind": "ADVANCE",
                                                      "amount": {"amount": "50000.00", "currency": "INR"}}), 201)
    finally:
        loans.set_pay_figures_provider(previous)
    for item in (loan, advance):
        ok(w["acme"].post(f"{LOANS}/{item['id']}/approve", json={}))
        assert _disburse(cid, item["id"], date(2026, 8, 31)) == "ACTIVE"
    claim = ok(w["processor"].post(CLAIMS, json={"employee_id": emma, "claim_type": "TRAVEL",
                                                 "expense_date": "2026-09-10",
                                                 "amount": {"amount": "300.00", "currency": "INR"}}), 201)
    assert ok(w["acme"].post(f"{CLAIMS}/{claim['id']}/approve", json={}))["status"] == "APPROVED"

    run_id = calculated_run(w, group, "2026-09-01")
    amounts, kinds = lines(run_id, emma)
    gross = D("100000.00") + D("5000.00") + D("2345.67")
    assert amounts == {"BASIC": D("50000.00"), "HRA": D("20000.00"), "FIXED_ALLOWANCE": D("30000.00"),
                       "BONUS": D("5000.00"), "COMMISSION": D("2345.67"), "LOAN_RECOVERY": D("10000.00"),
                       "REIMBURSEMENT": D("300.00"), "NET_ROUNDING": D("4.33")}, amounts
    assert (kinds["LOAN_RECOVERY"], kinds["REIMBURSEMENT"], kinds["NET_ROUNDING"]) == (
        "DEDUCTION", "REIMBURSEMENT", "ROUNDING")
    before = gross - D("10000.00") + D("300.00")
    assert before == D("97645.67")
    stored = figures(run_id)
    assert stored[emma]["gross"] == gross and stored[emma]["deductions"] == D("10000.00")
    assert stored[emma]["reimbursements"] == D("300.00") and stored[emma]["net"] == D("97650.00")
    assert stored[emma]["employer_cost"] == gross  # reimbursements and rounding are not employer pay cost

    amounts, _ = lines(run_id, ivan)
    floor = D("100000.00") * D("60") / 100
    recovered = D("100000.00") - floor
    assert recovered == D("40000.00")
    assert amounts == {"BASIC": D("50000.00"), "HRA": D("20000.00"), "FIXED_ALLOWANCE": D("30000.00"),
                       "ADVANCE_RECOVERY": recovered}, amounts
    assert stored[ivan]["net"] == D("60000.00")
    protected = [f for f in open_findings(w["acme"], run_id) if f["employee_id"] == ivan]
    assert protected, "the partial recovery is raised for review (EC-VAR-05)"
    # Run totals per currency = Σ employees (hand sums).
    totals = run_totals(run_id)["INR"]
    assert D(totals["net"]) == D("97650.00") + D("60000.00")
    assert D(totals["gross"]) == gross + D("100000.00")
    assert D(totals["deductions"]) == D("10000.00") + D("40000.00")
    assert D(totals["reimbursements"]) == D("300.00")
    reconcile(run_id)


# ---------------------------------------------------------------------------
# Currencies: 0 and 3 decimals, FX conversion of a contract currency (never 1:1)
# ---------------------------------------------------------------------------
def test_jpy_and_kwd_pay_groups_round_to_their_minor_units(world):
    """September 2026 (30 days), CALENDAR_DAYS, FLAT structure.
    JPY (0 decimals): annual 3,650,000 -> monthly 304,166.666.. -> 304,167; with one LOP day (Wed 16 Sep):
      304,166.666.. x 29 / 30 = 294,027.77.. -> 294,028 (rounding the rate first gives the same 294,028.1 -> 294,028).
    KWD (3 decimals): annual 10,001 -> monthly 833.41666.. -> 833.417; one LOP day:
      833.41666.. x 29 / 30 = 805.63611.. -> 805.636 (833.417 x 29 / 30 = 805.6364 -> 805.636 as well).
    Totals stay per currency: JPY 304,167 + 294,028 = 598,195; KWD 833.417 + 805.636 = 1,639.053."""
    w = world
    jpy = gold_group(w, "PG-JPY", currency="JPY")
    kwd = gold_group(w, "PG-KWD", currency="KWD")
    j1, j2 = hire(w, "Jun", jpy, "3650000"), hire(w, "Jiro", jpy, "3650000")
    k1, k2 = hire(w, "Kam", kwd, "10001"), hire(w, "Karim", kwd, "10001")
    lop(w, j2, "2026-09-16")
    lop(w, k2, "2026-09-16")
    jpy_run = calculated_run(w, jpy, "2026-09-01")
    kwd_run = calculated_run(w, kwd, "2026-09-01")
    monthly_jpy = D("3650000") / 12
    monthly_kwd = D("10001") / 12
    assert lines(jpy_run, j1)[0] == {"BASIC": q(monthly_jpy, "1")} == {"BASIC": D("304167")}
    assert lines(jpy_run, j2)[0] == {"BASIC": q(monthly_jpy * 29 / 30, "1")} == {"BASIC": D("294028")}
    assert lines(kwd_run, k1)[0] == {"BASIC": q(monthly_kwd, "0.001")} == {"BASIC": D("833.417")}
    assert lines(kwd_run, k2)[0] == {"BASIC": q(monthly_kwd * 29 / 30, "0.001")} == {"BASIC": D("805.636")}
    assert run_totals(jpy_run) == {"JPY": {"gross": "598195", "deductions": "0", "reimbursements": "0",
                                           "net": "598195", "employer_cost": "598195", "employees": 2}}
    assert run_totals(kwd_run) == {"KWD": {"gross": "1639.053", "deductions": "0.000", "reimbursements": "0.000",
                                           "net": "1639.053", "employer_cost": "1639.053", "employees": 2}}
    # The API speaks money as decimal strings with the currency's minor units, never floats.
    item = next(i for i in ok(w["acme"].get(f"{RUNS}/{kwd_run}/employees"))["items"] if i["employee_id"] == k2)
    assert item["net"] == {"amount": "805.636", "currency": "KWD"}
    for run_id in (jpy_run, kwd_run):
        reconcile(run_id)


def test_fx_contract_currency_blocks_without_a_rate_then_converts_at_the_period_snapshot(world):
    """Fiona's contract is USD 36,000 a year paid by an INR pay group (fx_rule PERIOD_SNAPSHOT). Without a USD->INR
    rate for the conversion date (the pay date, payroll.fx.conversion_date_rule = PAY_DATE) the calculation blocks
    with FX_MISSING: never 1:1. With a manual MID rate of 83.25 on the pay date:
      36,000 x 83.25 = 29,97,000.00 INR a year -> BASIC = 29,97,000 / 12 = 2,49,750.00."""
    w = world
    cid = w["cid"]
    set_setting(cid, "payroll.fx.manual_rate_requires_approval", False)
    group = gold_group(w, "PG-FX")
    fiona = hire(w, "Fiona", group, "36000", currency="USD", fx_rule={"source": "PERIOD_SNAPSHOT"})
    period = period_of(group, "2026-09-01")
    run = ok(w["acme"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": group["id"],
                                        "pay_period_id": period["id"]}), 201)
    blocked = run["id"]
    # The pre-check already refuses: FX_MISSING is BLOCKING and the run waits in INPUTS_PENDING.
    assert ok(w["acme"].post(f"{RUNS}/{blocked}/precheck"))["status"] == "INPUTS_PENDING"
    codes = {f["code"] for f in open_findings(w["acme"], blocked, "BLOCKING") if f["employee_id"] == fiona}
    assert "FX_MISSING" in codes, codes
    r = w["acme"].post(f"{RUNS}/{blocked}/calculate", headers={"Idempotency-Key": f"gold-fx-early-{blocked}"})
    assert r.status_code == 409, r.text
    # Never converted 1:1: no line at all exists (36,000 / 12 = 3,000 is never paid as 3,000 INR).
    assert sql("SELECT count(*) FROM payroll_lines WHERE run_id = :r", r=blocked)[0][0] == 0

    rate = ok(w["acme"].post(f"{PAY}/fx-rates", json={"base_currency": "USD", "quote_currency": "INR",
                                                       "rate": "83.25", "as_of": period["pay_date"]}), 201)
    assert rate["status"] == "ACTIVE", rate
    assert ok(w["acme"].post(f"{RUNS}/{blocked}/precheck"))["status"] == "READY"
    r = w["acme"].post(f"{RUNS}/{blocked}/calculate", headers={"Idempotency-Key": f"gold-fx-{blocked}"})
    assert r.status_code == 202, r.text
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=blocked)[0][0] == "REVIEW_REQUIRED",         [(f["code"], f["message"]) for f in open_findings(w["acme"], blocked, "BLOCKING")]
    annual_inr = D("36000") * D("83.25")
    assert annual_inr == D("2997000.00")
    assert lines(blocked, fiona)[0] == {"BASIC": q(annual_inr / 12)} == {"BASIC": D("249750.00")}
    assert figures(blocked)[fiona]["currency"] == "INR"
    summary = ok(w["acme"].get(f"{RUNS}/{blocked}/summary"))
    assert summary["fx"], "the FX snapshot used is shown with the run"
    reconcile(blocked)


# ---------------------------------------------------------------------------
# Overtime from attendance with daily + weekly thresholds; hourly pay (3.4 §17.1)
# ---------------------------------------------------------------------------
OT_POLICY = {
    "source": "ATTENDANCE_AUTO", "daily_threshold": "9", "weekly_threshold_hours": "48",
    "threshold_combination": "DAILY_THEN_WEEKLY", "min_minutes": 30,
    "rounding": {"increment_minutes": 15, "mode": "DOWN"},
    # No rate_formula: the policy default SUM_TAG_HOURLY("OT_BASE") is exercised (3.4 §9.4: period rates / basis
    # days / standard hours, day rates / standard hours, hourly rates unchanged).
    "buckets":[{"day_type": "WORKDAY", "after_hours": "0", "multiplier": "1.5"},
                {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": "2"},
                {"day_type": "HOLIDAY", "after_hours": "0", "multiplier": "2"}],
}


def worked(company_id, employee_id, day, start, end):
    """An approved attendance session from `start` to `end` (local time, HH:MM) on `day`."""
    import pytz

    zone = pytz.timezone(sql("SELECT timezone FROM companies WHERE id = :c", c=company_id)[0][0] or "UTC")

    def at(hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        return zone.localize(datetime.combine(day, time(h, m)))

    sql("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
        "status, created_at, updated_at) VALUES (:c, :e, :i, :o, :d, true, 'manual', 'present', now(), now())",
        c=company_id, e=employee_id, d=day, i=at(start), o=at(end))


def september_working_days():
    day, result = date(2026, 9, 1), []
    while day.month == 9:
        if day.weekday() != 6:   # Sunday is the weekly off
            result.append(day)
        day += timedelta(days=1)
    assert len(result) == 26
    return result


def test_overtime_daily_then_weekly_thresholds_and_hourly_pay_from_attendance(world):
    """3.4 §17.1 in September 2026 (attendance ON, overtime ON, FIXED_26 at PAY_GROUP scope, Mon-Sat week).

    Omar (salaried, FLAT, annual 3,12,000 -> OT_BASE BASIC 26,000.00): 8 h on every working day, except the week
    Mon 7 - Sat 12 Sep: 10, 9, 9, 11, 9, 8 h. Policy: daily 9 h, weekly 48 h, DAILY_THEN_WEEKLY, 1.5x workday.
      daily overtime: Mon 1 h, Thu 2 h = 3 h; regular hours 9+9+9+9+9+8 = 53 -> weekly 53 - 48 = 5 h (Saturday)
      other weeks: <= 6 x 8 = 48 h -> none. Total 8 h, each counted once.
      hourly rate = 26,000 / 26 / 8 = 125.00; overtime = 8 x 125.00 x 1.5 = 1,500.00
      BASIC (FIXED_26, no LOP): 26,000.00. Gross 27,500.00.
      Regression (defect found here): the generated ATTENDANCE_AUTO entries were paid AND the engine derived the
      same threshold hours again from the ledger -> 16 h = 3,000.00. Fixed in engine/pipeline.overtime_stage
      (pure regression: tests/payroll_unit/test_overtime_auto_stored_entries.py).
    Hana (HOURLY, 250.00 an hour): 8 h on every working day except Tue 1 Sep 09:00-15:00 (6 h), Mon 14 Sep
    09:00-19:00 (10 h) and Tue 15 Sep 09:00-16:00 (7 h).
      week 14-19 Sep: 10, 7, 8, 8, 8, 8 -> daily overtime Mon 10 - 9 = 1 h; regular 9 + 7 + 4 x 8 = 48 -> no
      weekly overtime. Other weeks: at most 6 x 8 = 48 h. Total overtime 1 h.
      worked hours = 23 x 8 + 6 + 10 + 7 = 207
      3.4 §9.4-§9.5: "an OT multiplier is the total rate for the hour, because overtime hours are not included
      in salary"; workday hours beyond the threshold go to the WORKDAY bucket only -> the overtime hour is not
      also a payable hour: payable hours = 207 - 1 = 206; BASIC = 206 x 250.00 = 51,500.00.
      OT rate (default SUM_TAG_HOURLY, hourly rate unchanged) = 250.00; OT = 1 x 250.00 x 1.5 = 375.00.
      Gross 51,500.00 + 375.00 = 51,875.00 (the overtime hour is paid 1.5x, not 1.0x + 1.5x = 2.5x).
      Regression (defect found here, 23 Oct 2026): BASIC was 207 x 250.00 = 51,750.00 - the overtime hour was
      also a payable hour. Fixed in engine/earnings.payable_hours (+ OvertimeHours.from_worked_time); pure
      regression: tests/payroll_unit/test_overtime_hourly_payable_hours.py.
    """
    w = world
    cid = w["cid"]
    structure = ok(w["acme"].post(f"{PAY}/salary-structures", json={
        "code": "HOURLY", "name": "Hourly rate", "type": "HOURLY",
        "lines": [{"component": "BASIC", "method": "RATE_X_UNITS"}], "test_range": {"min": "10", "max": "5000"}}), 201)
    w["hourly"] = active_version_id(ok(w["acme"].post(f"{PAY}/salary-structures/{structure['id']}/publish",
                                                     json={"version": 1})))
    group = gold_group(w, "PG-OT", settings={"payroll.attendance.enabled": True, "payroll.overtime.enabled": True,
                                             "payroll.lop.proration_basis": "FIXED_26"})
    activate_policy(w, "OVERTIME_POLICY", OT_POLICY)
    # Attendance pay needs an explicit policy decision before the first run (CONFIG_DECISION_REQUIRED otherwise).
    activate_policy(w, "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT", "missing_punch_out": "EXCEPTION"})
    omar = hire(w, "Omar", group, "312000")
    hana = hire(w, "Hanna", group, "250", structure="hourly")
    week = {date(2026, 9, 7): "19:00", date(2026, 9, 8): "18:00", date(2026, 9, 9): "18:00",
            date(2026, 9, 10): "20:00", date(2026, 9, 11): "18:00", date(2026, 9, 12): "17:00"}
    hana_days = {date(2026, 9, 1): "15:00", date(2026, 9, 14): "19:00", date(2026, 9, 15): "16:00"}
    for day in september_working_days():
        worked(cid, omar, day, "09:00", week.get(day, "17:00"))
        worked(cid, hana, day, "09:00", hana_days.get(day, "17:00"))
    generated = ok(w["acme"].post("/api/v1/overtime/proposals/generate",
                                  json={"start": "2026-09-01", "end": "2026-09-30", "employee_ids": [omar, hana]}))
    proposed = sorted((c["employee_id"], c["date"], c["minutes"], c["status"]) for c in generated["created"])
    assert proposed == sorted([(omar, "2026-09-07", 60, "APPROVED"), (omar, "2026-09-10", 120, "APPROVED"),
                               (omar, "2026-09-12", 300, "APPROVED"), (hana, "2026-09-14", 60, "APPROVED")]), proposed

    run_id = calculated_run(w, group, "2026-09-01")
    rate = D("26000.00") / 26 / 8
    assert rate == D("125")
    overtime = (1 + 2 + 5) * rate * D("1.5")
    amounts, _ = lines(run_id, omar)
    assert amounts == {"BASIC": D("26000.00"), "OT_WORKDAY": q(overtime)} == {
        "BASIC": D("26000.00"), "OT_WORKDAY": D("1500.00")}, amounts
    worked_hours = 23 * 8 + 6 + 10 + 7
    assert worked_hours == 207
    overtime_hours = 10 - 9
    payable = worked_hours - overtime_hours
    amounts, _ = lines(run_id, hana)
    assert amounts == {"BASIC": q(D("250.00") * payable), "OT_WORKDAY": q(overtime_hours * D("250.00") * D("1.5"))} \
        == {"BASIC": D("51500.00"), "OT_WORKDAY": D("375.00")}, amounts
    assert figures(run_id)[hana]["gross"] == D("51875.00")
    reconcile(run_id)


# ---------------------------------------------------------------------------
# Employer contributions outside net: the IN fixture pack (labelled FIXTURE - not law)
# ---------------------------------------------------------------------------
def test_in_fixture_pack_employer_contributions_stay_outside_net(app):
    """LE01 stays an India entity (IN fixture pack, test environment only, every line badged fixture). EPF is ON
    for the entity; ESI and PT stay OFF; income-tax withholding is ON (locked by the pack).
    August 2026 (before the 17 Sep 2026 ceiling change): PF wage 20,000.00 (PF_BASIC, 100% of the monthly CTC
    2,40,000 / 12) -> base = min(20,000, ceiling 15,000) = 15,000.
      IN_EPF_EE = 12% x 15,000 = 1,800.00 (employee deduction, whole rupees)
      IN_EPF_ER = 12% x 15,000 = 1,800.00; IN_EDLI_ER = 0.50% x 15,000 = 75.00; IN_EPF_ADMIN_ER = 0.50% x 15,000 = 75.00
      income tax (new regime, PF_BASIC is tagged TAXABLE_INCOME so the projection is not silently empty - the
      known fixture defect with untagged earnings does not apply): tax year April-March, August leaves 8 periods;
      projected = YTD 0 + 20,000 + 20,000 x 7 = 1,60,000; - 75,000 standard deduction = 85,000, inside the
      0% band up to 4,00,000 -> annual tax 0 -> no IN_TDS line (even a full year, 2,40,000 - 75,000, is nil).
      net = 20,000 - 1,800 = 18,200.00; employer cost = 20,000 + 1,800 + 75 + 75 = 21,950.00.
    """
    from app.main import start_payroll_runtime
    from app.payroll.settings_registry import pack_setting_keys, unregister_pack_settings

    before = set(pack_setting_keys())
    start_payroll_runtime()      # the API start-up hook registers the pack's settings (JOBS_MODE=inline: no worker)
    try:
        _in_fixture_golden(app)
    finally:
        for country in sorted({key.split(".")[2] for key in set(pack_setting_keys()) - before}):
            unregister_pack_settings(country)


def _in_fixture_golden(app):
    w = gold_world(app, "Fixture Co", country="IN")
    cid = w["cid"]
    from tests.payroll.test_components import add_component

    ok(add_component(w["acme"], "PF_BASIC", category="BASE", method="FIXED", name="Basic (PF wage)",
                     tags=["LOP_APPLICABLE", "PART_OF_CTC", "PART_OF_GROSS", "IN_PF_WAGE", "TAXABLE_INCOME"],
                     proration="BY_PAID_DAYS"), 201)
    sql("UPDATE pay_component_versions SET effective_from = '2020-01-01' WHERE company_id = :c", c=cid)
    w["pf"] = active_version_id(make_structure(w["acme"], code="PF_FLAT", test_range={"min": "100000", "max": "5000000"},
                                               lines=[{"component": "PF_BASIC", "method": "FORMULA",
                                                       "formula": "ctc.monthly * 100%"}]))
    set_scoped(cid, "payroll.statutory.IN.epf.enabled", True, "LEGAL_ENTITY", w["le01"])
    group = gold_group(w, "PG-INF")
    priya = hire(w, "Priya", group, "240000", structure="pf")
    # Income-tax withholding is mandatory: the pack resolves it ON (REQUIRED_BY_LAW), nobody switched it on.
    values = ok(w["acme"].get(f"{PAY}/settings/values", params={"scope_type": "LEGAL_ENTITY", "scope_id": w["le01"]}))
    tds = next(i for i in values["items"] if i["key"] == "payroll.statutory.IN.tds.enabled")
    assert tds["value"] is True and tds["level"] == "COUNTRY_PACK"
    run_id = calculated_run(w, group, "2026-08-01")
    base = min(D("20000.00"), D("15000"))
    expected = {"PF_BASIC": D("20000.00"), "IN_EPF_EE": q(base * 12 / 100), "IN_EPF_ER": q(base * 12 / 100),
                "IN_EDLI_ER": q(base * D("0.5") / 100), "IN_EPF_ADMIN_ER": q(base * D("0.5") / 100)}
    amounts, kinds = lines(run_id, priya)
    assert amounts == expected, amounts
    assert kinds["IN_EPF_EE"] == "DEDUCTION" and {kinds[c] for c in ("IN_EPF_ER", "IN_EDLI_ER", "IN_EPF_ADMIN_ER")} == {
        "EMPLOYER_CONTRIBUTION"}
    assert sql("SELECT bool_and(fixture) FROM payroll_lines WHERE run_id = :r AND statutory", r=run_id)[0][0] is True
    stored = figures(run_id)[priya]
    assert stored["net"] == D("20000.00") - D("1800.00") == D("18200.00")
    assert stored["employer_cost"] == D("20000.00") + D("1800.00") + D("75.00") + D("75.00") == D("21950.00")
    assert stored["deductions"] == D("1800.00")
    reconcile(run_id)
    # Employer liabilities stay apart from withholding in what reviewers see: employee portions 1,800.00 (the
    # EPF deduction withheld from net), employer portions 1,800.00 + 75.00 + 75.00 = 1,950.00 (on top of gross).
    from tests.payroll.verification.test_reconciliation import assert_summary_matches

    summary = assert_summary_matches(w["acme"], run_id)
    assert sum((D(i["employee_amount"]) for i in summary["liabilities"]), D(0)) == D("1800.00")
    assert sum((D(i["employer_amount"]) for i in summary["liabilities"]), D(0)) == D("1950.00")
    assert all(i.get("fixture") is True for i in summary["liabilities"]), summary["liabilities"]


# ---------------------------------------------------------------------------
# Final settlement (3.5 §15)
# ---------------------------------------------------------------------------
def test_final_settlement_pays_salary_to_the_last_day_less_a_contractual_notice_recovery(world):
    """Leila (FLAT, annual 3,60,000 -> 30,000.00 a month) resigns; last working day Thursday 10 September 2026.
    CALENDAR_DAYS: salary to the last day = 30,000 x 10 / 30 = 10,000.00. A notice shortfall recovery of
    5,000.00 is confirmed with its contract reference; every other checklist item is waived with a reason.
    Net of the FINAL_SETTLEMENT run = 10,000.00 - 5,000.00 = 5,000.00 (nothing invented: no gratuity amount
    without a qualified source)."""
    w = world
    acme = w["acme"]
    cases = f"{PAY}/final-settlements"
    group = gold_group(w, "PG-FNF")
    leila = hire(w, "Leila", group, "360000")
    terminate(w, leila, "2026-09-10")
    created = ok(acme.post(cases, json={"employee_id": leila, "termination_type": "RESIGNATION",
                                        "last_working_day": "2026-09-10", "notice_given_on": "2026-09-01"}), 201)
    data = ok(acme.get(f"{cases}/{created['id']}"))
    notice = next(i for i in data["items"] if i["item_type"] == "NOTICE_PAY")
    ok(acme.post(f"{cases}/{created['id']}/items/{notice['id']}/confirm",
                 json={"amount": {"amount": "-5000.00", "currency": "INR"}, "contract_ref": "Offer letter clause 9",
                       "note": "Notice shortfall"}))
    for entry in ok(acme.get(f"{cases}/{created['id']}"))["items"]:
        if entry["status"] not in ("PENDING", "BLOCKED"):
            continue
        if entry["item_type"] in ("SALARY_TO_LAST_DAY", "TAX", "PENDING_ARREARS", "PENDING_CLAIMS", "PENDING_INPUTS",
                                  "LOAN_RECOVERY") and entry["status"] == "PENDING":
            ok(acme.post(f"{cases}/{created['id']}/items/{entry['id']}/confirm", json={}))
        else:
            ok(acme.post(f"{cases}/{created['id']}/items/{entry['id']}/waive", json={"note": "Not due under the contract"}))
    data = ok(acme.get(f"{cases}/{created['id']}"))
    assert data["status"] == "READY" and data["totals"]["recovered"] == "-5000.00", data
    run = ok(acme.post(f"{cases}/{created['id']}/run", json={}), 201)
    assert run["run_type"] == "FINAL_SETTLEMENT"
    # The confirmed recovery is a system wage input awaiting approval: the settlement cannot be calculated
    # without it (regression: it used to be a WARNING, so the case could settle without the recovery).
    assert ok(acme.post(f"{RUNS}/{run['id']}/precheck"))["status"] == "INPUTS_PENDING"
    assert "PENDING_INPUTS" in {f["code"] for f in open_findings(acme, run["id"], "BLOCKING")}
    [(input_id, status)] = sql("SELECT id, status FROM wage_inputs WHERE employee_id = :e", e=leila)
    assert status == "PENDING_APPROVAL"
    assert ok(w["processor"].post(f"{WAGE_INPUTS}/{input_id}/approve", json={}))["status"] == "APPROVED"
    assert ok(acme.post(f"{RUNS}/{run['id']}/precheck"))["status"] == "READY"
    assert acme.post(f"{RUNS}/{run['id']}/calculate", headers={"Idempotency-Key": "fnf-1"}).status_code == 202
    # Without a protected-net percentage an automated recovery is never deducted silently: the run is blocked.
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run["id"])[0][0] == "VALIDATION_FAILED"
    assert "PROTECTED_NET_NOT_SET" in {f["code"] for f in open_findings(acme, run["id"], "BLOCKING")}
    # Floor 40% of gross 10,000.00 = 4,000.00; net after the recovery 5,000.00 >= floor: recovered in full.
    set_setting(w["cid"], "payroll.deductions.min_net_pay_percent", "40")
    assert acme.post(f"{RUNS}/{run['id']}/calculate", headers={"Idempotency-Key": "fnf-2"}).status_code == 202
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run["id"])[0][0] == "REVIEW_REQUIRED", \
        [(f["code"], f["message"]) for f in open_findings(acme, run["id"], "BLOCKING")]
    amounts, kinds = lines(run["id"], leila)
    salary = q(D("30000.00") * 10 / 30)
    assert amounts["BASIC"] == salary == D("10000.00"), amounts
    recovery = {c: a for c, a in amounts.items() if c != "BASIC"}
    # The recovery reduces net pay by exactly 5,000.00, whatever its line kind.
    signed = sum((a if kinds[c] != "DEDUCTION" else -a) for c, a in recovery.items())
    assert signed == D("-5000.00"), (amounts, kinds)
    assert figures(run["id"])[leila]["net"] == salary - D("5000.00") == D("5000.00")
    reconcile(run["id"])


def test_hourly_salary_is_assignable_and_fails_closed_when_attendance_is_off(world):
    """Regression (defect found by the golden suite): assigning an HOURLY structure was refused with
    STRUCTURE_ERROR because the assignment preview has no attendance (att.payable_hours is NULL there). The
    preview-only NULL is no longer a structure error for HOURLY pay; the run still fails closed: with attendance
    OFF the hourly worker gets a blocking FORMULA_ERROR, never a silent zero."""
    w = world
    structure = ok(w["acme"].post(f"{PAY}/salary-structures", json={
        "code": "HOURLY2", "name": "Hourly rate", "type": "HOURLY",
        "lines": [{"component": "BASIC", "method": "RATE_X_UNITS"}], "test_range": {"min": "10", "max": "5000"}}), 201)
    w["hourly"] = active_version_id(ok(w["acme"].post(f"{PAY}/salary-structures/{structure['id']}/publish",
                                                     json={"version": 1})))
    group = gold_group(w, "PG-HR-OFF")
    hugo = hire(w, "Hugo", group, "250", structure="hourly")          # 201 + approved (was 400 STRUCTURE_ERROR)
    run_id = calculated_run(w, group, "2026-09-01", expect_status="VALIDATION_FAILED")
    codes = {f["code"] for f in open_findings(w["acme"], run_id, "BLOCKING") if f["employee_id"] == hugo}
    assert "FORMULA_ERROR" in codes, codes
    assert "BASIC" not in lines(run_id, hugo)[0]                      # no hourly pay without payable hours
    # A CTC structure whose formula needs attendance is still refused at assignment (the exemption is HOURLY only).
    att = ok(w["acme"].post(f"{PAY}/salary-structures", json={
        "code": "ATT_CTC", "name": "Attendance formula", "type": "CTC_BASED",
        "lines": [{"component": "BASIC", "method": "FORMULA", "formula": "att.payable_hours * 100"}],
        "test_range": {"min": "1000", "max": "5000000"}}), 201)
    published = w["acme"].post(f"{PAY}/salary-structures/{att['id']}/publish", json={"version": 1})
    if published.status_code == 200:
        w["att_ctc"] = active_version_id(published.json())
        employee = create_employee(w["acme"], "Ines")
        place(employee["id"], w["le01"], group["id"])
        r = w["acme"].post(f"{PAY}/employees/{employee['id']}/compensation", json={
            "template_version_id": w["att_ctc"], "effective_from": "2026-01-01", "reason": "HIRE",
            "pay_basis_amount": {"amount": "120000", "currency": "INR"}})
        assert r.status_code == 400 and r.json()["code"] == "STRUCTURE_ERROR", r.text
