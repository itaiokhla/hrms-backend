"""Tenant isolation and object scope of the run tables and endpoints (3.9 §5.4, contract §9).

EC-SEC-01 (row-level security, composite keys, three tenants), EC-SEC-02 (foreign ids are 404 on every
endpoint), EC-SEC-03/EC-SEC-05 (scoped users and counts), EC-SEC-06/EC-SEC-07 (server-owned fields).
"""

from datetime import date

from sqlalchemy import text

from tests.helpers import create_employee, invite_and_activate
from tests.payroll.test_components import ok
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    current_period,
    new_run,
    run_employees,
    runs_world,
)
from tests.payroll.test_wage_inputs import CHECK, DENIED, FOREIGN_KEY, expect_error, sql, tenant

TABLES = ("pay_calendars", "pay_periods", "payroll_runs", "payroll_run_employees", "payroll_input_snapshots",
          "payroll_lines", "payroll_line_sources", "payroll_exceptions", "payroll_approvals", "ytd_balances",
          "statutory_liabilities", "statutory_attestations")
APPEND_ONLY = ("payroll_input_snapshots",)


def _seed(client, group, period_id, entity):
    """One row in every run table for the client's tenant (runs and calendars through the API)."""
    cid = client.company_id
    run = new_run(client, group, period_id)
    run_employee, employee = sql("SELECT id, employee_id FROM payroll_run_employees WHERE run_id = :r ORDER BY id "
                                 "LIMIT 1", r=run["id"])[0]
    user = sql("SELECT id FROM users WHERE email = :m", m=client.email)[0][0]
    ids = {"payroll_runs": run["id"], "payroll_run_employees": run_employee,
           "pay_calendars": sql("SELECT id FROM pay_calendars WHERE pay_group_id = :g", g=group)[0][0],
           "pay_periods": period_id}
    with tenant(cid) as conn:
        def insert(statement, **params):
            return conn.execute(text(statement), {"c": cid, **params}).scalar_one()

        ids["payroll_input_snapshots"] = insert(
            "INSERT INTO payroll_input_snapshots (company_id, run_employee_id, version, content, content_hash, "
            "created_at, updated_at) VALUES (:c, :e, 1, '{}'::jsonb, repeat('b', 64), now(), now()) RETURNING id",
            e=run_employee)
        ids["payroll_lines"] = insert(
            "INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, kind, stage, amount, "
            "currency, created_at, updated_at) VALUES (:c, :r, :e, 'BASIC', 'EARNING', 4, 100, 'INR', now(), now()) "
            "RETURNING id", r=run["id"], e=run_employee)
        ids["payroll_line_sources"] = insert(
            "INSERT INTO payroll_line_sources (company_id, line_id, run_id, run_employee_id, source_type, source_id, "
            "created_at, updated_at) VALUES (:c, :l, :r, :e, 'WAGE_INPUT', 1, now(), now()) RETURNING id",
            l=ids["payroll_lines"], r=run["id"], e=run_employee)
        ids["payroll_exceptions"] = insert(
            "INSERT INTO payroll_exceptions (company_id, run_id, code, severity, message, dedupe_key, created_at, "
            "updated_at) VALUES (:c, :r, 'TEST', 'FLAG', 'test', 'TEST', now(), now()) RETURNING id", r=run["id"])
        ids["payroll_approvals"] = insert(
            "INSERT INTO payroll_approvals (company_id, run_id, step_order, approver_user_id, decision, acted_at, "
            "created_at, updated_at) VALUES (:c, :r, 1, :u, 'APPROVED', now(), now(), now()) RETURNING id",
            r=run["id"], u=user)
        ids["ytd_balances"] = insert(
            "INSERT INTO ytd_balances (company_id, employee_id, legal_entity_id, tax_year, bucket, amount, currency, "
            "created_at, updated_at) VALUES (:c, :e, :le, '2026-27', 'GROSS', 10, 'INR', now(), now()) RETURNING id",
            e=employee, le=entity)
        ids["statutory_liabilities"] = insert(
            "INSERT INTO statutory_liabilities (company_id, run_id, legal_entity_id, scheme, payee_authority, currency, "
            "created_at, updated_at) VALUES (:c, :r, :le, 'ZZ_PENSION', 'ZZ Authority', 'INR', now(), now()) "
            "RETURNING id", r=run["id"], le=entity)
        ids["statutory_attestations"] = insert(
            "INSERT INTO statutory_attestations (company_id, legal_entity_id, effective_from, reason, "
            "attested_by_user_id, created_at, updated_at) VALUES (:c, :le, '2026-01-01', 'outside', :u, now(), now()) "
            "RETURNING id", le=entity, u=user)
    return ids


def test_ec_sec_01_every_run_table_is_isolated_across_three_tenants(runs_world):
    from app.models import TENANT_TABLES

    w = runs_world
    assert set(TABLES) <= set(TENANT_TABLES)
    tenants = {w["cid"]: (w["acme"], w["group"], w["period"]["id"], w["le01"])}
    for key in ("beta", "cobalt"):
        t = w[key]
        tenants[t["cid"]] = (t["client"], t["group"], t["period"]["id"], t["entity"])
    seeded = {cid: _seed(*args) for cid, args in tenants.items()}
    companies = list(seeded)

    with tenant(None) as conn:
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES} == {t: 0 for t in TABLES}
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in TABLES:
                owners = {o for (o,) in conn.execute(text(f"SELECT company_id FROM {table}"))}
                assert owners == {cid}, table
                for other in others:
                    foreign = seeded[other][table]
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"),
                                        {"i": foreign}).scalar() == 0, table
                    if table not in APPEND_ONLY:
                        assert conn.execute(text(f"UPDATE {table} SET updated_at = now() WHERE id = :i"),
                                            {"i": foreign}).rowcount == 0, table
                # Moving a row to another tenant: RLS refuses it (immutable snapshots refuse any update).
                expect_error(conn, CHECK if table in APPEND_ONLY else DENIED,
                             f"UPDATE {table} SET company_id = :o WHERE id = :i", o=others[0], i=seeded[cid][table])
    a, b = companies[0], companies[1]
    with tenant(a) as conn:  # composite keys refuse another tenant's period, pay group, run or employee
        # payroll_runs_guard refuses a period outside the run's pay group before the composite key does.
        expect_error(conn, CHECK,
                     "INSERT INTO payroll_runs (company_id, number, label, legal_entity_id, pay_group_id, pay_period_id, "
                     "run_type, status, currency, environment, created_at, updated_at) VALUES (:c, 'PR-2026-777777', "
                     "'x', :le, :g, :p, 'OFF_CYCLE', 'DRAFT', 'INR', 'test', now(), now())",
                     c=a, le=tenants[a][3], g=tenants[a][1], p=seeded[b]["pay_periods"])
        expect_error(conn, FOREIGN_KEY,  # another tenant's pay group (with this tenant's entity)
                     "INSERT INTO pay_calendars (company_id, pay_group_id, frequency, rules, effective_from, status, "
                     "created_at, updated_at) VALUES (:c, :g, 'MONTHLY', '{}'::jsonb, '2030-01-01', 'ACTIVE', now(), "
                     "now())", c=a, g=tenants[b][1])
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO payroll_exceptions (company_id, run_id, code, severity, message, dedupe_key, "
                     "created_at, updated_at) VALUES (:c, :r, 'X', 'FLAG', 'x', 'X2', now(), now())",
                     c=a, r=seeded[b]["payroll_runs"])
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO statutory_attestations (company_id, legal_entity_id, effective_from, reason, "
                     "attested_by_user_id, created_at, updated_at) VALUES (:c, :le, '2026-01-01', 'x', :u, now(), now())",
                     c=a, le=tenants[b][3], u=sql("SELECT id FROM users WHERE company_id = :c LIMIT 1", c=a)[0][0])
        expect_error(conn, DENIED,
                     "INSERT INTO payroll_exceptions (company_id, run_id, code, severity, message, dedupe_key, "
                     "created_at, updated_at) VALUES (:o, :r, 'X', 'FLAG', 'x', 'X3', now(), now())",
                     o=b, r=seeded[b]["payroll_runs"])


def test_ec_sec_02_another_tenants_ids_are_404_on_every_endpoint(runs_world):
    w = runs_world
    acme = w["acme"]
    run = new_run(acme, w["group"], w["period"]["id"])
    ok(acme.post(f"{RUNS}/{run['id']}/precheck"))
    emma = run_employees(run["id"])[w["emp"]["Emma"]["id"]][2]
    exception = ok(acme.get(f"{RUNS}/{run['id']}/exceptions"))["items"][0]["id"]
    attestation = ok(acme.post(f"{PAY}/statutory-attestations", json={
        "legal_entity_id": w["ae"], "effective_from": "2026-01-01", "reason": "outside"}), 201)["id"]
    beta = w["beta"]["client"]
    calls = [
        ("get", f"{RUNS}/{run['id']}", None), ("get", f"{RUNS}/{run['id']}/population", None),
        ("get", f"{RUNS}/{run['id']}/exceptions", None), ("post", f"{RUNS}/{run['id']}/precheck", None),
        ("post", f"{RUNS}/{run['id']}/cancel", {"reason": "x"}),
        ("post", f"{RUNS}/{run['id']}/employees/{emma}/hold", {"hold_type": "FULL", "reason_code": "OTHER", "note": "x"}),
        ("post", f"{RUNS}/{run['id']}/employees/{emma}/release", {"note": "x"}),
        ("post", f"{PAY}/exceptions/{exception}/waive", {"reason": "x"}),
        ("post", f"{PAY}/exceptions/{exception}/defer", {"to": "RETRO", "reason": "x"}),
        ("post", f"{PAY}/exceptions/{exception}/fix", None),
        ("get", f"{PAY}/pay-groups/{w['group']}/calendar", None), ("get", f"{PAY}/pay-groups/{w['group']}/periods", None),
        ("post", f"{PAY}/pay-groups/{w['group']}/calendar", {"frequency": "MONTHLY", "rules": {}}),
        ("get", f"{PAY}/pay-periods/{w['period']['id']}/pay-date-check", None),
        ("get", f"{PAY}/run-population-preview?pay_group_id={w['group']}&pay_period_id={w['period']['id']}", None),
        ("get", f"{PAY}/statutory-attestations/{attestation}", None),
        ("post", f"{PAY}/statutory-attestations/{attestation}/approve", None),
        ("post", f"{PAY}/statutory-attestations/{attestation}/revoke", {"reason": "x"}),
    ]
    for method, url, body in calls:
        r = getattr(beta, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (url, r.status_code, r.text)
    assert ok(beta.get(RUNS))["total"] == 0
    assert ok(beta.get(f"{PAY}/statutory-attestations"))["total"] == 0
    r = beta.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["group"], "pay_period_id": w["period"]["id"]})
    assert r.status_code == 404, r.text


def test_ec_sec_05_entity_scoped_user_sees_and_counts_only_its_runs(runs_world):
    w = runs_world
    in_run = new_run(w["acme"], w["group"], w["period"]["id"])
    ae_run = new_run(w["acme"], w["ae_group"], current_period(w["acme"], w["ae_group"])["id"])
    cleo = w["cleo"]
    listed = ok(cleo.get(RUNS))
    assert listed["total"] == 1 and [r["id"] for r in listed["items"]] == [ae_run["id"]]
    assert ok(cleo.get(RUNS, params={"pay_group_id": w["group"]}))["total"] == 0
    assert cleo.get(f"{RUNS}/{in_run['id']}").status_code == 404
    assert cleo.get(f"{RUNS}/{in_run['id']}/population").status_code == 404
    assert ok(cleo.get(f"{RUNS}/{ae_run['id']}"))["id"] == ae_run["id"]
    assert ok(w["acme"].get(RUNS))["total"] == 2


def test_ec_sec_03_users_without_run_permissions_get_403(app, runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    worker = invite_and_activate(app, w["acme"], create_employee(w["acme"], "Walt"), ["employee"])
    for method, url, body in (("get", RUNS, None), ("get", f"{RUNS}/{run['id']}", None),
                              ("post", f"{RUNS}/{run['id']}/precheck", None),
                              ("post", f"{RUNS}/{run['id']}/cancel", {"reason": "x"}),
                              ("post", RUNS, {"run_type": "REGULAR", "pay_group_id": w["group"],
                                              "pay_period_id": w["period"]["id"]}),
                              ("get", f"{PAY}/statutory-attestations", None)):
        r = getattr(worker, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (url, r.text)
    r = w["approver"].post(f"{RUNS}/{run['id']}/cancel", json={"reason": "x"})
    assert r.status_code == 403  # payroll:run:read only
    assert ok(w["approver"].get(f"{RUNS}/{run['id']}"))["available_actions"] == []


def test_ec_sec_07_forged_tenant_or_state_in_requests_is_rejected(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    r = w["acme"].post(f"{RUNS}/{run['id']}/cancel", json={"reason": "x", "status": "DRAFT"})
    assert r.status_code == 422
    r = w["acme"].post(f"{RUNS}/{run['id']}/employees/1/hold",
                       json={"hold_type": "FULL", "reason_code": "OTHER", "note": "x", "company_id": w["beta"]["cid"]})
    assert r.status_code == 422
    r = w["acme"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["beta"]["group"],
                                   "pay_period_id": w["beta"]["period"]["id"]})
    assert r.status_code == 404  # another tenant's pay group is unknown here
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run["id"]) == [("DRAFT",)]
    assert date.fromisoformat(w["period"]["start"]) <= date.fromisoformat(w["period"]["end"])
