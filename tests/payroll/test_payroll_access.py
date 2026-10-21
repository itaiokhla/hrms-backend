"""Payroll data scopes and the access preview.

3.9 §5.4, 02 §15.2 (history follows the date), audit 01 §6.2 steps 3 and 5, §6.3, audit 06 G-17;
EC-ORG-14, EC-SEC-02, EC-SEC-03, EC-SEC-05.
"""

from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import and_, func, select, text

from app.core import authz
from app.core.errors import AppError
from app.models.employee import Employee
from app.models.org import EmployeeAssignment
from app.models.pay_org import PayGroup
from app.payroll.services import access
from tests.helpers import create_employee, invite_and_activate, new_client, role_id, signup_company

MARCH = (date(2024, 3, 1), date(2024, 3, 31))
MAY_JUNE = (date(2024, 5, 15), date(2024, 6, 15))
JULY = (date(2024, 7, 1), date(2024, 7, 31))
PAY_GRANTS = ("payroll:run:read", "payroll:compensation:read", "payroll:payslip:read")


def _sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


@contextmanager
def _session(company_id):
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        yield db
    finally:
        db.rollback()
        db.close()


def _principal(db, company_id, user_id):
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User

    return build_principal(db, db.get(User, user_id), None, db.get(Company, company_id))


def _pay_group(company_id, entity_id, code, currency="INR", frequency="MONTHLY"):
    return _sql(
        "INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency, calendar_rules, "
        "status, created_at, updated_at) VALUES (:c, :le, :code, :name, :cur, :f, '{}'::jsonb, 'ACTIVE', now(), now()) "
        "RETURNING id", c=company_id, le=entity_id, code=code, name=f"Group {code}", cur=currency, f=frequency,
    )[0][0]


def _set_pay_group(employee_id, entity_id, pay_group_id):
    _sql("UPDATE employee_assignments SET pay_group_id = :pg WHERE employee_id = :e AND legal_entity_id = :le",
         pg=pay_group_id, e=employee_id, le=entity_id)


def _role(admin, name, scope, codes=PAY_GRANTS):
    r = admin.post("/api/v1/roles", json={"name": name, "grants": [{"permission": c, "scope": scope} for c in codes]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _user_with_role(app, admin, first, role):
    client = invite_and_activate(app, admin, create_employee(admin, first), [])
    r = admin.put(f"/api/v1/users/{client.user_id}/roles", json={"role_ids": [role]})
    assert r.status_code == 200, r.text
    return client


def _pay_group_target(user_id, role, pay_group_ids):
    assignment = _sql("SELECT id, company_id FROM user_role_assignments WHERE user_id = :u AND role_id = :r",
                      u=user_id, r=role)[0]
    for target in pay_group_ids:
        _sql("INSERT INTO user_role_assignment_targets (company_id, user_role_assignment_id, scope, target_id, "
             "created_at, updated_at) VALUES (:c, :a, 'PAY_GROUP', :t, now(), now())",
             c=assignment[1], a=assignment[0], t=target)


@pytest.fixture
def world(app):
    """Acme: LE01 (pay groups IN-M, IN-W) and AE01 (AE-M); a mover LE01 -> AE01 on 2024-06-01; Other Co."""
    acme = signup_company(app, "Acme Corp")
    other = signup_company(app, "Other Co")
    cid = acme.company_id
    le01 = next(e for e in acme.get("/api/v1/legal-entities").json() if e["code"] == "LE01")
    r = acme.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Acme Gulf FZ-LLC",
                                                   "country_code": "AE", "base_currency": "AED", "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    ae = r.json()
    r = acme.post("/api/v1/branches", json={"legal_entity_id": ae["id"], "code": "DXB", "name": "Dubai"})
    assert r.status_code == 200, r.text
    dubai = r.json()
    pg = {
        "IN-M": _pay_group(cid, le01["id"], "TPG-INM"),
        "IN-W": _pay_group(cid, le01["id"], "TPG-INW", frequency="WEEKLY"),
        "AE-M": _pay_group(cid, ae["id"], "TPG-AEM", currency="AED"),
    }
    other_le = other.get("/api/v1/legal-entities").json()[0]
    other_pg = _pay_group(other.company_id, other_le["id"], "TPG-OTH")

    emp = {
        "indian": create_employee(acme, "Indian", hire_date="2024-01-15"),
        "weekly": create_employee(acme, "Weekly", hire_date="2024-01-15"),
        "mover": create_employee(acme, "Mover", hire_date="2024-01-15"),
        "gulf": create_employee(acme, "Gulf", hire_date="2024-01-15"),
    }
    r = acme.post(f"/api/v1/employees/{emp['mover']['id']}/assignments",
                  json={"branch_id": dubai["id"], "effective_from": "2024-06-01", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    r = acme.post(f"/api/v1/employees/{emp['gulf']['id']}/assignments",
                  json={"branch_id": dubai["id"], "effective_from": "2024-01-16", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    _set_pay_group(emp["indian"]["id"], le01["id"], pg["IN-M"])
    _set_pay_group(emp["weekly"]["id"], le01["id"], pg["IN-W"])
    _set_pay_group(emp["mover"]["id"], le01["id"], pg["IN-M"])
    _set_pay_group(emp["mover"]["id"], ae["id"], pg["AE-M"])
    _set_pay_group(emp["gulf"]["id"], ae["id"], pg["AE-M"])

    entity_role = _role(acme, "Entity payroll", "LEGAL_ENTITY")
    group_role = _role(acme, "Pay group payroll", "PAY_GROUP")
    users = {
        "in_clerk": _user_with_role(app, acme, "InClerk", entity_role),
        "ae_clerk": _user_with_role(app, acme, "AeClerk", entity_role),
        "wk_clerk": _user_with_role(app, acme, "WkClerk", group_role),
        "untargeted": _user_with_role(app, acme, "Untargeted", entity_role),
        "indian": invite_and_activate(app, acme, emp["indian"], ["employee"]),
    }
    for key, entity in (("in_clerk", le01), ("ae_clerk", ae)):
        r = acme.put(f"/api/v1/users/{users[key].user_id}/scope-targets",
                     json={"assignments": [{"role_id": entity_role, "legal_entity_ids": [entity["id"]]}]})
        assert r.status_code == 200, r.text
    # Pay-group targets have no write API yet (org_structure TargetWrite), so they are inserted directly.
    _pay_group_target(users["wk_clerk"].user_id, group_role, [pg["IN-W"]])
    founder_id = acme.me()["user"]["id"]
    return {"acme": acme, "other": other, "cid": cid, "le01": le01, "ae": ae, "dubai": dubai, "pg": pg,
            "other_pg": other_pg, "emp": emp, "users": users, "founder_id": founder_id,
            "roles": {"entity": entity_role, "group": group_role}}


def _ids(db, scope):
    return set(db.execute(select(Employee.id).where(scope.clause(Employee.id))).scalars())


def _count(db, scope):
    return db.execute(select(func.count(Employee.id)).where(scope.clause(Employee.id))).scalar_one()


# ---------------------------------------------------------------------------
# authz: PAY_GROUP in the targeted-scope loop, period-aware variant
# ---------------------------------------------------------------------------
def test_pay_group_scope_reaches_only_employees_placed_in_the_targeted_pay_groups(world):
    emp, users = world["emp"], world["users"]
    with _session(world["cid"]) as db:
        clerk = _principal(db, world["cid"], users["wk_clerk"].user_id)
        assert clerk.scope_targets("payroll:compensation:read", "PAY_GROUP") == {world["pg"]["IN-W"]}
        today = authz.employee_scope(db, clerk, "payroll:compensation:read")
        assert _ids(db, today) == {emp["weekly"]["id"]}
        assert _count(db, today) == 1
        march = authz.employee_scope_during(db, clerk, "payroll:compensation:read", *MARCH)
        assert _ids(db, march) == {emp["weekly"]["id"]}


def test_targeted_scope_without_targets_grants_nothing(world):
    with _session(world["cid"]) as db:
        clerk = _principal(db, world["cid"], world["users"]["untargeted"].user_id)
        for period in (None, MARCH):
            scope = authz.employee_scope(db, clerk, "payroll:compensation:read", period=period)
            assert scope.is_empty and _ids(db, scope) == set() and _count(db, scope) == 0


def test_existing_callers_keep_todays_placement(world):
    """Without a period the scope is today's placement exactly as before (EC-ORG-12)."""
    emp = world["emp"]
    with _session(world["cid"]) as db:
        in_clerk = _principal(db, world["cid"], world["users"]["in_clerk"].user_id)
        ids = _ids(db, authz.employee_scope(db, in_clerk, "payroll:compensation:read"))
        assert emp["indian"]["id"] in ids and emp["weekly"]["id"] in ids
        assert emp["mover"]["id"] not in ids and emp["gulf"]["id"] not in ids


def test_ec_org_14_history_follows_the_date_after_a_transfer(world):
    emp = world["emp"]
    mover = emp["mover"]["id"]
    with _session(world["cid"]) as db:
        in_clerk = _principal(db, world["cid"], world["users"]["in_clerk"].user_id)
        ae_clerk = _principal(db, world["cid"], world["users"]["ae_clerk"].user_id)

        def during(principal, period):
            return _ids(db, access.payroll_employee_scope(db, principal, "payroll:payslip:read", *period))

        assert mover in during(in_clerk, MARCH) and mover not in during(ae_clerk, MARCH)
        assert mover not in during(in_clerk, JULY) and mover in during(ae_clerk, JULY)
        assert mover in during(in_clerk, MAY_JUNE) and mover in during(ae_clerk, MAY_JUNE)  # transfer period
        # The Gulf employee was in LE01 only on 15 January 2024.
        assert emp["gulf"]["id"] in during(in_clerk, (date(2024, 1, 1), date(2024, 1, 31)))
        assert emp["gulf"]["id"] not in during(in_clerk, (date(2024, 2, 1), date(2024, 2, 29)))


def test_period_must_be_ordered_dates(world):
    with _session(world["cid"]) as db:
        clerk = _principal(db, world["cid"], world["users"]["in_clerk"].user_id)
        with pytest.raises(ValueError):
            authz.employee_scope_during(db, clerk, "payroll:compensation:read", date(2024, 2, 1), date(2024, 1, 1))


def test_department_scope_with_a_period_uses_department_placements(app, world):
    acme, cid = world["acme"], world["cid"]
    head = create_employee(acme, "Head", hire_date="2024-01-01")
    ops = acme.post("/api/v1/departments", json={"name": "Ops", "head_employee_id": head["id"]})
    sales = acme.post("/api/v1/departments", json={"name": "Sales"})
    assert ops.status_code == 200 and sales.status_code == 200, (ops.text, sales.text)
    worker = create_employee(acme, "Worker", hire_date="2024-01-15", department_id=ops.json()["id"])
    main = acme.get(f"/api/v1/branches?legal_entity_id={world['le01']['id']}").json()[0]
    r = acme.post(f"/api/v1/employees/{worker['id']}/assignments", json={
        "branch_id": main["id"], "department_id": sales.json()["id"], "effective_from": "2024-06-01", "reason": "REORG"})
    assert r.status_code == 200, r.text
    role = _role(acme, "Dept comp", "DEPARTMENT", ("payroll:compensation:read",))
    reader = invite_and_activate(app, acme, head, [])
    assert acme.put(f"/api/v1/users/{reader.user_id}/roles", json={"role_ids": [role]}).status_code == 200
    with _session(cid) as db:
        principal = _principal(db, cid, reader.user_id)
        assert worker["id"] not in _ids(db, authz.employee_scope(db, principal, "payroll:compensation:read"))
        assert worker["id"] in _ids(db, authz.employee_scope_during(db, principal, "payroll:compensation:read", *MARCH))
        assert worker["id"] not in _ids(db, authz.employee_scope_during(db, principal, "payroll:compensation:read", *JULY))


# ---------------------------------------------------------------------------
# Run-level scope (runs, batches and other non-employee objects); pay groups stand in for runs
# ---------------------------------------------------------------------------
def _visible_groups(db, principal, permission="payroll:run:read", with_pay_group=True):
    clause = access.run_scope_clause(principal, permission, PayGroup.legal_entity_id,
                                     PayGroup.id if with_pay_group else None)
    ids = set(db.execute(select(PayGroup.id).where(PayGroup.code.like("TPG-%"), clause)).scalars())
    count = db.execute(select(func.count(PayGroup.id)).where(PayGroup.code.like("TPG-%"), clause)).scalar_one()
    entities = db.execute(
        select(func.count(func.distinct(PayGroup.legal_entity_id))).where(PayGroup.code.like("TPG-%"), clause)
    ).scalar_one()
    assert count == len(ids)  # EC-SEC-05: counts and aggregates see exactly the listed set
    return ids, entities


def test_run_scope_clause_follows_company_entity_and_pay_group_targets(world):
    pg, cid, users = world["pg"], world["cid"], world["users"]
    with _session(cid) as db:
        founder = _principal(db, cid, world["founder_id"])
        assert _visible_groups(db, founder) == ({pg["IN-M"], pg["IN-W"], pg["AE-M"]}, 2)
        in_clerk = _principal(db, cid, users["in_clerk"].user_id)
        assert _visible_groups(db, in_clerk) == ({pg["IN-M"], pg["IN-W"]}, 1)
        ae_clerk = _principal(db, cid, users["ae_clerk"].user_id)
        assert _visible_groups(db, ae_clerk) == ({pg["AE-M"]}, 1)
        wk_clerk = _principal(db, cid, users["wk_clerk"].user_id)
        assert _visible_groups(db, wk_clerk) == ({pg["IN-W"]}, 1)
        # Entity-owned objects (payment batches, funding accounts) have no pay group: PAY_GROUP reaches nothing.
        assert _visible_groups(db, wk_clerk, with_pay_group=False) == (set(), 0)
        assert _visible_groups(db, in_clerk, with_pay_group=False) == ({pg["IN-M"], pg["IN-W"]}, 1)


def test_ec_sec_05_run_scope_denies_without_targets_self_only_or_no_permission(world):
    cid, users = world["cid"], world["users"]
    with _session(cid) as db:
        untargeted = _principal(db, cid, users["untargeted"].user_id)
        assert _visible_groups(db, untargeted) == (set(), 0)
        employee = _principal(db, cid, users["indian"].user_id)
        assert employee.scopes("payroll:payslip:read") == {"SELF"}
        assert _visible_groups(db, employee, "payroll:payslip:read") == (set(), 0)  # SELF never covers a run
        assert _visible_groups(db, employee, "payroll:run:read") == (set(), 0)
        scope = access.target_scope(employee, "payroll:payslip:read")
        assert not scope.all_company and scope.employee_select is not None and not scope.is_empty
        assert access.target_scope(employee, "payroll:run:read").is_empty


def test_target_scope_lists_targets(world):
    cid, users, pg = world["cid"], world["users"], world["pg"]
    with _session(cid) as db:
        assert access.target_scope(_principal(db, cid, world["founder_id"]), "payroll:run:read").all_company
        in_scope = access.target_scope(_principal(db, cid, users["in_clerk"].user_id), "payroll:run:read")
        assert in_scope.legal_entity_ids == {world["le01"]["id"]} and not in_scope.pay_group_ids
        assert in_scope.employee_select is None
        wk_scope = access.target_scope(_principal(db, cid, users["wk_clerk"].user_id), "payroll:run:read")
        assert wk_scope.pay_group_ids == {pg["IN-W"]} and not wk_scope.legal_entity_ids


def test_ensure_in_scope_returns_404_outside_scope_and_403_without_permission(world):
    cid, users, pg = world["cid"], world["users"], world["pg"]
    le01, ae = world["le01"]["id"], world["ae"]["id"]
    with _session(cid) as db:
        in_clerk = _principal(db, cid, users["in_clerk"].user_id)
        access.ensure_in_scope(in_clerk, "payroll:run:read", le01, pg["IN-M"])
        with pytest.raises(AppError) as err:
            access.ensure_in_scope(in_clerk, "payroll:run:read", ae, pg["AE-M"], "Payroll run")
        assert err.value.status_code == 404
        with pytest.raises(AppError) as err:
            access.ensure_in_scope(in_clerk, "payroll:run:approve", le01, pg["IN-M"])
        assert err.value.status_code == 403
        wk_clerk = _principal(db, cid, users["wk_clerk"].user_id)
        access.ensure_in_scope(wk_clerk, "payroll:run:read", le01, pg["IN-W"])
        with pytest.raises(AppError) as err:
            access.ensure_in_scope(wk_clerk, "payroll:run:read", le01, pg["IN-M"])
        assert err.value.status_code == 404


def test_ec_sec_02_ensure_run_access_checks_tenant_and_scope_of_a_loaded_object(world):
    cid, users, pg = world["cid"], world["users"], world["pg"]
    with _session(cid) as db:
        in_clerk = _principal(db, cid, users["in_clerk"].user_id)
        access.ensure_run_access(in_clerk, "payroll:run:read", db.get(PayGroup, pg["IN-W"]))
        with pytest.raises(AppError) as err:
            access.ensure_run_access(in_clerk, "payroll:run:read", db.get(PayGroup, pg["AE-M"]))
        assert err.value.status_code == 404
        foreign = PayGroup(company_id=world["other"].company_id, legal_entity_id=world["le01"]["id"], id=pg["IN-M"])
        with pytest.raises(AppError) as err:  # same ids, other tenant
            access.ensure_run_access(_principal(db, cid, world["founder_id"]), "payroll:run:read", foreign)
        assert err.value.status_code == 404
        with pytest.raises(AppError) as err:
            access.ensure_run_access(in_clerk, "payroll:run:finalize", db.get(PayGroup, pg["IN-M"]))
        assert err.value.status_code == 403


def test_ec_sec_02_get_in_scope_hides_other_tenants_and_out_of_scope_objects(world):
    cid, users, pg = world["cid"], world["users"], world["pg"]
    with _session(cid) as db:
        founder = _principal(db, cid, world["founder_id"])
        assert access.get_in_scope(db, founder, "payroll:run:read", PayGroup, pg["AE-M"], pay_group_attr="id").id == pg["AE-M"]
        with pytest.raises(AppError) as err:  # another tenant's object, even with COMPANY scope
            access.get_in_scope(db, founder, "payroll:run:read", PayGroup, world["other_pg"], "Payroll run",
                                pay_group_attr="id")
        assert err.value.status_code == 404
        in_clerk = _principal(db, cid, users["in_clerk"].user_id)
        with pytest.raises(AppError) as err:
            access.get_in_scope(db, in_clerk, "payroll:run:read", PayGroup, pg["AE-M"], pay_group_attr="id")
        assert err.value.status_code == 404
        with pytest.raises(AppError) as err:
            access.get_in_scope(db, in_clerk, "payroll:run:close", PayGroup, pg["IN-M"], pay_group_attr="id")
        assert err.value.status_code == 403
        wk_clerk = _principal(db, cid, users["wk_clerk"].user_id)
        assert access.get_in_scope(db, wk_clerk, "payroll:run:read", PayGroup, pg["IN-W"], pay_group_attr="id")
        with pytest.raises(AppError) as err:
            access.get_in_scope(db, wk_clerk, "payroll:run:read", PayGroup, pg["IN-M"], pay_group_attr="id")
        assert err.value.status_code == 404
        with pytest.raises(AppError) as err:  # entity-owned lookups ignore pay-group targets
            access.get_in_scope(db, wk_clerk, "payroll:run:read", PayGroup, pg["IN-W"], pay_group_attr=None)
        assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# Employee-level scope (run employees, payslips); assignments stand in for run-employee rows
# ---------------------------------------------------------------------------
def test_self_payslip_scope_is_own_employee_for_any_period(world):
    cid, emp = world["cid"], world["emp"]
    with _session(cid) as db:
        employee = _principal(db, cid, world["users"]["indian"].user_id)
        for period in (MARCH, JULY):
            scope = access.payroll_employee_scope(db, employee, "payroll:payslip:read", *period)
            assert _ids(db, scope) == {emp["indian"]["id"]} and _count(db, scope) == 1
        assert _ids(db, access.payroll_employee_scope(db, employee, "payroll:run:read", *MARCH)) == set()
        access.ensure_employee_in_scope(db, employee, "payroll:payslip:read", emp["indian"]["id"], *MARCH)
        with pytest.raises(AppError) as err:
            access.ensure_employee_in_scope(db, employee, "payroll:payslip:read", emp["weekly"]["id"], *MARCH)
        assert err.value.status_code == 404
        with pytest.raises(AppError) as err:
            access.ensure_employee_in_scope(db, employee, "payroll:run:read", emp["indian"]["id"], *MARCH)
        assert err.value.status_code == 403


def _rows(db, principal, permission="payroll:payslip:read"):
    clause = access.run_employee_clause(principal, permission, EmployeeAssignment.legal_entity_id,
                                        EmployeeAssignment.pay_group_id, EmployeeAssignment.employee_id)
    rows = set(db.execute(select(EmployeeAssignment.employee_id, EmployeeAssignment.legal_entity_id).where(clause)))
    count = db.execute(select(func.count(EmployeeAssignment.id)).where(clause)).scalar_one()
    return rows, count


def test_ec_org_14_run_employee_rows_follow_the_run_entity(world):
    cid, emp, users = world["cid"], world["emp"], world["users"]
    le01, ae, mover = world["le01"]["id"], world["ae"]["id"], emp["mover"]["id"]
    with _session(cid) as db:
        in_rows, in_count = _rows(db, _principal(db, cid, users["in_clerk"].user_id))
        assert (mover, le01) in in_rows and (mover, ae) not in in_rows
        assert {e for _, e in in_rows} == {le01}
        ae_rows, _ = _rows(db, _principal(db, cid, users["ae_clerk"].user_id))
        assert (mover, ae) in ae_rows and (mover, le01) not in ae_rows
        wk_rows, wk_count = _rows(db, _principal(db, cid, users["wk_clerk"].user_id))
        assert wk_rows == {(emp["weekly"]["id"], le01)} and wk_count == 1
        own_rows, own_count = _rows(db, _principal(db, cid, users["indian"].user_id))
        assert own_rows == {(emp["indian"]["id"], le01)} and own_count == 1
        none_rows, none_count = _rows(db, _principal(db, cid, users["untargeted"].user_id))
        assert none_rows == set() and none_count == 0
        all_rows, all_count = _rows(db, _principal(db, cid, world["founder_id"]))
        total = db.execute(select(func.count(EmployeeAssignment.id))).scalar_one()
        assert all_count == total and (mover, ae) in all_rows


def test_run_and_dated_employee_scope_combine_for_one_run(world):
    """A run's employee list = run in scope AND employee inside the scope during the period (3.9 §5.4)."""
    cid, emp, users = world["cid"], world["emp"], world["users"]
    le01 = world["le01"]["id"]
    with _session(cid) as db:
        in_clerk = _principal(db, cid, users["in_clerk"].user_id)
        scope = access.payroll_employee_scope(db, in_clerk, "payroll:payslip:read", *JULY)
        run_clause = access.run_scope_clause(in_clerk, "payroll:payslip:read", EmployeeAssignment.legal_entity_id,
                                             EmployeeAssignment.pay_group_id)
        rows = set(db.execute(select(EmployeeAssignment.employee_id).where(
            and_(run_clause, scope.clause(EmployeeAssignment.employee_id), EmployeeAssignment.legal_entity_id == le01)
        )).scalars())
        assert emp["indian"]["id"] in rows and emp["mover"]["id"] not in rows


# ---------------------------------------------------------------------------
# Access preview (G-17)
# ---------------------------------------------------------------------------
def _preview(client, user_id=None):
    params = {"user_id": user_id} if user_id is not None else {}
    return client.get("/api/v1/payroll/access-preview", params=params)


def _by_code(body):
    return {p["permission"]: p for p in body["permissions"]}


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _assert_no_amounts(body):
    """The preview explains access; it never carries pay data."""
    keys = set(_keys(body))
    assert keys <= {
        "user", "id", "full_name", "email", "status", "account_active", "is_company_admin", "has_full_access", "roles",
        "name", "system_key", "registry_version", "permissions", "permission", "label", "description", "module",
        "sensitivity", "deprecated", "held", "grants", "scope", "scope_label", "from_full_access", "full_access_roles",
        "effective", "note", "targets", "target_type", "code", "summary", "total", "ineffective_grants",
    }, keys


def test_g17_access_preview_matches_enforced_payroll_grants(world):
    founder = world["acme"]
    body = _preview(founder).json()
    me = founder.me()
    assert body["user"]["id"] == world["founder_id"] and body["is_company_admin"] and body["has_full_access"]
    perms = _by_code(body)
    payroll_codes = {c for c in me["permissions"] if c.startswith("payroll:") or c.startswith("compensation:")}
    assert {c for c, p in perms.items() if p["held"]} == payroll_codes
    for code in payroll_codes:
        assert [g["scope"] for g in perms[code]["grants"]] == me["permissions"][code], code
    _assert_no_amounts(body)


def test_g17_ec_sec_03_preview_separates_full_access_from_explicit_payroll_roles(app, world):
    acme = world["acme"]
    perms = _by_code(_preview(acme).json())
    config = {g["scope"]: g for g in perms["payroll:configuration:read"]["grants"]}["COMPANY"]
    assert config["from_full_access"] and "payroll_manager" in {r["system_key"] for r in config["roles"]}
    create = {g["scope"]: g for g in perms["payroll:run:create"]["grants"]}["COMPANY"]
    assert not create["from_full_access"] and not create["full_access_roles"]
    assert {r["system_key"] for r in create["roles"]} == {"payroll_manager"}
    own = {g["scope"]: g for g in perms["payroll:payslip:read"]["grants"]}["SELF"]
    assert own["from_full_access"]

    # A second administrator without a payroll role: no run access, configuration only through full access.
    second = invite_and_activate(app, acme, create_employee(acme, "Second"), ["company_admin"])
    body = _preview(acme, second.user_id).json()
    perms = _by_code(body)
    assert body["is_company_admin"] and not perms["payroll:run:read"]["held"]
    assert not perms["payroll:compensation:read"]["grants"][0]["roles"]
    assert [g["scope"] for g in perms["payroll:compensation:read"]["grants"]] == ["SELF"]
    config = perms["payroll:configuration:read"]["grants"][0]
    assert config["from_full_access"] and config["roles"] == []
    assert {r["system_key"] for r in config["full_access_roles"]} == {"company_admin"}


def test_g17_preview_names_entity_and_pay_group_targets_and_flags_untargeted_grants(world):
    acme, users = world["acme"], world["users"]
    perms = _by_code(_preview(acme, users["in_clerk"].user_id).json())
    grant = perms["payroll:run:read"]["grants"][0]
    assert grant["scope"] == "LEGAL_ENTITY" and grant["effective"] and grant["target_type"] == "legal_entities"
    assert [t["code"] for t in grant["targets"]] == ["LE01"]
    assert grant["roles"][0]["name"] == "Entity payroll" and not grant["from_full_access"]

    perms = _by_code(_preview(acme, users["wk_clerk"].user_id).json())
    grant = perms["payroll:payslip:read"]["grants"][0]
    assert grant["scope"] == "PAY_GROUP" and grant["target_type"] == "pay_groups"
    assert grant["targets"] == [{"id": world["pg"]["IN-W"], "code": "TPG-INW", "name": "Group TPG-INW"}]

    body = _preview(acme, users["untargeted"].user_id).json()
    grant = _by_code(body)["payroll:run:read"]["grants"][0]
    assert not grant["effective"] and grant["targets"] == [] and "covers nothing" in grant["note"]
    assert body["summary"]["ineffective_grants"] == len(PAY_GRANTS)


def test_g17_employee_previews_own_access_without_user_read(world):
    employee = world["users"]["indian"]
    body = _preview(employee).json()
    assert body["user"]["id"] == employee.user_id and not body["is_company_admin"]
    perms = _by_code(body)
    own = perms["payroll:payslip:read"]["grants"]
    assert [g["scope"] for g in own] == ["SELF"] and own[0]["effective"]
    assert {r["system_key"] for r in own[0]["roles"]} == {"employee"}
    assert not perms["payroll:run:read"]["held"] and not perms["payroll:run:read"]["grants"]
    assert _preview(employee, employee.user_id).status_code == 200
    _assert_no_amounts(body)


def test_g17_preview_of_another_user_needs_user_read(world):
    users = world["users"]
    r = _preview(users["indian"], world["founder_id"])
    assert r.status_code == 403
    r = _preview(users["in_clerk"], users["indian"].user_id)  # payroll role without user:read
    assert r.status_code == 403
    assert _preview(world["acme"], users["indian"].user_id).status_code == 200


def test_g17_ec_sec_02_preview_of_another_tenants_or_unknown_user_is_404(app, world):
    other_user = world["other"].me()["user"]["id"]
    assert _preview(world["acme"], other_user).status_code == 404
    assert _preview(world["acme"], 999999).status_code == 404
    assert _preview(world["other"], world["founder_id"]).status_code == 404
    # The employee of tenant A cannot probe tenant B either (403 before any lookup).
    assert _preview(world["users"]["indian"], other_user).status_code == 403


def test_g17_preview_validation_and_authentication(app, world):
    assert _preview(world["acme"], 0).status_code == 422
    assert world["acme"].get("/api/v1/payroll/access-preview?user_id=abc").status_code == 422
    assert new_client(app).get("/api/v1/payroll/access-preview").status_code == 401


def test_g17_preview_reflects_role_removal_immediately(world):
    acme, users = world["acme"], world["users"]
    assert _by_code(_preview(acme, users["ae_clerk"].user_id).json())["payroll:run:read"]["held"]
    r = acme.put(f"/api/v1/users/{users['ae_clerk'].user_id}/roles", json={"role_ids": [role_id(acme, "employee")]})
    assert r.status_code == 200, r.text
    body = _preview(acme, users["ae_clerk"].user_id).json()
    assert not _by_code(body)["payroll:run:read"]["held"]
    assert [r["system_key"] for r in body["roles"]] == ["employee"]
