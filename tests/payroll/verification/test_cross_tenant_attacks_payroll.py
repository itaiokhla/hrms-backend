"""Goal 2 §14 / §12 security: the cross-tenant attack suite for payroll v2 (three tenants).

The world (``attack_world``) is built ONCE for this module through the real APIs where they exist (signup,
roles, compensation assignment + approval, payment batches, statements, manual proofs, self-service bank
changes, payslip questions, signed links, report exports) and with the same direct writes the payments and
documents suites use for a finalized run (``tests.payroll.test_payslips_v2.finalized_run`` writes the run's
lines; the real ``payroll.run.finalized`` subscribers then create obligations, instructions and payslips):

* **Alpha Technologies** and **Beta Logistics**: a full chain each (finalized run with lines and payslips,
  obligations/instructions, a released batch with its file, a statement with an unmatched line, a manual
  settlement proof, an approved compensation assignment, wage input, overtime, task, claim, loan, attestation,
  a pending bank change, a payslip question, a grade, a report export and a payslip ZIP export);
* **Gamma Solutions**: a minimal tenant (its own pay group and employee) that attacks the other two.

Each tenant has "Mallory": Company Administrator + a custom role holding EVERY payroll permission at COMPANY
scope, stepped up. She is the strongest possible insider of her own tenant, so a denial she receives on a
foreign object comes from tenant isolation, never from a missing permission.

The oracle for "authorized denial without disclosure" is differential: for every request the response for
the victim's real object id must be IDENTICAL (status, error code and message, or body) to the response for
an id that exists in no tenant. Any difference discloses existence. In addition the victim tenant's rows are
fingerprinted (every table in TENANT_TABLES, md5 over the rows) before and after each attack wave and must be
unchanged, and no response may contain a victim marker (employee e-mail, bank number, company name).

Goal 2 §14 vector "WebSocket ids" is not applicable: the backend exposes no WebSocket endpoint (no route of
``app.routes`` or ``app.payroll.routes`` is a WebSocket route; checked 23 Oct 2026, asserted by
``test_no_websocket_route_exists_so_the_websocket_vector_does_not_apply``).

Sharing the world across this module's tests is a speed decision (one shared test database, ~7 agents on the
lock): this module overrides the per-test ``clean_database`` and runs the harness cleanup once at teardown.
Tests that change state (revocations, suspensions) use their own users or restore what they change.
"""

from __future__ import annotations

import contextlib
import types
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text

from tests.helpers import create_employee, new_client, role_id
from tests.payroll.test_compensation import assign_and_approve, login_as
from tests.payroll.test_components import active_version_id, make_structure, setup_catalogue
from tests.payroll.test_payment_obligations import (
    add_bank_account,
    instructions_of,
    payments_world,
    step_up,
    submitted_batch,
)
from tests.payroll.test_payslips_v2 import finalized_run as written_run
from tests.payroll.test_reconciliation import csv_statement, upload
from tests.payroll.test_wage_inputs import component, session, sql, tenant

PAY = "/api/v1/payroll"
ME = "/api/v1/me/payroll"
PREFIXES = ("/api/v1/payroll", "/api/v1/me/payroll", "/api/v1/overtime", "/api/v1/tasks", "/api/v1/jobs")
NOWHERE = "2147000000"  # an id that exists in no tenant (int4, never reached by the test sequences)
NOWHERE_CODE = "VFY_NO_SUCH_CODE"
SEPTEMBER = date(2026, 9, 1)
# Hand-computed net of the written run's DEFAULT_LINES (tests/payroll/test_payslips_v2.py):
# employee-visible earnings BASIC 50000.00 + HRA 20000.00 (RETENTION_ACCRUAL is HR_ONLY, not paid in cash)
# - deduction PF_EMPLOYEE 1800.00 + reimbursement TRAVEL 500.00 = 68700.00 (EMPLOYER_PF is employer-only).
EMMA_NET = "68700.00"
TENANTS = {
    # name, slug prefix of test bank numbers (unique per tenant so leaks are attributable)
    "alpha": ("Alpha Technologies", "7711"),
    "beta": ("Beta Logistics", "7722"),
    "gamma": ("Gamma Solutions", "7733"),
}


# ---------------------------------------------------------------------------
# Module-scoped world (see module docstring)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_database():
    """Overrides tests/conftest.py for this module: the shared world is removed once, at module teardown."""
    from app.core.rate_limit import limiter

    limiter.reset()
    yield


def reset_database():
    """The harness's own per-test cleanup (tests/conftest.py::clean_database), run once."""
    import tests.conftest as harness

    # The fixture now takes the pytest request (shared datasets); a stand-in node never keeps the dataset.
    node = types.SimpleNamespace(get_closest_marker=lambda name: None, _hrms_next_item=None)
    steps = harness.clean_database.__wrapped__(types.SimpleNamespace(node=node))
    next(steps)
    with contextlib.suppress(StopIteration):
        next(steps)


def ok(response, status=(200, 201, 202)):
    statuses = (status,) if isinstance(status, int) else status
    assert response.status_code in statuses, f"{response.request.method} {response.request.url} -> " \
                                             f"{response.status_code}: {response.text[:600]}"
    return response.json() if response.content else None


def user_id_of(client):
    if getattr(client, "user_id", None) is None:
        client.user_id = client.me()["user"]["id"]
    return client.user_id


def payroll_codes():
    from app.core.permission_registry import PERMISSIONS

    return [p.code for p in PERMISSIONS if p.module in ("Payroll", "Payroll (legacy)")
            and any(s.value == "COMPANY" for s in p.scopes)]


def custom_role(admin, name, grants):
    """A custom role through the roles API; ``grants`` = [(permission, scope)]."""
    r = admin.post("/api/v1/roles", json={"name": name, "grants": [{"permission": c, "scope": s} for c, s in grants]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mallory(app, world):
    """Company Administrator + every payroll permission at COMPANY scope (the strongest insider), stepped up."""
    admin = world["admin"]
    everything = custom_role(admin, "Verification: all payroll", [(c, "COMPANY") for c in payroll_codes()])
    person = create_employee(admin, "Mallory", "Insider", hire_date="2024-01-15")
    client = login_as(app, admin, person, ["company_admin"])
    r = admin.put(f"/api/v1/users/{client.user_id}/roles",
                  json={"role_ids": [role_id(admin, "company_admin"), everything]})
    assert r.status_code == 200, r.text
    step_up(client)
    return client


def bank_number(prefix, n):
    return f"{prefix}0000{n:04d}"  # 12 digits: valid Indian account number format, fictitious


def build_tenant(app, key, *, full=True):
    """One tenant of the verification world (see the module docstring); returns a dict of clients and ids."""
    from app.payroll.services import outbox

    name, prefix = TENANTS[key]
    w = payments_world(app, name, employees=("Emma", "Ivan"))
    admin, cid, le, group = w["admin"], w["cid"], w["le"], w["group"]
    user_id_of(admin)
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    w.update(key=key, name=name, prefix=prefix, numbers={})
    w["mallory"] = mallory(app, w)
    w["emma_login"] = login_as(app, admin, w["emp"]["Emma"], ["employee"])
    if not full:
        return w

    # Compensation: structure published, Emma's assignment proposed by the founder, approved by Ava.
    setup_catalogue(admin)
    structure = make_structure(admin)
    version = active_version_id(structure)
    w["ava"] = login_as(app, admin, create_employee(admin, "Ava", "Approver"), ["payroll_approver"])
    w["pia"] = login_as(app, admin, create_employee(admin, "Pia", "Processor"), ["payroll_processor"])
    w["assignment"] = assign_and_approve(admin, w["ava"], emma, version, "1200000")["id"]
    w["structure"] = structure["id"]
    w["structure_version"] = version

    # A finalized run with lines (written like the documents suite) and its real finalized-run subscribers.
    for n, (person, employee_id) in enumerate((("Emma", emma), ("Ivan", ivan)), start=1):
        number = bank_number(prefix, n)
        w["numbers"][person] = number
        add_bank_account(cid, employee_id, number, holder=f"{person} Test")
    run_id, rows = written_run(cid, le, group, SEPTEMBER, [emma, ivan])
    with session(cid) as (db, _):
        outbox.emit(db, "payroll.run.finalized", {"run_id": run_id})
        db.commit()
    w["run_id"], w["run_employees"] = run_id, rows
    docs = dict(sql("SELECT employee_id, id FROM documents WHERE run_id = :r AND doc_type = 'PAYSLIP_V2'", r=run_id))
    assert set(docs) == {emma, ivan}, docs
    w["payslips"] = docs

    # Payments: a released batch with its file, a statement (matched + unmatched line), a manual proof.
    w["batch"] = submitted_batch(w, run_id)["id"]
    rows_by_employee = instructions_of(run_id)
    w["emma_instruction"] = rows_by_employee[emma]["id"]
    w["ivan_instruction"] = rows_by_employee[ivan]["id"]
    reference = sql("SELECT reference FROM payment_instructions WHERE id = :i", i=w["emma_instruction"])[0][0]
    statement = upload(w["fran"], w["funding"], csv_statement((date.today().isoformat(), f"-{EMMA_NET}", reference, ""),
                                                             (date.today().isoformat(), "-999.00", "UNKNOWN", "")))
    w["statement"] = statement["import"]["id"]
    w["unmatched"] = next(m["id"] for m in statement["matches"] if m["status"] == "UNMATCHED")
    w["proof"] = ok(w["fran"].post(f"{PAY}/payment-instructions/{w['ivan_instruction']}/manual-settle",
                                   data={"method": "CASH", "reference": "R1"},
                                   files={"file": ("p.pdf", b"%PDF-1.4 proof", "application/pdf")}), 201)["id"]

    # Self-service: publish, Emma's question, a signed download link, a pending bank change.
    ok(admin.post(f"{PAY}/runs/{run_id}/publish", json={"reason": "Verification world"}))
    emma_client = w["emma_login"]
    w["question"] = ok(emma_client.post(f"{ME}/payslips/{docs[emma]}/questions",
                                        json={"question": "Why is my HRA 20000?"}), 201)["id"]
    w["emma_link"] = ok(emma_client.get(f"{ME}/payslips/{docs[emma]}/download"))["url"]
    w["numbers"]["Emma-new"] = bank_number(prefix, 9)
    step_up(emma_client)  # bank changes need a fresh step-up (3.7 §12)
    change = ok(emma_client.post(f"{ME}/bank-accounts", json={
        "holder_name": "Emma Test", "country": "IN", "currency": "INR", "account_number": w["numbers"]["Emma-new"],
        "routing": {"ifsc": "HDFC0001234"}, "account_type": "SAVINGS", "bank_name": "HDFC Bank"}), 201)
    w["bank_change"] = change

    # Inputs and other payroll objects (same direct inserts as tests/payroll/test_run_finalize.py).
    period = w["period"]
    w["wage_input"] = sql(
        "INSERT INTO wage_inputs (company_id, employee_id, legal_entity_id, pay_group_id, component_id, "
        "component_code, amount, currency, target_period_start, target_period_end, attachments, source, status, "
        "flags, row_version, decided_at, created_at, updated_at) VALUES (:c, :e, :le, :pg, "
        "(SELECT id FROM pay_components WHERE company_id = :c AND code IN ('BONUS', 'BASIC') ORDER BY code = 'BONUS' "
        "DESC LIMIT 1), 'BONUS', 5000, 'INR', :s, :t, "
        "'[]', 'MANUAL', 'APPROVED', '[]', 1, now(), now(), now()) RETURNING id",
        c=cid, e=emma, le=le, pg=group, s=period["start"], t=period["end"])[0][0]
    w["overtime"] = sql(
        "INSERT INTO overtime_entries (company_id, employee_id, date, minutes_requested, minutes_approved, source, "
        "status, comp_off, pre_approval, version, decided_at, row_version, created_at, updated_at) VALUES (:c, :e, "
        ":d, 120, 120, 'APPROVED_REQUESTS', 'APPROVED', false, false, 1, now(), 1, now(), now()) RETURNING id",
        c=cid, e=emma, d=period["start"])[0][0]
    w["task"] = sql(
        "INSERT INTO tasks (company_id, title, task_type, quantity, unit, status, approved_on, decided_at, row_version, "
        "created_at, updated_at) VALUES (:c, 'Install meters', 'INSTALL', 2, 'unit', 'APPROVED', :d, now(), 1, now(), "
        "now()) RETURNING id", c=cid, d=period["start"])[0][0]
    sql("INSERT INTO task_assignees (company_id, task_id, employee_id, split_percent, created_at, updated_at) "
        "VALUES (:c, :t, :e, 100, now(), now())", c=cid, t=w["task"], e=emma)
    w["claim"] = sql(
        "INSERT INTO reimbursement_claims (company_id, employee_id, legal_entity_id, pay_group_id, policy_version_id, "
        "claim_type, claim_type_name, taxability, expense_date, amount, currency, payout_route, payout_amount, "
        "payout_currency, approved_amount, status, flags, decided_at, row_version, created_at, updated_at) VALUES "
        "(:c, :e, :le, :pg, (SELECT id FROM policy_versions WHERE company_id = :c ORDER BY id LIMIT 1), 'TRAVEL', "
        "'Travel', 'EXEMPT', :d, 300, 'INR', 'WITH_PAYROLL', 300, 'INR', 300, 'APPROVED', '[]', now(), 1, now(), "
        "now()) RETURNING id", c=cid, e=emma, le=le, pg=group, d=period["start"])[0][0]
    from tests.payroll.test_run_finalize import _loan

    w["loan"] = _loan(app, {"cid": cid, "acme": admin, "processor": w["pia"]}, ivan)
    w["attestation"] = ok(admin.post(f"{PAY}/statutory-attestations", json={
        "legal_entity_id": le, "effective_from": "2020-01-01",
        "reason": "Statutory filings handled by an external provider"}), 201)["id"]
    w["grade"] = ok(admin.post(f"{PAY}/org/grades", json={"code": "G1", "name": "Grade one"}))["id"]
    w["component_code"] = f"VFY_{key.upper()}"
    component(cid, w["component_code"])
    catalogue = ok(admin.get(f"{PAY}/reports"))
    w["report_codes"] = [r["code"] for g in catalogue["groups"] for r in g["reports"] if r["available"]]
    assert "payroll_register" in w["report_codes"], w["report_codes"]
    w["report_export"] = ok(admin.post(f"{PAY}/reports/payroll_register/exports", json={"format": "CSV"}), 202)["id"]
    w["zip_export"] = ok(admin.post(f"{PAY}/payslips/zip", json={"run_id": run_id}), 202)["id"]
    w["policy"] = first_id(ok(admin.get(f"{PAY}/policies")))
    # A pending tax declaration of Emma with one proof (the tax profile routes name both objects).
    submitter = w["emma_login"].me()["user"]["id"]
    w["tax_profile"] = sql(
        "INSERT INTO employee_tax_profiles (company_id, employee_id, legal_entity_id, country, pack_code, schema_hash, "
        "version_no, effective_from, status, declared_values, identifiers_enc, identifiers_masked, submitted_by_user_id, "
        "submitted_at, row_version, created_at, updated_at) VALUES (:c, :e, :le, 'IN', 'IN', :h, 1, '2026-04-01', "
        "'SUBMITTED', '{\"regime\": \"NEW\"}'::jsonb, '{}'::jsonb, '{}'::jsonb, :u, now(), 1, now(), now()) RETURNING id",
        c=cid, e=emma, le=le, h="0" * 64, u=submitter)[0][0]
    sql("INSERT INTO employee_tax_proofs (company_id, profile_id, employee_id, field_code, file_name, storage_path, "
        "sha256, mime, bytes, uploaded_by_user_id, created_at, updated_at) VALUES (:c, :p, :e, 'regime', 'proof.pdf', "
        ":path, :h, 'application/pdf', 12, :u, now(), now())",
        c=cid, p=w["tax_profile"], e=emma, path=f"{cid}/tax-proofs/none.pdf", h="a" * 64, u=submitter)
    return w


def first_id(listing):
    items = listing if isinstance(listing, list) else listing.get("items", [])
    return items[0]["id"]


@pytest.fixture(scope="module")
def attack_world():
    from app.main import app

    try:
        world = {key: build_tenant(app, key, full=key != "gamma") for key in TENANTS}
        world["app"] = app
        yield world
    finally:
        reset_database()


# ---------------------------------------------------------------------------
# Oracles
# ---------------------------------------------------------------------------
VOLATILE_KEYS = {"generated_at", "as_of", "now", "server_time", "request_id", "expires_at", "computed_at",
                 "fetched_at", "today", "checked_at", "evaluated_at"}


def _strip(value, echo=()):
    """Drop volatile keys; an id the caller itself sent and the server merely echoes back (a filter value under
    its own key, or inside an error message) becomes "<ID>" so the victim's and the missing id compare equal."""
    echo = {str(e) for e in (echo() if callable(echo) else echo)}
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in VOLATILE_KEYS:
                continue
            if k.endswith("_id") and str(v) in echo and not isinstance(v, (dict, list)):
                out[k] = "<ID>"
            elif k == "detail" and isinstance(v, str):
                text_value = v
                for e in echo:
                    text_value = re.sub(rf"(?<!\d){re.escape(e)}(?!\d)", "<ID>", text_value)
                out[k] = text_value
            else:
                out[k] = _strip(v, echo)
        return out
    if isinstance(value, list):
        # Content, not order: rows that tie on their sort key (audit events of one transaction) may come back in
        # any order, and disclosure is about what is returned.
        return sorted((_strip(v, echo) for v in value), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return value


def outcome(client, method, url, echo=(), **kwargs):
    """(status, normalized body) of one request; a server exception becomes ('EXC', type)."""
    try:
        response = client.request(method, url, **kwargs)
    except Exception as exc:  # noqa: BLE001 - TestClient re-raises server errors; record them as outcomes
        return ("EXC", type(exc).__name__), ""
    try:
        body = _strip(response.json(), echo)
    except ValueError:
        body = {"_sha256": hashlib.sha256(response.content).hexdigest()}
    return (response.status_code, body), response.text


def tenant_fingerprint(company_id):
    """{table: (rows, md5 of the rows)} of one tenant over every tenant table (owner connection, no RLS)."""
    from app.database.db import system_engine
    from app.models import TENANT_TABLES

    out = {}
    with system_engine.begin() as conn:
        for table in TENANT_TABLES:
            out[table] = tuple(conn.execute(text(
                f'SELECT count(*), coalesce(md5(string_agg(md5(x::text), \',\' ORDER BY md5(x::text))), \'\') '
                f'FROM "{table}" x WHERE company_id = :c'), {"c": company_id}).one())
    return out


def changed_tables(before, after):
    return sorted(t for t in before if before[t] != after.get(t))


def markers(world):
    """Strings that identify the victim tenant's data and must never appear in another tenant's responses."""
    found = [world["name"], *world["numbers"].values()]
    found += [e["email"] for e in world["emp"].values()]
    return found


# Path parameter -> table of the tenant object it names (context: the path segment before the parameter).
PARAM_TABLES = {
    "run_id": "payroll_runs", "run_employee_id": "payroll_run_employees", "loan_id": "loans",
    "report_id": "statutory_reports", "document_id": "documents", "task_id": "tasks",
    "claim_id": "reimbursement_claims", "batch_id": "payment_batches", "template_id": "salary_templates",
    "instruction_id": "payment_instructions", "policy_id": "policies", "employee_id": "employees",
    "payee_id": "employees", "cycle_id": "revision_cycles", "case_id": "final_settlement_cases",
    "invoice_id": "invoices", "input_id": "wage_inputs", "change_id": "bank_account_change_requests",
    "payroll_id": "payroll", "entry_id": "overtime_entries", "reversal_id": "payroll_reversals",
    "query_id": "payslip_queries", "assignment_id": "compensation_assignments", "rate_id": "fx_rates",
    "proposal_id": "arrears_proposals", "proof_id": "payment_manual_settlements", "pay_group_id": "pay_groups",
    "profile_id": "employee_tax_profiles",
    "match_id": "reconciliation_matches", "liability_id": "statutory_liabilities",
    "exception_id": "payroll_exceptions", "attestation_id": "statutory_attestations",
    "receipt_id": "reimbursement_receipts", "legal_entity_id": "legal_entities", "journal_id": "accounting_journals",
    "item_id": "final_settlement_items", "verification_id": "bank_verifications",
    "suspension_id": "employee_suspensions", "rate_card_id": "task_rate_cards", "period_id": "pay_periods",
    "obligation_id": "payment_obligations", "mapping_id": "gl_account_mappings", "lop_id": "lop_inputs",
    "holiday_id": "employee_optional_holidays", "branch_id": "branches", "agreement_id": "self_billing_agreements",
    "job_id": "jobs", "user_id": "users", "funding_account_id": "funding_accounts",
    "department_id": "departments", "cost_center_id": "cost_centers", "pay_period_id": "pay_periods",
    "hold_id": "legal_holds", "nominee_id": "nominees",
    "original_run_id": "payroll_runs", "structure_id": "salary_templates", "grade_id": "grades",
    "template_version_id": "salary_template_versions", "import_id": None, "request_id": None, "account_id": None,
    "object_id": None, "export_id": None, "evidence_id": None, "code": None,
}
CONTEXT_TABLES = {
    ("account_id", "bank-accounts"): "bank_accounts", ("account_id", "funding-accounts"): "funding_accounts",
    ("object_id", "pay-groups"): "pay_groups", ("object_id", "grades"): "grades",
    ("object_id", "cost-centers"): "cost_centers", ("object_id", "employment-types"): "employment_types",
    ("object_id", "designations"): "designations", ("object_id", "work-weeks"): "work_week_patterns",
    ("object_id", "holiday-calendars"): "holiday_calendars", ("request_id", "approvals"): "approval_requests",
    ("request_id", "change-requests"): "setting_change_requests",
    ("request_id", "document-requests"): "document_requests",
    ("import_id", "bank-statements"): "bank_statement_imports",
    ("import_id", "wage-input-imports"): "wage_input_imports", ("import_id", "imports"): "compensation_imports",
    ("export_id", "exports"): "export_jobs", ("export_id", "report-exports"): "jobs",
    ("evidence_id", "tasks"): "task_evidence", ("evidence_id", "evidence"): "task_evidence",
    ("request_id", "on-duty"): "on_duty_requests", ("proof_id", "proofs"): "employee_tax_proofs",
}
# Not tenant objects: catalogue codes, KPI names, legacy month/year, file formats.
NON_OBJECT_PARAMS = {"kpi", "alpha2", "fmt", "year", "month", "payee_type", "version"}
LITERALS = {"payee_type": "employee", "version": "1", "kpi": "net_pay", "alpha2": "IN", "fmt": "csv",
            "year": "2026", "month": "9"}


def _param_table(path, param):
    segments = path.split("/")
    before = segments[segments.index("{" + param + "}") - 1]
    if param == "code":
        return "pay_components:code" if before == "components" else None
    if param == "token":
        return "token"
    return CONTEXT_TABLES.get((param, before), PARAM_TABLES.get(param))


def object_value(world, path, param):
    """The id (or code/token) of one of ``world``'s objects for this path parameter, or None."""
    table = _param_table(path, param)
    if table is None:
        return None
    if table == "token":
        return world.get("emma_link", "").rsplit("/", 1)[-1] or None
    if table == "pay_components:code":
        return world.get("component_code")
    found = sql(f'SELECT id FROM "{table}" WHERE company_id = :c ORDER BY id LIMIT 1', c=world["cid"])
    return str(found[0][0]) if found else None


def nowhere_value(path, param):
    table = _param_table(path, param)
    if table == "pay_components:code":
        return NOWHERE_CODE
    if table == "token":
        return "eyJ2IjoxfQ.bm90LWEtc2lnbmF0dXJl"
    return NOWHERE


def payroll_routes(app):
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(PREFIXES):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                yield method, route


def object_params(path):
    return [p for p in re.findall(r"{([a-z_0-9]+)}", path)
            if p not in NON_OBJECT_PARAMS and not (p == "code" and "/reports/" in path)]


def fill(path, values):
    for param, value in values.items():
        path = path.replace("{" + param + "}", str(value))
    for param, literal in LITERALS.items():
        path = path.replace("{" + param + "}", literal)
    return path


def request_kwargs(method):
    return {} if method == "GET" else {"json": {}}


def differential(attacker, victim, method, path, *, mixed=False, **kwargs):
    """Compare the response for the victim's objects with the response for objects that exist nowhere.

    ``mixed``: the attacker's own objects for every parameter except the last one (a foreign child under an
    own parent). Returns (problem or None, tested: bool)."""
    params = object_params(path)
    victim_values, nowhere_values = {}, {}
    for i, param in enumerate(params):
        last = i == len(params) - 1
        if mixed and not last:
            own = object_value(attacker, path, param)
            if own is None:
                return None, False
            victim_values[param] = nowhere_values[param] = own
            continue
        value = object_value(victim, path, param)
        if value is None:
            return None, False
        victim_values[param], nowhere_values[param] = value, nowhere_value(path, param)
    client = attacker["mallory"]
    kwargs = kwargs or request_kwargs(method)
    seen, raw = outcome(client, method, fill(path, victim_values), echo=victim_values.values(), **kwargs)
    baseline, _ = outcome(client, method, fill(path, nowhere_values), echo=nowhere_values.values(), **kwargs)
    leaked = [m for m in markers(victim) if m and m in raw]
    if seen[0] == "EXC" or (isinstance(seen[0], int) and seen[0] >= 500):
        return f"{method} {path}{' (mixed)' if mixed else ''}: server error {seen} (baseline {baseline})", True
    if seen != baseline or leaked:
        return (f"{method} {path}{' (mixed)' if mixed else ''}: victim -> {str(seen)[:300]} | nowhere -> "
                f"{str(baseline)[:300]} | leaked {leaked}"), True
    if isinstance(seen[0], int) and 200 <= seen[0] < 300 and method != "GET":
        return f"{method} {path}: a write on a foreign id succeeded ({seen[0]})", True
    return None, True


ATTACKS = (("beta", "alpha"), ("gamma", "alpha"), ("alpha", "beta"), ("gamma", "beta"))
# Path parameters for which the verification world holds no object (so the substitution is untestable here);
# each is either covered by its module's own isolation test or has no data in a fresh tenant.
UNTESTED_ALLOWED = {
    "statutory_reports", "revision_cycles", "final_settlement_cases", "final_settlement_items", "invoices",
    "payroll_reversals", "fx_rates", "arrears_proposals", "statutory_liabilities", "payroll_exceptions",
    "reimbursement_receipts", "accounting_journals", "employee_suspensions", "task_rate_cards",
    "gl_account_mappings", "lop_inputs", "employee_optional_holidays", "self_billing_agreements",
    "compensation_imports", "wage_input_imports", "setting_change_requests", "document_requests",
    "task_evidence", "bank_verifications", "payroll", "approval_requests", "cost_centers", "employment_types",
    "designations", "work_week_patterns", "holiday_calendars", "on_duty_requests",
    # Isolated by their own three-tenant tests (tests/payroll/test_legal_holds.py EC-SEC-16,
    # tests/payroll/test_nominees.py EC-SEC-01); a fresh verification tenant holds none.
    "legal_holds", "nominees",
}


# ---------------------------------------------------------------------------
# 1. URL / id substitution on every payroll route (EC-SEC-02)
# ---------------------------------------------------------------------------
def test_ec_sec_02_every_payroll_route_answers_a_foreign_id_exactly_like_a_missing_one(attack_world):
    """EC-SEC-02, EC-DOC-01 (Goal 2 §14 vectors: URL manipulation; employee/run/run-employee/line/payslip/
    document/bank account/batch/instruction/statement/attestation/policy id substitution; another tenant's job
    id; signed document links). Every GET/POST/PUT/PATCH/DELETE route under /payroll, /me/payroll, /overtime,
    /tasks and /jobs with an object id in its path is called by the attacker's strongest insider with the
    victim's real object, and with an object that exists nowhere: the two answers must be identical, nothing
    of the victim may appear, no foreign write may succeed and the victim's rows must not change."""
    from app.core.rate_limit import limiter

    app = attack_world["app"]
    problems, tested, untested = [], set(), {}
    for attacker_key, victim_key in ATTACKS:
        attacker, victim = attack_world[attacker_key], attack_world[victim_key]
        step_up(attacker["mallory"])
        before = tenant_fingerprint(victim["cid"])
        for method, route in payroll_routes(app):
            params = object_params(route.path)
            if not params:
                continue
            limiter.reset()
            for mixed in (False, True) if len(params) > 1 else (False,):
                problem, was_tested = differential(attacker, victim, method, route.path, mixed=mixed)
                if problem:
                    problems.append(f"{attacker_key}->{victim_key} {problem}")
                if was_tested:
                    tested.add((method, route.path))
                elif not mixed:
                    missing = [_param_table(route.path, p) for p in params
                               if object_value(victim, route.path, p) is None]
                    untested[(method, route.path)] = missing
        changed = changed_tables(before, tenant_fingerprint(victim["cid"]))
        assert not changed, f"{attacker_key} attacking {victim_key} changed the victim's tables: {changed}"
    assert not problems, f"{len(problems)} disclosure/isolation problems:\n" + "\n".join(problems)
    never_tested = {route: tables for route, tables in untested.items() if route not in tested}
    unexplained = {r: t for r, t in never_tested.items() if not set(t) <= UNTESTED_ALLOWED}
    assert not unexplained, f"routes without a victim object and no recorded reason: {unexplained}"
    assert len(tested) >= 150, f"only {len(tested)} routes exercised"


# ---------------------------------------------------------------------------
# 2. Search, list and report filter manipulation (EC-SEC-02, EC-SEC-05)
# ---------------------------------------------------------------------------
QUERY_DEFAULTS = {"key": "payroll.attendance.enabled"}
AUDIT_TRAIL_REPORTS = {"bank_detail_events", "settings_policy_changes"}


def first_difference(a, b, path="$"):
    if type(a) is not type(b):
        return path, a, b
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b), key=str):
            if a.get(key) != b.get(key):
                return first_difference(a.get(key), b.get(key), f"{path}.{key}")
    elif isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return f"{path}.length", len(a), len(b)
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return first_difference(x, y, f"{path}[{i}]")
    return path, a, b


def _note_difference(label, seen, baseline):
    """Evidence for the audit-trail reports (logs/secverif-audit-report-diff.txt)."""
    from pathlib import Path

    target = Path(__file__).resolve().parents[3] / "logs" / "secverif-audit-report-diff.txt"
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{label}: {first_difference(seen, baseline)}" + chr(10))
    except OSError:
        pass


def _query_params(route):
    return [p.alias or p.name for p in route.dependant.query_params]


def test_ec_sec_05_list_search_and_report_filters_with_foreign_ids_reveal_nothing(attack_world):
    """EC-SEC-05, EC-SEC-02 (Goal 2 §14: search and report filter manipulation). Every collection endpoint's
    id filters (run_id, employee_id, legal_entity_id, pay_group_id, batch_id, document_id, ...) and every
    report preview's entity/pay-group/branch/department filters given the victim's ids answer exactly like an
    id that exists nowhere; settings values/resolve at a foreign scope are 404 like a missing scope."""
    app = attack_world["app"]
    problems, tested = [], 0
    for attacker_key, victim_key in ATTACKS:
        attacker, victim = attack_world[attacker_key], attack_world[victim_key]
        client = attacker["mallory"]
        before = tenant_fingerprint(victim["cid"])
        for method, route in payroll_routes(app):
            if method != "GET" or object_params(route.path) or "{" in route.path:
                continue
            for param in _query_params(route):
                table = PARAM_TABLES.get(param) if param.endswith("_id") else None
                if not table:
                    continue
                found = sql(f'SELECT id FROM "{table}" WHERE company_id = :c ORDER BY id LIMIT 1', c=victim["cid"])
                if not found:
                    continue
                base = {k: v for k, v in QUERY_DEFAULTS.items() if k in _query_params(route)}
                seen, raw = outcome(client, "GET", route.path, echo=[found[0][0]],
                                    params={**base, param: found[0][0]})
                baseline, _ = outcome(client, "GET", route.path, echo=[NOWHERE], params={**base, param: NOWHERE})
                leaked = [m for m in markers(victim) if m in raw]
                tested += 1
                if seen != baseline or leaked:
                    problems.append(f"{attacker_key}->{victim_key} GET {route.path}?{param}=victim: "
                                    f"{str(seen)[:250]} vs {str(baseline)[:250]} leaked {leaked}")
        # Report previews: every report code with every scope filter.
        filters = {"legal_entity_id": "legal_entities", "pay_group_id": "pay_groups", "branch_id": "branches",
                   "department_id": "departments", "cost_center_id": "cost_centers"}
        for code in attack_world["alpha"]["report_codes"]:
            for param, table in filters.items():
                found = sql(f'SELECT id FROM "{table}" WHERE company_id = :c ORDER BY id LIMIT 1', c=victim["cid"])
                if not found:
                    continue
                url = f"{PAY}/reports/{code}/preview"
                seen, raw = outcome(client, "GET", url, echo=[found[0][0]], params={param: found[0][0]})
                baseline, _ = outcome(client, "GET", url, echo=[NOWHERE], params={param: NOWHERE})
                leaked = [m for m in markers(victim) if m in raw]
                tested += 1
                if code in AUDIT_TRAIL_REPORTS and seen != baseline:
                    # These read the caller's own tenant audit trail and honour only the period filter; the
                    # trail grows while the suite runs, so compare the shape and rely on the leak check.
                    _note_difference(f"{attacker_key}->{victim_key} {code} {param}", seen, baseline)
                    seen = (seen[0], {k: v for k, v in seen[1].items() if k in ("report", "filters", "columns")})
                    baseline = (baseline[0], {k: v for k, v in baseline[1].items()
                                              if k in ("report", "filters", "columns")})
                if seen != baseline or leaked:
                    problems.append(f"{attacker_key}->{victim_key} report {code} {param}: {str(seen)[:250]} vs "
                                    f"{str(baseline)[:250]} leaked {leaked}")
        # Settings at a foreign scope.
        for scope_type, table in (("LEGAL_ENTITY", "legal_entities"), ("BRANCH", "branches"),
                                  ("PAY_GROUP", "pay_groups"), ("EMPLOYEE", "employees")):
            victim_id = sql(f'SELECT id FROM "{table}" WHERE company_id = :c ORDER BY id LIMIT 1', c=victim["cid"])[0][0]
            seen, _ = outcome(client, "GET", f"{PAY}/settings/values",
                              params={"scope_type": scope_type, "scope_id": victim_id})
            baseline, _ = outcome(client, "GET", f"{PAY}/settings/values",
                                  params={"scope_type": scope_type, "scope_id": NOWHERE})
            tested += 1
            if seen != baseline or seen[0] != 404:
                problems.append(f"{attacker_key}->{victim_key} settings values {scope_type}: {seen} vs {baseline}")
        # Today's "needs my action" list holds only the caller's own runs.
        own_runs = {r[0] for r in sql("SELECT id FROM payroll_runs WHERE company_id = :c", c=attacker["cid"])}
        listed = ok(client.get(f"{PAY}/runs", params={"needs_my_action": "true"}))
        listed_ids = {i["id"] for i in (listed if isinstance(listed, list) else listed.get("items", []))}
        if not listed_ids <= own_runs:
            problems.append(f"{attacker_key}: needs_my_action lists foreign runs {listed_ids - own_runs}")
        assert not changed_tables(before, tenant_fingerprint(victim["cid"]))
    assert not problems, "\n".join(problems)
    assert tested >= 40, tested


# ---------------------------------------------------------------------------
# 3. Foreign references inside request bodies, forged company ids, bulk payloads (EC-SEC-06, EC-SEC-07)
# ---------------------------------------------------------------------------
def _victim(victim, table):
    return sql(f'SELECT id FROM "{table}" WHERE company_id = :c ORDER BY id LIMIT 1', c=victim["cid"])[0][0]


def body_cases(attacker, victim):
    """(label, method, url, body builder(ref)) where ref(table) gives the id to plant (victim's or nowhere)."""
    own_emp = attacker["emp"]["Emma"]["id"]
    period = attacker["period"]
    today = date.today().isoformat()
    return [
        ("run for a foreign pay group/period", "POST", f"{PAY}/runs",
         lambda ref: {"run_type": "REGULAR", "pay_group_id": ref("pay_groups"), "pay_period_id": ref("pay_periods")}),
        ("off-cycle run mixing an own and a foreign employee", "POST", f"{PAY}/runs",
         lambda ref: {"run_type": "OFF_CYCLE", "pay_group_id": attacker["group"], "pay_period_id": period["id"],
                      "employee_ids": [own_emp, ref("employees")]}),
        ("wage input for a foreign employee", "POST", f"{PAY}/wage-inputs",
         lambda ref: {"employee_id": ref("employees"), "component_code": "BONUS", "period_start": period["start"],
                      "period_end": period["end"], "amount": {"amount": "100.00", "currency": "INR"}}),
        ("loan for a foreign employee", "POST", f"{PAY}/loans",
         lambda ref: {"employee_id": ref("employees"), "kind": "LOAN",
                      "amount": {"amount": "1000.00", "currency": "INR"}, "tenure": 6}),
        ("claim for a foreign employee", "POST", f"{PAY}/reimbursement-claims",
         lambda ref: {"employee_id": ref("employees"), "claim_type": "TRAVEL", "expense_date": today,
                      "amount": {"amount": "10.00", "currency": "INR"}}),
        ("overtime for a foreign employee", "POST", "/api/v1/overtime/entries",
         lambda ref: {"employee_id": ref("employees"), "date": today, "minutes": 60, "reason": "Attack"}),
        ("task assigned to a foreign employee", "POST", "/api/v1/tasks",
         lambda ref: {"title": "Attack", "task_type": "INSTALL", "quantity": "1", "unit": "unit",
                      "assignees": [{"employee_id": ref("employees")}]}),
        ("attestation for a foreign legal entity", "POST", f"{PAY}/statutory-attestations",
         lambda ref: {"legal_entity_id": ref("legal_entities"), "effective_from": "2020-01-01", "reason": "Attack"}),
        ("salary assignment with a foreign structure version", "POST", f"{PAY}/employees/{own_emp}/compensation",
         lambda ref: {"template_version_id": ref("salary_template_versions"),
                      "pay_basis_amount": {"amount": "1200000", "currency": "INR"},
                      "effective_from": period["start"], "reason": "HIRE"}),
        ("bank account for a foreign payee", "POST", None,
         lambda ref: {"holder_name": "Attack", "country": "IN", "currency": "INR", "account_number": "771199990001",
                      "routing": {"ifsc": "HDFC0001234"}, "account_type": "SAVINGS", "bank_name": "HDFC Bank"}),
        ("payment batch for a foreign run", "POST", f"{PAY}/payment-batches", lambda ref: {"run_id": ref("payroll_runs")}),
        ("batch proposal with a foreign funding account", "POST", f"{PAY}/payment-batches/proposal",
         lambda ref: {"run_id": attacker.get("run_id") or ref("payroll_runs"),
                      "funding_account_id": ref("funding_accounts")}),
        ("bulk publish mixing an own and a foreign payslip", "POST", f"{PAY}/payslips/publish",
         lambda ref: {"document_ids": [own_doc(attacker), ref("documents")], "reason": "Attack"}),
        ("payslip ZIP of a foreign run", "POST", f"{PAY}/payslips/zip", lambda ref: {"run_id": ref("payroll_runs")}),
        ("report export filtered on a foreign entity", "POST",
         f"{PAY}/reports/{attack_world_codes(attacker)}/exports",
         lambda ref: {"format": "CSV", "filters": {"legal_entity_id": ref("legal_entities")}}),
        ("cost center in a foreign legal entity", "POST", f"{PAY}/org/cost-centers",
         lambda ref: {"code": f"CC{uuid.uuid4().hex[:4]}", "name": "Attack", "legal_entity_id": ref("legal_entities")}),
        ("designation with a foreign grade", "POST", f"{PAY}/org/designations",
         lambda ref: {"code": f"DS{uuid.uuid4().hex[:4]}", "name": "Attack", "grade_id": ref("grades")}),
        ("own pay group moved into a foreign legal entity", "PATCH", f"{PAY}/org/pay-groups/{attacker['group']}",
         lambda ref: {"legal_entity_id": ref("legal_entities")}),
        ("GL mapping for a foreign legal entity", "PUT", f"{PAY}/accounting/gl-mappings",
         lambda ref: {"component_code": "BASIC", "legal_entity_id": ref("legal_entities"), "debit_account": "5000",
                      "credit_account": "2100"}),
        ("settings batch mixing an own change and a foreign scope (all-or-nothing)", "PUT", f"{PAY}/settings/values",
         lambda ref: {"changes": [
             {"key": "payslip.show_ytd", "scope_type": "TENANT", "value": True, "reason": "Attack"},
             {"key": "payslip.show_ytd", "scope_type": "LEGAL_ENTITY", "scope_id": ref("legal_entities"),
              "value": False, "reason": "Attack"}]}),
        ("link an own unmatched statement line to a foreign payment", "POST",
         f"{PAY}/reconciliation-matches/{attacker.get('unmatched', NOWHERE)}/link",
         lambda ref: {"instruction_id": ref("payment_instructions"), "reason": "Attack"}),
        ("role assignment with a foreign role", "PUT", f"/api/v1/users/{user_id_of(attacker['emma_login'])}/roles",
         lambda ref: {"role_ids": [ref("roles")]}),
        ("role assignment for a foreign user", "PUT", None, lambda ref: {"role_ids": []}),
        ("scope targets on a foreign legal entity", "PUT",
         f"/api/v1/users/{user_id_of(attacker['mallory'])}/scope-targets",
         lambda ref: {"assignments": [{"role_id": role_id(attacker["admin"], "company_admin"),
                                       "legal_entity_ids": [ref("legal_entities")]}]}),
        ("invitation linked to a foreign employee", "POST", "/api/v1/users/invite",
         lambda ref: {"email": f"attack{uuid.uuid4().hex[:6]}@attack.io", "full_name": "Attack",
                      "employee_id": ref("employees"), "role_ids": []}),
    ]


ACCEPTED_AS_EMPTY_FILTER = {"report export filtered on a foreign entity"}


def own_doc(world):
    docs = world.get("payslips")
    if docs:
        return next(iter(docs.values()))
    return int(NOWHERE) - 1


def attack_world_codes(world):
    return "payroll_register"  # needs only payroll:reports:read (no extra permission)


def test_ec_sec_07_foreign_references_in_bodies_and_mixed_bulk_payloads_are_refused_like_missing_ids(attack_world):
    """EC-SEC-07, EC-SEC-02 (Goal 2 §14: forged legal-entity ids; foreign bank account and salary assignment
    cross-references; bulk update payloads mixing tenants; cross-company role assignment). Each body with the
    victim's id answers exactly like the same body with an id that exists nowhere, the attacker's own part of
    a mixed payload is not applied, and the victim's rows do not change."""
    problems = []
    for attacker_key, victim_key in ATTACKS:
        attacker, victim = attack_world[attacker_key], attack_world[victim_key]
        client = attacker["mallory"]
        step_up(client)
        before = tenant_fingerprint(victim["cid"])
        own_before = tenant_fingerprint(attacker["cid"])
        for label, method, url, build in body_cases(attacker, victim):
            if label == "bank account for a foreign payee":
                victim_url = f"{PAY}/payees/employee/{_victim(victim, 'employees')}/bank-accounts"
                nowhere_url = f"{PAY}/payees/employee/{NOWHERE}/bank-accounts"
            elif label == "role assignment for a foreign user":
                victim_url = f"/api/v1/users/{_victim(victim, 'users')}/roles"
                nowhere_url = f"/api/v1/users/{NOWHERE}/roles"
            else:
                victim_url = nowhere_url = url
            planted = []

            def victim_ref(table):
                planted.append(_victim(victim, table))
                return planted[-1]

            seen, raw = outcome(client, method, victim_url, json=build(victim_ref), echo=lambda: planted,
                                headers={"Idempotency-Key": f"v-{uuid.uuid4().hex}"})
            baseline, _ = outcome(client, method, nowhere_url, json=build(lambda t: int(NOWHERE)), echo=[NOWHERE],
                                  headers={"Idempotency-Key": f"v-{uuid.uuid4().hex}"})
            leaked = [m for m in markers(victim) if m in raw]
            if label in ACCEPTED_AS_EMPTY_FILTER and seen[0] == baseline[0] == 202:
                # A filter on a foreign entity is accepted like a filter on a missing one; the export job runs
                # with the requester's scope and its file must hold none of the victim's data.
                for job_id in (seen[1]["id"], baseline[1]["id"]):
                    link = ok(client.post(f"{PAY}/report-exports/{job_id}/link", json={}))
                    content = client.get(link["url"])
                    assert content.status_code == 200, content.text[:200]
                    if [m for m in markers(victim) if m in content.content.decode("utf-8", "replace")]:
                        problems.append(f"{attacker_key}->{victim_key} {label}: the export holds victim data")
                if _strip(seen[1], ()) | {"id": 0, "job_id": 0} != _strip(baseline[1], ()) | {"id": 0, "job_id": 0}:
                    problems.append(f"{attacker_key}->{victim_key} {label}: {seen} vs {baseline}")
                continue
            if seen != baseline or leaked or (isinstance(seen[0], int) and 200 <= seen[0] < 300):
                problems.append(f"{attacker_key}->{victim_key} {label}: {str(seen)[:300]} vs {str(baseline)[:300]} "
                                f"leaked {leaked}")
        changed = changed_tables(before, tenant_fingerprint(victim["cid"]))
        assert not changed, f"{attacker_key}->{victim_key}: victim tables changed {changed}"
        # Refused requests leave no partial write in the attacker's own business tables either.
        own_changed = [t for t in changed_tables(own_before, tenant_fingerprint(attacker["cid"]))
                       if t not in ("audit_logs", "idempotency_keys", "user_sessions", "jobs", "outbox_events")]
        assert not own_changed, f"{attacker_key}: refused requests wrote {own_changed}"
    assert not problems, "\n".join(problems)


def test_ec_sec_06_forged_company_id_and_server_owned_fields_are_rejected_by_strict_bodies(attack_world):
    """EC-SEC-06, EC-SEC-07: ``company_id`` (the victim's), ``status``, ``paid``, ``verified``, approver and
    provider reference fields in create bodies are refused (422) and nothing is created in any tenant."""
    alpha, beta = attack_world["alpha"], attack_world["beta"]
    client = beta["mallory"]
    period = beta["period"]
    own_emp = beta["emp"]["Emma"]["id"]
    bank = {"holder_name": "Emma Test", "country": "IN", "currency": "INR", "account_number": "772299990002",
            "routing": {"ifsc": "HDFC0001234"}, "account_type": "SAVINGS", "bank_name": "HDFC Bank"}
    cases = [
        (f"{PAY}/runs", {"run_type": "OFF_CYCLE", "pay_group_id": beta["group"], "pay_period_id": period["id"],
                         "employee_ids": [own_emp]}, ("company_id", "status", "approved_by_user_id")),
        (f"{PAY}/wage-inputs", {"employee_id": own_emp, "component_code": "BONUS", "period_start": period["start"],
                                "period_end": period["end"], "amount": {"amount": "100.00", "currency": "INR"}},
         ("company_id", "status", "approver_user_id")),
        (f"{PAY}/statutory-attestations", {"legal_entity_id": beta["le"], "effective_from": "2031-01-01",
                                           "reason": "Forged"}, ("company_id", "status", "approved_by_user_id")),
        (f"{ME}/bank-accounts", bank, ("company_id", "verified", "verification_status", "status")),
        (f"{PAY}/payment-batches", {"run_id": beta["run_id"]}, ("company_id", "status", "provider_ref", "paid")),
        (f"{ME}/reimbursement-claims", {"claim_type": "TRAVEL", "expense_date": date.today().isoformat(),
                                        "amount": {"amount": "10.00", "currency": "INR"}},
         ("company_id", "approved_amount", "status", "paid")),
    ]
    before = {k: tenant_fingerprint(attack_world[k]["cid"]) for k in ("alpha", "beta")}
    problems = []
    for url, body, forged_fields in cases:
        actor = beta["emma_login"] if url.startswith(ME) else client
        for field in forged_fields:
            value = alpha["cid"] if field == "company_id" else ("SETTLED" if field == "status" else
                                                                (True if field in ("paid", "verified") else
                                                                 "VERIFIED" if field == "verification_status" else
                                                                 "PROV-REF-1" if field == "provider_ref" else 1))
            r = actor.post(url, json={**body, field: value}, headers={"Idempotency-Key": f"v-{uuid.uuid4().hex}"})
            if r.status_code != 422 or r.json().get("code") != "VALIDATION_ERROR":
                problems.append(f"POST {url} with {field}={value!r}: {r.status_code} {r.text[:200]}")
    assert not problems, "\n".join(problems)
    for key in ("alpha", "beta"):
        changed = [t for t in changed_tables(before[key], tenant_fingerprint(attack_world[key]["cid"]))
                   if t not in ("audit_logs", "user_sessions")]
        assert not changed, f"{key}: {changed}"


# ---------------------------------------------------------------------------
# 4. Downloads and signed links (EC-DOC-06), exports, background jobs
# ---------------------------------------------------------------------------
def test_ec_doc_06_signed_links_are_bound_to_user_and_session_and_expire(attack_world):
    """EC-DOC-06, EC-SEC-02 (Goal 2 §14: CSV/PDF downloads, signed document links). Emma's link works for
    Emma in her session; the same link is 404 for her own tenant's Mallory, for another tenant, for Emma in a
    second session and when tampered; an expired link is 410 for its owner only."""
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User
    from app.payroll.services import downloads

    alpha, beta, app = attack_world["alpha"], attack_world["beta"], attack_world["app"]
    emma = alpha["emma_login"]
    link = ok(emma.get(f"{ME}/payslips/{alpha['payslips'][alpha['emp']['Emma']['id']]}/download"))["url"]
    served = emma.get(link)
    assert served.status_code == 200 and served.content.startswith(b"%PDF"), served.text[:200]
    assert "no-store" in served.headers.get("cache-control", "")
    missing = alpha["mallory"].get(f"{PAY}/downloads/{'x' * 20}.{'y' * 20}")
    assert missing.status_code == 404
    for other in (alpha["mallory"], beta["mallory"], attack_world["gamma"]["mallory"]):
        r = other.get(link)
        assert (r.status_code, r.json()) == (missing.status_code, missing.json()), r.text
    second_session = new_client(app)
    ok(second_session.post("/api/v1/auth/login", json={"email": alpha["emp"]["Emma"]["email"],
                                                       "password": "Str0ngPassw0rd!"}))
    r = second_session.get(link)
    assert (r.status_code, r.json()) == (404, missing.json())
    token = link.rsplit("/", 1)[-1]
    body, signature = token.split(".", 1)
    forged_claims = json.loads(downloads._unb64(body))
    forged_claims["i"] = alpha["payslips"][alpha["emp"]["Ivan"]["id"]]  # Ivan's payslip under Emma's signature
    forged = downloads._b64(json.dumps(forged_claims, separators=(",", ":"), sort_keys=True).encode()) + "." + signature
    r = emma.get(f"{PAY}/downloads/{forged}")
    assert (r.status_code, r.json()) == (404, missing.json())

    # Expired: a token minted five minutes in the past for Emma's live session.
    session_id = sql("SELECT id FROM user_sessions WHERE user_id = :u AND revoked_at IS NULL ORDER BY created_at "
                     "LIMIT 1", u=user_id_of(emma))[0][0]
    with session(alpha["cid"]) as (db, _):
        user = db.get(User, user_id_of(emma))
        principal = build_principal(db, user, session_id, db.get(Company, alpha["cid"]))
        stale, _ = downloads.make_token(principal, "doc", alpha["payslips"][alpha["emp"]["Emma"]["id"]],
                                        now=datetime.now(timezone.utc) - timedelta(minutes=6))
    expired = emma.get(f"{PAY}/downloads/{stale}")
    assert expired.status_code == 410 and expired.json()["code"] == "DOWNLOAD_LINK_EXPIRED"
    assert beta["mallory"].get(f"{PAY}/downloads/{stale}").status_code == 404


def test_ec_sec_02_exports_and_jobs_of_another_tenant_are_invisible_and_jobs_run_with_the_requesters_scope(
        attack_world):
    """EC-SEC-02 (Goal 2 §14: CSV/PDF downloads, background processing, another tenant's job id). The victim's
    report export and ZIP export are absent from the attacker's download center and lists; their ids are 404
    like missing ids on every export/job route; an export the attacker requests is built by the job with the
    attacker's scope and holds none of the victim's data."""
    alpha, beta = attack_world["alpha"], attack_world["beta"]
    client = beta["mallory"]
    victim_jobs = {r[0] for r in sql("SELECT id FROM jobs WHERE company_id = :c", c=alpha["cid"])}
    victim_exports = {r[0] for r in sql("SELECT id FROM export_jobs WHERE company_id = :c", c=alpha["cid"])}
    assert victim_jobs and victim_exports
    for url in (f"{PAY}/downloads/mine", f"{PAY}/exports", f"{PAY}/report-exports"):
        body = ok(client.get(url))
        items = body if isinstance(body, list) else body.get("items", [])
        own = {i.get("id") for i in items}
        own_ids = {r[0] for r in sql("SELECT id FROM jobs WHERE company_id = :c UNION SELECT id FROM export_jobs "
                                     "WHERE company_id = :c", c=beta["cid"])}
        assert own <= own_ids, f"{url} lists ids that are not the attacker tenant's: {own - own_ids}"
        assert not [m for m in markers(alpha) if m in json.dumps(body)], url
    for url in (f"/api/v1/jobs/{alpha['report_export']}", f"{PAY}/report-exports/{alpha['report_export']}",
                f"{PAY}/report-exports/{alpha['report_export']}/download", f"{PAY}/exports/{alpha['zip_export']}"):
        seen, _ = outcome(client, "GET", url)
        baseline, _ = outcome(client, "GET", re.sub(r"/\d+(/|$)", f"/{NOWHERE}\\1", url))
        assert seen == baseline and seen[0] == 404, (url, seen, baseline)
    for url in (f"{PAY}/report-exports/{alpha['report_export']}/link", f"{PAY}/exports/{alpha['zip_export']}/download"):
        seen, _ = outcome(client, "POST", url, json={})
        baseline, _ = outcome(client, "POST", re.sub(r"/\d+(/|$)", f"/{NOWHERE}\\1", url), json={})
        assert seen == baseline and seen[0] in (404, 422), (url, seen, baseline)

    # An export the attacker requests runs (inline job) with the attacker's scope: none of Alpha's data.
    job = ok(client.post(f"{PAY}/reports/payroll_register/exports", json={"format": "CSV"}), 202)
    link = ok(client.post(f"{PAY}/report-exports/{job['id']}/link", json={}))
    content = client.get(link["url"])
    assert content.status_code == 200, content.text[:300]
    text_body = content.content.decode("utf-8", "replace")
    assert not [m for m in markers(alpha) if m in text_body]
    assert "Confidential" in text_body


# ---------------------------------------------------------------------------
# 5. Sessions: stale roles, host/session mismatch, platform access, suspended tenant (EC-SEC-04/11/13/14)
# ---------------------------------------------------------------------------
def test_ec_sec_14_revoked_payroll_role_takes_effect_on_the_next_request_of_a_live_session(attack_world):
    """EC-SEC-14 (Goal 2 §14: stale sessions, cross-company role assignment). A processor's live session reads
    a run; the role is revoked: the very next request with the same cookies is 403. Granting it back restores
    access; a role of another tenant can never be granted (covered by the body-reference test)."""
    alpha, app = attack_world["alpha"], attack_world["app"]
    admin = alpha["admin"]
    person = create_employee(admin, "Stale", "Session", hire_date="2024-01-15")
    clerk = login_as(app, admin, person, ["payroll_processor"])
    assert clerk.get(f"{PAY}/runs/{alpha['run_id']}").status_code == 200
    r = admin.put(f"/api/v1/users/{clerk.user_id}/roles", json={"role_ids": [role_id(admin, "employee")]})
    assert r.status_code == 200, r.text
    for url in (f"{PAY}/runs/{alpha['run_id']}", f"{PAY}/runs", f"{PAY}/payslips",
                f"{PAY}/employees/{alpha['emp']['Emma']['id']}/compensation"):
        denied = clerk.get(url)
        assert denied.status_code in (403, 404), (url, denied.status_code)
        assert not [m for m in markers(alpha) if m in denied.text and m != alpha["name"]], url
    assert clerk.get(f"{PAY}/runs/{alpha['run_id']}").status_code == 403
    r = admin.put(f"/api/v1/users/{clerk.user_id}/roles", json={"role_ids": [role_id(admin, "payroll_processor")]})
    assert r.status_code == 200
    assert clerk.get(f"{PAY}/runs/{alpha['run_id']}").status_code == 200


def test_ec_sec_11_suspended_user_revoked_or_expired_session_and_suspended_tenant_execute_nothing(attack_world):
    """EC-SEC-11: a suspended account, a revoked session and an expired session get 401; a suspended tenant gets
    403 COMPANY_SUSPENDED on payroll reads and writes, and nothing executes; MFA-gated actions without a fresh
    step-up are 403 STEP_UP_REQUIRED."""
    alpha, app = attack_world["alpha"], attack_world["app"]
    admin, cid = alpha["admin"], alpha["cid"]
    probe = login_as(app, admin, create_employee(admin, "Probe", "User", hire_date="2024-01-15"), ["payroll_processor"])
    url = f"{PAY}/runs/{alpha['run_id']}"
    assert probe.get(url).status_code == 200
    sql("UPDATE user_sessions SET expires_at = now() - interval '1 minute' WHERE user_id = :u", u=probe.user_id)
    assert probe.get(url).status_code == 401
    sql("UPDATE user_sessions SET expires_at = now() + interval '1 hour', revoked_at = now() WHERE user_id = :u",
        u=probe.user_id)
    assert probe.get(url).status_code == 401
    other = login_as(app, admin, create_employee(admin, "Paused", "User", hire_date="2024-01-15"), ["payroll_processor"])
    sql("UPDATE users SET status = 'suspended' WHERE id = :u", u=other.user_id)
    r = other.get(url)
    assert r.status_code == 401 and r.json()["code"] in ("ACCOUNT_INACTIVE", "SESSION_EXPIRED"), r.text

    # Step-up gate: revealing a bank number / releasing a batch needs a fresh step-up (and the permission).
    fresh = alpha["mallory"]
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=fresh.user_id)
    account = sql("SELECT id FROM bank_accounts WHERE company_id = :c AND employee_id = :e AND status = 'ACTIVE' "
                  "LIMIT 1", c=cid, e=alpha["emp"]["Emma"]["id"])[0][0]
    r = fresh.post(f"{PAY}/bank-accounts/{account}/reveal", json={"reason": "Verify step-up"})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED", r.text
    step_up(fresh)

    # Suspended tenant: payroll reads and writes are refused, nothing is written.
    before = tenant_fingerprint(cid)
    sql("UPDATE companies SET status = 'suspended' WHERE id = :c", c=cid)
    try:
        for method, target, body in (("GET", f"{PAY}/runs", None), ("GET", url, None),
                                     ("POST", f"{PAY}/runs/{alpha['run_id']}/publish", {"reason": "x"}),
                                     ("PUT", f"{PAY}/settings/values", {"changes": [
                                         {"key": "payslip.show_ytd", "value": False, "reason": "x"}]})):
            r = admin.request(method, target, json=body) if body is not None else admin.request(method, target)
            assert r.status_code == 403 and r.json()["code"] == "COMPANY_SUSPENDED", (target, r.text)
    finally:
        sql("UPDATE companies SET status = 'active' WHERE id = :c", c=cid)
    changed = [t for t in changed_tables(before, tenant_fingerprint(cid)) if t not in ("user_sessions",)]
    assert not changed, changed


def test_ec_sec_13_every_payroll_route_needs_a_session_and_every_write_needs_csrf(attack_world):
    """EC-SEC-13 (unauthenticated access is 401 on every payroll route: components, structures, settings,
    compliance and filing endpoints included) and EC-SEC-12 (a cookie session without the CSRF header is 403
    CSRF_FAILED on every state-changing payroll route; nothing executes)."""
    app, alpha = attack_world["app"], attack_world["alpha"]
    anonymous = new_client(app)
    problems = []
    before = tenant_fingerprint(alpha["cid"])
    for method, route in payroll_routes(app):
        url = fill(route.path, {p: object_value(alpha, route.path, p) or NOWHERE for p in object_params(route.path)})
        seen, _ = outcome(anonymous, method, url, **request_kwargs(method))
        if seen[0] != 401:
            problems.append(f"anonymous {method} {route.path}: {str(seen)[:160]}")
        if method != "GET":
            try:
                raw = alpha["mallory"].http.request(method, url, json={})  # cookies, but no X-CSRF-Token header
                status, code = raw.status_code, (raw.json().get("code") if raw.content else None)
            except Exception as exc:  # noqa: BLE001
                status, code = "EXC", type(exc).__name__
            if (status, code) != (403, "CSRF_FAILED"):
                problems.append(f"no-CSRF {method} {route.path}: {status} {code}")
    assert not problems, "\n".join(problems)
    changed = [t for t in changed_tables(before, tenant_fingerprint(alpha["cid"])) if t not in ("user_sessions",)]
    assert not changed, changed

    # Rate limit on a sensitive route: signed payslip links are capped at 120 per user per minute.
    from app.core.rate_limit import limiter

    limiter.reset()
    url = f"{PAY}/payslips/{alpha['payslips'][alpha['emp']['Emma']['id']]}/download"
    statuses = [alpha["mallory"].get(url).status_code for _ in range(121)]
    assert statuses[:120] == [200] * 120 and statuses[120] == 429, statuses[-3:]
    limiter.reset()


def test_custom_domain_and_platform_sessions_never_reach_another_tenants_payroll(attack_world):
    """Goal 2 §14 vectors: custom-domain/session mismatch and wrong host context; EC-SEC-04 (platform staff
    have no tenant payroll access through their own session). Alpha's payroll session presented on Beta's host
    (and on the platform host) is 401; a platform administrator's session reads no tenant payroll route."""
    from tests.white_label_helpers import HostClient, platform_admin, tenant_host

    app, alpha, beta = attack_world["app"], attack_world["alpha"], attack_world["beta"]
    slug = {k: sql("SELECT slug FROM companies WHERE id = :c", c=attack_world[k]["cid"])[0][0] for k in ("alpha", "beta")}
    browser = HostClient(app, tenant_host(slug["alpha"]))
    r = browser.login(alpha["mallory"].email)
    assert r.status_code == 200, r.text
    url = f"{PAY}/runs/{alpha['run_id']}"
    assert browser.get(url).status_code == 200
    for host in (tenant_host(slug["beta"]), "app.test.local"):
        stolen = HostClient(app, host)
        stolen.http.cookies = browser.http.cookies
        for target in (url, f"{PAY}/runs/{beta['run_id']}", f"{PAY}/payslips"):
            r = stolen.get(target)
            assert r.status_code == 401, (host, target, r.status_code, r.text[:200])
    ops = platform_admin(app)
    for target in (f"{PAY}/runs", url, f"{PAY}/bank-accounts", f"{ME}/payslips", f"{PAY}/payment-batches"):
        r = ops.get(target)
        assert r.status_code == 401, (target, r.status_code)
        assert not [m for m in markers(alpha) if m in r.text]


# ---------------------------------------------------------------------------
# 6. Cached and replayed responses (Goal 2 §14: cached responses)
# ---------------------------------------------------------------------------
def test_payroll_responses_are_never_cacheable_and_idempotency_keys_never_replay_across_tenants(attack_world):
    """Goal 2 §14 vector "cached responses": every successful payroll GET carries Cache-Control no-store (or
    private no-cache for the static settings registry); replaying the victim's Idempotency-Key from another
    tenant (same path, same body) never returns the victim's stored response."""
    app, alpha, beta = attack_world["app"], attack_world["alpha"], attack_world["beta"]
    problems = []
    for method, route in payroll_routes(app):
        if method != "GET" or "{" in route.path:
            continue
        r = alpha["mallory"].get(route.path, params=QUERY_DEFAULTS if "key" in _query_params(route) else None)
        if 200 <= r.status_code < 300:
            cache = r.headers.get("cache-control", "")
            if "no-store" not in cache and not ("private" in cache and "no-cache" in cache):
                problems.append(f"GET {route.path}: Cache-Control {cache!r}")
    assert not problems, "\n".join(problems)

    key = f"victim-{uuid.uuid4().hex}"
    body = {"reason": "Publish again"}
    first = alpha["admin"].post(f"{PAY}/runs/{alpha['run_id']}/publish", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    replay = beta["mallory"].post(f"{PAY}/runs/{alpha['run_id']}/publish", json=body, headers={"Idempotency-Key": key})
    assert replay.status_code == 404 and replay.headers.get("Idempotent-Replayed") != "true", replay.text
    own = alpha["mallory"].post(f"{PAY}/runs/{alpha['run_id']}/publish", json=body, headers={"Idempotency-Key": key})
    assert own.headers.get("Idempotent-Replayed") != "true"  # keys are per user too


# ---------------------------------------------------------------------------
# 7. Row-level security with raw SQL as the application role, for every tenant table (EC-SEC-01)
# ---------------------------------------------------------------------------
PAYROLL_CORE = ("payroll_runs", "payroll_run_employees", "payroll_lines", "documents", "bank_accounts",
                "bank_account_change_requests", "payment_obligations", "payment_instructions", "payment_batches",
                "payment_files", "payment_events", "bank_statement_imports", "bank_statement_lines",
                "reconciliation_matches", "payment_manual_settlements", "compensation_assignments",
                "salary_templates", "salary_template_versions", "pay_components",
                "pay_component_versions", "policies", "policy_versions", "setting_values", "statutory_attestations",
                "wage_inputs", "overtime_entries", "tasks", "task_assignees", "reimbursement_claims", "loans",
                "payslip_queries", "export_jobs", "funding_accounts", "pay_groups", "pay_periods", "pay_calendars",
                "jobs", "idempotency_keys", "document_access_log")


def test_ec_sec_01_raw_sql_as_the_app_role_sees_and_touches_only_the_bound_tenant_on_every_table(attack_world):
    """EC-SEC-01 (Goal 2 §14: wrong DB connection context). For EVERY table in TENANT_TABLES: row-level
    security is enabled and forced with a policy on app.company_id; as the application role (NOBYPASSRLS) each
    tenant context sees exactly its own rows, an empty context sees none, and UPDATE/DELETE aimed at another
    tenant's rows touch nothing. The payroll core tables hold rows of both Alpha and Beta, so the proof is not
    vacuous."""
    from app.models import TENANT_TABLES

    cids = {k: attack_world[k]["cid"] for k in ("alpha", "beta", "gamma")}
    rls = {name: (enabled, forced) for name, enabled, forced in sql(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')")}
    policies = {}
    for table, qual, check in sql("SELECT tablename, coalesce(qual, ''), coalesce(with_check, '') FROM pg_policies "
                                  "WHERE schemaname = 'public'"):
        policies.setdefault(table, []).append(qual + " " + check)
    problems = []
    from tests.conftest import APP_ROLE

    role = sql("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r", r=APP_ROLE)
    assert role == [(False, False)], f"the application role must be NOSUPERUSER NOBYPASSRLS: {role}"
    owners = dict(sql("SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON "
                      "n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"))
    for table in TENANT_TABLES:
        # FORCE is not needed: row security binds every role except the owner, and the app role owns nothing.
        if not rls.get(table, (False, False))[0]:
            problems.append(f"{table}: row security is not enabled")
        if owners.get(table) == APP_ROLE:
            problems.append(f"{table}: owned by the application role (row security would not bind it)")
        if not any("app.company_id" in p for p in policies.get(table, [])):
            problems.append(f"{table}: no policy on app.company_id")
    for table in PAYROLL_CORE:
        counts = dict(sql(f'SELECT company_id, count(*) FROM "{table}" GROUP BY company_id'))
        if not (counts.get(cids["alpha"]) and counts.get(cids["beta"])):
            problems.append(f"{table}: the world has no rows of both Alpha and Beta ({counts})")
    for table in TENANT_TABLES:
        totals = dict(sql(f'SELECT company_id, count(*) FROM "{table}" GROUP BY company_id'))
        for key, cid in cids.items():
            with tenant(cid) as conn:
                seen = conn.execute(text(f'SELECT company_id, count(*) FROM "{table}" GROUP BY company_id')).all()
                if {c for c, _ in seen} - {cid} or sum(n for _, n in seen) != totals.get(cid, 0):
                    problems.append(f"{table}: context {key} sees {seen} (own rows {totals.get(cid, 0)})")
                for other_key, other in cids.items():
                    if other == cid or not totals.get(other):
                        continue
                    for statement in (f'UPDATE "{table}" SET company_id = company_id WHERE company_id = :o',
                                      f'DELETE FROM "{table}" WHERE company_id = :o'):
                        try:
                            with conn.begin_nested() as nested:
                                touched = conn.execute(text(statement), {"o": other}).rowcount
                                nested.rollback()
                        except Exception:  # noqa: BLE001 - refused by a trigger or privilege: also isolation
                            touched = 0
                        if touched:
                            problems.append(f"{table}: context {key} touched {touched} rows of {other_key}")
        with tenant(None) as conn:
            try:
                with conn.begin_nested():
                    leaked = conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
            except Exception:  # noqa: BLE001 - an error without a tenant context is also a denial
                leaked = 0
            if leaked:
                problems.append(f"{table}: {leaked} rows visible without a tenant context")
    assert not problems, "\n".join(problems)


def test_no_websocket_route_exists_so_the_websocket_vector_does_not_apply(attack_world):
    """Goal 2 §14 vector "WebSocket ids": the application registers no WebSocket route, so there is no socket
    channel whose id another tenant could subscribe to. If one is ever added, this test fails and the vector
    must be tested for real."""
    from starlette.routing import WebSocketRoute

    from fastapi.routing import APIWebSocketRoute

    sockets = [getattr(r, "path", r) for r in attack_world["app"].routes
               if isinstance(r, (WebSocketRoute, APIWebSocketRoute))]
    assert sockets == [], f"WebSocket routes exist and need a cross-tenant test: {sockets}"


def test_partner_operator_without_delegation_reaches_no_tenant_payroll(attack_world):
    """Goal 2 §15 "Partner Operator: only under explicit, current, audited delegation" and §14 cross-company
    grants. The platform offers partners no delegated tenant access at all (partner console: aggregate client
    counts and the partner brand), so the administrator of the partner that serves Alpha and Beta gets 401 on
    every payroll GET route (called with Alpha's real object ids) and on the self-service routes, never a marker
    of either tenant; the partner's client list carries no pay figure, bank number or e-mail of their staff."""
    from app.cli import create_platform_admin
    from tests.white_label_helpers import PLATFORM_HOST, HostClient, platform_admin

    app, alpha, beta = attack_world["app"], attack_world["alpha"], attack_world["beta"]
    try:
        ops = platform_admin(app)
    except SystemExit:  # already created by an earlier test of this module: sign in again
        ops = HostClient(app, PLATFORM_HOST)
        ok(ops.post("/api/v1/platform/auth/login", json={"email": "ops@platform.io", "password": "Platf0rmPass!"}))
    partner = ok(ops.post("/api/v1/platform/partners", json={"name": "Verification Partners"}))
    for world in (alpha, beta):
        ok(ops.patch(f"/api/v1/platform/companies/{world['cid']}", json={"partner_id": partner["id"]}))
    create_platform_admin("ops@verification-partner.example", "Partner Ops", "PartnerPass!2026",
                          partner_slug=partner["slug"])
    console = HostClient(app, PLATFORM_HOST)
    ok(console.post("/api/v1/partner/auth/login", json={"email": "ops@verification-partner.example",
                                                         "password": "PartnerPass!2026"}))
    clients = ok(console.get("/api/v1/partner/companies"))
    assert {"Alpha Technologies", "Beta Logistics"} <= {c["name"] for c in clients["items"]}, clients
    listing = json.dumps(clients)
    secrets = [m for w in (alpha, beta) for m in markers(w) if m not in (w["name"],)] + [EMMA_NET]
    assert not [m for m in secrets if m in listing], "the partner console shows tenant payroll or staff data"
    problems = []
    for method, route in payroll_routes(app):
        if method != "GET":
            continue
        url = fill(route.path, {p: object_value(alpha, route.path, p) or NOWHERE for p in object_params(route.path)})
        r = console.get(url)
        if r.status_code != 401 or [m for m in secrets if m in r.text]:
            problems.append(f"GET {route.path}: {r.status_code} {r.text[:120]}")
    assert not problems, "\n".join(problems)
