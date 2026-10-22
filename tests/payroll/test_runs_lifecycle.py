"""Pay run lifecycle: creation, identity, population, the single transition function, cancel, holds,
idempotent retries and concurrency (3.5 §3-§5, §9, §16; 3.9 §3.6, §4 Runs).

EC-RUN-01, EC-RUN-02, EC-RUN-08, EC-RUN-11, EC-RUN-13, EC-RUN-15, EC-RUN-16, EC-RUN-17, EC-VAR-01.

The fixtures and helpers here (``runs_world``, ``calendar_for``, ``current_period``, ``new_run`` ...)
are shared with test_run_prechecks.py, test_run_isolation.py and the later run test modules.
"""

import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.helpers import create_employee, signup_company
from tests.payroll.test_compensation import assign_and_approve, login_as
from tests.payroll.test_components import active_version_id, make_structure, ok, setup_catalogue
from tests.payroll.test_wage_inputs import (
    CHECK,
    UNIQUE,
    audit_actions,
    expect_error,
    pay_group,
    place,
    role,
    session,
    sql,
    tenant,
    user_with_role,
)

PAY = "/api/v1/payroll"
RUNS = f"{PAY}/runs"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def calendar_for(client, group_id, frequency="MONTHLY", rules=None, expect=200, **extra):
    body = {"frequency": frequency, "rules": {"monthly_start_day": 1} if rules is None else rules, **extra}
    r = client.post(f"{PAY}/pay-groups/{group_id}/calendar", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def periods_of(client, group_id, **params):
    r = client.get(f"{PAY}/pay-groups/{group_id}/periods", params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def current_period(client, group_id, day=None):
    from tests.payroll.test_wage_inputs import today_of

    day = day or today_of(client.company_id)
    return next(p for p in periods_of(client, group_id) if p["start"] <= day.isoformat() <= p["end"])


def new_run(client, group_id, period_id, run_type="REGULAR", expect=201, headers=None, **extra):
    body = {"run_type": run_type, "pay_group_id": group_id, "pay_period_id": period_id, **extra}
    r = client.post(RUNS, json=body, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def run_row(run_id):
    return sql("SELECT status, row_version, stale, population FROM payroll_runs WHERE id = :r", r=run_id)[0]


def run_employees(run_id):
    return {e: (s, reason, rid) for rid, e, s, reason in sql(
        "SELECT id, employee_id, status, exclusion_reason FROM payroll_run_employees WHERE run_id = :r ORDER BY id",
        r=run_id)}


def force_status(run_id, status, **columns):
    """Move a run's status directly (as the owner, bypassing the service) to set up later states."""
    sets = ", ".join(f"{k} = :{k}" for k in columns)
    sql(f"UPDATE payroll_runs SET status = :s{', ' + sets if sets else ''} WHERE id = :r", s=status, r=run_id,
        **columns)


def principal_of(client, user_id=None):
    """(session, principal) of a client's user."""
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User

    db = session_for(client.company_id)
    uid = user_id or db.query(User.id).filter(User.email == client.email).scalar()
    return db, build_principal(db, db.get(User, uid), None, db.get(Company, client.company_id))


def session_for(company_id):
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    bind_tenant(db, company_id)
    return db


def minimal_tenant(app, name):
    """A tenant with one pay group, a monthly calendar and an employee without salary (isolation tests)."""
    client = signup_company(app, name)
    cid = client.company_id
    entity = sql("SELECT id FROM legal_entities WHERE company_id = :c AND code = 'LE01'", c=cid)[0][0]
    group = pay_group(cid, entity, "PG-X")
    employee = create_employee(client, f"{name.split()[0]}Emp", hire_date="2024-01-15")
    place(employee["id"], entity, group)
    calendar_for(client, group)
    period = current_period(client, group)
    return {"client": client, "cid": cid, "entity": entity, "group": group, "employee": employee, "period": period}


@pytest.fixture
def runs_world(app):
    """Acme: LE01 with pay group PG-IN (INR, monthly calendar), Emma and Ivan with an approved salary from the
    current period, Nora without salary; the founder (payroll manager), approver Ava, processor Pia (COMPANY),
    entity clerk Cleo (run permissions on AE01 only, which has pay group PG-AE with Gus). Beta and Cobalt
    are minimal tenants with a run each."""
    acme = signup_company(app, "Acme Corp")
    cid = acme.company_id
    setup_catalogue(acme)
    structure = make_structure(acme)
    version = active_version_id(structure)
    approver = login_as(app, acme, create_employee(acme, "Ava", "Approver"), ["payroll_approver"])
    le01 = sql("SELECT id FROM legal_entities WHERE company_id = :c AND code = 'LE01'", c=cid)[0][0]
    group = pay_group(cid, le01, "PG-IN")
    emp = {name: create_employee(acme, name, hire_date="2024-01-15") for name in ("Emma", "Ivan", "Nora", "Pia")}
    for e in emp.values():
        place(e["id"], le01, group)
    calendar_for(acme, group)
    period = current_period(acme, group)
    start = date.fromisoformat(period["start"])
    for name in ("Emma", "Ivan"):
        assign_and_approve(acme, approver, emp[name]["id"], version, "1200000", effective_from=start)
    processor = login_as(app, acme, emp["Pia"], ["payroll_processor"])

    r = acme.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Acme Gulf FZ-LLC",
                                                   "country_code": "AE", "base_currency": "AED",
                                                   "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    ae = r.json()["id"]
    r = acme.post("/api/v1/branches", json={"legal_entity_id": ae, "code": "DXB", "name": "Dubai"})
    assert r.status_code == 200, r.text
    dubai = r.json()["id"]
    ae_group = pay_group(cid, ae, "PG-AE", currency="AED")
    gus = create_employee(acme, "Gus", hire_date="2024-01-15")
    r = acme.post(f"/api/v1/employees/{gus['id']}/assignments",
                  json={"branch_id": dubai, "effective_from": "2024-01-16", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    place(gus["id"], ae, ae_group)
    calendar_for(acme, ae_group)
    clerk_role = role(acme, "AE run clerk", "LEGAL_ENTITY", ("payroll:run:read", "payroll:run:create",
                                                             "payroll:run:review", "payroll:run:finalize"))
    cleo = user_with_role(app, acme, create_employee(acme, "Cleo", hire_date="2024-01-15"), clerk_role, [ae])
    return {"acme": acme, "cid": cid, "le01": le01, "group": group, "period": period, "emp": emp,
            "approver": approver, "processor": processor, "version": version, "ae": ae, "ae_group": ae_group,
            "gus": gus, "cleo": cleo, "beta": minimal_tenant(app, "Beta Industries"),
            "cobalt": minimal_tenant(app, "Cobalt Labs")}


# ---------------------------------------------------------------------------
# Create, number, label, population (EC-RUN-11)
# ---------------------------------------------------------------------------
def test_ec_run_11_create_regular_run_with_population_reconciliation(runs_world):
    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    assert created["status"] == "DRAFT" and created["number"].startswith("PR-")
    population = created["population"]
    assert population["eligible"] == population["included"] + population["excluded"]  # EC-RUN-11
    assert population["excluded_by_reason"] == {"NO_SALARY": 2}  # Nora and Pia have no salary
    rows = run_employees(created["id"])
    assert rows[w["emp"]["Emma"]["id"]][0] == "PENDING" and rows[w["emp"]["Ivan"]["id"]][0] == "PENDING"
    assert rows[w["emp"]["Nora"]["id"]][:2] == ("EXCLUDED", "NO_SALARY")
    detail = ok(w["acme"].get(f"{RUNS}/{created['id']}"))
    assert detail["currency"] == "INR" and detail["stage"] == 1 and detail["period"]["status"] == "PROCESSING"
    assert "PRECHECK_PASSED" in detail["available_actions"] and "CANCEL" in detail["available_actions"]
    assert detail["label"].endswith("Group PG-IN")
    assert "RUN_CREATED" in audit_actions(w["cid"], "payroll_run", created["id"])
    second = new_run(w["acme"], w["group"], w["period"]["id"], run_type="OFF_CYCLE",
                     employee_ids=[w["emp"]["Emma"]["id"]])
    assert int(second["number"][-6:]) == int(created["number"][-6:]) + 1


def test_population_preview_matches_the_created_run(runs_world):
    w = runs_world
    preview = ok(w["acme"].get(f"{PAY}/run-population-preview",
                               params={"pay_group_id": w["group"], "pay_period_id": w["period"]["id"]}))
    assert preview["eligible"] == preview["included"] + preview["excluded"] == 4
    assert {i["employee_id"] for i in preview["items"] if i["status"] == "INCLUDED"} == {
        w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]}
    assert sql("SELECT count(*) FROM payroll_runs WHERE company_id = :c", c=w["cid"])[0][0] == 0


def test_population_reasons_joined_left_and_suspended(runs_world):
    w = runs_world
    start, end = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    late, gone, away = (create_employee(w["acme"], n, hire_date="2024-01-15") for n in ("Late", "Gone", "Away"))
    for e in (late, gone, away):
        place(e["id"], w["le01"], w["group"])
        assign_and_approve(w["acme"], w["approver"], e["id"], w["version"], "600000", effective_from=start)
    sql("UPDATE employees SET hire_date = :d WHERE id = :e", d=end + timedelta(days=3), e=late["id"])
    sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=start - timedelta(days=1), e=gone["id"])
    sql("INSERT INTO employee_suspensions (company_id, employee_id, start_date, end_date, paid, reason, status, "
        "row_version, created_at, updated_at) VALUES (:c, :e, :s, :t, false, 'Inquiry', 'ACTIVE', 1, now(), now())",
        c=w["cid"], e=away["id"], s=start - timedelta(days=5), t=end + timedelta(days=5))
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    rows = run_employees(created["id"])
    assert rows[late["id"]][1] == "JOINED_AFTER_PERIOD"
    assert rows[gone["id"]][1] == "LEFT_BEFORE_PERIOD"
    assert rows[away["id"]][1] == "SUSPENDED_ALL_PERIOD"
    population = created["population"]
    assert population["eligible"] == population["included"] + population["excluded"] == 7


# ---------------------------------------------------------------------------
# Identity (EC-RUN-01, EC-RUN-02)
# ---------------------------------------------------------------------------
def test_ec_run_01_second_regular_run_is_refused_until_the_first_is_cancelled(runs_world):
    w = runs_world
    first = new_run(w["acme"], w["group"], w["period"]["id"])
    r = w["acme"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                                   "pay_period_id": w["period"]["id"]})
    assert r.status_code == 409 and r.json()["code"] == "DUPLICATE_RUN", r.text
    ok(w["acme"].post(f"{RUNS}/{first['id']}/cancel", json={"reason": "Wrong setup"}))
    assert ok(w["acme"].get(f"{RUNS}/{first['id']}"))["period"]["status"] == "OPEN"
    again = new_run(w["acme"], w["group"], w["period"]["id"])
    assert again["id"] != first["id"]


def test_ec_run_01_partial_unique_index_and_ec_run_02_payment_trigger(runs_world):
    w = runs_world
    first = new_run(w["acme"], w["group"], w["period"]["id"])
    with tenant(w["cid"]) as conn:
        expect_error(conn, UNIQUE,
                     "INSERT INTO payroll_runs (company_id, number, label, legal_entity_id, pay_group_id, pay_period_id, "
                     "run_type, status, regular_sequence, currency, environment, created_at, updated_at) VALUES "
                     "(:c, 'PR-2026-999999', 'dup', :le, :g, :p, 'REGULAR', 'DRAFT', 1, 'INR', 'test', now(), now())",
                     c=w["cid"], le=w["le01"], g=w["group"], p=w["period"]["id"])
        # A second regular sequence (the allow-multiple setting) may exist, but it cannot pay Emma again.
        conn.execute(text(
            "INSERT INTO payroll_runs (company_id, number, label, legal_entity_id, pay_group_id, pay_period_id, "
            "run_type, status, regular_sequence, currency, environment, created_at, updated_at) VALUES "
            "(:c, 'PR-2026-999998', 'second', :le, :g, :p, 'REGULAR', 'DRAFT', 2, 'INR', 'test', now(), now())"),
            {"c": w["cid"], "le": w["le01"], "g": w["group"], "p": w["period"]["id"]})
        second = conn.execute(text(
            "SELECT id FROM payroll_runs WHERE number = 'PR-2026-999998'")).scalar_one()
        assignment = sql("SELECT employee_assignment_id FROM payroll_run_employees WHERE run_id = :r AND "
                         "employee_id = :e", r=first["id"], e=w["emp"]["Emma"]["id"])[0][0]
        expect_error(conn, UNIQUE,
                     "INSERT INTO payroll_run_employees (company_id, run_id, employee_id, employee_assignment_id, status, "
                     "currency, created_at, updated_at) VALUES (:c, :r, :e, :a, 'PENDING', 'INR', now(), now())",
                     c=w["cid"], r=second, e=w["emp"]["Emma"]["id"], a=assignment)


def test_ec_run_01_two_concurrent_creates_one_wins(app, runs_world):
    w = runs_world
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        from tests.helpers import login

        client = login(app, w["acme"].email)
        barrier.wait()
        r = client.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                                    "pay_period_id": w["period"]["id"]})
        results.append((r.status_code, r.json().get("code")))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s for s, _ in results) == [201, 409], results
    assert sql("SELECT count(*) FROM payroll_runs WHERE company_id = :c AND status <> 'CANCELLED'",
               c=w["cid"])[0][0] == 1


def test_create_guards_period_open_pay_group_active_and_types(runs_world):
    w = runs_world
    acme = w["acme"]
    later = periods_of(acme, w["group"])[3]
    sql("UPDATE pay_periods SET status = 'CLOSED' WHERE id = :p", p=later["id"])
    r = acme.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"], "pay_period_id": later["id"]})
    assert r.status_code == 409 and r.json()["code"] == "PERIOD_NOT_OPEN", r.text
    r = acme.post(RUNS, json={"run_type": "OFF_CYCLE", "pay_group_id": w["group"], "pay_period_id": w["period"]["id"]})
    assert r.status_code == 400 and r.json()["code"] == "EMPLOYEES_REQUIRED", r.text
    r = acme.post(RUNS, json={"run_type": "RETRO_ADJUSTMENT", "pay_group_id": w["group"],
                              "pay_period_id": w["period"]["id"]})
    assert r.status_code == 409 and r.json()["code"] == "ORIGINAL_PERIODS_REQUIRED", r.text
    r = acme.post(RUNS, json={"run_type": "REVERSAL", "pay_group_id": w["group"], "pay_period_id": w["period"]["id"]})
    assert r.status_code == 422, r.text
    other_group_period = periods_of(acme, w["ae_group"])[0]
    r = acme.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                              "pay_period_id": other_group_period["id"]})
    assert r.status_code == 404, r.text
    sql("UPDATE pay_groups SET status = 'INACTIVE' WHERE id = :g", g=w["group"])
    r = acme.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"], "pay_period_id": w["period"]["id"]})
    assert r.status_code == 409 and r.json()["code"] == "PAY_GROUP_INACTIVE", r.text


def test_create_requires_permission_and_scope(runs_world):
    w = runs_world
    r = w["approver"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                                       "pay_period_id": w["period"]["id"]})
    assert r.status_code == 403, r.text
    r = w["cleo"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                                   "pay_period_id": w["period"]["id"]})
    assert r.status_code == 404, r.text  # LE01 is outside Cleo's entity scope
    ae_period = current_period(w["acme"], w["ae_group"])
    created = new_run(w["cleo"], w["ae_group"], ae_period["id"])
    assert created["status"] == "DRAFT"


def test_idempotent_create_retry_returns_the_first_run(runs_world):
    w = runs_world
    headers = {"Idempotency-Key": "create-run-1"}
    first = new_run(w["acme"], w["group"], w["period"]["id"], headers=headers)
    r = w["acme"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                                   "pay_period_id": w["period"]["id"]}, headers=headers)
    assert r.status_code == 201 and r.json()["id"] == first["id"], r.text
    assert r.headers.get("Idempotent-Replayed") == "true" and r.json() == first
    assert sql("SELECT count(*) FROM payroll_runs WHERE company_id = :c", c=w["cid"])[0][0] == 1


def test_mass_assignment_of_server_owned_fields_is_rejected(runs_world):
    w = runs_world
    for extra in ({"status": "FINALIZED"}, {"company_id": w["beta"]["cid"]}, {"totals": {}}, {"stale": False},
                  {"number": "PR-2026-000001"}):
        r = w["acme"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"],
                                       "pay_period_id": w["period"]["id"], **extra})
        assert r.status_code == 422, (extra, r.text)


# ---------------------------------------------------------------------------
# The transition function
# ---------------------------------------------------------------------------
def test_transition_checks_version_permission_system_actions_and_guards(runs_world):
    from app.core.errors import AppError
    from app.payroll.services import runs as service

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    db, principal = principal_of(w["acme"])
    try:
        with pytest.raises(AppError) as error:
            service.transition(db, principal, created["id"], "PRECHECK_PASSED", expected_version=99)
        assert error.value.status_code == 409 and error.value.code == "ROW_VERSION_CONFLICT"
        db.rollback()
        with pytest.raises(AppError) as error:
            service.transition(db, principal, created["id"], "COMPLETE_CALCULATION")
        assert error.value.code == "INVALID_TRANSITION"
        db.rollback()
        force_status(created["id"], "CALCULATING")
        with pytest.raises(AppError) as error:
            service.transition(db, principal, created["id"], "COMPLETE_CALCULATION")
        assert error.value.status_code == 403  # a system action
        db.rollback()
        force_status(created["id"], "READY")
        with pytest.raises(AppError) as error:
            service.transition(db, principal, created["id"], "CALCULATE")
        assert error.value.code == "GUARD_NOT_CHECKED"  # IDEMPOTENCY_KEY is the caller's verdict
        db.rollback()
        with pytest.raises(AppError) as error:
            service.transition(db, principal, created["id"], "CALCULATE", IDEMPOTENCY_KEY=False)
        assert error.value.code == "IDEMPOTENCY_KEY_REQUIRED"
        db.rollback()
        with pytest.raises(ValueError):
            service.transition(db, principal, created["id"], "CALCULATE", NOT_A_GUARD=True)
        db.rollback()
        run = service.transition(db, principal, created["id"], "CALCULATE", IDEMPOTENCY_KEY=True)
        assert run.status == "CALCULATING" and run.calculated_by_user_id == principal.user_id
        db.commit()
        run = service.transition(db, None, run, "COMPLETE_CALCULATION", system=True)
        assert run.status == "CALCULATED"
        db.commit()
    finally:
        db.close()
    db, approver = principal_of(w["approver"])
    try:
        with pytest.raises(AppError) as error:
            service.transition(db, approver, created["id"], "CANCEL", reason="no")
        assert error.value.status_code == 403  # the approver lacks payroll:run:finalize
    finally:
        db.close()
    assert "INPUTS_LOCKED" in audit_actions(w["cid"], "payroll_run", created["id"])


def test_ec_run_17_finalized_run_is_immutable_with_correction_hint(runs_world):
    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(created["id"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    for path, body in (("cancel", {"reason": "late"}), ("precheck", None)):
        r = w["acme"].post(f"{RUNS}/{created['id']}/{path}", json=body)
        assert r.status_code == 409 and r.json()["code"] == "RUN_FINALIZED", r.text
        assert r.headers.get("X-Payroll-Hint") == "CREATE_CORRECTION"
    with tenant(w["cid"]) as conn:
        expect_error(conn, CHECK, "UPDATE payroll_runs SET totals = '{\"INR\": {}}'::jsonb WHERE id = :r",
                     r=created["id"])
        expect_error(conn, CHECK, "UPDATE payroll_runs SET status = 'DRAFT' WHERE id = :r", r=created["id"])


def test_ec_run_16_reopen_clears_approvals_with_audit(runs_world):
    from app.payroll.services import runs as service

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(created["id"], "APPROVAL_PENDING")
    approver_id = sql("SELECT id FROM users WHERE email = :m", m=w["approver"].email)[0][0]
    sql("INSERT INTO payroll_approvals (company_id, run_id, step_order, approver_user_id, acted_as, decision, "
        "acted_at, created_at, updated_at) VALUES (:c, :r, 1, :u, 'approver', 'APPROVED', now(), now(), now())",
        c=w["cid"], r=created["id"], u=approver_id)
    db, principal = principal_of(w["acme"])
    try:
        run = service.transition(db, principal, created["id"], "REOPEN", reason="Found an error")
        assert run.status == "REOPENED_FOR_CORRECTION"
        db.commit()
    finally:
        db.close()
    assert sql("SELECT cleared_at IS NOT NULL, cleared_reason FROM payroll_approvals WHERE run_id = :r",
               r=created["id"]) == [(True, "Found an error")]
    assert "RUN_REOPENED" in audit_actions(w["cid"], "payroll_run", created["id"])


# ---------------------------------------------------------------------------
# Cancel (EC-RUN-15, EC-VAR-01)
# ---------------------------------------------------------------------------
def test_ec_run_15_cancel_releases_inputs_and_leaves_no_documents(runs_world):
    from app.payroll.services import wage_inputs

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    emma = run_employees(created["id"])[w["emp"]["Emma"]["id"]][2]
    component = sql("SELECT id FROM pay_components WHERE company_id = :c ORDER BY id LIMIT 1", c=w["cid"])[0][0]
    sql("INSERT INTO wage_inputs (company_id, employee_id, legal_entity_id, pay_group_id, component_id, component_code, "
        "amount, currency, target_period_start, target_period_end, status, row_version, created_at, updated_at) VALUES "
        "(:c, :e, :le, :g, :comp, 'BONUS', 1000, 'INR', :s, :t, 'APPROVED', 1, now(), now())",
        c=w["cid"], e=w["emp"]["Emma"]["id"], le=w["le01"], g=w["group"], comp=component,
        s=w["period"]["start"], t=w["period"]["end"])
    input_id = sql("SELECT id FROM wage_inputs WHERE company_id = :c", c=w["cid"])[0][0]
    with session(w["cid"]) as (db, _):
        wage_inputs.reserve_inputs(db, w["cid"], emma, [input_id])
        db.commit()
    r = w["acme"].post(f"{RUNS}/{created['id']}/cancel", json={"reason": "   "})
    assert r.status_code == 422, r.text
    cancelled = ok(w["acme"].post(f"{RUNS}/{created['id']}/cancel", json={"reason": "Wrong period"}))
    assert cancelled["status"] == "CANCELLED"
    assert sql("SELECT status, reserved_by_run_employee_id FROM wage_inputs WHERE id = :i", i=input_id) == [
        ("APPROVED", None)]
    assert sql("SELECT count(*) FROM issued_documents WHERE company_id = :c", c=w["cid"])[0][0] == 0
    assert sql("SELECT count(*) FROM payroll_lines WHERE run_id = :r", r=created["id"])[0][0] == 0
    assert "RUN_CANCELLED" in audit_actions(w["cid"], "payroll_run", created["id"])
    r = w["acme"].post(f"{RUNS}/{created['id']}/cancel", json={"reason": "again"})
    assert r.status_code == 409 and r.json()["code"] == "RUN_CANCELLED", r.text


def test_ec_run_08_two_concurrent_cancels_one_succeeds(app, runs_world):
    from tests.helpers import login

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    clients = [login(app, w["acme"].email) for _ in range(2)]
    barrier = threading.Barrier(2)
    results = []

    def attempt(client):
        barrier.wait()
        r = client.post(f"{RUNS}/{created['id']}/cancel", json={"reason": "duplicate", "row_version": 1})
        results.append((r.status_code, r.json().get("code")))

    threads = [threading.Thread(target=attempt, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s for s, _ in results) == [200, 409], results
    assert [c for s, c in results if s == 409][0] in ("RUN_CANCELLED", "ROW_VERSION_CONFLICT")
    assert audit_actions(w["cid"], "payroll_run", created["id"]).count("RUN_CANCELLED") == 1


def test_cancel_retry_with_the_same_idempotency_key_replays(runs_world):
    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    headers = {"Idempotency-Key": "cancel-1"}
    first = w["acme"].post(f"{RUNS}/{created['id']}/cancel", json={"reason": "dup"}, headers=headers)
    again = w["acme"].post(f"{RUNS}/{created['id']}/cancel", json={"reason": "dup"}, headers=headers)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert audit_actions(w["cid"], "payroll_run", created["id"]).count("RUN_CANCELLED") == 1


# ---------------------------------------------------------------------------
# Holds (3.5 §9, EC-RUN-13)
# ---------------------------------------------------------------------------
def test_full_and_payment_holds_and_release(runs_world):
    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    rows = run_employees(created["id"])
    emma, ivan, nora = (rows[w["emp"][n]["id"]][2] for n in ("Emma", "Ivan", "Nora"))
    base = f"{RUNS}/{created['id']}/employees"
    held = ok(w["acme"].post(f"{base}/{emma}/hold", json={"hold_type": "FULL", "reason_code": "DISPUTE",
                                                         "note": "Salary dispute"}))
    assert held["status"] == "ON_HOLD" and held["hold_type"] == "FULL"
    r = w["acme"].post(f"{base}/{emma}/hold", json={"hold_type": "PAYMENT", "reason_code": "DISPUTE", "note": "x"})
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_ON_HOLD"
    paid_hold = ok(w["acme"].post(f"{base}/{ivan}/hold", json={"hold_type": "PAYMENT", "reason_code": "BANK_ISSUE",
                                                              "note": "Account closed"}))
    assert paid_hold["status"] == "PENDING" and paid_hold["hold_type"] == "PAYMENT"
    r = w["acme"].post(f"{base}/{nora}/hold", json={"hold_type": "FULL", "reason_code": "OTHER", "note": "x"})
    assert r.status_code == 409 and r.json()["code"] == "EMPLOYEE_EXCLUDED"
    r = w["acme"].post(f"{base}/{emma}/hold", json={"hold_type": "FULL", "reason_code": "VACATION", "note": "x"})
    assert r.status_code == 422
    assert run_row(created["id"])[3]["on_hold"] == 2
    released = ok(w["acme"].post(f"{base}/{emma}/release", json={"note": "Resolved"}))
    assert released["status"] == "PENDING" and released["hold_type"] is None and released["payment_intent"] is None
    r = w["acme"].post(f"{base}/{emma}/release", json={"note": "again"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_ON_HOLD"
    assert {"EMPLOYEE_HELD", "EMPLOYEE_RELEASED"} <= set(audit_actions(w["cid"], "payroll_run_employee", emma))
    population = ok(w["acme"].get(f"{RUNS}/{created['id']}/population"))
    assert population["on_hold"] == 1 and population["eligible"] == population["included"] + population["excluded"]


def test_ec_run_13_payment_hold_release_after_finalization_creates_a_pending_intent(app, runs_world):
    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    ivan = run_employees(created["id"])[w["emp"]["Ivan"]["id"]][2]
    base = f"{RUNS}/{created['id']}/employees/{ivan}"
    ok(w["acme"].post(f"{base}/hold", json={"hold_type": "PAYMENT", "reason_code": "BANK_ISSUE", "note": "Closed"}))
    force_status(created["id"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    r = w["acme"].post(f"{base}/release", json={"note": "New account"})
    assert r.status_code == 403, r.text  # after finalization releasing a payment needs payroll:payment:approve
    finance = login_as(app, w["acme"], create_employee(w["acme"], "Fay", "Finance"), ["finance_payments"])
    released = ok(finance.post(f"{base}/release", json={"note": "New account verified"}))
    from app.payroll.services import outbox
    from app.payroll.services import runs as service

    intent = released["payment_intent"]
    assert released["hold_type"] is None
    if outbox.subscribers(service.PAYMENT_HOLD_RELEASED_TOPIC):  # the payments milestone pays it later
        assert intent["status"] == "QUEUED"
        assert sql("SELECT payload->>'run_employee_id' FROM outbox_events WHERE id = :i",
                   i=intent["outbox_event_id"]) == [(str(ivan),)]
    else:
        assert intent["status"] == "PENDING_PAYMENTS_MILESTONE"
    emma = run_employees(created["id"])[w["emp"]["Emma"]["id"]][2]
    r = finance.post(f"{RUNS}/{created['id']}/employees/{emma}/hold",
                     json={"hold_type": "FULL", "reason_code": "OTHER", "note": "x"})
    assert r.status_code == 409 and r.json()["code"] == "RUN_FINALIZED", r.text


def test_nobody_holds_or_releases_their_own_pay(app, runs_world):
    w = runs_world
    pia_salary = assign_and_approve(w["acme"], w["approver"], w["emp"]["Pia"]["id"], w["version"], "900000",
                                    effective_from=date.fromisoformat(w["period"]["start"]))
    assert pia_salary["status"] in ("ACTIVE", "SCHEDULED")
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    own = run_employees(created["id"])[w["emp"]["Pia"]["id"]][2]
    from tests.helpers import login

    pia = login(app, w["processor"].email)
    r = pia.post(f"{RUNS}/{created['id']}/employees/{own}/hold",
                 json={"hold_type": "FULL", "reason_code": "OTHER", "note": "self"})
    assert r.status_code == 403, r.text
    emma = run_employees(created["id"])[w["emp"]["Emma"]["id"]][2]
    assert ok(pia.post(f"{RUNS}/{created['id']}/employees/{emma}/hold",
                       json={"hold_type": "PAYMENT", "reason_code": "OTHER", "note": "check"}))["hold_type"] == "PAYMENT"


def test_hold_of_an_unknown_or_foreign_run_employee_is_404(runs_world):
    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    beta_run = new_run(w["beta"]["client"], w["beta"]["group"], w["beta"]["period"]["id"])
    beta_employee = list(run_employees(beta_run["id"]).values())[0][2]
    for run_id, eid in ((created["id"], beta_employee), (beta_run["id"], beta_employee), (created["id"], 999999)):
        r = w["acme"].post(f"{RUNS}/{run_id}/employees/{eid}/hold",
                           json={"hold_type": "FULL", "reason_code": "OTHER", "note": "x"})
        assert r.status_code == 404, r.text


def test_snapshots_are_immutable_and_lines_follow_their_run(runs_world):
    from app.database.db import system_engine

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    emma = run_employees(created["id"])[w["emp"]["Emma"]["id"]][2]
    other = new_run(w["acme"], w["group"], w["period"]["id"], run_type="OFF_CYCLE",
                    employee_ids=[w["emp"]["Ivan"]["id"]])
    with tenant(w["cid"]) as conn:
        snapshot = conn.execute(text(
            "INSERT INTO payroll_input_snapshots (company_id, run_employee_id, version, content, content_hash, "
            "created_at, updated_at) VALUES (:c, :e, 1, '{}'::jsonb, repeat('a', 64), now(), now()) RETURNING id"),
            {"c": w["cid"], "e": emma}).scalar_one()
    with tenant(w["cid"]) as conn:
        # The immutability trigger refuses every update, whatever the role's grants.
        expect_error(conn, CHECK, "UPDATE payroll_input_snapshots SET version = 2 WHERE id = :s", s=snapshot)
        expect_error(conn, CHECK,
                     "INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, kind, stage, amount, "
                     "currency, created_at, updated_at) VALUES (:c, :r, :e, 'BASIC', 'EARNING', 4, 1, 'INR', now(), now())",
                     c=w["cid"], r=other["id"], e=emma)
    with system_engine.begin() as owner:  # even the owner cannot change a snapshot (trigger)
        expect_error(owner, CHECK, "UPDATE payroll_input_snapshots SET version = 2 WHERE id = :s", s=snapshot)


# ---------------------------------------------------------------------------
# Hooks of earlier milestones answered by runs (FOLLOW-UPS F-35, F-37; EC-VAR-03, EC-CMP-06, EC-CMP-13,
# EC-CFG-03, EC-RUN-09)
# ---------------------------------------------------------------------------
def test_ec_var_03_period_status_decides_whether_inputs_are_accepted(runs_world):
    from app.payroll.services import wage_inputs

    w = runs_world
    start, end = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    new_run(w["acme"], w["group"], w["period"]["id"])  # the period becomes PROCESSING
    with session(w["cid"]) as (db, _):
        assert wage_inputs.period_is_open(db, w["cid"], w["group"], start, end)  # PROCESSING still accepts
    sql("UPDATE pay_periods SET status = 'CLOSED' WHERE id = :p", p=w["period"]["id"])
    with session(w["cid"]) as (db, _):
        assert not wage_inputs.period_is_open(db, w["cid"], w["group"], start, end)
        later = end + timedelta(days=400)
        assert wage_inputs.period_is_open(db, w["cid"], w["group"], later, later)  # not generated yet: open
        # Another tenant's closed period is invisible (RLS): Beta's pay group id matches nothing here.
        assert wage_inputs.period_is_open(db, w["cid"], w["beta"]["group"], start, end)


def test_ec_run_09_ec_cmp_13_approved_salary_change_marks_locked_runs_stale(runs_world):
    from app.core.errors import AppError
    from app.payroll.services import compensation
    from app.payroll.services import runs as service

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    draft = new_run(w["acme"], w["group"], w["period"]["id"], run_type="OFF_CYCLE",
                    employee_ids=[w["emp"]["Ivan"]["id"]])
    force_status(created["id"], "REVIEW_REQUIRED")
    start = date.fromisoformat(w["period"]["start"])
    with session(w["cid"]) as (db, _):
        assert compensation.mark_runs_stale(db, w["cid"], w["emp"]["Ivan"]["id"], start) == 1  # DRAFT is not locked
        assert compensation.mark_runs_stale(db, w["cid"], w["emp"]["Nora"]["id"], start) == 0  # excluded employee
        db.commit()
    stale, reasons = sql("SELECT stale, stale_reasons FROM payroll_runs WHERE id = :r", r=created["id"])[0]
    assert stale and reasons[0]["source"] == "compensation" and reasons[0]["employee_id"] == w["emp"]["Ivan"]["id"]
    assert run_row(draft["id"])[2] is False
    assert "RUN_MARKED_STALE" in audit_actions(w["cid"], "payroll_run", created["id"])
    db, principal = principal_of(w["acme"])
    try:
        with pytest.raises(AppError) as error:
            service.transition(db, principal, created["id"], "SUBMIT_FOR_APPROVAL", VARIANCE_FLAGS_ACKNOWLEDGED=True)
        assert error.value.status_code == 409 and error.value.code == "RUN_STALE"
        assert "RUN_STALE" in {b["code"] for b in service.finalization_blockers(
            db, service.load_run(db, w["cid"], created["id"]))}
    finally:
        db.close()


def test_ec_cfg_14_settings_change_marks_runs_of_the_scope_stale(runs_world):
    from app.payroll import settings_registry as reg
    from app.payroll.services import settings as settings_engine

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(created["id"], "CALCULATED")
    start = date.fromisoformat(w["period"]["start"])
    with session(w["cid"]) as (db, _):
        # Another entity's change does not touch this run; a pay-group change does.
        assert settings_engine.mark_runs_stale(db, w["cid"], ["payroll.overtime.enabled"], reg.Level.LEGAL_ENTITY,
                                               w["ae"], start) == 0
        assert settings_engine.mark_runs_stale(db, w["cid"], ["payroll.overtime.enabled"], reg.Level.PAY_GROUP,
                                               w["group"], start) == 1
        db.commit()
    assert run_row(created["id"])[2] is True


def test_ec_cfg_03_ec_cmp_12_finalized_periods_are_reported_to_settings_and_compensation(runs_world):
    from app.payroll import settings_registry as reg
    from app.payroll.services import compensation
    from app.payroll.services import settings as settings_engine

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    start, end = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    with session(w["cid"]) as (db, _):
        assert compensation.finalized_periods(db, w["cid"], w["emp"]["Emma"]["id"], start) == []
        assert not settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.overtime.enabled",
                                                             reg.Level.TENANT, None, start)
    sql("UPDATE payroll_run_employees SET status = 'CALCULATED' WHERE run_id = :r AND status = 'PENDING'",
        r=created["id"])
    force_status(created["id"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    with session(w["cid"]) as (db, _):
        periods = compensation.finalized_periods(db, w["cid"], w["emp"]["Emma"]["id"], start)
        assert [(p["run_id"], p["period_id"], p["start"], p["end"]) for p in periods] == [
            (created["id"], w["period"]["id"], start.isoformat(), end.isoformat())]
        assert compensation.finalized_periods(db, w["cid"], w["emp"]["Nora"]["id"], start) == []  # excluded
        assert compensation.finalized_periods(db, w["cid"], w["emp"]["Emma"]["id"], end + timedelta(days=1)) == []
        assert settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.overtime.enabled",
                                                         reg.Level.TENANT, None, start)
        assert settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.overtime.enabled",
                                                         reg.Level.PAY_GROUP, w["group"], end)
        assert not settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.overtime.enabled",
                                                             reg.Level.PAY_GROUP, w["ae_group"], start)
        assert not settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.overtime.enabled",
                                                             reg.Level.TENANT, None, end + timedelta(days=1))


def test_ec_cmp_06_component_used_in_run_lines_is_reported(runs_world):
    from app.payroll.services import components

    w = runs_world
    created = new_run(w["acme"], w["group"], w["period"]["id"])
    emma = run_employees(created["id"])[w["emp"]["Emma"]["id"]][2]
    component = sql("SELECT id, code FROM pay_components WHERE company_id = :c ORDER BY id LIMIT 1", c=w["cid"])[0]
    with session(w["cid"]) as (db, _):
        assert not components.component_in_runs(db, w["cid"], component[0])
    with tenant(w["cid"]) as conn:
        conn.execute(text(
            "INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, component_id, kind, stage, "
            "amount, currency, created_at, updated_at) VALUES (:c, :r, :e, :code, :comp, 'EARNING', 4, 1, 'INR', "
            "now(), now())"), {"c": w["cid"], "r": created["id"], "e": emma, "code": component[1], "comp": component[0]})
    with session(w["cid"]) as (db, _):
        assert components.component_in_runs(db, w["cid"], component[0])
    with session(w["beta"]["cid"]) as (db, _):  # another tenant never sees Acme's lines
        assert not components.component_in_runs(db, w["beta"]["cid"], component[0])
