"""Three-tenant isolation of the payments tables and endpoints, cross-tenant reference attacks (foreign
bank accounts, funding accounts, runs, instructions, statements) and entity scope (3.9 §5.4, §9;
EC-SEC-01; contract §2 invariants)."""

import json
from datetime import date

import pytest
from sqlalchemy import text

from tests.helpers import create_employee
from tests.payroll.test_payment_obligations import (
    BALANCES,
    BATCHES,
    INSTRUCTIONS,
    MY_PAYMENTS,
    OBLIGATIONS,
    err,
    finalized_run,
    instructions_of,
    ok,
    payments_world,
    step_up,
    submitted_batch,
)
from tests.payroll.test_reconciliation import IMPORT, MAPPING, MATCHES, csv_statement, upload
from tests.payroll.test_wage_inputs import DENIED, FOREIGN_KEY, expect_error, role, sql, tenant, user_with_role

PAY = "/api/v1/payroll"
TENANT_TABLES = ("payment_obligations", "payment_batches", "payment_instructions", "payment_events", "payment_files",
                 "bank_statement_imports", "bank_statement_lines", "reconciliation_matches",
                 "payment_manual_settlements")


@pytest.fixture
def tenants(app):
    """Acme and Beta with a submitted batch, a statement and a manual proof each; Cobalt with nothing."""
    worlds = {}
    for name in ("Acme Pay", "Beta Pay"):
        world = payments_world(app, name, employees=("Emma", "Ivan"))
        run_id = finalized_run(world, {"Emma": "100.00", "Ivan": "200.00"})
        batch = submitted_batch(world, run_id)
        rows = instructions_of(run_id)
        emma = rows[world["emp"]["Emma"]["id"]]["id"]
        ivan = rows[world["emp"]["Ivan"]["id"]]["id"]
        ref = sql("SELECT reference FROM payment_instructions WHERE id = :i", i=emma)[0][0]
        statement = upload(world["fran"], world["funding"],
                           csv_statement((date.today().isoformat(), "-100.00", ref, ""),
                                         (date.today().isoformat(), "-999.00", "UNKNOWN", "")))
        proof = ok(world["fran"].post(f"{INSTRUCTIONS}/{ivan}/manual-settle", data={"method": "CASH", "reference": "R1"},
                                      files={"file": ("p.pdf", b"%PDF-1.4 proof", "application/pdf")}), 201)
        world.update(run_id=run_id, batch=batch, emma_instruction=emma, ivan_instruction=ivan, statement=statement,
                     unmatched=[m for m in statement["matches"] if m["status"] == "UNMATCHED"][0], proof=proof,
                     obligation=sql("SELECT id FROM payment_obligations WHERE run_id = :r LIMIT 1", r=run_id)[0][0])
        worlds[name.split()[0].lower()] = world
    worlds["cobalt"] = payments_world(app, "Cobalt Pay", employees=("Emma",))
    return worlds


def test_ec_sec_01_rls_hides_every_payments_table_across_three_tenants(tenants):
    acme, beta, cobalt = tenants["acme"], tenants["beta"], tenants["cobalt"]
    for table in TENANT_TABLES:
        totals = {r[0]: r[1] for r in sql(f"SELECT company_id, count(*) FROM {table} GROUP BY company_id")}
        assert totals.get(acme["cid"]) and totals.get(beta["cid"]), table
        for world in (acme, beta, cobalt):
            with tenant(world["cid"]) as conn:
                seen = conn.execute(text(f"SELECT company_id, count(*) FROM {table} GROUP BY company_id")).all()
                assert {c for c, _ in seen} <= {world["cid"]}, table
                assert sum(n for _, n in seen) == totals.get(world["cid"], 0), table
        with tenant(None) as conn:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table


def test_rls_refuses_writes_into_another_tenant_and_the_webhook_inbox_is_closed(tenants):
    acme, beta = tenants["acme"], tenants["beta"]
    with tenant(beta["cid"]) as conn:
        expect_error(conn, DENIED, "INSERT INTO payment_obligations (company_id, source_type, payee_type, employee_id, "
                     "legal_entity_id, amount, currency, status) VALUES (:c, 'CLAIM', 'EMPLOYEE', :e, :le, 1, 'INR', "
                     "'OPEN')", c=acme["cid"], e=acme["emp"]["Emma"]["id"], le=acme["le"])
        updated = conn.execute(text("UPDATE payment_instructions SET status_note = 'x' WHERE id = :i"),
                               {"i": acme["emma_instruction"]}).rowcount
        assert updated == 0
        expect_error(conn, DENIED, "SELECT count(*) FROM payment_webhook_events")


def test_composite_foreign_keys_block_cross_tenant_references(tenants):
    """A row of one tenant can never point at another tenant's bank account, funding account, run or batch."""
    acme, beta = tenants["acme"], tenants["beta"]
    acme_account = sql("SELECT id FROM bank_accounts WHERE company_id = :c LIMIT 1", c=acme["cid"])[0][0]
    beta_obligation = sql("SELECT id, employee_id, legal_entity_id FROM payment_obligations WHERE company_id = :c "
                          "LIMIT 1", c=beta["cid"])[0]
    with tenant(beta["cid"]) as conn:
        expect_error(conn, FOREIGN_KEY, "INSERT INTO payment_instructions (company_id, obligation_id, employee_id, "
                     "legal_entity_id, bank_account_id, sequence, idempotency_key, amount, currency, status, "
                     "evidence_status) VALUES (:c, :o, :e, :le, :a, 9, 'attack-1', 1, 'INR', 'NOT_READY', "
                     "'UNVERIFIED')", c=beta["cid"], o=beta_obligation[0], e=beta_obligation[1],
                     le=beta_obligation[2], a=acme_account)
        expect_error(conn, FOREIGN_KEY, "INSERT INTO payment_batches (company_id, reference, legal_entity_id, "
                     "funding_account_id, method, file_format, currency, status, prepared_by_user_id) VALUES "
                     "(:c, 'PB-ATTACK', :le, :f, 'BANK_FILE', 'GENERIC_CSV', 'INR', 'DRAFT', :u)",
                     c=beta["cid"], le=beta_obligation[2], f=acme["funding"], u=beta["pete"].user_id)
        expect_error(conn, FOREIGN_KEY, "INSERT INTO payment_obligations (company_id, source_type, run_id, "
                     "run_employee_id, payee_type, employee_id, legal_entity_id, amount, currency, status) VALUES "
                     "(:c, 'RUN_EMPLOYEE', :r, :re, 'EMPLOYEE', :e, :le, 1, 'INR', 'OPEN')", c=beta["cid"],
                     r=acme["run_id"], re=sql("SELECT id FROM payroll_run_employees WHERE run_id = :r LIMIT 1",
                                              r=acme["run_id"])[0][0], e=beta_obligation[1], le=beta_obligation[2])


def test_every_endpoint_returns_404_for_another_tenants_objects(tenants):
    acme, beta, cobalt = tenants["acme"], tenants["beta"], tenants["cobalt"]
    for attacker in (beta["fran"], cobalt["fran"]):
        step_up(attacker)
        for url in (f"{BATCHES}/{acme['batch']['id']}", f"{BATCHES}/{acme['batch']['id']}/file",
                    f"{OBLIGATIONS}/{acme['obligation']}", f"{INSTRUCTIONS}/{acme['emma_instruction']}",
                    f"{INSTRUCTIONS}/{acme['emma_instruction']}/events",
                    f"{PAY}/bank-statements/{acme['statement']['import']['id']}",
                    f"{PAY}/bank-statements/{acme['statement']['import']['id']}/matches",
                    f"{PAY}/runs/{acme['run_id']}/payments"):
            err(attacker.get(url), 404)
        for url, body in (
                (f"{BATCHES}/{acme['batch']['id']}/approve", {}),
                (f"{BATCHES}/{acme['batch']['id']}/release", {}),
                (f"{BATCHES}/{acme['batch']['id']}/cancel", {"reason": "attack"}),
                (f"{BATCHES}/{acme['batch']['id']}/generate-file", {}),
                (f"{BATCHES}/{acme['batch']['id']}/bank-changes", {"decision": "PROCEED", "reason": "attack"}),
                (f"{INSTRUCTIONS}/{acme['ivan_instruction']}/reissue", {"reason": "attack"}),
                (f"{INSTRUCTIONS}/{acme['ivan_instruction']}/cancel", {"reason": "attack"}),
                (f"{INSTRUCTIONS}/{acme['ivan_instruction']}/release-hold", {"reason": "attack"}),
                (f"{PAY}/manual-settlements/{acme['proof']['id']}/approve", {}),
                (f"{MATCHES}/{acme['unmatched']['id']}/escalate", {"reason": "attack"}),
                (f"{MATCHES}/{acme['unmatched']['id']}/link", {"instruction_id": acme["ivan_instruction"],
                                                                "reason": "attack"}),
                (f"{PAY}/runs/{acme['run_id']}/payments/reconcile", {"reason": "attack"}),
                (BATCHES, {"run_id": acme["run_id"]}),
                (f"{BATCHES}/proposal", {"run_id": acme["run_id"]})):
            err(attacker.post(url, json=body), 404)
        err(attacker.post(f"{INSTRUCTIONS}/{acme['ivan_instruction']}/manual-settle",
                          data={"method": "CASH", "reference": "x"},
                          files={"file": ("p.pdf", b"%PDF-1.4", "application/pdf")}), 404)
        err(attacker.post(IMPORT, data={"funding_account_id": str(acme["funding"]), "format": "CSV",
                                        "mapping": json.dumps(MAPPING)},
                          files={"file": ("s.csv", b"Value Date,Amount\r\n", "text/csv")}), 404)
    # Lists never contain another tenant's rows.
    acme_ids = {
        OBLIGATIONS: {r[0] for r in sql("SELECT id FROM payment_obligations WHERE company_id = :c", c=acme["cid"])},
        BATCHES: {r[0] for r in sql("SELECT id FROM payment_batches WHERE company_id = :c", c=acme["cid"])},
        INSTRUCTIONS: {r[0] for r in sql("SELECT id FROM payment_instructions WHERE company_id = :c", c=acme["cid"])},
        MATCHES: {r[0] for r in sql("SELECT id FROM reconciliation_matches WHERE company_id = :c", c=acme["cid"])},
        f"{PAY}/bank-statements": {r[0] for r in sql("SELECT id FROM bank_statement_imports WHERE company_id = :c",
                                                     c=acme["cid"])},
    }
    for world in (beta, cobalt):
        for url, foreign in acme_ids.items():
            assert foreign, url
            assert not {i["id"] for i in ok(world["fran"].get(url))["items"]} & foreign, url
        balances = ok(world["fran"].get(BALANCES))["items"]
        assert all(b["employee_id"] not in (acme["emp"]["Emma"]["id"], acme["emp"]["Ivan"]["id"]) for b in balances)


def test_cross_tenant_reference_attacks_on_own_objects(tenants):
    """Own run + a foreign funding account, own match + a foreign instruction, own world + foreign ids: 404."""
    acme, beta = tenants["acme"], tenants["beta"]
    err(beta["pete"].post(f"{BATCHES}/proposal", json={"run_id": beta["run_id"], "funding_account_id": acme["funding"]}),
        404)
    err(beta["pete"].post(BATCHES, json={"run_id": beta["run_id"], "funding_account_id": acme["funding"]}), 404)
    err(beta["fran"].post(f"{MATCHES}/{beta['unmatched']['id']}/link",
                          json={"instruction_id": acme["emma_instruction"], "reason": "attack"}), 404)
    err(beta["pete"].get(f"{OBLIGATIONS}/{acme['obligation']}"), 404)
    listed = ok(beta["pete"].get(OBLIGATIONS, params={"run_id": acme["run_id"]}))
    assert listed == {"total": 0, "items": []}
    # The employee's own My Payments never shows another tenant's (or another employee's) payments.
    from tests.payroll.test_compensation import login_as

    emma = login_as(beta["app"], beta["admin"], beta["emp"]["Emma"], ["employee"])
    mine = ok(emma.get(MY_PAYMENTS))["items"]
    assert {i["employee_id"] for i in mine} == {beta["emp"]["Emma"]["id"]}


def test_out_of_scope_legal_entities_are_invisible(app, tenants):
    """A finance user scoped to another legal entity sees none of LE01's payments (404, empty lists)."""
    acme = tenants["acme"]
    admin = acme["admin"]
    r = admin.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Acme Gulf FZ-LLC",
                                                   "country_code": "AE", "base_currency": "AED",
                                                   "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    ae = r.json()["id"]
    scoped_role = role(admin, "AE payments", "LEGAL_ENTITY", ("payroll:payment:prepare", "payroll:payment:approve",
                                                              "payroll:payment:submit", "payroll:payment:reconcile",
                                                              "payroll:run:read"))
    clerk = user_with_role(app, admin, create_employee(admin, "Cleo", hire_date="2024-01-15"), scoped_role, [ae])
    assert ok(clerk.get(OBLIGATIONS)) == {"total": 0, "items": []}
    assert ok(clerk.get(BATCHES))["total"] == 0
    assert ok(clerk.get(MATCHES))["total"] == 0
    assert ok(clerk.get(f"{PAY}/bank-statements"))["total"] == 0
    for url in (f"{OBLIGATIONS}/{acme['obligation']}", f"{BATCHES}/{acme['batch']['id']}",
                f"{INSTRUCTIONS}/{acme['emma_instruction']}", f"{PAY}/bank-statements/{acme['statement']['import']['id']}"):
        err(clerk.get(url), 404)
    err(clerk.post(f"{BATCHES}/{acme['batch']['id']}/cancel", json={"reason": "scope"}), 404)
    err(clerk.post(f"{MATCHES}/{acme['unmatched']['id']}/escalate", json={"reason": "scope"}), 404)
    err(clerk.post(IMPORT, data={"funding_account_id": str(acme["funding"]), "format": "CSV",
                                 "mapping": json.dumps(MAPPING)}, files={"file": ("s.csv", b"x", "text/csv")}), 404)
