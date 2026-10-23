"""Goal 2 §15 / §5: the payroll role and permission matrix, field masking, separation of duties and self-scope.

One tenant ("Alpha Technologies", built once for this module by ``build_tenant`` of
test_cross_tenant_attacks_payroll.py: a finalized run with payslips, payments, inputs, loan, claim, compensation)
gets one signed-in user per role of Goal 2 §5/§15:

====  =======================================================================================================
CA    Company Administrator WITHOUT an explicit payroll role (full access; payroll data needs a payroll role)
HR    HR Administrator (system role hr_admin)
PROC  Payroll Processor (system role, COMPANY)
APPR  Payroll Approver (system role, COMPANY)
FIN   Finance & Payments (system role, COMPANY)
DEPT  Department/second-level manager (system role manager; Emma is in the reporting tree)
LEAD  Team lead (system role manager; Emma is a direct report)
EMP   Employee (Emma herself, system role employee)
LEP   Entity-scoped processor: custom role, processor read/create codes at LEGAL_ENTITY, target LE01 (the run's)
LEX   The same role with target AE01 (another legal entity of the tenant, no payroll data)
PGP   Pay-group-scoped processor: same codes at PAY_GROUP, target PG-1 (the run's pay group)
PGX   The same role with target PG-2 (another pay group of LE01, no payroll data)
APX   Entity-scoped approver: run read/approve + compensation read at LEGAL_ENTITY, target AE01
CFG   Custom role with ONLY payroll:configuration:read
SET   Custom role with ONLY settings:read (company settings, not payroll configuration)
====  =======================================================================================================

The expected cell values are written out by hand from Goal 2 §15, contract §9 (decision I-08) and the registry
of 3.9 §5.1 — never computed from the role tables at runtime: ``403`` = FORBIDDEN (missing permission);
``404`` = the permission is held but the object is outside the scope; ``SEES``/``HIDE`` = 2xx and the target row
is / is not in the list; ``OK`` = 2xx; ``PASS`` = authorized (anything but 401, 403 FORBIDDEN, 404 and 5xx: the
request then fails on state or input, e.g. 409 or 422). The full matrix is written to
``logs/permmatrix_result.md`` as evidence (Goal 2 §24 D). ``NOT`` (self-service endpoints) = anything but
seeing another employee's record.

Verified design choices recorded by the matrix (not defects): the staff list ``GET /payroll/payslips`` is 403 for
self-scope holders (they use ``/me/payroll/payslips``); a single payroll document answers 404 to every caller who
may not see it, with or without the permission (``documents.get_visible``: staff scope first, then the caller's own
published document), so it never reveals existence; overtime and task lists always return the caller's OWN rows
(an empty list, not 403, for users without team or payroll permissions); a Payroll Approver lists masked bank
accounts (needed to approve changes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import create_employee
from tests.payroll.test_compensation import login_as
from tests.payroll.test_payment_obligations import step_up
from tests.payroll.test_wage_inputs import pay_group, sql
from tests.payroll.verification.test_cross_tenant_attacks_payroll import (
    ME,
    NOWHERE,
    PAY,
    build_tenant,
    custom_role,
    reset_database,
    user_id_of,
)

ROLES = ("CA", "HR", "PROC", "APPR", "FIN", "DEPT", "LEAD", "EMP", "LEP", "LEX", "PGP", "PGX", "APX", "CFG", "SET")
SCOPED_PROCESSOR = ("payroll:run:read", "payroll:run:create", "payroll:run:calculate", "payroll:run:review",
                    "payroll:inputs:read", "payroll:compensation:read", "payroll:payslip:read",
                    "payroll:bank_details:read_masked", "payroll:reports:read", "payroll:loans:read",
                    "payroll:reimbursements:read")
SCOPED_APPROVER = ("payroll:run:read", "payroll:run:approve", "payroll:compensation:read")
ANNUAL_CTC = "1200000"  # Emma's approved annual pay basis in the world (never visible without compensation:read)
# Cells whose expected value fails today because of an open product defect (none: the task-visibility defect of
# payroll:inputs:read holders was fixed in app/payroll/services/tasks.py on 23 Oct 2026, regression test below).
KNOWN_DEFECT_CELLS: set = set()
EVIDENCE = Path(__file__).resolve().parents[3] / "logs" / "permmatrix_result.md"


@pytest.fixture(autouse=True)
def clean_database():
    """Overrides tests/conftest.py for this module: the shared world is removed once, at module teardown."""
    from app.core.rate_limit import limiter

    limiter.reset()
    yield


def _targets(user_id, role, scope, target_ids):
    """Pay-group targets have no write API (org_structure TargetWrite); inserted like test_payroll_access.py."""
    assignment = sql("SELECT id, company_id FROM user_role_assignments WHERE user_id = :u AND role_id = :r",
                     u=user_id, r=role)[0]
    for target in target_ids:
        sql("INSERT INTO user_role_assignment_targets (company_id, user_role_assignment_id, scope, target_id, "
            "created_at, updated_at) VALUES (:c, :a, :s, :t, now(), now())",
            c=assignment[1], a=assignment[0], s=scope, t=target)


def _user(app, admin, first, role_keys=(), custom=None):
    client = login_as(app, admin, create_employee(admin, first, "Matrix", hire_date="2024-01-15"), list(role_keys))
    if custom is not None:
        r = admin.put(f"/api/v1/users/{client.user_id}/roles", json={"role_ids": [custom]})
        assert r.status_code == 200, r.text
    return client


@pytest.fixture(scope="module")
def matrix_world():
    from app.main import app

    try:
        w = build_tenant(app, "alpha")
        admin, cid, le = w["admin"], w["cid"], w["le"]
        r = admin.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Alpha Gulf FZ-LLC",
                                                       "country_code": "AE", "base_currency": "AED",
                                                       "timezone": "Asia/Dubai"})
        assert r.status_code == 200, r.text
        w["ae"] = r.json()["id"]
        w["pg2"] = pay_group(cid, le, "PG-2")

        # Reporting line: Dora (department manager) -> Tom (team lead) -> Emma.
        dora = login_as(app, admin, create_employee(admin, "Dora", "Head", hire_date="2024-01-15"), ["manager"])
        tom_employee = create_employee(admin, "Tom", "Lead", hire_date="2024-01-15", manager_id=dora.employee["id"])
        tom = login_as(app, admin, tom_employee, ["manager"])
        r = admin.put(f"/api/v1/org/employees/{w['emp']['Emma']['id']}/manager", json={"manager_id": tom_employee["id"]})
        assert r.status_code == 200, r.text

        processor_le = custom_role(admin, "Entity payroll processor", [(c, "LEGAL_ENTITY") for c in SCOPED_PROCESSOR])
        processor_pg = custom_role(admin, "Pay group payroll processor", [(c, "PAY_GROUP") for c in SCOPED_PROCESSOR])
        approver_le = custom_role(admin, "Entity payroll approver", [(c, "LEGAL_ENTITY") for c in SCOPED_APPROVER])
        config_only = custom_role(admin, "Payroll configuration viewer", [("payroll:configuration:read", "COMPANY")])
        settings_only = custom_role(admin, "Company settings viewer", [("settings:read", "COMPANY")])
        users = {
            "CA": _user(app, admin, "Carla", ["company_admin"]),
            "HR": _user(app, admin, "Hana", ["hr_admin"]),
            "PROC": w["pia"], "APPR": w["ava"], "FIN": w["pete"], "DEPT": dora, "LEAD": tom, "EMP": w["emma_login"],
            "LEP": _user(app, admin, "Leon", custom=processor_le),
            "LEX": _user(app, admin, "Lexi", custom=processor_le),
            "PGP": _user(app, admin, "Paige", custom=processor_pg),
            "PGX": _user(app, admin, "Pax", custom=processor_pg),
            "APX": _user(app, admin, "Apex", custom=approver_le),
            "CFG": _user(app, admin, "Cfg", custom=config_only),
            "SET": _user(app, admin, "Setty", custom=settings_only),
        }
        for key, role, entity in (("LEP", processor_le, le), ("LEX", processor_le, w["ae"]),
                                  ("APX", approver_le, w["ae"])):
            r = admin.put(f"/api/v1/users/{users[key].user_id}/scope-targets",
                          json={"assignments": [{"role_id": role, "legal_entity_ids": [entity]}]})
            assert r.status_code == 200, r.text
        _targets(users["PGP"].user_id, processor_pg, "PAY_GROUP", [w["group"]])
        _targets(users["PGX"].user_id, processor_pg, "PAY_GROUP", [w["pg2"]])
        w["users"] = users
        w["emma_account"] = sql("SELECT id FROM bank_accounts WHERE company_id = :c AND employee_id = :e AND "
                                "status = 'ACTIVE' ORDER BY id LIMIT 1", c=cid, e=w["emp"]["Emma"]["id"])[0][0]
        w["obligation"] = sql("SELECT id FROM payment_obligations WHERE run_id = :r AND employee_id = :e",
                              r=w["run_id"], e=w["emp"]["Emma"]["id"])[0][0]
        yield w
    finally:
        reset_database()


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------
#                       CA   HR   PROC APPR FIN  DEPT LEAD EMP  LEP  LEX  PGP  PGX  APX  CFG  SET
MATRIX = {
    "settings.values":   "OK   403  OK   403  403  403  403  403  403  403  403  403  403  OK   403",
    "policies.list":     "OK   403  OK   403  403  403  403  403  403  403  403  403  403  OK   403",
    "components.list":   "OK   403  OK   403  403  403  403  403  403  403  403  403  403  403  403",
    "structures.list":   "OK   403  OK   403  403  403  403  403  403  403  403  403  403  403  403",
    "compensation.emma": "404  404  OK   OK   403  404  404  OK   OK   404  OK   404  404  403  403",
    "inputs.list":       "403  403  SEES 403  403  403  403  403  SEES HIDE SEES HIDE 403  403  403",
    "overtime.list":     "SEES SEES SEES HIDE HIDE SEES SEES SEES SEES HIDE SEES HIDE HIDE HIDE HIDE",
    "tasks.list":        "SEES SEES SEES HIDE HIDE SEES SEES SEES SEES HIDE SEES HIDE HIDE HIDE HIDE",
    "claims.list":       "HIDE HIDE SEES 403  403  HIDE HIDE SEES SEES HIDE SEES HIDE 403  403  403",
    "loans.list":        "HIDE HIDE SEES 403  403  HIDE HIDE HIDE SEES HIDE SEES HIDE 403  403  403",
    "runs.list":         "403  403  SEES SEES SEES 403  403  403  SEES HIDE SEES HIDE HIDE 403  403",
    "runs.needs_action": "403  403  OK   OK   OK   403  403  403  OK   OK   OK   OK   OK   403  403",
    "runs.detail":       "403  403  OK   OK   OK   403  403  403  OK   404  OK   404  404  403  403",
    "runs.employees":    "403  403  OK   OK   OK   403  403  403  OK   404  OK   404  404  403  403",
    "runs.create":       "403  403  404  403  403  403  403  403  404  404  404  404  403  403  403",
    "runs.approve":      "403  403  403  PASS 403  403  403  403  403  403  403  403  404  403  403",
    "payslips.list":     "403  403  SEES SEES 403  403  403  403  SEES HIDE SEES HIDE 403  403  403",
    "me.payslips":       "NOT  NOT  NOT  NOT  NOT  NOT  NOT  SEES NOT  NOT  NOT  NOT  NOT  NOT  NOT",
    "payslips.emma":     "404  404  OK   OK   404  404  404  OK   OK   404  OK   404  404  404  404",
    "bank.list":         "HIDE HIDE SEES SEES SEES HIDE HIDE SEES SEES HIDE SEES HIDE 403  403  403",
    "bank.reveal":       "403  403  403  403  403  403  403  403  403  403  403  403  403  403  403",
    "payments.batches":  "403  403  403  403  SEES 403  403  403  403  403  403  403  403  403  403",
    "payments.oblig":    "403  403  403  403  SEES 403  403  403  403  403  403  403  403  403  403",
    "payments.release":  "403  403  403  403  PASS 403  403  403  403  403  403  403  403  403  403",
    "funding.options":   "403  403  403  403  OK   403  403  403  403  403  403  403  403  403  403",
    "proof.file":        "403  403  403  403  OK   403  403  403  403  403  403  403  403  403  403",
    "reports.preview":   "403  403  OK   OK   OK   403  403  403  OK   OK   OK   OK   403  403  403",
    "reports.export":    "403  403  OK   403  403  403  403  403  403  403  403  403  403  403  403",
    "compliance":        "403  403  OK   OK   OK   403  403  403  OK   OK   OK   OK   403  403  403",
    "audit.payments":    "SEES HIDE 403  403  403  403  403  403  403  403  403  403  403  403  403",
    "compensation.edit": "403  403  404  403  403  403  403  403  403  403  403  403  403  403  403",
    "settings.write":    "PASS 403  403  403  403  403  403  403  403  403  403  403  403  403  403",
    "downloads.mine":    "OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK",
    # Added 23 Oct 2026 (approvals inbox, run submit/finalize, payslip questions, staff loan-policy preview).
    # approvals.inbox: every signed-in user has an inbox (only requests awaiting them; no permission needed).
    "approvals.inbox":   "OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK   OK",
    # runs.submit needs payroll:run:review (processor roles); the run is FINALIZED, so holders fail on state.
    "runs.submit":       "403  403  PASS 403  403  403  403  403  PASS 404  PASS 404  403  403  403",
    # runs.finalize needs payroll:run:finalize, which only Payroll Manager holds: nobody in the matrix.
    "runs.finalize":     "403  403  403  403  403  403  403  403  403  403  403  403  403  403  403",
    # Staff payslip questions: payroll:payslip:read beyond SELF, rows of the caller's scope only.
    "questions.staff":   "403  403  SEES SEES 403  403  403  403  SEES HIDE SEES HIDE 403  403  403",
    # Staff loan-policy preview: payroll:loans:read covering Emma's entity/pay group; SELF holders (incl. Emma
    # herself) use /me/payroll/loan-policy, so the staff route is 404 for them.
    "loans.policy.emma": "404  404  OK   403  403  404  404  404  OK   404  OK   404  403  403  403",
}


def _items(body):
    if isinstance(body, list):
        return body
    for key in ("items", "logs", "batches", "accounts"):
        if isinstance(body, dict) and isinstance(body.get(key), list):
            return body[key]
    return []


def families(w):
    """family -> (method, url, kwargs, target id for SEES/HIDE or None)."""
    emma = w["emp"]["Emma"]["id"]
    doc = w["payslips"][emma]
    period = w["period"]
    run_version = sql("SELECT row_version FROM payroll_runs WHERE id = :r", r=w["run_id"])[0][0]
    return {
        "settings.values": ("GET", f"{PAY}/settings/values", {}, None),
        "policies.list": ("GET", f"{PAY}/policies", {}, None),
        "components.list": ("GET", f"{PAY}/components", {}, None),
        "structures.list": ("GET", f"{PAY}/salary-structures", {}, None),
        "compensation.emma": ("GET", f"{PAY}/employees/{emma}/compensation", {}, None),
        "inputs.list": ("GET", f"{PAY}/wage-inputs", {}, w["wage_input"]),
        "overtime.list": ("GET", "/api/v1/overtime/entries", {}, w["overtime"]),
        "tasks.list": ("GET", "/api/v1/tasks", {}, w["task"]),
        "claims.list": ("GET", f"{PAY}/reimbursement-claims", {}, w["claim"]),
        "loans.list": ("GET", f"{PAY}/loans", {}, w["loan"]),
        "runs.list": ("GET", f"{PAY}/runs", {}, w["run_id"]),
        "runs.needs_action": ("GET", f"{PAY}/runs", {"params": {"needs_my_action": "true"}}, None),
        "runs.detail": ("GET", f"{PAY}/runs/{w['run_id']}", {}, None),
        "runs.employees": ("GET", f"{PAY}/runs/{w['run_id']}/employees", {}, None),
        "runs.create": ("POST", f"{PAY}/runs", {"json": {"run_type": "REGULAR", "pay_group_id": int(NOWHERE),
                                                         "pay_period_id": int(NOWHERE)}}, None),
        "runs.approve": ("POST", f"{PAY}/runs/{w['run_id']}/approve", {"json": {"row_version": run_version}}, None),
        "payslips.list": ("GET", f"{PAY}/payslips", {}, doc),
        "me.payslips": ("GET", f"{ME}/payslips", {}, doc),
        "payslips.emma": ("GET", f"{PAY}/payslips/{doc}", {}, None),
        "bank.list": ("GET", f"{PAY}/bank-accounts", {}, w["emma_account"]),
        "bank.reveal": ("POST", f"{PAY}/bank-accounts/{w['emma_account']}/reveal",
                        {"json": {"reason": "Matrix check"}}, None),
        "payments.batches": ("GET", f"{PAY}/payment-batches", {}, w["batch"]),
        "payments.oblig": ("GET", f"{PAY}/payment-obligations", {}, w["obligation"]),
        "payments.release": ("POST", f"{PAY}/payment-batches/{w['batch']}/release", {"json": {}}, None),
        "funding.options": ("GET", f"{PAY}/funding-accounts/options", {}, None),
        "proof.file": ("GET", f"{PAY}/manual-settlements/{w['proof']}/file", {}, None),
        "reports.preview": ("GET", f"{PAY}/reports/payroll_register/preview", {}, None),
        "reports.export": ("POST", f"{PAY}/reports/payroll_register/exports", {"json": {"format": "CSV"}}, None),
        "compliance": ("GET", f"{PAY}/compliance", {}, None),
        "audit.payments": ("GET", "/api/v1/audit-logs", {"params": {"module": "Payments"}}, "nonempty"),
        "compensation.edit": ("POST", f"{PAY}/employees/{emma}/compensation", {"json": {
            "template_version_id": int(NOWHERE), "pay_basis_amount": {"amount": "1300000", "currency": "INR"},
            "effective_from": period["start"], "reason": "PROMOTION"}}, None),
        "settings.write": ("PUT", f"{PAY}/settings/values", {"json": {"changes": [
            {"key": "payslip.show_ytd", "scope_type": "TENANT", "value": True, "reason": "Matrix check"}]}}, None),
        "downloads.mine": ("GET", f"{PAY}/downloads/mine", {}, None),
        "approvals.inbox": ("GET", f"{PAY}/approvals", {}, None),
        "runs.submit": ("POST", f"{PAY}/runs/{w['run_id']}/submit", {"json": {"row_version": run_version}}, None),
        "runs.finalize": ("POST", f"{PAY}/runs/{w['run_id']}/finalize",
                          {"json": {"confirm_label": "Matrix check", "row_version": run_version}}, None),
        "questions.staff": ("GET", f"{PAY}/payslip-questions", {}, w["question"]),
        "loans.policy.emma": ("GET", f"{PAY}/employees/{emma}/loan-policy", {}, None),
    }


def _actual(response, target):
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if status == 403 and code == "FORBIDDEN":
        return "403"
    if status == 404:
        return "404"
    if 200 <= status < 300:
        if target == "nonempty":
            return "SEES" if (body or {}).get("total", 0) > 0 else "HIDE"
        if target is not None:
            return "SEES" if any(i.get("id") == target for i in _items(body)) else "HIDE"
        return "OK"
    if status in (401,) or status >= 500:
        return f"ERR{status}"
    return f"PASS({status} {code})"


def _matches(expected, actual):
    if expected == "NOT":  # self-service endpoint: anything but seeing someone else's (Emma's) record
        return actual != "SEES" and not actual.startswith("ERR")
    if expected == "PASS":
        return actual.startswith("PASS") or actual == "OK"
    return expected == actual


def test_ec_sec_10_role_permission_matrix_over_every_payroll_endpoint_family(matrix_world):
    """EC-SEC-10, EC-SEC-03, EC-SEC-05, EC-SEC-09 (Goal 2 §15 matrix): every role x every payroll endpoint family
    (settings, policies, components, structures, compensation, inputs, overtime, tasks, claims, loans, runs and
    their actions, approvals, payslips, bank accounts, payments, reports, compliance, exports, audit and today's
    endpoints) answers exactly the hand-written cell: 403 without the permission, 404 out of scope, the target
    row listed only within scope. No response of any role carries a full bank number, and no role without
    compensation access sees Emma's annual pay."""
    w = matrix_world
    users = w["users"]
    for client in users.values():
        user_id_of(client)
    numbers = [n for n in w["numbers"].values()]
    no_pay_roles = {"CA", "HR", "FIN", "DEPT", "LEAD", "LEX", "PGX", "APX", "CFG", "SET"}
    mismatches, leaks, table = [], [], []
    for family, (method, url, kwargs, target) in families(w).items():
        expected_row = MATRIX[family].split()
        assert len(expected_row) == len(ROLES), family
        actual_row = []
        for role_key, expected in zip(ROLES, expected_row):
            client = users[role_key]
            if family == "payments.release":
                step_up(client)
            response = client.request(method, url, **kwargs)
            actual = _actual(response, target)
            actual_row.append(actual)
            if (family, role_key) in KNOWN_DEFECT_CELLS:
                actual_row[-1] = f"{actual} (known defect)"
            elif not _matches(expected, actual):
                mismatches.append(f"{family:18} {role_key:4} expected {expected:5} got {actual}: "
                                  f"{response.text[:160]}")
            text_body = response.text
            leaked = [n for n in numbers if n in text_body]
            if role_key in no_pay_roles and ANNUAL_CTC in text_body.replace(",", ""):
                leaked.append(f"annual pay {ANNUAL_CTC}")
            if leaked:
                leaks.append(f"{family} {role_key}: {leaked}")
        table.append((family, expected_row, actual_row))
    _write_evidence(table)
    assert not mismatches, f"{len(mismatches)} matrix cells differ:\n" + "\n".join(mismatches)
    assert not leaks, "masked data visible:\n" + "\n".join(leaks)


def _write_evidence(table):
    lines = ["# Payroll role/permission matrix — verification result", "",
             "Generated by tests/payroll/verification/test_permission_matrix.py (expected / actual).", "",
             "| Family | " + " | ".join(ROLES) + " |", "|---|" + "---|" * len(ROLES)]
    for family, expected, actual in table:
        cells = [e if _matches(e, a) else f"**{e} ≠ {a}**" for e, a in zip(expected, actual)]
        lines.append(f"| {family} | " + " | ".join(cells) + " |")
    try:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Field-level masking and managers (EC-SEC-09, EC-BNK-11)
# ---------------------------------------------------------------------------
def test_ec_sec_09_managers_and_hr_never_see_pay_even_for_their_reports(matrix_world):
    """EC-SEC-09: the team lead (Emma's manager) and the department manager (two levels up) see Emma in the
    directory, her overtime and her tasks, but never a salary: base_salary is empty, task rates are absent,
    and compensation, payslips and runs are 404/403; HR Administrator likewise."""
    w = matrix_world
    emma = w["emp"]["Emma"]["id"]
    for key in ("LEAD", "DEPT", "HR"):
        client = w["users"][key]
        detail = client.get(f"/api/v1/employees/{emma}")
        assert detail.status_code == 200, (key, detail.text)
        assert detail.json().get("base_salary") in (None, {}), (key, detail.json().get("base_salary"))
        listing = client.get("/api/v1/employees", params={"limit": 200})
        assert listing.status_code == 200
        assert all(e.get("base_salary") in (None, {}) for e in listing.json()["items"]), key
        tasks = client.get("/api/v1/tasks")
        assert tasks.status_code == 200, (key, tasks.text)
        for task in _items(tasks.json()):
            assert task.get("rate") in (None, {}) and "amount" not in json.dumps(task.get("rate") or {}), (key, task)
        assert ANNUAL_CTC not in json.dumps([detail.json(), listing.json(), tasks.json()]).replace(",", "")
        assert client.get(f"{PAY}/employees/{emma}/compensation").status_code == 404
        assert client.get(f"{PAY}/payslips/{w['payslips'][emma]}").status_code == 404
        assert client.get(f"{PAY}/runs/{w['run_id']}/employees").status_code == 403


def test_ec_bnk_11_bank_numbers_are_masked_for_every_reader_and_ec_sec_17_unmasking_needs_permission_reason_step_up(
        matrix_world):
    """EC-BNK-11, EC-SEC-17: processors, finance and the employee herself read masked numbers only (the last
    digits); revealing needs payroll:bank_details:read_full (403 for every system role), a reason (422) and a
    fresh step-up (403 STEP_UP_REQUIRED); the reveal is audited with the reason and without the number."""
    w = matrix_world
    users, account = w["users"], w["emma_account"]
    number = w["numbers"]["Emma"]
    for key, url in (("PROC", f"{PAY}/bank-accounts/{account}"), ("FIN", f"{PAY}/bank-accounts/{account}"),
                     ("EMP", f"{ME}/bank-accounts/{account}"), ("FIN", f"{PAY}/payment-obligations/{w['obligation']}"),
                     ("EMP", f"{ME}/payments")):
        r = users[key].get(url)
        assert r.status_code == 200, (key, url, r.text[:200])
        assert number not in r.text and number[-4:] in r.text, (key, url)
    revealer = w["mallory"]  # holds payroll:bank_details:read_full through the all-payroll custom role
    r = revealer.post(f"{PAY}/bank-accounts/{account}/reveal", json={})
    assert r.status_code == 422, r.text
    from tests.payroll.test_wage_inputs import sql as raw_sql

    raw_sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=revealer.user_id)
    r = revealer.post(f"{PAY}/bank-accounts/{account}/reveal", json={"reason": "Payee called about a return"})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED", r.text
    step_up(revealer)
    r = revealer.post(f"{PAY}/bank-accounts/{account}/reveal", json={"reason": "Payee called about a return"})
    assert r.status_code == 200 and number in r.text, r.text
    assert "no-store" in r.headers.get("cache-control", "")
    audits = raw_sql("SELECT action, description, details::text FROM audit_logs WHERE company_id = :c AND "
                     "entity_type = 'bank_account' AND entity_id = :e AND action LIKE '%REVEAL%' ORDER BY id DESC",
                     c=w["cid"], e=str(account))
    assert audits, "the reveal is audited"
    assert "Payee called about a return" in audits[0][1] + audits[0][2]
    assert number not in audits[0][1] + audits[0][2]


# ---------------------------------------------------------------------------
# Separation of duties and self-grants (EC-RUN-05, EC-SEC-03, EC-SEC-10)
# ---------------------------------------------------------------------------
def test_ec_run_05_whoever_submitted_a_run_cannot_approve_it_even_with_the_approve_permission(matrix_world):
    """EC-RUN-05, EC-SEC-10 (separation of duties): a user holding run create/review AND approve submitted the
    run (recorded on the run), so approving or rejecting it is 403 SEPARATION_OF_DUTIES; the setting is ON and
    locked by the starter profile."""
    w = matrix_world
    admin = w["admin"]
    from app.main import app as fastapi_app

    both = custom_role(admin, "Prepare and approve", [(c, "COMPANY") for c in (
        "payroll:run:read", "payroll:run:create", "payroll:run:review", "payroll:run:approve")])
    sam = _user(fastapi_app, admin, "Sam", custom=both)
    run = sam.post(f"{PAY}/runs", json={"run_type": "OFF_CYCLE", "pay_group_id": w["group"],
                                        "pay_period_id": w["period"]["id"], "employee_ids": [w["emp"]["Ivan"]["id"]]})
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    # Setup only: move the run to APPROVAL_PENDING as submitted by Sam (submission itself is tested elsewhere).
    sql("UPDATE payroll_runs SET status = 'APPROVAL_PENDING', submitted_by_user_id = :u, submitted_at = now() "
        "WHERE id = :r", u=sam.user_id, r=run_id)
    version = sql("SELECT row_version FROM payroll_runs WHERE id = :r", r=run_id)[0][0]
    for action, body in (("approve", {"row_version": version}), ("reject", {"reason": "Self-reject", "row_version": version})):
        r = sam.post(f"{PAY}/runs/{run_id}/{action}", json=body)
        assert r.status_code == 403 and r.json()["code"] == "SEPARATION_OF_DUTIES", (action, r.text)
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run_id)[0][0] == "APPROVAL_PENDING"
    r = sam.post(f"{PAY}/runs/{run_id}/approve", json={"row_version": version})  # still refused, nothing changed
    assert r.status_code == 403


def test_ec_sec_03_company_admin_without_payroll_role_sees_no_payroll_and_a_self_grant_is_visible_and_audited(
        matrix_world):
    """EC-SEC-03 (decision I-08): an administrator without a payroll role reads no runs, payslips, compensation or
    bank details; granting herself the Payroll Manager role works only as an audited, visible assignment."""
    w = matrix_world
    from app.main import app as fastapi_app
    from tests.helpers import role_id

    admin = w["admin"]
    sam = _user(fastapi_app, admin, "Selma", ["company_admin"])
    assert sam.get(f"{PAY}/runs").status_code == 403
    assert sam.get(f"{PAY}/runs/{w['run_id']}").status_code == 403
    preview = sam.get(f"{PAY}/access-preview")
    assert preview.status_code == 200, preview.text
    held = {p["permission"] for p in preview.json()["permissions"] if p["held"]}
    assert "payroll:run:read" not in held and "payroll:compensation:read" in held  # own records only
    r = sam.put(f"/api/v1/users/{sam.user_id}/roles",
                json={"role_ids": [role_id(admin, "company_admin"), role_id(admin, "payroll_manager")]})
    assert r.status_code == 200, r.text
    assert sam.get(f"{PAY}/runs/{w['run_id']}").status_code == 200
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND module = 'Payroll Roles' "
                                 "AND entity_id = :e ORDER BY id", c=w["cid"], e=str(sam.user_id))]
    assert "ROLE_SELF_GRANTED" in actions, actions
    roles = {r["system_key"] for r in sam.get(f"{PAY}/access-preview").json()["roles"]}
    assert "payroll_manager" in roles
    r = admin.put(f"/api/v1/users/{sam.user_id}/roles", json={"role_ids": [role_id(admin, "company_admin")]})
    assert r.status_code == 200
    assert sam.get(f"{PAY}/runs/{w['run_id']}").status_code == 403


def test_ec_sec_10_hr_with_compensation_rights_cannot_move_money_and_finance_cannot_change_pay(matrix_world):
    """EC-SEC-10 role boundaries: an HR administrator granted compensation editing proposes salaries but cannot
    prepare, approve or release payments; finance releases payments but cannot propose or approve salaries,
    edit inputs or create runs."""
    w = matrix_world
    from app.main import app as fastapi_app

    admin = w["admin"]
    hr_comp = custom_role(admin, "HR with compensation", [(c, "COMPANY") for c in (
        "employee:read", "payroll:compensation:read", "payroll:compensation:edit")])
    hana = _user(fastapi_app, admin, "Hedda", custom=hr_comp)
    proposal = {"template_version_id": int(NOWHERE), "pay_basis_amount": {"amount": "1300000", "currency": "INR"},
                "effective_from": w["period"]["start"], "reason": "PROMOTION"}
    emma = w["emp"]["Emma"]["id"]
    assert hana.post(f"{PAY}/employees/{emma}/compensation", json=proposal).status_code in (400, 404, 409, 422)
    for method, url, body in (("POST", f"{PAY}/payment-batches", {"run_id": w["run_id"]}),
                              ("POST", f"{PAY}/payment-batches/{w['batch']}/approve", {}),
                              ("POST", f"{PAY}/payment-batches/{w['batch']}/release", {}),
                              ("GET", f"{PAY}/payment-obligations", None)):
        r = hana.request(method, url, json=body) if body is not None else hana.request(method, url)
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", (url, r.text)
    finance = w["users"]["FIN"]
    for method, url, body in (
            ("POST", f"{PAY}/employees/{emma}/compensation", proposal),
            ("POST", f"{PAY}/compensation/{w['assignment']}/approve", {}),
            ("POST", f"{PAY}/wage-inputs", {"employee_id": emma, "component_code": "BONUS",
                                            "period_start": w["period"]["start"], "period_end": w["period"]["end"],
                                            "amount": {"amount": "1.00", "currency": "INR"}}),
            ("POST", f"{PAY}/runs", {"run_type": "OFF_CYCLE", "pay_group_id": w["group"],
                                     "pay_period_id": w["period"]["id"], "employee_ids": [emma]}),
            ("PUT", f"{PAY}/settings/values", {"changes": [{"key": "payslip.show_ytd", "value": False,
                                                            "reason": "x"}]})):
        r = finance.request(method, url, json=body)
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", (url, r.text)


def test_payroll_inputs_readers_see_the_task_pay_inputs_of_their_scope(matrix_world):
    """Registry 3.9 §5.1 (payroll:inputs:read: "View wage inputs, overtime, task pay and arrears that feed
    payroll") and F-42. Regression for the defect found on 22 Oct 2026: GET /api/v1/tasks scoped rows only by
    task:read/manage/approve, so the Payroll Processor and entity/pay-group processors saw none of the task inputs
    feeding their runs (overtime already honoured payroll:inputs:read). Processors whose scope covers Emma see her
    approved task, with its detail; processors scoped elsewhere (LEX: other entity, PGX: other pay group) see
    neither the row nor the detail (404, like a missing task)."""
    w = matrix_world
    for key in ("PROC", "LEP", "PGP"):
        r = w["users"][key].get("/api/v1/tasks")
        assert r.status_code == 200, (key, r.text)
        assert w["task"] in {t["id"] for t in _items(r.json())}, key
        detail = w["users"][key].get(f"/api/v1/tasks/{w['task']}")
        assert detail.status_code == 200 and detail.json()["id"] == w["task"], (key, detail.text[:200])
    for key in ("LEX", "PGX"):
        r = w["users"][key].get("/api/v1/tasks")
        assert r.status_code == 200, (key, r.text)
        assert w["task"] not in {t["id"] for t in _items(r.json())}, key
        missing = w["users"][key].get(f"/api/v1/tasks/{NOWHERE}")
        detail = w["users"][key].get(f"/api/v1/tasks/{w['task']}")
        assert detail.status_code == 404 and missing.status_code == 404, (key, detail.text[:200])
        assert detail.json()["code"] == missing.json()["code"], key
