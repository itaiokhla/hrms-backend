"""Lock inputs and calculate as background jobs (3.5 §4.1, §5.3-§5.4, §8.2, §10, §16; 3.9 §6).

EC-RUN-02 (one job per calculate, idempotent retries, two tabs), EC-RUN-03 (a crashed chunk is retried
without duplicate lines), EC-RUN-14 (chunked, errors isolated to the employee), EC-RUN-19 (totals per
currency), EC-RUN-11 / EC-RUN-24 (population and line reconciliation), EC-RUN-17 (finalized runs are
immutable), EC-SEC-01/02 (isolation of another tenant's runs and employees), EC-SEC-06 (strict bodies).

Two snapshot sources are used: the real snapshot builder (``app.payroll.services.snapshot``) for the golden
run of a seeded tenant, and ``StandIn``, a test double of ``snapshot.lock_inputs`` that stores hand-written
engine snapshots (3.3 §11 Example A), so the expected amounts can be worked out by hand in the tests.
The helpers here are shared with test_run_review.py and test_run_replay.py.
"""

import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import text

from tests.payroll.test_components import ok
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    force_status,
    new_run,
    run_employees,
    runs_world,
)
from tests.payroll.test_wage_inputs import audit_actions, set_setting, sql, tenant

D = Decimal
CENT = D("0.01")
CALC_TABLES = ("payroll_input_snapshots", "payroll_lines", "payroll_line_sources", "payroll_exceptions")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def r2(value):
    """Round half up to the paisa, as the engine's line rounding (payroll.rounding.line_mode = HALF_UP)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def key():
    return f"t-{uuid.uuid4().hex}"


def calculate(client, run_id, expect=202, idem=None, **body):
    headers = {} if idem is False else {"Idempotency-Key": idem or key()}
    r = client.post(f"{RUNS}/{run_id}/calculate", json=body or None, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def precheck_ready(client, run_id):
    r = client.post(f"{RUNS}/{run_id}/precheck")
    assert r.status_code == 200, r.text
    items = ok(client.get(f"{RUNS}/{run_id}/exceptions", params={"status": "OPEN", "severity": "BLOCKING"}))["items"]
    assert r.json()["status"] == "READY", [(e["code"], e["message"]) for e in items]
    return r.json()


def run_state(run_id):
    return sql("SELECT status, row_version, totals, population, calculation_job_id, stale, engine_version "
               "FROM payroll_runs WHERE id = :r", r=run_id)[0]


def rows_of(run_id):
    """{employee_id: (run_employee_id, status, gross, deductions, net, employer_cost, calc_hash, snapshot_id)}"""
    return {e: tuple(rest) for e, *rest in sql(
        "SELECT employee_id, id, status, gross, deductions, net, employer_cost, calc_hash, snapshot_id "
        "FROM payroll_run_employees WHERE run_id = :r ORDER BY id", r=run_id)}


def lines_of(run_employee_id):
    return {code: (D(amount), kind) for code, amount, kind in sql(
        "SELECT component_code, amount, kind FROM payroll_lines WHERE run_employee_id = :e ORDER BY sort_order",
        e=run_employee_id)}


def jobs_of(run_id):
    return sql("SELECT id, type, status, parent_job_id, attempts, progress_done, progress_total FROM jobs "
               "WHERE type LIKE 'payroll.calculate_%' AND (payload->>'run_id')::int = :r ORDER BY id", r=run_id)


def findings(run_id, run_employee_id=None, status="OPEN"):
    query = "SELECT code, severity, source, status FROM payroll_exceptions WHERE run_id = :r"
    params = {"r": run_id}
    if run_employee_id is not None:
        query += " AND run_employee_id = :e"
        params["e"] = run_employee_id
    if status is not None:
        query += " AND status = :s"
        params["s"] = status
    return sql(query + " ORDER BY id", **params)


def no_pack_country(w, country="GB"):
    sql("UPDATE legal_entities SET country_code = :cc WHERE id = :e", cc=country, e=w["le01"])


class StandIn:
    """Test double of ``snapshot.lock_inputs``: hand-written engine snapshots per employee (3.3 §11 Example A).

    ``config[employee_id]`` may set: ``ctc`` (annual, default 12,00,000), ``lop`` (number of LOP days at the
    start of the period), ``bonus`` (an approved BONUS wage input, id 7001), ``previous`` (previous finalized
    period figures), ``hr_only`` (HRA is an HR_ONLY component), ``broken`` (a malformed snapshot),
    ``fail`` (the builder cannot freeze the inputs)."""

    def __init__(self):
        self.config = {}
        self.calls = []

    def content(self, db, run, row):
        from app.models.employee import Employee
        from app.models.pay_runs import PayPeriod
        from tests.payroll_unit.test_engine_fixtures import SETTINGS, absent, catalogue, days, example_a_structure

        period = db.query(PayPeriod).filter(PayPeriod.id == run.pay_period_id).one()
        employee = db.query(Employee).filter(Employee.id == row.employee_id).one()
        cfg = self.config.get(row.employee_id, {})
        start, end = period.period_start, period.period_end
        lop = [start + timedelta(days=i) for i in range(int(cfg.get("lop", 0)))]
        components = catalogue()
        if cfg.get("hr_only"):
            components = [dict(c, visibility="HR_ONLY") if c["code"] == "HRA" else c for c in components]
        content = {
            "run": {"run_id": run.id, "run_employee_id": row.id, "run_type": run.run_type, "environment": "test"},
            "employee": {"employee_id": employee.id, "code": employee.employee_code,
                         "hire_date": employee.hire_date.isoformat(), "country": "IN",
                         "standard_hours_per_day": "8"},
            "period": {"period_id": period.id, "start": start.isoformat(), "end": end.isoformat(),
                       "pay_date": period.pay_date.isoformat(), "number_in_tax_year": period.number_in_tax_year},
            "currency": run.currency,
            "settings": {**SETTINGS, "payroll.approval.variance_review_threshold_percent": "20"},
            "components": components,
            "segments": [{"key": "S1", "start": start.isoformat(), "end": end.isoformat(),
                          "assignment_id": row.employee_assignment_id,
                          "compensation": example_a_structure(cfg.get("ctc", "1200000"))}],
            "days": days(start, end, per_day=absent(*lop)),
            "bank_accounts": [{"account_id": 9000 + row.employee_id, "type": "PRIMARY"}],
        }
        if cfg.get("bonus"):
            content["wage_inputs"] = [{"id": 7001, "component": "BONUS", "amount": cfg["bonus"]}]
        if cfg.get("previous"):
            content["previous"] = {"period_ref": "PREVIOUS", "currency": run.currency, **cfg["previous"]}
        if cfg.get("broken"):
            content["unexpected_key"] = True
        if cfg.get("no_salary"):
            content["segments"][0]["compensation"] = None
        if cfg.get("ghost"):
            content["segments"][0]["compensation"]["lines"].append(
                {"component": "GHOST", "method": "FIXED", "amount": "5", "order": 9})
        return content

    def lock_inputs(self, db, run, employees, *, lock_time=None, actor_user_id=None):
        from types import SimpleNamespace

        from sqlalchemy import func

        from app.models.pay_runs import PayrollInputSnapshot, PayrollRunEmployee
        from app.payroll import engine

        result = SimpleNamespace(snapshots={}, failed={}, issues={}, fx_snapshot_id=None)
        ids = sorted(e if isinstance(e, int) else e.id for e in employees)
        self.calls.append(ids)
        for row in db.query(PayrollRunEmployee).filter(PayrollRunEmployee.id.in_(ids)).order_by(PayrollRunEmployee.id):
            if self.config.get(row.employee_id, {}).get("fail"):
                result.failed[row.id] = "The test builder could not freeze these inputs."
                continue
            content = self.content(db, run, row)
            digest = "0" * 64 if self.config.get(row.employee_id, {}).get("broken") else engine.content_hash(content)
            version = (db.query(func.coalesce(func.max(PayrollInputSnapshot.version), 0))
                       .filter(PayrollInputSnapshot.run_employee_id == row.id).scalar()) + 1
            stored = PayrollInputSnapshot(company_id=run.company_id, run_employee_id=row.id, version=version,
                                          content=content, content_hash=digest, source_hashes={},
                                          created_by_user_id=actor_user_id)
            db.add(stored)
            db.flush()
            row.snapshot_id, row.content_hash = stored.id, digest
            result.snapshots[row.id] = stored
        db.flush()
        return result


@pytest.fixture
def standin(monkeypatch):
    from app.payroll.services import snapshot

    double = StandIn()
    monkeypatch.setattr(snapshot, "lock_inputs", double.lock_inputs)
    return double


@pytest.fixture
def calc_world(runs_world, standin):
    """runs_world with a READY regular run of PG-IN (Emma and Ivan included; Nora and Pia without salary).

    LE01 is moved to a country without a country pack (GB): the hand-written snapshots carry no pack, so the
    statutory stage adds nothing and the frozen pack matches the live one (no pack change, EC-STA-03)."""
    w = runs_world
    no_pack_country(w)
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    precheck_ready(w["acme"], run["id"])
    rows = run_employees(run["id"])
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    return {**w, "run": run["id"], "standin": standin, "emma": emma, "ivan": ivan,
            "emma_row": rows[emma][2], "ivan_row": rows[ivan][2],
            "start": date.fromisoformat(w["period"]["start"]), "end": date.fromisoformat(w["period"]["end"])}


def calculated(w, expect_status="REVIEW_REQUIRED", **body):
    result = calculate(w["acme"], w["run"], **body)
    assert run_state(w["run"])[0] == expect_status, findings(w["run"])
    return result


# ---------------------------------------------------------------------------
# Golden run (expected values worked out by hand, never by the engine)
# ---------------------------------------------------------------------------
def test_golden_run_hand_computed_example_a_ec_run_19_ec_run_24(calc_world):
    """3.3 §11 Example A for three cases; every expected amount below is computed by hand.

    Emma, CTC 12,00,000, no LOP, approved bonus 10,000.00:
        ctc.monthly = 12,00,000 / 12 = 1,00,000.00; BASIC = 50% = 50,000.00; HRA = 40% of BASIC = 20,000.00;
        FIXED_ALLOWANCE (balancing) = 1,00,000 − 50,000 − 20,000 − 3,000 (employer PF inside CTC) = 27,000.00;
        BONUS = 10,000.00; gross = 1,07,000.00; no deductions; net = employer cost = 1,07,000.00.
    Ivan, CTC 9,00,000 and 2 LOP days in a period of N calendar days (CALENDAR_DAYS basis):
        monthly = 75,000.00; BASIC 37,500.00, HRA 15,000.00, FIXED_ALLOWANCE 75,000 − 37,500 − 15,000 − 3,000
        = 19,500.00, each × (N − 2) / N rounded half up to the paisa.
    """
    w = calc_world
    w["standin"].config = {w["emma"]: {"bonus": "10000.00"}, w["ivan"]: {"ctc": "900000", "lop": 2}}
    result = calculated(w)
    assert result["action"] == "CALCULATE" and result["employees"] == 2
    n = (w["end"] - w["start"]).days + 1
    emma_lines = lines_of(w["emma_row"])
    assert {k: v[0] for k, v in emma_lines.items()} == {
        "BASIC": D("50000.00"), "HRA": D("20000.00"), "FIXED_ALLOWANCE": D("27000.00"), "BONUS": D("10000.00")}
    ivan_expected = {"BASIC": r2(D("37500") * (n - 2) / n), "HRA": r2(D("15000") * (n - 2) / n),
                     "FIXED_ALLOWANCE": r2(D("19500") * (n - 2) / n)}
    assert {k: v[0] for k, v in lines_of(w["ivan_row"]).items()} == ivan_expected
    ivan_gross = sum(ivan_expected.values())
    rows = rows_of(w["run"])
    _, status, gross, deductions, net, cost, calc_hash, snapshot_id = rows[w["emma"]]
    assert (status, gross, deductions, net, cost) == ("CALCULATED", D("107000.0000"), D("0"), D("107000.0000"),
                                                     D("107000.0000"))
    assert calc_hash and snapshot_id
    assert rows[w["ivan"]][1:5] == ("CALCULATED", ivan_gross, D("0"), ivan_gross)
    status, version, totals, population, job_id, stale, engine_version = run_state(w["run"])
    # Run totals per currency (EC-RUN-19) = Σ employee totals (EC-RUN-24): 1,07,000.00 + Ivan's gross.
    expected_total = D("107000.00") + ivan_gross
    assert totals == {"INR": {"gross": f"{expected_total:.2f}", "deductions": "0.00", "reimbursements": "0.00",
                              "net": f"{expected_total:.2f}", "employer_cost": f"{expected_total:.2f}",
                              "employees": 2}}
    assert population["eligible"] == population["included"] + population["excluded"] == 4   # EC-RUN-11
    assert population["calculated"] == 2 and population["errors"] == 0
    assert engine_version and job_id and not stale
    # The bonus line names its wage input as a consumable source (consumed only at finalization).
    assert sql("SELECT source_type, source_id, consumed FROM payroll_line_sources WHERE run_employee_id = :e",
               e=w["emma_row"]) == [("WAGE_INPUT", 7001, False)]
    actions = audit_actions(w["cid"], "payroll_run", w["run"])
    assert actions.count("INPUTS_LOCKED") == 1 and actions.count("RUN_CALCULATED") == 1
    detail = ok(w["acme"].get(f"{RUNS}/{w['run']}"))
    assert detail["status"] == "REVIEW_REQUIRED" and detail["calculated_by_user_id"]


def test_golden_run_seeded_tenant_through_the_real_snapshot_builder(runs_world):
    """Emma and Ivan of the seeded tenant: CTC 12,00,000 on the 3.3 §11 Example A structure, attendance OFF,
    legal entity in a country without a country pack (statutory deductions not calculated; the attestation
    gate applies at finalization, decision I-15).

    By hand: ctc.monthly = 12,00,000 / 12 = 1,00,000.00; BASIC = 50% = 50,000.00; HRA = 40% × 50,000 =
    20,000.00; FIXED_ALLOWANCE balances to 1,00,000 − 50,000 − 20,000 = 30,000.00 (no employer contribution
    inside the CTC without a country pack). Gross = net = employer cost = 1,00,000.00 each;
    run totals 2,00,000.00.
    """
    w = runs_world
    no_pack_country(w)
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    precheck_ready(w["acme"], run["id"])
    calculate(w["acme"], run["id"])
    status = run_state(run["id"])[0]
    rows = rows_of(run["id"])
    assert status == "REVIEW_REQUIRED", findings(run["id"])
    for name in ("Emma", "Ivan"):
        row_id, row_status, gross, deductions, net, cost, calc_hash, snapshot_id = rows[w["emp"][name]["id"]]
        earnings = {k: v[0] for k, v in lines_of(row_id).items() if v[1] == "EARNING"}
        assert earnings == {"BASIC": D("50000.00"), "HRA": D("20000.00"), "FIXED_ALLOWANCE": D("30000.00")}
        assert (row_status, gross, deductions, net, cost) == ("CALCULATED", D("100000"), D("0"), D("100000"),
                                                             D("100000"))
        assert snapshot_id and calc_hash
    totals = run_state(run["id"])[2]
    assert totals["INR"]["gross"] == totals["INR"]["net"] == "200000.00" and totals["INR"]["employees"] == 2
    # The calculation read only the frozen snapshot: replaying it reproduces calc_hash (EC-STA-04).
    replayed = ok(w["acme"].post(f"{RUNS}/{run['id']}/replay"))
    assert replayed["checked"] == 2 and replayed["matched"] == 2


# ---------------------------------------------------------------------------
# Jobs: chunking, crash and retry, isolation of errors
# ---------------------------------------------------------------------------
def test_ec_run_14_chunked_calculation_with_progress(calc_world, monkeypatch):
    from app.payroll.services import calculation

    monkeypatch.setattr(calculation, "CHUNK_SIZE", 1)
    w = calc_world
    result = calculated(w)
    jobs = jobs_of(w["run"])
    parent = [j for j in jobs if j[1] == calculation.CALCULATE_RUN_JOB]
    children = [j for j in jobs if j[1] == calculation.CALCULATE_CHUNK_JOB]
    assert [p[0] for p in parent] == [result["job"]["id"]]
    assert len(children) == 2 and all(c[2] == "SUCCEEDED" and c[3] == parent[0][0] for c in children)
    assert parent[0][5:] == (2, 2)  # progress done / total
    assert audit_actions(w["cid"], "payroll_run", w["run"]).count("RUN_CALCULATED") == 1  # completed once
    summary = ok(w["acme"].get(f"{RUNS}/{w['run']}/summary"))
    assert summary["calculation"]["progress"]["done"] == 2 and summary["calculation"]["progress"]["chunks"] == {
        "SUCCEEDED": 2}
    job = ok(w["acme"].get(f"/api/v1/jobs/{result['job']['id']}"))
    assert job["status"] == "SUCCEEDED"


def test_ec_run_03_crashed_chunk_is_retried_without_duplicate_lines(calc_world, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.payroll.services import calculation, jobs

    w = calc_world
    real = calculation.calculate_employee
    crashed = []

    def crash_on_second(db, run, row, catalogue):
        outcome = real(db, run, row, catalogue)       # the first employee's lines are written ...
        if not crashed and row.id == w["ivan_row"]:
            crashed.append(row.id)
            raise OperationalError("SELECT 1", {}, Exception("worker lost its connection"))  # ... then a crash
        return outcome

    monkeypatch.setattr(calculation, "calculate_employee", crash_on_second)
    calculate(w["acme"], w["run"])
    assert crashed and run_state(w["run"])[0] == "CALCULATING"
    chunk = [j for j in jobs_of(w["run"]) if j[1] == calculation.CALCULATE_CHUNK_JOB][0]
    assert chunk[2] == "QUEUED" and chunk[4] == 1                       # re-queued with backoff
    assert sql("SELECT count(*) FROM payroll_lines WHERE run_id = :r", r=w["run"])[0][0] == 0  # rolled back
    sql("UPDATE jobs SET run_at = now() - interval '1 second' WHERE id = :j", j=chunk[0])
    assert jobs.run_pending(ids=[chunk[0]]) == 1, (
        sql("SELECT status, run_at, now(), locked_by, attempts FROM jobs WHERE id = :j", j=chunk[0]),
        sql("SELECT a.pid, a.state, left(a.query, 120) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.relation = 'jobs'::regclass AND a.pid <> pg_backend_pid()"))
    assert run_state(w["run"])[0] == "REVIEW_REQUIRED"
    chunk = [j for j in jobs_of(w["run"]) if j[1] == calculation.CALCULATE_CHUNK_JOB][0]
    assert chunk[2] == "SUCCEEDED" and chunk[4] == 2
    for row in (w["emma_row"], w["ivan_row"]):
        assert sql("SELECT count(*), count(DISTINCT component_code) FROM payroll_lines WHERE run_employee_id = :e",
                   e=row)[0] == (3, 3)
    # Calculating the same employee again (a retried job) replaces its lines: still no duplicates.
    from app.models.pay_runs import PayrollRun, PayrollRunEmployee
    from tests.payroll.test_runs_lifecycle import session_for

    db = session_for(w["cid"])
    try:
        run = db.get(PayrollRun, w["run"])
        row = db.get(PayrollRunEmployee, w["emma_row"])
        before = row.calc_hash
        assert real(db, run, row, calculation._Catalogue(db, w["cid"])) == "CALCULATED"
        db.flush()
        assert row.calc_hash == before
        assert db.execute(text("SELECT count(*) FROM payroll_lines WHERE run_employee_id = :e"),
                          {"e": row.id}).scalar() == 3
    finally:
        db.rollback()
        db.close()


def test_ec_run_14_error_is_isolated_to_the_employee_then_fixed_by_recalculating_it(calc_world):
    w = calc_world
    w["standin"].config = {w["ivan"]: {"broken": True}}
    calculated(w, "VALIDATION_FAILED")
    rows = rows_of(w["run"])
    assert rows[w["emma"]][1] == "CALCULATED" and len(lines_of(w["emma_row"])) == 3   # Emma is unaffected
    assert rows[w["ivan"]][1] == "ERROR" and lines_of(w["ivan_row"]) == {} and rows[w["ivan"]][6] is None
    assert ("CALCULATION_ERROR", "BLOCKING", "CALCULATION", "OPEN") in findings(w["run"], w["ivan_row"])
    population = run_state(w["run"])[3]
    assert population["errors"] == 1 and population["calculated"] == 1
    totals = run_state(w["run"])[2]
    assert totals["INR"]["employees"] == 1 and totals["INR"]["gross"] == "97000.00"   # Emma only
    w["standin"].config = {}
    version = run_state(w["run"])[1]
    result = calculate(w["acme"], w["run"], employee_ids=[w["ivan"]], row_version=version)
    assert result["action"] == "RECALCULATE" and result["employees"] == 1
    assert run_state(w["run"])[0] == "REVIEW_REQUIRED"
    assert rows_of(w["run"])[w["ivan"]][1] == "CALCULATED"
    old = [f for f in findings(w["run"], w["ivan_row"], status=None) if f[0] == "CALCULATION_ERROR"]
    assert old == [("CALCULATION_ERROR", "BLOCKING", "CALCULATION", "FIXED")]          # superseded, kept


def test_blocking_finding_with_a_result_fails_validation_but_keeps_the_lines(calc_world):
    """A structure line naming an unknown component: Ivan keeps his other lines, the finding is BLOCKING and
    the run is VALIDATION_FAILED (3.5 §10); never silently zero."""
    w = calc_world
    w["standin"].config = {w["ivan"]: {"ghost": True}}
    calculated(w, "VALIDATION_FAILED")
    row = rows_of(w["run"])[w["ivan"]]
    assert row[1] == "CALCULATED" and row[6] and set(lines_of(w["ivan_row"])) >= {"BASIC", "HRA"}
    blocking = [f for f in findings(w["run"], w["ivan_row"]) if f[1] == "BLOCKING"]
    assert blocking and all(f[2] == "CALCULATION" for f in blocking)
    assert run_state(w["run"])[3]["errors"] == 0


def test_ec_cmp_14_no_salary_in_the_snapshot_excludes_the_employee_never_zero(calc_world):
    w = calc_world
    w["standin"].config = {w["ivan"]: {"no_salary": True}}
    calculated(w)
    row = rows_of(w["run"])[w["ivan"]]
    assert row[1] == "EXCLUDED" and row[2] is None and lines_of(w["ivan_row"]) == {}
    population = run_state(w["run"])[3]
    assert population["excluded_by_reason"]["NO_SALARY"] == 3 and population["included"] == 1
    assert population["eligible"] == population["included"] + population["excluded"]
    assert run_state(w["run"])[2]["INR"]["employees"] == 1


def test_snapshot_failure_marks_the_employee_without_using_an_older_snapshot(calc_world):
    w = calc_world
    calculated(w)
    first_snapshot = rows_of(w["run"])[w["ivan"]][7]
    w["standin"].config = {w["ivan"]: {"fail": True}}
    calculate(w["acme"], w["run"], employee_ids=[w["ivan"]])
    assert run_state(w["run"])[0] == "VALIDATION_FAILED"
    row = rows_of(w["run"])[w["ivan"]]
    assert row[1] == "ERROR" and row[7] is None and lines_of(w["ivan_row"]) == {} and first_snapshot
    assert sql("SELECT error->>'code' FROM payroll_run_employees WHERE id = :e", e=w["ivan_row"])[0][0] == \
        "SNAPSHOT_FAILED"


def test_recalculate_selected_employees_resnapshots_only_them(calc_world):
    w = calc_world
    calculated(w)
    ivan_lines = sql("SELECT id FROM payroll_lines WHERE run_employee_id = :e ORDER BY id", e=w["ivan_row"])
    w["standin"].config = {w["emma"]: {"bonus": "5000.00"}}
    calculate(w["acme"], w["run"], employee_ids=[w["emma"]], row_version=run_state(w["run"])[1])
    assert run_state(w["run"])[0] == "REVIEW_REQUIRED"
    assert w["standin"].calls[-1] == [w["emma_row"]]
    versions = dict(sql("SELECT run_employee_id, max(version) FROM payroll_input_snapshots WHERE run_employee_id IN "
                        "(:a, :b) GROUP BY run_employee_id", a=w["emma_row"], b=w["ivan_row"]))
    assert versions == {w["emma_row"]: 2, w["ivan_row"]: 1}
    assert lines_of(w["emma_row"])["BONUS"][0] == D("5000.00")
    assert sql("SELECT id FROM payroll_lines WHERE run_employee_id = :e ORDER BY id", e=w["ivan_row"]) == ivan_lines
    assert run_state(w["run"])[2]["INR"]["gross"] == "199000.00"  # 97,000 + 5,000 + 97,000
    assert audit_actions(w["cid"], "payroll_run", w["run"]).count("INPUTS_LOCKED") == 2


def test_full_hold_is_calculated_but_stays_on_hold(calc_world):
    w = calc_world
    r = w["acme"].post(f"{RUNS}/{w['run']}/employees/{w['ivan_row']}/hold",
                       json={"hold_type": "FULL", "reason_code": "DISPUTE", "note": "Under review"})
    assert r.status_code == 200, r.text
    calculated(w)
    row = rows_of(w["run"])[w["ivan"]]
    assert row[1] == "ON_HOLD" and row[4] == D("97000") and row[6]
    assert sql("SELECT hold->>'status_before' FROM payroll_run_employees WHERE id = :e", e=w["ivan_row"])[0][0] == \
        "CALCULATED"


# ---------------------------------------------------------------------------
# Idempotency and concurrency (EC-RUN-02)
# ---------------------------------------------------------------------------
def test_ec_run_02_same_idempotency_key_returns_the_same_job(calc_world):
    w = calc_world
    idem = key()
    first = w["acme"].post(f"{RUNS}/{w['run']}/calculate", headers={"Idempotency-Key": idem})
    again = w["acme"].post(f"{RUNS}/{w['run']}/calculate", headers={"Idempotency-Key": idem})
    assert first.status_code == again.status_code == 202, again.text
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json() == first.json()
    assert len([j for j in jobs_of(w["run"]) if j[1] == "payroll.calculate_run"]) == 1
    assert "amount" not in again.text and "gross" not in again.text   # no pay data in idempotency records


def test_ec_run_02_two_tabs_calculate_concurrently_one_job(app, calc_world):
    from tests.helpers import login

    w = calc_world
    version = run_state(w["run"])[1]
    results = []
    clients = [login(app, w["acme"].email) for _ in range(2)]
    barrier = threading.Barrier(2)

    def click(client):
        barrier.wait()
        r = client.post(f"{RUNS}/{w['run']}/calculate", json={"row_version": version},
                        headers={"Idempotency-Key": key()})
        results.append((r.status_code, r.json().get("code")))

    threads = [threading.Thread(target=click, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s for s, _ in results) == [202, 409], results
    assert [c for s, c in results if s == 409][0] in ("ROW_VERSION_CONFLICT", "CALCULATION_RUNNING",
                                                     "INVALID_TRANSITION")
    assert len([j for j in jobs_of(w["run"]) if j[1] == "payroll.calculate_run"]) == 1
    assert run_state(w["run"])[0] == "REVIEW_REQUIRED"


def test_calculate_guards_key_state_selection_and_versions(calc_world):
    w = calc_world
    assert calculate(w["acme"], w["run"], expect=400, idem=False)["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert calculate(w["acme"], w["run"], expect=400, employee_ids=[w["emma"]])["code"] == "SELECTION_NOT_ALLOWED"
    assert calculate(w["acme"], w["run"], expect=409, row_version=1)["code"] == "ROW_VERSION_CONFLICT"
    assert calculate(w["acme"], w["run"], expect=422, status="REVIEW_REQUIRED")  # EC-SEC-06 strict body
    assert calculate(w["acme"], w["run"], expect=422, employee_ids=[])
    draft = new_run(w["acme"], w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[w["emma"]])
    assert calculate(w["acme"], draft["id"], expect=409)["code"] == "INVALID_TRANSITION"
    calculated(w)
    assert calculate(w["acme"], w["run"], expect=404, employee_ids=[w["emp"]["Nora"]["id"]])  # excluded
    assert calculate(w["acme"], w["run"], expect=404, employee_ids=[w["beta"]["employee"]["id"]])  # other tenant


def test_ec_run_20_no_included_employee_cannot_be_calculated(calc_world):
    w = calc_world
    sql("UPDATE payroll_run_employees SET status = 'EXCLUDED', exclusion_reason = 'NO_SALARY' WHERE run_id = :r",
        r=w["run"])
    assert calculate(w["acme"], w["run"], expect=409)["code"] == "EMPTY_POPULATION"
    assert run_state(w["run"])[0] == "READY" and jobs_of(w["run"]) == []
    assert w["standin"].calls == []                          # nothing was locked


def test_calculation_running_refuses_a_second_calculation(calc_world):
    w = calc_world
    force_status(w["run"], "CALCULATING")
    assert calculate(w["acme"], w["run"], expect=409)["code"] == "CALCULATION_RUNNING"


def test_ec_run_17_finalized_or_cancelled_runs_are_not_calculated(calc_world):
    w = calc_world
    force_status(w["run"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    r = w["acme"].post(f"{RUNS}/{w['run']}/calculate", headers={"Idempotency-Key": key()})
    assert r.status_code == 409 and r.json()["code"] == "RUN_FINALIZED"
    assert r.headers.get("X-Payroll-Hint") == "CREATE_CORRECTION"
    other = new_run(w["acme"], w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[w["emma"]])
    sql("UPDATE payroll_runs SET status = 'CANCELLED', cancelled_at = now() WHERE id = :r", r=other["id"])
    assert calculate(w["acme"], other["id"], expect=409)["code"] == "RUN_CANCELLED"


def test_stopped_calculation_discards_late_chunks(calc_world, monkeypatch):
    """A chunk of a calculation that was stopped (run reopened) writes nothing."""
    from app.payroll.services import calculation

    w = calc_world
    original = calculation.calculate_employee

    def reopen_meanwhile(db, run, row, catalogue):
        outcome = original(db, run, row, catalogue)
        if row.id == w["ivan_row"]:
            sql("UPDATE payroll_runs SET calculation_job_id = NULL WHERE id = :r", r=w["run"])
        return outcome

    monkeypatch.setattr(calculation, "calculate_employee", reopen_meanwhile)
    calculate(w["acme"], w["run"])
    assert run_state(w["run"])[0] == "CALCULATING"
    assert sql("SELECT count(*) FROM payroll_lines WHERE run_id = :r", r=w["run"])[0][0] == 0
    chunk = [j for j in jobs_of(w["run"]) if j[1] == calculation.CALCULATE_CHUNK_JOB][0]
    assert chunk[2] == "SUCCEEDED"


def test_payment_only_and_reversal_runs_are_never_calculated(calc_world):
    w = calc_world
    other = new_run(w["acme"], w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[w["emma"]])
    sql("UPDATE payroll_runs SET selection = jsonb_set(selection, '{off_cycle}', "
        "'{\"kind\": \"REISSUE\", \"payment_only\": true}'::jsonb), status = 'READY' WHERE id = :r", r=other["id"])
    assert calculate(w["acme"], other["id"], expect=409)["code"] == "RUN_NOT_CALCULABLE"


# ---------------------------------------------------------------------------
# Authorization and isolation (EC-SEC-01, EC-SEC-02)
# ---------------------------------------------------------------------------
def test_calculate_needs_permission_and_scope(calc_world):
    w = calc_world
    r = w["approver"].post(f"{RUNS}/{w['run']}/calculate", headers={"Idempotency-Key": key()})
    assert r.status_code == 403, r.text                     # payroll:run:read without payroll:run:calculate
    r = w["cleo"].post(f"{RUNS}/{w['run']}/calculate", headers={"Idempotency-Key": key()})
    assert r.status_code in (403, 404), r.text              # entity clerk of AE01 only
    assert run_state(w["run"])[0] == "READY"
    calculate(w["processor"], w["run"])                     # the payroll processor may calculate
    assert run_state(w["run"])[0] == "REVIEW_REQUIRED"


def test_ec_sec_02_another_tenants_run_and_employees_are_404(calc_world):
    w = calc_world
    beta = w["beta"]
    beta_run = new_run(beta["client"], beta["group"], beta["period"]["id"])
    for path in ("calculate", "replay"):
        r = w["acme"].post(f"{RUNS}/{beta_run['id']}/{path}", headers={"Idempotency-Key": key()})
        assert r.status_code == 404, (path, r.text)
    r = beta["client"].post(f"{RUNS}/{w['run']}/calculate", headers={"Idempotency-Key": key()})
    assert r.status_code in (403, 404), r.text
    calculated(w)
    for path in ("summary", "employees", f"employees/{w['emma_row']}", "variance", "stale"):
        assert beta["client"].get(f"{RUNS}/{w['run']}/{path}").status_code in (403, 404), path
        assert w["acme"].get(f"{RUNS}/{beta_run['id']}/{path}").status_code == 404, path


def test_ec_sec_01_calculation_rows_are_isolated_across_three_tenants(calc_world):
    w = calc_world
    calculated(w)
    for other in (w["beta"]["cid"], w["cobalt"]["cid"]):
        with tenant(other) as conn:
            for table in (*CALC_TABLES, "payroll_runs", "payroll_run_employees"):
                count = conn.execute(text(f"SELECT count(*) FROM {table} WHERE company_id = :c"),
                                     {"c": w["cid"]}).scalar()
                assert count == 0, table
            assert conn.execute(text("SELECT count(*) FROM payroll_run_employees WHERE run_id = :r"),
                                {"r": w["run"]}).scalar() == 0
    with tenant(w["cid"]) as conn:
        assert conn.execute(text("SELECT count(*) FROM payroll_lines WHERE run_id = :r"), {"r": w["run"]}).scalar() == 6
        stage = conn.execute(text("SELECT stage_outputs->>'calc_hash' = calc_hash FROM payroll_run_employees "
                                  "WHERE id = :e"), {"e": w["emma_row"]}).scalar()
        assert stage is True


# ---------------------------------------------------------------------------
# The real chain end to end (no stand-ins): create -> precheck -> calculation job -> review -> submit ->
# approve by another user -> finalize with step-up -> payroll.run.finalized drained -> obligations + payslips
# ---------------------------------------------------------------------------
def test_end_to_end_real_chain_from_calculation_job_to_obligations_and_payslips(app, runs_world):
    """Seeded tenant, real snapshot builder, real calculation job, real approval and finalization services and
    the real subscribers of payroll.run.finalized (drained inline after commit).

    By hand (3.3 §11 Example A, CTC 12,00,000, no country pack, attendance OFF): BASIC 50,000.00, HRA
    20,000.00, FIXED_ALLOWANCE 30,000.00; gross = net = 1,00,000.00 for Emma and for Ivan; the run owes
    2,00,000.00 in INR, one obligation of 1,00,000.00 per employee and one payslip each."""
    from app.payroll.services import documents
    from tests.payroll.test_compensation import login_as
    from tests.payroll.test_payment_obligations import NUMBERS, add_bank_account
    from tests.payroll.test_run_finalize import ATTESTATIONS, approve, confirm_countries, finalize, step_up, submit
    from tests.payroll.test_run_finalize import user_id_of

    w = runs_world
    acme, cid = w["acme"], w["cid"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    no_pack_country(w)
    confirm_countries(cid)
    set_setting(cid, "payroll.attendance.enabled", False)
    for name in ("Emma", "Ivan"):
        add_bank_account(cid, w["emp"][name]["id"], NUMBERS[name], holder=f"{name} Test")
    # No verified pack covers GB: statutory obligations are attested as handled outside (I-15, maker-checker).
    from tests.helpers import create_employee

    attestation = acme.post(ATTESTATIONS, json={"legal_entity_id": w["le01"], "effective_from": "2020-01-01",
                                                "reason": "Statutory filings handled by our UK bureau"})
    assert attestation.status_code == 201, attestation.text
    checker = login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation.json()['id']}/approve"))

    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    started = calculate(acme, run["id"])
    assert started["action"] == "CALCULATE" and started["job"]["id"]
    assert run_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])

    summary = ok(acme.get(f"{RUNS}/{run['id']}/summary"))
    assert summary["totals"]["INR"]["net"] == "200000.00" and summary["totals"]["INR"]["employees"] == 2
    assert summary["reconciles"] and summary["calculation"]["calculated_by_user_id"] == user_id_of(acme)
    listed = ok(acme.get(f"{RUNS}/{run['id']}/employees", params={"filter": "calculated"}))
    assert {i["employee_id"]: i["net"]["amount"] for i in listed["items"]} == {emma: "100000.00", ivan: "100000.00"}
    emma_row = next(i["id"] for i in listed["items"] if i["employee_id"] == emma)
    detail = ok(acme.get(f"{RUNS}/{run['id']}/employees/{emma_row}"))
    assert {l["component"]: l["amount"]["amount"] for l in detail["lines"] if l["kind"] == "EARNING"} == {
        "BASIC": "50000.00", "HRA": "20000.00", "FIXED_ALLOWANCE": "30000.00"}
    assert all(l["explanation"] for l in detail["lines"])

    submit(acme, run["id"])
    approve(w["approver"], run["id"])                         # another user (separation of duties)
    assert run_state(run["id"])[0] == "APPROVED"
    step_up(acme)
    finalize(acme, run["id"], key=key())
    assert run_state(run["id"])[0] in ("FINALIZED", "PAYMENT_READY")

    event = sql("SELECT dispatched_at IS NOT NULL FROM outbox_events WHERE company_id = :c AND topic = "
                "'payroll.run.finalized' AND payload->>'run_id' = :r", c=cid, r=str(run["id"]))
    assert event == [(True,)]
    owed = dict(sql("SELECT employee_id, amount FROM payment_obligations WHERE run_id = :r", r=run["id"]))
    assert owed == {emma: D("100000.00"), ivan: D("100000.00")}
    payslips = sql("SELECT employee_id FROM documents WHERE company_id = :c AND run_id = :r AND doc_type = :t "
                   "ORDER BY employee_id", c=cid, r=run["id"], t=documents.PAYSLIP)
    assert sorted(e for (e,) in payslips) == sorted([emma, ivan])
    # The finalized results are what the engine calculated from the frozen snapshots (EC-STA-04).
    replayed = ok(acme.post(f"{RUNS}/{run['id']}/replay"))
    assert replayed["matched"] == 2 and replayed["mismatched"] == 0
