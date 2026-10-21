"""Permissions v2: registry codes, payroll confidentiality, PAY_GROUP, default roles, founder grant, RBAC fixes, 0010.

3.9 §5, contract §9/§25/§28.9, audit 01 D-1/D-2/D-3/D-9, EC-SEC-03, EC-SEC-09, EC-SEC-14, EC-MIG-07, EC-MIG-08.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core import permission_registry as reg
from app.core.permission_registry import Scope
from tests.helpers import create_employee, invite_and_activate, role_id, signup_company

ROOT = Path(__file__).resolve().parents[2]
_S, _DR, _RT, _D, _AG = "SELF", "DIRECT_REPORTS", "REPORTING_TREE", "DEPARTMENT", "ASSIGNED_GROUP"
_LE, _BR, _PG, _C = "LEGAL_ENTITY", "BRANCH", "PAY_GROUP", "COMPANY"

# 3.9 §5.1: code -> (scopes, sensitivity, excluded from implicit full access: "yes" | "except SELF" | "no").
# Approve codes of loans/reimbursements and task:approve omit SELF: nobody approves their own request.
SPEC_5_1 = {
    "settings:read": ({_C}, "standard", "no"),
    "settings:approve_changes": ({_C}, "confidential", "no"),
    "branding:manage": ({_C}, "standard", "no"),
    "email_template:manage": ({_C}, "standard", "no"),
    "domain:manage": ({_C}, "confidential", "no"),
    "org:read": ({_C, _LE, _BR}, "standard", "no"),
    "org:manage_entities": ({_C}, "standard", "no"),
    "org:manage_dimensions": ({_C}, "standard", "no"),
    "org:manage_branches": ({_C, _LE}, "standard", "no"),
    **{f"payroll:configuration:{a}": ({_C, _LE, _PG}, "confidential", "no") for a in ("read", "write")},
    **{f"payroll:salary_structure:{a}": ({_C, _LE}, "confidential", "no") for a in ("read", "edit", "approve")},
    "payroll:compensation:read": ({_S, _AG, _D, _PG, _LE, _C}, "restricted", "except SELF"),
    **{f"payroll:compensation:{a}": ({_PG, _LE, _C}, "restricted", "yes") for a in ("edit", "approve")},
    **{f"payroll:inputs:{a}": ({_PG, _LE, _C}, "restricted", "yes") for a in ("read", "edit", "import", "approve")},
    **{f"payroll:run:{a}": ({_PG, _LE, _C}, "restricted", "yes")
       for a in ("read", "create", "calculate", "review", "approve", "finalize", "reverse", "close")},
    "payroll:payslip:read": ({_S, _PG, _LE, _C}, "restricted", "except SELF"),
    "payroll:payslip:publish": ({_PG, _LE, _C}, "restricted", "yes"),
    **{f"payroll:payment:{a}": ({_LE, _C}, "restricted", "yes") for a in ("prepare", "approve", "submit", "reconcile")},
    "payroll:funding_accounts:manage": ({_LE, _C}, "restricted", "yes"),
    "payroll:bank_details:read_masked": ({_S, _PG, _LE, _C}, "restricted", "except SELF"),
    "payroll:bank_details:edit": ({_S, _LE, _C}, "restricted", "except SELF"),
    "payroll:bank_details:approve": ({_LE, _C}, "restricted", "yes"),
    "payroll:bank_details:read_full": ({_LE, _C}, "restricted", "yes"),
    **{f"payroll:{o}:{a}": ({_S, _PG, _LE, _C}, "restricted", "except SELF")
       for o in ("loans", "reimbursements") for a in ("read", "manage")},
    **{f"payroll:{o}:approve": ({_PG, _LE, _C}, "restricted", "yes") for o in ("loans", "reimbursements")},
    "payroll:tax_profile:read": ({_S, _LE, _C}, "restricted", "except SELF"),
    "payroll:tax_profile:verify": ({_LE, _C}, "restricted", "yes"),
    **{f"payroll:statutory:{a}": ({_LE, _C}, "restricted", "yes") for a in ("review", "submit")},
    **{f"payroll:reports:{a}": ({_PG, _LE, _C}, "restricted", "yes") for a in ("read", "export")},
    "payroll:audit:read": ({_C}, "confidential", "no"),
    "overtime:request": ({_S}, "standard", "no"),
    "overtime:approve": ({_DR, _RT, _D, _AG, _C}, "standard", "no"),
    "task:read": ({_S, _DR, _RT, _D, _AG, _C}, "standard", "no"),
    "task:manage": ({_S, _DR, _RT, _D, _AG, _C}, "standard", "no"),
    "task:approve": ({_DR, _RT, _D, _AG, _C}, "standard", "no"),
}
LEGACY = ("payroll:read", "payroll:prepare", "payroll:approve", "payroll:process", "payroll:export",
          "compensation:read", "compensation:manage")
EMPLOYEE_PAYROLL_SELF = {
    "payroll:payslip:read", "payroll:compensation:read", "payroll:bank_details:read_masked", "payroll:bank_details:edit",
    "payroll:tax_profile:read", "payroll:loans:read", "payroll:reimbursements:read",
}


def _migration():
    spec = importlib.util.spec_from_file_location("m0010", ROOT / "alembic" / "versions" / "0010_permissions_v2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


def _payroll_perms(permissions: dict) -> dict:
    return {c: s for c, s in permissions.items() if c.startswith("payroll:") or c.startswith("compensation:")}


def _audit(admin, **params):
    r = admin.get("/api/v1/audit-logs", params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _second_admin(app, founder):
    return invite_and_activate(app, founder, create_employee(founder, "Second"), ["company_admin"])


# ---------------------------------------------------------------------------
# Registry (pure)
# ---------------------------------------------------------------------------
def test_every_3_9_5_1_code_is_registered_with_its_scopes_sensitivity_and_full_access_rule():
    for code, (scopes, sensitivity, excluded) in SPEC_5_1.items():
        perm = reg.PERMISSIONS_BY_CODE.get(code)
        assert perm is not None, code
        assert {s.value for s in perm.scopes} == scopes, code
        assert perm.sensitivity == sensitivity, code
        implied = {s.value for s in perm.implied_scopes}
        expected = {"yes": set(), "except SELF": {_S} & scopes, "no": scopes - {_LE, _BR, _PG}}[excluded]
        assert implied == expected, code
        assert perm.payroll_confidential == (excluded != "no"), code
        assert not perm.deprecated, code


def test_ec_mig_07_legacy_codes_stay_registered_deprecated_and_never_implied_beyond_self():
    for code in LEGACY:
        perm = reg.PERMISSIONS_BY_CODE[code]
        assert perm.deprecated and perm.payroll_confidential
        assert {s.value for s in perm.implied_scopes} <= {_S}
        assert reg.is_valid_grant(code, _C)  # still grantable for one release
    assert set(reg.DEPRECATED_CODES) == set(LEGACY)
    assert set(reg.LEGACY_PAYROLL_MAPPING) == set(_migration().LEGACY_MAPPING)
    for legacy, v2 in reg.LEGACY_PAYROLL_MAPPING.items():
        assert reg.is_valid_grant(*legacy)
        assert all(reg.is_valid_grant(*g) for g in v2)
        assert set(v2) == set(_migration().LEGACY_MAPPING[legacy])


def test_ec_sec_03_full_access_excludes_payroll_confidential_codes():
    implied = reg.full_access_grants()
    assert reg.is_payroll_grant("payroll:run:approve", _C)
    for code, scope in implied:
        assert not reg.is_payroll_grant(code, scope)
        assert Scope(scope) not in reg.TARGETED_SCOPES
    codes = {c for c, _ in implied}
    assert {"payroll:run:read", "payroll:compensation:edit", "payroll:bank_details:read_full"}.isdisjoint(codes)
    assert ("payroll:payslip:read", _S) in implied and ("payroll:payslip:read", _C) not in implied
    assert ("payroll:read", _S) in implied and ("payroll:read", _C) not in implied
    assert ("compensation:read", _C) not in implied and ("compensation:manage", _C) not in implied
    assert {("payroll:audit:read", _C), ("payroll:configuration:write", _C), ("settings:read", _C)} <= implied


def test_pay_group_scope_is_targeted_and_offered_only_on_payroll_codes():
    assert Scope.PAY_GROUP in reg.TARGETED_SCOPES
    assert reg.SCOPE_LABELS[Scope.PAY_GROUP] == "Selected pay groups"
    assert reg.TARGET_ASSIGNMENT_COLUMNS == {
        Scope.LEGAL_ENTITY: "legal_entity_id", Scope.BRANCH: "branch_id", Scope.PAY_GROUP: "pay_group_id",
    }
    with_pay_group = {p.code for p in reg.PERMISSIONS if Scope.PAY_GROUP in p.scopes}
    assert with_pay_group and all(code.startswith("payroll:") for code in with_pay_group)
    for code in ("employee:read", "employee:read_confidential", "attendance:read", "location:read", "leave:read",
                 "asset:read", "report:read", "employee:update"):
        assert not reg.is_valid_grant(code, _PG), code
    assert reg.is_valid_grant("payroll:run:read", _PG)


def test_default_roles_are_valid_and_only_payroll_roles_hold_confidential_payroll_codes():
    keys = {r.system_key for r in reg.DEFAULT_ROLES}
    assert {"payroll_processor", "payroll_approver", "finance_payments", "payroll_manager"} <= keys
    for role in reg.DEFAULT_ROLES:
        for code, scope in role.grants:
            assert reg.is_valid_grant(code, scope.value), (role.system_key, code, scope)
        confidential = {(c, s.value) for c, s in role.grants if reg.is_payroll_grant(c, s.value)}
        if role.system_key in reg.PAYROLL_ROLE_KEYS:
            assert confidential, role.system_key
        elif role.system_key == "auditor":
            # 3.9 §5.3: the auditor reads payroll reports (redacted exports), nothing else of payroll.
            assert confidential == {("payroll:reports:read", _C)}
        else:
            assert not confidential, (role.system_key, confidential)
    assert reg.REGISTRY_VERSION == "2026.10.4"


def test_default_role_contents_follow_3_9_5_3():
    grants = {r.system_key: {(c, s.value) for c, s in r.grants} for r in reg.DEFAULT_ROLES}
    processor, manager = grants["payroll_processor"], grants["payroll_manager"]
    assert ("payroll:run:approve", _C) not in processor | manager  # approval is a separate role
    assert processor <= manager
    assert {("payroll:run:finalize", _C), ("payroll:run:reverse", _C), ("payroll:run:close", _C),
            ("payroll:configuration:write", _C), ("payroll:payslip:publish", _C), ("payroll:statutory:review", _C),
            ("payroll:statutory:submit", _C)} <= manager
    assert {(c, _C) for c in ("payroll:read", "payroll:prepare", "payroll:approve", "payroll:process",
                               "payroll:export", "compensation:read", "compensation:manage")} <= manager
    assert ("payroll:run:approve", _C) in grants["payroll_approver"]
    assert {("payroll:payment:prepare", _C), ("payroll:payment:submit", _C), ("payroll:funding_accounts:manage", _C)} \
        <= grants["finance_payments"]
    assert {("org:manage_entities", _C), ("org:manage_dimensions", _C), ("overtime:approve", _C),
            ("task:approve", _C)} <= grants["hr_admin"]
    assert {("overtime:approve", _RT), ("task:approve", _RT)} <= grants["manager"]
    employee = grants["employee"]
    assert {(c, _S) for c in EMPLOYEE_PAYROLL_SELF | {"overtime:request", "task:read", "payroll:read",
                                                       "compensation:read"}} <= employee
    assert all(s == _S for c, s in employee if c.startswith("payroll:") or c.startswith("compensation:"))
    assert {("payroll:audit:read", _C), ("payroll:reports:read", _C)} <= grants["auditor"]


def test_loses_last_payroll_approver_helper():
    approve, create = ("payroll:run:approve", _C), ("payroll:run:create", _C)
    before = {1: {approve}, 2: {create}}
    assert reg.loses_last_payroll_approver(before, {1: set(), 2: {create}})
    assert not reg.loses_last_payroll_approver(before, {1: {approve}, 2: {create}})
    assert not reg.loses_last_payroll_approver(before, {1: set(), 2: set()})  # nobody creates runs any more
    assert not reg.loses_last_payroll_approver({2: {create}}, {2: {create}})  # there was no approver to lose
    # An entity-scoped approver does not count as a company-wide one.
    assert reg.loses_last_payroll_approver({1: {approve, ("payroll:run:approve", _LE)}, 2: {create}},
                                           {1: {("payroll:run:approve", _LE)}, 2: {create}})


def _principal(grants: dict, admin: bool = False):
    from app.core.principal import Principal

    return Principal(1, 1, None, "x@y.io", "X", None, {c: frozenset(s) for c, s in grants.items()},
                     frozenset(), frozenset(), admin, 1)


def test_d3_can_delegate_requires_company_scope_for_targeted_and_assigned_group_scopes():
    from app.services.rbac_service import can_delegate

    entity_editor = _principal({"employee:read": {_LE}, "attendance:read": {_C}, "leave:read": {_AG}})
    assert not can_delegate(entity_editor, "employee:read", _LE)
    assert not can_delegate(entity_editor, "leave:read", _AG)
    assert can_delegate(entity_editor, "attendance:read", _LE)
    assert can_delegate(entity_editor, "attendance:read", _BR)
    assert not can_delegate(entity_editor, "employee:read", _PG)  # invalid grant
    manager = _principal({"leave:approve": {_RT}})
    assert can_delegate(manager, "leave:approve", _DR)
    # D-9: administrators can still edit roles holding stale codes; others cannot.
    assert can_delegate(_principal({}, admin=True), "removed:code", _C)
    assert not can_delegate(entity_editor, "removed:code", _C)


# ---------------------------------------------------------------------------
# Founder grant and administrators (EC-SEC-03, decision I-08)
# ---------------------------------------------------------------------------
def test_ec_sec_03_founder_holds_payroll_through_explicit_audited_role(app):
    founder = signup_company(app, "Founder Co")
    me = founder.me()
    assert me["is_company_admin"]
    assert "payroll_manager" in {r["system_key"] for r in me["roles"]}
    perms = me["permissions"]
    assert perms["payroll:run:create"] == [_C] and perms["payroll:read"] == [_S, _C]
    assert "payroll:run:approve" not in perms
    rows = [r for r in _audit(founder, module="Payroll Roles")["logs"] if r["action"] == "ROLE_GRANTED"]
    assert len(rows) == 1 and rows[0]["actor_type"] == "system"
    assert "founding administrator" in rows[0]["description"] and "I-08" in rows[0]["description"]
    assert founder.get("/api/v1/payroll").status_code == 200


def test_ec_sec_03_admin_without_payroll_role_has_no_payroll_data(app):
    founder = signup_company(app, "Confidential Co")
    payee = create_employee(founder, "Payee")
    founder.post("/api/v1/salary-structures", json={"employee_id": payee["id"], "basic_salary": "50000",
                                                    "effective_from": "2024-01-01"})
    assert founder.post(f"/api/v1/payroll/generate?employee_id={payee['id']}&year=2024&month=2").status_code == 200
    admin2 = _second_admin(app, founder)
    me = admin2.me()
    assert me["is_company_admin"] and me["roles"] == [{"id": role_id(founder, "company_admin"),
                                                      "name": "Company Administrator", "system_key": "company_admin"}]
    payroll = _payroll_perms(me["permissions"])
    assert all(scopes == [_S] for code, scopes in payroll.items() if code != "payroll:audit:read"
               and not code.startswith("payroll:configuration") and not code.startswith("payroll:salary_structure"))
    assert "payroll:run:read" not in payroll and "payroll:compensation:edit" not in payroll
    assert admin2.get("/api/v1/payroll").status_code == 403  # company list needs payroll:read at COMPANY
    assert admin2.post(f"/api/v1/payroll/generate?employee_id={payee['id']}&year=2024&month=3").status_code == 403
    assert founder.get(f"/api/v1/salary-structures?employee_id={payee['id']}").json() != []
    assert admin2.get(f"/api/v1/salary-structures?employee_id={payee['id']}").json() == []
    # The Roles page shows the administrator role without payroll codes.
    admin_role = next(r for r in admin2.get("/api/v1/roles").json() if r["system_key"] == "company_admin")
    assert not any(reg.is_payroll_grant(g["permission"], g["scope"]) for g in admin_role["grants"])


def test_ec_sec_03_removing_the_founders_payroll_role_removes_access_immediately(app):
    founder = signup_company(app, "Revoke Co")
    admin2 = _second_admin(app, founder)
    founder_id = founder.me()["user"]["id"]
    version = founder.me()["user"]["permission_version"]
    r = admin2.put(f"/api/v1/users/{founder_id}/roles", json={"role_ids": [role_id(founder, "company_admin")]})
    assert r.status_code == 200, r.text
    me = founder.me()  # same session, next request
    assert "payroll:run:create" not in me["permissions"] and me["user"]["permission_version"] > version
    assert founder.get("/api/v1/payroll").status_code == 403
    assert founder.post("/api/v1/payroll/generate?employee_id=1&year=2024&month=2").status_code == 403
    revoked = [r for r in _audit(admin2, module="Payroll Roles")["logs"] if r["action"] == "ROLE_REVOKED"]
    assert len(revoked) == 1


def test_ec_sec_03_admin_self_grant_is_allowed_and_audited_other_self_changes_refused(app):
    founder = signup_company(app, "Self Grant Co")
    admin2 = _second_admin(app, founder)
    me = admin2.me()
    own = f"/api/v1/users/{me['user']['id']}/roles"
    admin_role, approver = role_id(founder, "company_admin"), role_id(founder, "payroll_approver")
    assert admin2.put(own, json={"role_ids": [admin_role, role_id(founder, "hr_admin")]}).status_code == 403
    assert admin2.put(own, json={"role_ids": []}).status_code == 403
    assert admin2.put(own, json={"role_ids": [admin_role]}).status_code == 403  # no change at all
    r = admin2.put(own, json={"role_ids": [admin_role, approver]})
    assert r.status_code == 200, r.text
    assert admin2.me()["permissions"]["payroll:run:approve"] == [_C]
    logs = _audit(founder, module="Payroll Roles")["logs"]
    granted = [l for l in logs if l["action"] == "ROLE_SELF_GRANTED"]
    assert len(granted) == 1 and admin2.email in granted[0]["description"] and "EC-SEC-03" in granted[0]["description"]
    # Giving the payroll role up again is allowed and audited (once another approver exists); dropping
    # the admin role is not.
    assert admin2.put(own, json={"role_ids": [approver]}).status_code == 403
    r = admin2.put(own, json={"role_ids": [admin_role]})
    assert r.status_code == 409 and r.json()["code"] == "LAST_PAYROLL_APPROVER"
    invite_and_activate(app, founder, create_employee(founder, "Approver"), ["payroll_approver"])
    assert admin2.put(own, json={"role_ids": [admin_role]}).status_code == 200
    assert any(l["action"] == "ROLE_SELF_REVOKED" for l in _audit(founder, module="Payroll Roles")["logs"])
    assert "payroll:run:approve" not in admin2.me()["permissions"]


def test_ec_sec_03_non_admin_cannot_self_grant_payroll_roles(app):
    founder = signup_company(app, "No Self Co")
    pm = invite_and_activate(app, founder, create_employee(founder, "Pm"), ["payroll_manager"])
    hr = invite_and_activate(app, founder, create_employee(founder, "Hr"), ["hr_admin"])
    pm_roles = [role_id(founder, "payroll_manager"), role_id(founder, "payroll_approver")]
    assert pm.put(f"/api/v1/users/{pm.user_id}/roles", json={"role_ids": pm_roles}).status_code == 403
    hr_roles = [role_id(founder, "hr_admin"), role_id(founder, "payroll_processor")]
    assert hr.put(f"/api/v1/users/{hr.user_id}/roles", json={"role_ids": hr_roles}).status_code == 403
    # HR holds user:assign_role but no payroll codes, so it cannot hand payroll roles to anyone either.
    target = invite_and_activate(app, founder, create_employee(founder, "Tgt"), ["employee"])
    r = hr.put(f"/api/v1/users/{target.user_id}/roles", json={"role_ids": [role_id(founder, "payroll_processor")]})
    assert r.status_code == 403
    assert "payroll_processor" not in {r["system_key"] for r in target.me()["roles"]}


# ---------------------------------------------------------------------------
# Manager, employee, PAY_GROUP grants
# ---------------------------------------------------------------------------
def test_ec_sec_09_manager_has_no_payroll_and_employee_has_self_payroll_only(app):
    founder = signup_company(app, "Teams Co")
    boss = create_employee(founder, "Boss")
    report = create_employee(founder, "Report", manager_id=boss["id"])
    manager = invite_and_activate(app, founder, boss, ["manager"])
    employee = invite_and_activate(app, founder, report, ["employee"])
    m_perms = manager.me()["permissions"]
    assert all(scopes == [_S] for scopes in _payroll_perms(m_perms).values())
    assert m_perms["overtime:approve"] == [_RT] and m_perms["task:approve"] == [_RT]
    e_payroll = _payroll_perms(employee.me()["permissions"])
    assert set(e_payroll) == EMPLOYEE_PAYROLL_SELF | {"payroll:read", "compensation:read"}
    assert all(scopes == [_S] for scopes in e_payroll.values())
    founder.post("/api/v1/salary-structures", json={"employee_id": report["id"], "basic_salary": "40000",
                                                    "effective_from": "2024-01-01"})
    assert manager.get(f"/api/v1/salary-structures?employee_id={report['id']}").json() == []
    assert manager.get("/api/v1/payroll/summary/2024/2").status_code == 403


def test_pay_group_grants_accepted_on_payroll_codes_and_rejected_elsewhere(app):
    founder = signup_company(app, "Pay Group Co")
    ok = founder.post("/api/v1/roles", json={"name": "Group payroll", "grants": [
        {"permission": "payroll:run:read", "scope": "PAY_GROUP"},
        {"permission": "payroll:compensation:read", "scope": "PAY_GROUP"}]})
    assert ok.status_code == 200, ok.text
    assert {(g["permission"], g["scope"]) for g in ok.json()["grants"]} == {
        ("payroll:run:read", _PG), ("payroll:compensation:read", _PG)}
    for code in ("employee:read", "attendance:read", "leave:read", "location:read", "asset:read",
                 "payroll:payment:prepare"):
        r = founder.post("/api/v1/roles", json={"name": f"Bad {code}", "grants": [{"permission": code, "scope": "PAY_GROUP"}]})
        assert r.status_code == 400, code
    catalog = founder.get("/api/v1/permissions").json()
    assert {"value": "PAY_GROUP", "label": "Selected pay groups"} in catalog["scopes"]
    entries = {p["permission"]: p for m in catalog["modules"] for p in m["permissions"]}
    assert entries["payroll:run:read"]["full_access_scopes"] == [] and entries["payroll:run:read"]["payroll_confidential"]
    assert entries["payroll:payslip:read"]["full_access_scopes"] == ["SELF"]
    assert entries["payroll:read"]["deprecated"] and not entries["payroll:run:read"]["deprecated"]


# ---------------------------------------------------------------------------
# Delegation fixes D-2 and D-3
# ---------------------------------------------------------------------------
def test_d2_hr_admin_cannot_widen_assigned_group_of_a_payroll_clerk(app):
    founder = signup_company(app, "Clerk Co")
    other = signup_company(app, "Other Clerk Co")
    clerk_role = founder.post("/api/v1/roles", json={"name": "Payroll clerk", "grants": [
        {"permission": "payroll:compensation:read", "scope": "ASSIGNED_GROUP"}]}).json()["id"]
    clerk = invite_and_activate(app, founder, create_employee(founder, "Clerk"), [])
    assert founder.put(f"/api/v1/users/{clerk.user_id}/roles", json={"role_ids": [clerk_role]}).status_code == 200
    hr = invite_and_activate(app, founder, create_employee(founder, "Hr"), ["hr_admin"])
    everyone = [e["id"] for e in founder.get("/api/v1/employees?limit=200").json()["items"]]
    r = hr.put(f"/api/v1/users/{clerk.user_id}/assigned-employees", json={"employee_ids": everyone})
    assert r.status_code == 403 and "payroll:compensation:read" in r.text
    assert _sql("SELECT count(*) FROM scope_assignments WHERE user_id = :u", u=clerk.user_id)[0][0] == 0
    # The administrator may, and HR may for permissions it holds company-wide.
    assert founder.put(f"/api/v1/users/{clerk.user_id}/assigned-employees",
                       json={"employee_ids": everyone[:1]}).status_code == 200
    leave_role = founder.post("/api/v1/roles", json={"name": "Leave desk", "grants": [
        {"permission": "leave:read", "scope": "ASSIGNED_GROUP"}]}).json()["id"]
    desk = invite_and_activate(app, founder, create_employee(founder, "Desk"), [])
    assert hr.put(f"/api/v1/users/{desk.user_id}/roles", json={"role_ids": [leave_role]}).status_code == 200
    assert hr.put(f"/api/v1/users/{desk.user_id}/assigned-employees", json={"employee_ids": everyone}).status_code == 200
    # Employees of another tenant are unknown here.
    foreign = other.get("/api/v1/employees?limit=1").json()["items"][0]["id"]
    r = hr.put(f"/api/v1/users/{desk.user_id}/assigned-employees", json={"employee_ids": [foreign]})
    assert r.status_code == 404


def test_d2_assigned_group_list_needs_employees_in_the_callers_scope(app):
    founder = signup_company(app, "Scope Co")
    boss = create_employee(founder, "Lead")
    mine = create_employee(founder, "Mine", manager_id=boss["id"])
    stranger = create_employee(founder, "Stranger")
    assigner = founder.post("/api/v1/roles", json={"name": "Team assigner", "grants": [
        {"permission": "user:assign_role", "scope": "COMPANY"}, {"permission": "employee:read", "scope": "REPORTING_TREE"},
        {"permission": "attendance:read", "scope": "COMPANY"}]}).json()["id"]
    lead = invite_and_activate(app, founder, boss, [])
    founder.put(f"/api/v1/users/{lead.user_id}/roles", json={"role_ids": [assigner]})
    viewer_role = founder.post("/api/v1/roles", json={"name": "Viewer AG", "grants": [
        {"permission": "attendance:read", "scope": "ASSIGNED_GROUP"}]}).json()["id"]
    viewer = invite_and_activate(app, founder, create_employee(founder, "Viewer"), [])
    founder.put(f"/api/v1/users/{viewer.user_id}/roles", json={"role_ids": [viewer_role]})
    url = f"/api/v1/users/{viewer.user_id}/assigned-employees"
    assert lead.put(url, json={"employee_ids": [stranger["id"]]}).status_code == 404
    assert lead.put(url, json={"employee_ids": [mine["id"]]}).status_code == 200


def test_d3_entity_scoped_role_editor_cannot_add_entity_grants_to_another_role(app):
    founder = signup_company(app, "Editor Co")
    editor_role = founder.post("/api/v1/roles", json={"name": "Entity editor", "grants": [
        {"permission": "role:read", "scope": "COMPANY"}, {"permission": "role:update", "scope": "COMPANY"},
        {"permission": "employee:read", "scope": "LEGAL_ENTITY"}, {"permission": "attendance:read", "scope": "COMPANY"}]})
    target_role = founder.post("/api/v1/roles", json={"name": "Gulf staff", "grants": [
        {"permission": "attendance:read", "scope": "SELF"}]}).json()["id"]
    editor = invite_and_activate(app, founder, create_employee(founder, "Editor"), [])
    founder.put(f"/api/v1/users/{editor.user_id}/roles", json={"role_ids": [editor_role.json()["id"]]})
    keep = {"permission": "attendance:read", "scope": "SELF"}
    r = editor.patch(f"/api/v1/roles/{target_role}", json={"grants": [keep, {"permission": "employee:read", "scope": "LEGAL_ENTITY"}]})
    assert r.status_code == 403
    r = editor.patch(f"/api/v1/roles/{target_role}", json={"grants": [keep, {"permission": "attendance:read", "scope": "LEGAL_ENTITY"}]})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Payroll audit visibility
# ---------------------------------------------------------------------------
def test_payroll_audit_rows_hidden_without_payroll_audit_read(app):
    founder = signup_company(app, "Audit Co")
    other = signup_company(app, "Other Audit Co")
    hr = invite_and_activate(app, founder, create_employee(founder, "Hr"), ["hr_admin"])
    auditor = invite_and_activate(app, founder, create_employee(founder, "Aud"), ["auditor"])
    from app.payroll.audit import PAYROLL_AUDIT_MODULES

    for module in ("Payroll Runs", "Bank Accounts", "Payroll"):
        _sql("INSERT INTO audit_logs (company_id, actor_type, module, action, description, created_at, updated_at) "
             "VALUES (:c, 'system', :m, 'TEST', 'payroll secret', now(), now())", c=founder.company_id, m=module)
    _sql("INSERT INTO audit_logs (company_id, actor_type, module, action, description, created_at, updated_at) "
         "VALUES (:c, 'system', 'Payroll Runs', 'TEST', 'other tenant', now(), now())", c=other.company_id)
    hr_view = _audit(hr)
    assert not any(r["module"] in PAYROLL_AUDIT_MODULES for r in hr_view["logs"])
    assert hr_view["total"] == len(hr_view["logs"])
    assert _audit(hr, module="Payroll Runs") == {"total": 0, "logs": []}
    assert _audit(hr, module="Payroll Roles")["total"] == 0
    for reader in (auditor, founder):
        view = _audit(reader)
        modules = [r["module"] for r in view["logs"]]
        assert {"Payroll Runs", "Bank Accounts", "Payroll", "Payroll Roles"} <= set(modules)
        assert view["total"] == hr_view["total"] + 4
        assert _audit(reader, module="Payroll Runs")["total"] == 1  # never another tenant's rows


# ---------------------------------------------------------------------------
# Last payroll approver (contract §28.9)
# ---------------------------------------------------------------------------
def test_last_company_wide_payroll_approver_is_protected(app):
    founder = signup_company(app, "Approver Co")
    approver = invite_and_activate(app, founder, create_employee(founder, "Appr"), ["payroll_approver"])
    employee_role = role_id(founder, "employee")
    r = founder.put(f"/api/v1/users/{approver.user_id}/roles", json={"role_ids": [employee_role]})
    assert r.status_code == 409 and r.json()["code"] == "LAST_PAYROLL_APPROVER"
    # Editing the role itself is guarded the same way.
    approver_role = role_id(founder, "payroll_approver")
    grants = [g for g in next(x for x in founder.get("/api/v1/roles").json() if x["id"] == approver_role)["grants"]
              if g["permission"] != "payroll:run:approve"]
    r = founder.patch(f"/api/v1/roles/{approver_role}", json={"grants": grants})
    assert r.status_code == 409 and r.json()["code"] == "LAST_PAYROLL_APPROVER"
    # With a second approver the first may go.
    invite_and_activate(app, founder, create_employee(founder, "Appr2"), ["payroll_approver"])
    assert founder.put(f"/api/v1/users/{approver.user_id}/roles", json={"role_ids": [employee_role]}).status_code == 200

    from app.core.errors import AppError
    from app.database.db import SessionLocal, bind_tenant
    from app.services import rbac_service

    db = SessionLocal()
    try:
        bind_tenant(db, founder.company_id)
        last = _sql("SELECT a.user_id FROM user_role_assignments a WHERE a.role_id = :r", r=approver_role)[0][0]
        with pytest.raises(AppError) as exc:
            rbac_service.assert_not_last_payroll_approver(db, founder.company_id, last)
        assert exc.value.code == "LAST_PAYROLL_APPROVER"
    finally:
        db.close()


def test_payroll_approver_can_go_when_nobody_creates_runs(app):
    founder = signup_company(app, "No Runs Co")
    admin2 = _second_admin(app, founder)
    approver = invite_and_activate(app, founder, create_employee(founder, "Appr"), ["payroll_approver"])
    # The founder (only run creator) gives up payroll_manager; then the approver may lose their role.
    assert admin2.put(f"/api/v1/users/{founder.me()['user']['id']}/roles",
                      json={"role_ids": [role_id(founder, "company_admin")]}).status_code == 200
    assert founder.put(f"/api/v1/users/{approver.user_id}/roles", json={"role_ids": []}).status_code == 200


# ---------------------------------------------------------------------------
# Data migration 0010 (EC-MIG-07, EC-MIG-08)
# ---------------------------------------------------------------------------
def test_migration_literals_match_registry_2026_10_4():
    m = _migration()
    defaults = {r.system_key: {(c, s.value) for c, s in r.grants} for r in reg.DEFAULT_ROLES}
    for key, (name, description, grants) in m.NEW_ROLES.items():
        assert set(grants) == defaults[key], key
        assert (name, description) == (reg.DEFAULT_ROLES_BY_KEY[key].name, reg.DEFAULT_ROLES_BY_KEY[key].description)
    for key, additions in m.SYSTEM_ROLE_ADDITIONS.items():
        assert set(additions) <= defaults[key], key
    assert defaults["employee"] == set(m._LEGACY_SELF_SERVICE) | set(m.SYSTEM_ROLE_ADDITIONS["employee"])
    assert m.DESCRIPTIONS["company_admin"][1] == reg.DEFAULT_ROLES_BY_KEY["company_admin"].description
    assert m.legacy_equivalents({("payroll:approve", _C), ("employee:read", _C)}) == {("payroll:run:approve", _C)}


def _role_rows(company_id):
    rows = _sql("SELECT r.system_key, r.name, rp.permission_code, rp.scope FROM roles r "
                "LEFT JOIN role_permissions rp ON rp.role_id = r.id WHERE r.company_id = :c", c=company_id)
    result = {}
    for key, name, code, scope in rows:
        result.setdefault(key or name, set())
        if code:
            result[key or name].add((code, scope))
    return result


def _make_pre_0010(m, client, custom_grants):
    """Turn a freshly signed-up tenant back into what registry 2026.10.3 + migrations 0001-0009 left."""
    cid = client.company_id
    new_keys = "('payroll_processor', 'payroll_approver', 'finance_payments')"
    _sql("DELETE FROM user_role_assignments WHERE role_id IN (SELECT id FROM roles WHERE company_id = :c AND "
         f"system_key IN {new_keys})", c=cid)
    _sql(f"DELETE FROM roles WHERE company_id = :c AND system_key IN {new_keys}", c=cid)
    for key, additions in m.SYSTEM_ROLE_ADDITIONS.items():
        for code, scope in additions:
            _sql("DELETE FROM role_permissions WHERE permission_code = :p AND scope = :s AND role_id = "
                 "(SELECT id FROM roles WHERE company_id = :c AND system_key = :k)", p=code, s=scope, c=cid, k=key)
    _sql("DELETE FROM user_role_assignments WHERE company_id = :c AND role_id = "
         "(SELECT id FROM roles WHERE company_id = :c AND system_key = 'payroll_manager')", c=cid)
    for key, (old, new) in m.DESCRIPTIONS.items():
        _sql("UPDATE roles SET description = :old WHERE company_id = :c AND system_key = :k", old=old, c=cid, k=key)
    role = client.post("/api/v1/roles", json={"name": "Legacy payroll desk", "grants": [
        {"permission": c, "scope": s} for c, s in custom_grants]})
    assert role.status_code == 200, role.text
    return _role_rows(cid)


def test_ec_mig_08_migration_upgrade_is_complete_idempotent_and_reversible(app):
    m = _migration()
    acme = signup_company(app, "Migrated Co")
    beta = signup_company(app, "Untouched Co")
    admin2 = _second_admin(app, acme)
    hr = invite_and_activate(app, acme, create_employee(acme, "Hr"), ["hr_admin"])
    custom = [("payroll:read", _C), ("payroll:approve", _C), ("compensation:read", _S), ("employee:read", _C)]
    before = _make_pre_0010(m, acme, custom)
    beta_before = _role_rows(beta.company_id)
    assert "payroll_approver" not in before
    versions = dict(_sql("SELECT id, permission_version FROM users WHERE company_id = :c", c=acme.company_id))

    from app.database.db import system_engine

    with system_engine.begin() as conn:
        stats = m.apply(conn)
    assert stats[acme.company_id]["roles_created"] == 3 and stats[acme.company_id]["admins_granted"] == 2
    assert stats[beta.company_id] == {"roles_created": 0, "grants_added": 0, "admins_granted": 0}

    after = _role_rows(acme.company_id)
    defaults = {r.system_key: {(c, s.value) for c, s in r.grants} for r in reg.DEFAULT_ROLES}
    for key in ("hr_admin", "payroll_manager", "manager", "employee", "auditor", "payroll_processor",
                "payroll_approver", "finance_payments"):
        assert after[key] == defaults[key], key
    assert after["Legacy payroll desk"] == set(custom) | {
        ("payroll:run:read", _C), ("payroll:payslip:read", _C), ("payroll:reports:read", _C),
        ("payroll:run:approve", _C), ("payroll:compensation:read", _S)}
    assert _role_rows(beta.company_id) == beta_before
    holders = {u for (u,) in _sql("SELECT a.user_id FROM user_role_assignments a JOIN roles r ON r.id = a.role_id "
                                  "WHERE r.company_id = :c AND r.system_key = 'payroll_manager'", c=acme.company_id)}
    admin_ids = {acme.me()["user"]["id"], admin2.user_id}
    assert holders == admin_ids
    assert "payroll:run:create" in admin2.me()["permissions"] and "payroll:run:create" not in hr.me()["permissions"]
    new_versions = dict(_sql("SELECT id, permission_version FROM users WHERE company_id = :c", c=acme.company_id))
    assert all(new_versions[u] == versions[u] + 1 for u in versions)
    audit = _sql("SELECT actor_type, entity_id, description, details FROM audit_logs WHERE company_id = :c AND "
                 "module = 'Payroll Roles' AND action = 'ROLE_GRANTED' AND details->>'migration' = :m",
                 c=acme.company_id, m=m.MARKER)
    assert len(audit) == 2 and {int(r[1]) for r in audit} == admin_ids
    assert all(r[0] == "system" and "EC-MIG-08" in r[2] for r in audit)
    descriptions = dict(_sql("SELECT system_key, description FROM roles WHERE company_id = :c AND system_key IS NOT NULL",
                             c=acme.company_id))
    assert descriptions["company_admin"] == m.DESCRIPTIONS["company_admin"][1]

    # Idempotent: a second run changes nothing.
    with system_engine.begin() as conn:
        again = m.apply(conn)
    assert again[acme.company_id] == {"roles_created": 0, "grants_added": 0, "admins_granted": 0}
    assert _role_rows(acme.company_id) == after

    # Downgrade removes exactly what the upgrade added.
    with system_engine.begin() as conn:
        reverted = m.revert(conn)
    assert reverted[acme.company_id]["roles_deleted"] == 3 and reverted[acme.company_id]["assignments_removed"] == 2
    assert reverted[beta.company_id] == {"assignments_removed": 0, "grants_removed": 0, "roles_deleted": 0}
    assert _role_rows(acme.company_id) == before
    assert _sql("SELECT count(*) FROM user_role_assignments a JOIN roles r ON r.id = a.role_id "
                "WHERE r.company_id = :c AND r.system_key = 'payroll_manager'", c=acme.company_id)[0][0] == 0
    assert _role_rows(beta.company_id) == beta_before

    # Upgrade again after the rollback, then roll back again: only the new run is undone.
    with system_engine.begin() as conn:
        m.apply(conn)
    assert _role_rows(acme.company_id) == after
    with system_engine.begin() as conn:
        second = m.revert(conn)
    assert second[acme.company_id]["roles_deleted"] == 3
    assert _role_rows(acme.company_id) == before


def test_ec_mig_08_migration_recreates_a_deleted_payroll_manager_and_avoids_name_clashes(app):
    m = _migration()
    acme = signup_company(app, "Renamed Co")
    _make_pre_0010(m, acme, [("payroll:export", _C)])
    pm = role_id(acme, "payroll_manager")
    _sql("DELETE FROM roles WHERE id = :r", r=pm)
    taken = acme.post("/api/v1/roles", json={"name": "Payroll Approver", "grants": []})
    assert taken.status_code == 200
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        m.apply(conn)
    names = dict(_sql("SELECT system_key, name FROM roles WHERE company_id = :c AND system_key IS NOT NULL", c=acme.company_id))
    assert names["payroll_manager"] == "Payroll Manager"
    assert names["payroll_approver"] == "Payroll Approver (payroll_approver)"
    rows = _role_rows(acme.company_id)
    assert rows["payroll_manager"] == set(m.NEW_ROLES["payroll_manager"][2])
    assert rows["Legacy payroll desk"] == {("payroll:export", _C), ("payroll:reports:export", _C)}
    assert "payroll:run:create" in acme.me()["permissions"]
