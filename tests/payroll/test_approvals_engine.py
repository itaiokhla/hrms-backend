"""Generic approvals engine and inbox API (3.1 §5.14, 3.5 §11, 3.4 §9.1/§10.2/§13; audit 03 §4.2-§4.3).

EC-RUN-05, EC-RUN-06, EC-RUN-07, EC-RUN-08, EC-RUN-21, EC-SEC-05, EC-SEC-18, EC-CFG-12.
"""

import json
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.errors import AppError
from app.core.principal import build_principal
from app.database.db import SessionLocal, bind_tenant, system_engine
from app.models.company import Company
from app.models.user import User
from app.payroll.services import approvals
from app.services import org_service
from tests.helpers import create_employee, invite_and_activate, role_id, signup_company

BASE = "/api/v1/payroll/approvals"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sql(query, **params):
    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


@contextmanager
def _session(company_id, user_id=None):
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        principal = None
        if user_id is not None:
            principal = build_principal(db, db.get(User, user_id), None, db.get(Company, company_id))
        yield db, principal
    finally:
        db.close()


def _submit(company_id, user_id, **kwargs) -> int:
    with _session(company_id, user_id) as (db, principal):
        request = approvals.submit(db, principal, **kwargs)
        db.commit()
        return request.id


def _chain(company_id, module, *steps):
    for order, step in enumerate(steps, start=1):
        _sql(
            "INSERT INTO approval_chain_steps (company_id, module, step_order, approver_type, role_id, specific_user_id, "
            "min_days, min_amount, min_amount_currency, created_at, updated_at) "
            "VALUES (:c, :m, :o, :t, :r, :u, :d, :amt, :cur, now(), now())",
            c=company_id, m=module, o=order, t=step["type"], r=step.get("role_id"), u=step.get("user_id"),
            d=step.get("min_days"), amt=step.get("min_amount"), cur=step.get("currency"),
        )


def _delegate(company_id, delegator, delegate, module, starts=None, ends=None, revoked=False) -> int:
    today = date.today()
    return _sql(
        "INSERT INTO approval_delegations (company_id, delegator_employee_id, delegate_employee_id, module, starts_on, "
        "ends_on, revoked_at, created_at, updated_at) VALUES (:c, :a, :b, :m, :s, :e, "
        + ("now()" if revoked else "NULL") + ", now(), now()) RETURNING id",
        c=company_id, a=delegator, b=delegate, m=module, s=starts or today - timedelta(days=1),
        e=ends or today + timedelta(days=5),
    )[0][0]


def _audit_rows(company_id, request_id):
    return _sql(
        "SELECT module, action, actor_type, details FROM audit_logs WHERE company_id = :c AND entity_type = "
        "'approval_request' AND entity_id = :e ORDER BY id",
        c=company_id, e=str(request_id),
    )


def _inbox(client, **params):
    r = client.get(BASE, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _approve(client, request_id, **body):
    return client.post(f"{BASE}/{request_id}/approve", json=body or None)


@pytest.fixture
def world(app):
    """Acme: founder admin (company admin + payroll manager), manager Mona -> worker Walt, loner Lou (no
    manager), payroll approvers Ada and Ben, finance Fay. Beta: its founder admin."""
    admin = signup_company(app, "Acme Corp")
    beta = signup_company(app, "Beta Industries")
    mona = create_employee(admin, "Mona")
    walt = create_employee(admin, "Walt", manager_id=mona["id"])
    lou = create_employee(admin, "Lou")
    ada = create_employee(admin, "Ada")
    ben = create_employee(admin, "Ben")
    fay = create_employee(admin, "Fay")
    users = {
        "admin": admin,
        "beta": beta,
        "mona": invite_and_activate(app, admin, mona, ["manager"]),
        "walt": invite_and_activate(app, admin, walt, ["employee"]),
        "ada": invite_and_activate(app, admin, ada, ["employee", "payroll_approver"]),
        "ben": invite_and_activate(app, admin, ben, ["employee", "payroll_approver"]),
        "fay": invite_and_activate(app, admin, fay, ["employee", "finance_payments"]),
    }
    admin.user_id = admin.me()["user"]["id"]
    beta.user_id = beta.me()["user"]["id"]
    return {"cid": admin.company_id, "u": users, "emp": {"mona": mona, "walt": walt, "lou": lou, "ada": ada,
                                                          "ben": ben, "fay": fay}}


@pytest.fixture
def decided():
    """Records callback invocations for a test-only subject type, then unregisters it."""
    calls = []

    def callback(db, principal, request):
        calls.append((request.id, request.status, principal.user_id if principal else None))

    for module in ("compensation", "overtime", "payroll_run"):
        approvals.register_callback(module, "test_subject", callback)
    yield calls
    for module in ("compensation", "overtime", "payroll_run"):
        approvals._CALLBACKS.pop((module, "test_subject"), None)


# ---------------------------------------------------------------------------
# Default chain, inbox, approval, callbacks, audit, notifications
# ---------------------------------------------------------------------------
def test_default_chain_routes_to_holders_of_the_module_permission(world, decided):
    cid, u, emp = world["cid"], world["u"], world["emp"]
    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="test_subject", subject_id=7,
                  subject_employee_id=emp["walt"]["id"], amount="55000.00", currency="INR",
                  context={"reason": "annual revision"})

    detail = u["admin"].get(f"{BASE}/{rid}").json()  # the requester sees their own request
    assert detail["status"] == "PENDING" and detail["current_step"] == 1
    assert [(s["approver_type"], s["status"]) for s in detail["steps"]] == [("permission_holders", "pending")]
    assert detail["amount"] == {"amount": "55000.00", "currency": "INR"}
    assert detail["subject_employee_name"] == "Walt Test" and detail["can_act"] is False
    assert detail["context"] == {"reason": "annual revision"}

    for name in ("ada", "ben"):
        inbox = _inbox(u[name], module="compensation")
        assert inbox["total"] == 1 and inbox["items"][0]["id"] == rid and inbox["items"][0]["acting_as"] == "approver"
        notes = u[name].get("/api/v1/notifications").json()["notifications"]
        assert any(n["notification_type"] == "approval" and str(rid) in n["message"] for n in notes)
        assert not any("55000" in n["message"] for n in notes)  # notifications never carry amounts
    assert _inbox(u["admin"])["total"] == 0  # the requester never approves (and lacks the permission)
    assert u["walt"].get(f"{BASE}/{rid}").status_code == 404
    assert _inbox(u["walt"], mine="false")["total"] == 0

    r = _approve(u["ada"], rid, remarks="ok")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": rid, "module": "compensation", "subject_type": "test_subject", "subject_id": 7,
                        "status": "APPROVED", "current_step": None, "pending_reason": None}
    assert decided == [(rid, "APPROVED", u["ada"].user_id)]
    assert _inbox(u["ben"])["total"] == 0
    after = u["ada"].get(f"{BASE}/{rid}").json()
    assert after["steps"][0]["acted_by"] == "Ada Test" and after["steps"][0]["acted_as"] == "approver"
    assert after["decided_at"]

    rows = _audit_rows(cid, rid)
    assert [(m, a) for m, a, _, _ in rows] == [("Compensation", "APPROVAL_REQUESTED"), ("Compensation", "APPROVAL_APPROVED")]
    requested, approved = rows[0][3], rows[1][3]
    assert requested["amount"] == {"amount": "55000.00", "currency": "INR"} and requested["steps"] == ["permission_holders"]
    assert approved["final"] is True and approved["status"] == "APPROVED" and approved["remarks"] == "ok"
    notes = u["admin"].get("/api/v1/notifications").json()["notifications"]
    assert any("approved" in n["title"].lower() for n in notes)


def test_reject_requires_remarks_and_runs_callbacks(world, decided):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="test_subject", subject_id=1,
                  subject_employee_id=world["emp"]["lou"]["id"])
    assert u["ada"].post(f"{BASE}/{rid}/reject", json={}).status_code == 422
    assert u["ada"].post(f"{BASE}/{rid}/reject", json={"remarks": "   "}).status_code == 422
    assert u["ada"].post(f"{BASE}/{rid}/reject", json={"remarks": "x", "status": "APPROVED"}).status_code == 422
    with _session(cid, u["ada"].user_id) as (db, principal):
        with pytest.raises(AppError) as err:
            approvals.decide(db, principal, rid, "REJECT", "  ")
        assert err.value.code == "REMARKS_REQUIRED"
        with pytest.raises(AppError) as err:
            approvals.decide(db, principal, rid, "MAYBE")
        assert err.value.code == "INVALID_DECISION"

    r = u["ada"].post(f"{BASE}/{rid}/reject", json={"remarks": "Budget freeze"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert decided == [(rid, "REJECTED", u["ada"].user_id)]
    detail = u["admin"].get(f"{BASE}/{rid}").json()
    assert detail["steps"][0]["status"] == "rejected" and detail["steps"][0]["remarks"] == "Budget freeze"
    assert _audit_rows(cid, rid)[-1][1] == "APPROVAL_REJECTED"
    assert _approve(u["ben"], rid).status_code == 409


# ---------------------------------------------------------------------------
# Separation of duties, self-pay, unapprovable requests
# ---------------------------------------------------------------------------
def test_ec_run_05_requester_and_preparers_cannot_approve(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["ada"].user_id, module="payroll_run", subject_type="run", subject_id=11)
    r = _approve(u["ada"], rid)
    assert r.status_code == 403 and "prepared or submitted" in r.json()["detail"]
    assert _inbox(u["ada"])["total"] == 0 and _inbox(u["ben"])["total"] == 1
    assert _approve(u["ben"], rid).json()["status"] == "APPROVED"

    # EC-CFG-12: a maker named in the context (the user who last recalculated the run) is barred as well.
    rid = _submit(cid, u["admin"].user_id, module="payroll_run", subject_type="run", subject_id=12,
                  context={"maker_user_ids": [u["ben"].user_id]})
    assert _approve(u["ben"], rid).status_code == 403
    assert _approve(u["ada"], rid).json()["status"] == "APPROVED"


def test_unapprovable_request_stays_pending_with_a_reason_never_approved(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["ada"].user_id, module="payroll_run", subject_type="run", subject_id=13,
                  context={"maker_user_ids": [u["ben"].user_id]})
    detail = u["ada"].get(f"{BASE}/{rid}").json()
    assert detail["status"] == "PENDING" and detail["pending_reason"] == approvals.NO_ELIGIBLE_APPROVER
    assert [(s["approver_type"], s["status"]) for s in detail["steps"]] == [("permission_holders", "skipped"),
                                                                            ("role", "pending")]
    assert detail["steps"][1]["approver"] == "Company Administrator"
    # Administrators without the run-approve permission cannot decide it either.
    assert _approve(u["admin"], rid).status_code in (403, 404)
    assert _approve(u["ben"], rid).status_code == 403
    # Once an administrator holds the permission, the waiting request becomes theirs to decide.
    # (Administrators cannot change their own roles through the API, so the grant is written directly.)
    _sql("INSERT INTO user_role_assignments (company_id, user_id, role_id, effective_from, created_at, updated_at) "
         "VALUES (:c, :u, :r, now() - interval '1 minute', now(), now())",
         c=cid, u=u["admin"].user_id, r=role_id(u["admin"], "payroll_approver"))
    assert _inbox(u["admin"])["total"] == 1
    assert _approve(u["admin"], rid).json()["status"] == "APPROVED"


def test_ec_run_06_nobody_approves_a_request_about_their_own_record(world, decided):
    cid, u, emp = world["cid"], world["u"], world["emp"]
    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="test_subject", subject_id=2,
                  subject_employee_id=emp["ada"]["id"])
    detail = u["ada"].get(f"{BASE}/{rid}").json()
    assert detail["can_act"] is False
    r = _approve(u["ada"], rid)
    assert r.status_code == 403 and "your own record" in r.json()["detail"]
    assert _inbox(u["ada"])["total"] == 0
    assert _approve(u["ben"], rid).json()["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# Action-time checks and concurrency
# ---------------------------------------------------------------------------
def test_ec_run_07_grant_revoked_between_page_load_and_click_is_403(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="bank_change", subject_type="bank_account", subject_id=5,
                  subject_employee_id=world["emp"]["walt"]["id"])
    assert u["ada"].get(f"{BASE}/{rid}").json()["can_act"] is True  # page load
    r = u["admin"].put(f"/api/v1/users/{u['ada'].user_id}/roles", json={"role_ids": [role_id(u["admin"], "employee")]})
    assert r.status_code == 200, r.text
    r = _approve(u["ada"], rid)  # click
    assert r.status_code == 403
    assert u["admin"].get(f"{BASE}/{rid}").json()["status"] == "PENDING"
    assert _approve(u["ben"], rid).json()["status"] == "APPROVED"


def test_ec_run_08_concurrent_decisions_one_wins_the_other_gets_409(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="salary", subject_id=3,
                  subject_employee_id=world["emp"]["walt"]["id"])
    outcome = {}
    first_db = SessionLocal()
    try:
        bind_tenant(first_db, cid)
        first = build_principal(first_db, first_db.get(User, u["ada"].user_id), None, first_db.get(Company, cid))
        approvals.decide(first_db, first, rid, "APPROVE")  # holds the row lock, not committed yet

        def second():
            with _session(cid, u["ben"].user_id) as (db, principal):
                try:
                    approvals.decide(db, principal, rid, "APPROVE")
                    db.commit()
                    outcome["second"] = "approved"
                except AppError as exc:
                    db.rollback()
                    outcome["second"] = (exc.status_code, exc.code)

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the second decision must wait for the row lock"
        first_db.commit()
        thread.join(timeout=30)
    finally:
        first_db.close()
    assert outcome["second"] == (409, "APPROVAL_ALREADY_DECIDED")
    detail = u["admin"].get(f"{BASE}/{rid}").json()
    assert detail["status"] == "APPROVED" and detail["steps"][0]["acted_by"] == "Ada Test"
    assert [a for _, a, _, _ in _audit_rows(cid, rid)].count("APPROVAL_APPROVED") == 1


def test_ec_run_08_stale_expected_step_is_409(world):
    cid, u = world["cid"], world["u"]
    _chain(cid, "payroll_run", {"type": "specific_user", "user_id": u["ada"].user_id},
           {"type": "role", "role_id": role_id(u["admin"], "payroll_approver")})
    rid = _submit(cid, u["admin"].user_id, module="payroll_run", subject_type="run", subject_id=21)
    assert _inbox(u["ben"])["total"] == 0  # step 1 names Ada only
    assert _approve(u["ada"], rid, expected_step=1).json() == {
        "id": rid, "module": "payroll_run", "subject_type": "run", "subject_id": 21, "status": "PENDING",
        "current_step": 2, "pending_reason": None}
    # Ben loaded the page while step 1 was current: his click must not approve step 2 blindly.
    r = _approve(u["ben"], rid, expected_step=1)
    assert r.status_code == 409 and r.json().get("code", "APPROVAL_STEP_CHANGED") == "APPROVAL_STEP_CHANGED"
    # Ada approved step 1, so she cannot also approve step 2 of the same request.
    r = _approve(u["ada"], rid, expected_step=2)
    assert r.status_code == 403 and "earlier step" in r.json()["detail"]
    assert _approve(u["ben"], rid, expected_step=2).json()["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# Configured chains: hierarchy, thresholds, skips, fallback
# ---------------------------------------------------------------------------
def test_configured_chain_hierarchy_and_finance_steps_with_min_amount(world):
    cid, u, emp = world["cid"], world["u"], world["emp"]
    _chain(cid, "overtime", {"type": "primary_manager"}, {"type": "skip_level_manager"},
           {"type": "finance_role", "min_amount": Decimal("1000.00"), "currency": "INR"})
    walt = emp["walt"]["id"]

    small = _submit(cid, u["walt"].user_id, module="overtime", subject_type="entry", subject_id=1,
                    subject_employee_id=walt, amount="500.00", currency="INR")
    detail = u["mona"].get(f"{BASE}/{small}").json()
    # Mona has no manager: the skip-level step is skipped like leave does; the finance step is below its threshold.
    assert [(s["approver_type"], s["status"]) for s in detail["steps"]] == [("primary_manager", "pending"),
                                                                            ("skip_level_manager", "waiting")]
    assert detail["steps"][0]["approver"] == "Mona Test"
    assert _inbox(u["mona"])["items"][0]["acting_as"] == "approver"
    assert _approve(u["walt"], small).status_code == 403  # requester
    assert _approve(u["mona"], small).json()["status"] == "APPROVED"
    assert [s["status"] for s in u["mona"].get(f"{BASE}/{small}").json()["steps"]] == ["approved", "skipped"]

    large = _submit(cid, u["walt"].user_id, module="overtime", subject_type="entry", subject_id=2,
                    subject_employee_id=walt, amount="1500.00", currency="INR")
    assert _approve(u["fay"], large).status_code in (403, 404)  # not her step yet
    after = _approve(u["mona"], large).json()
    assert after["status"] == "PENDING" and after["current_step"] == 3
    assert _inbox(u["fay"])["items"][0]["id"] == large
    assert _approve(u["fay"], large).json()["status"] == "APPROVED"

    # An amount in another currency cannot be compared with the threshold: the step stays (fail closed).
    other = _submit(cid, u["walt"].user_id, module="overtime", subject_type="entry", subject_id=3,
                    subject_employee_id=walt, amount="10.00", currency="AED")
    steps = u["mona"].get(f"{BASE}/{other}").json()["steps"]
    assert [s["approver_type"] for s in steps] == ["primary_manager", "skip_level_manager", "finance_role"]


def test_skip_level_and_department_head_steps_notify_each_next_approver(app, world):
    cid, u, emp, admin = world["cid"], world["u"], world["emp"], world["u"]["admin"]
    boss = create_employee(admin, "Boss")
    mid = create_employee(admin, "Mid", manager_id=boss["id"])
    sub = create_employee(admin, "Sub", manager_id=mid["id"])
    boss_c = invite_and_activate(app, admin, boss, ["employee", "payroll_approver"])
    invite_and_activate(app, admin, mid, ["manager"])
    dept = admin.post("/api/v1/departments", json={"name": "Finance"}).json()["id"]
    assert admin.put(f"/api/v1/departments/{dept}", json={"name": "Finance", "head_employee_id": emp["ada"]["id"]}
                     ).status_code == 200
    assert admin.put(f"/api/v1/employees/{sub['id']}", json={"department_id": dept}).status_code == 200
    _chain(cid, "compensation", {"type": "primary_manager"}, {"type": "skip_level_manager"},
           {"type": "department_head"})

    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="salary", subject_id=50,
                  subject_employee_id=sub["id"])
    detail = admin.get(f"{BASE}/{rid}").json()
    # Mid is the primary manager but holds no compensation approval: that step is skipped, never auto-approved.
    assert [(s["approver_type"], s["status"]) for s in detail["steps"]] == [
        ("primary_manager", "skipped"), ("skip_level_manager", "pending"), ("department_head", "waiting")]
    assert detail["steps"][1]["approver"] == "Boss Test" and detail["steps"][2]["approver"] == "Ada Test"
    assert _inbox(boss_c)["total"] == 1 and _inbox(u["ada"])["total"] == 0

    def notified(client):
        return any(str(rid) in n["message"] for n in client.get("/api/v1/notifications").json()["notifications"])

    assert notified(boss_c) and not notified(u["ada"])
    assert _approve(u["ada"], rid).status_code == 403  # not her step yet
    assert _approve(boss_c, rid, expected_step=2).json()["current_step"] == 3
    assert notified(u["ada"]) and _inbox(u["ada"])["items"][0]["id"] == rid
    assert _approve(u["ada"], rid, expected_step=3).json()["status"] == "APPROVED"


def test_min_days_threshold_applies_from_the_context(world):
    cid, u = world["cid"], world["u"]
    _chain(cid, "lop", {"type": "role", "role_id": role_id(u["admin"], "company_admin")},
           {"type": "specific_user", "user_id": u["mona"].user_id, "min_days": 3})
    walt = world["emp"]["walt"]["id"]
    short = _submit(cid, u["fay"].user_id, module="lop", subject_type="lop", subject_id=1,
                    subject_employee_id=walt, context={"days": "2"})
    long = _submit(cid, u["fay"].user_id, module="lop", subject_type="lop", subject_id=2,
                   subject_employee_id=walt, context={"days": "3.5"})
    unknown = _submit(cid, u["fay"].user_id, module="lop", subject_type="lop", subject_id=3,
                      subject_employee_id=walt)
    steps = {r: [s["approver_type"] for s in u["admin"].get(f"{BASE}/{r}").json()["steps"]]
             for r in (short, long, unknown)}
    assert steps[short] == ["role"]
    assert steps[long] == steps[unknown] == ["role", "specific_user"]  # unknown duration keeps the step
    assert _approve(u["admin"], long).json()["current_step"] == 2
    assert _approve(u["mona"], long).json()["status"] == "APPROVED"  # attendance:regularize on her report


def test_steps_without_eligible_approver_fall_back_to_administrators_holding_the_permission(world, decided):
    cid, u, emp = world["cid"], world["u"], world["emp"]
    _chain(cid, "overtime", {"type": "primary_manager"})
    rid = _submit(cid, u["fay"].user_id, module="overtime", subject_type="test_subject", subject_id=4,
                  subject_employee_id=emp["lou"]["id"])  # Lou has no manager
    detail = u["admin"].get(f"{BASE}/{rid}").json()
    assert [(s["approver_type"], s["status"]) for s in detail["steps"]] == [("primary_manager", "skipped"),
                                                                            ("role", "pending")]
    assert detail["pending_reason"] is None and detail["can_act"] is True
    assert _approve(u["admin"], rid).json()["status"] == "APPROVED"
    assert decided == [(rid, "APPROVED", u["admin"].user_id)]


# ---------------------------------------------------------------------------
# Delegation and re-routing
# ---------------------------------------------------------------------------
def test_ec_sec_18_delegation_is_module_aware_and_stops_at_once(world):
    cid, u, emp = world["cid"], world["u"], world["emp"]
    _chain(cid, "overtime", {"type": "primary_manager"})
    mona, ada, walt = emp["mona"]["id"], emp["ada"]["id"], emp["walt"]["id"]
    rid = _submit(cid, u["fay"].user_id, module="overtime", subject_type="entry", subject_id=5, subject_employee_id=walt)

    leave_only = _delegate(cid, mona, ada, "leave")
    assert _inbox(u["ada"])["total"] == 0
    with _session(cid) as (db, _):
        assert org_service.active_delegators_for(db, ada) == [mona]  # the leave default keeps working
        assert org_service.active_delegators_for(db, ada, module="overtime") == []
    _sql("UPDATE approval_delegations SET revoked_at = now() WHERE id = :i", i=leave_only)

    expired = _delegate(cid, mona, ada, "overtime", starts=date.today() - timedelta(days=9),
                        ends=date.today() - timedelta(days=2))
    assert _inbox(u["ada"])["total"] == 0
    delegation = _delegate(cid, mona, ada, "overtime")
    inbox = _inbox(u["ada"])
    assert inbox["total"] == 1 and inbox["items"][0]["acting_as"] == "delegate"

    _sql("UPDATE approval_delegations SET revoked_at = now() WHERE id = :i", i=delegation)
    assert _approve(u["ada"], rid).status_code in (403, 404)
    assert _inbox(u["ada"])["total"] == 0
    assert expired

    # The delegate who is the subject of the request may never act on it.
    _delegate(cid, mona, walt, "overtime")
    assert _inbox(u["walt"])["total"] == 0
    assert _approve(u["walt"], rid).status_code in (403, 404)

    _delegate(cid, mona, ada, "overtime")
    r = _approve(u["ada"], rid)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    step = u["mona"].get(f"{BASE}/{rid}").json()["steps"][0]
    assert step["acted_as"] == "delegate" and step["acted_by"] == "Ada Test"


def test_ec_run_21_suspended_delegator_invalidates_the_delegation(world):
    cid, u, emp = world["cid"], world["u"], world["emp"]
    _chain(cid, "overtime", {"type": "primary_manager"})
    rid = _submit(cid, u["fay"].user_id, module="overtime", subject_type="entry", subject_id=6,
                  subject_employee_id=emp["walt"]["id"])
    _delegate(cid, emp["mona"]["id"], emp["ada"]["id"], "overtime")
    assert _inbox(u["ada"])["total"] == 1
    r = u["admin"].put(f"/api/v1/users/{u['mona'].user_id}/status", json={"status": "suspended"})
    assert r.status_code == 200, r.text
    assert _inbox(u["ada"])["total"] == 0
    assert _approve(u["ada"], rid).status_code in (403, 404)


def test_ec_run_21_reroute_skips_a_step_whose_approver_was_suspended(world):
    cid, u = world["cid"], world["u"]
    _chain(cid, "compensation", {"type": "specific_user", "user_id": u["ada"].user_id},
           {"type": "specific_user", "user_id": u["ben"].user_id})
    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="salary", subject_id=8,
                  subject_employee_id=world["emp"]["walt"]["id"])
    assert u["admin"].get(f"{BASE}/{rid}").json()["current_step"] == 1
    assert u["admin"].put(f"/api/v1/users/{u['ada'].user_id}/status", json={"status": "suspended"}).status_code == 200
    with _session(cid) as (db, _):
        assert approvals.reroute_pending(db, cid) == 1
        db.commit()
    detail = u["admin"].get(f"{BASE}/{rid}").json()
    assert [s["status"] for s in detail["steps"]] == ["skipped", "pending"] and detail["current_step"] == 2
    assert _audit_rows(cid, rid)[-1][1:3] == ("APPROVAL_REROUTED", "system")
    assert _approve(u["ben"], rid).json()["status"] == "APPROVED"
    with _session(cid) as (db, _):
        assert approvals.reroute_pending(db, cid) == 0


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
def test_cancel_only_by_the_requester_while_pending(world, decided):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="test_subject", subject_id=9,
                  subject_employee_id=world["emp"]["walt"]["id"])
    assert u["ada"].post(f"{BASE}/{rid}/cancel").status_code == 403
    assert u["walt"].post(f"{BASE}/{rid}/cancel").status_code == 404
    assert u["admin"].post(f"{BASE}/{rid}/cancel", json={"remarks": "x", "status": "APPROVED"}).status_code == 422
    r = u["admin"].post(f"{BASE}/{rid}/cancel", json={"remarks": "Raised by mistake"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert decided == [(rid, "CANCELLED", u["admin"].user_id)]
    assert u["admin"].post(f"{BASE}/{rid}/cancel").status_code == 409
    assert _approve(u["ada"], rid).status_code == 409
    assert [s["status"] for s in u["admin"].get(f"{BASE}/{rid}").json()["steps"]] == ["cancelled"]
    assert _audit_rows(cid, rid)[-1][3]["remarks"] == "Raised by mistake"
    # A new request for the same subject may be raised once nothing is pending.
    _submit(cid, u["admin"].user_id, module="compensation", subject_type="test_subject", subject_id=9)


def test_withdraw_by_the_owning_module_cancels_without_callbacks(world, decided):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["ada"].user_id, module="payroll_run", subject_type="test_subject", subject_id=10)
    with _session(cid, u["admin"].user_id) as (db, principal):
        request = approvals.withdraw(db, principal, module="payroll_run", subject_type="test_subject", subject_id=10,
                                     reason="Run reopened")
        assert request.id == rid and request.status == "CANCELLED"
        assert approvals.withdraw(db, principal, module="payroll_run", subject_type="test_subject", subject_id=10,
                                  reason="again") is None
        db.commit()
    assert decided == []
    assert _audit_rows(cid, rid)[-1][1] == "APPROVAL_WITHDRAWN"


# ---------------------------------------------------------------------------
# Tenancy, authorization and scope of lists
# ---------------------------------------------------------------------------
def test_cross_tenant_requests_are_404_on_every_endpoint(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="wage_input", subject_type="input", subject_id=1,
                  subject_employee_id=world["emp"]["walt"]["id"])
    beta = u["beta"]  # holds payroll:inputs:approve in its own tenant (payroll manager)
    assert beta.get(f"{BASE}/{rid}").status_code == 404
    assert _approve(beta, rid).status_code == 404
    assert beta.post(f"{BASE}/{rid}/reject", json={"remarks": "no"}).status_code == 404
    assert beta.post(f"{BASE}/{rid}/cancel").status_code == 404
    assert _inbox(beta) == {"total": 0, "items": []}
    assert _inbox(beta, mine="false") == {"total": 0, "items": []}
    assert u["admin"].get(f"{BASE}/{rid}").status_code == 200
    with _session(beta.company_id, beta.user_id) as (db, principal):
        with pytest.raises(AppError) as err:
            approvals.submit(db, principal, module="wage_input", subject_type="input", subject_id=1,
                             subject_employee_id=world["emp"]["walt"]["id"])
        assert err.value.status_code == 404


def test_deciding_without_any_approval_permission_is_403(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="wage_input", subject_type="input", subject_id=2,
                  subject_employee_id=world["emp"]["lou"]["id"])
    for path in (f"{BASE}/{rid}/approve", f"{BASE}/999999/approve"):
        assert u["walt"].post(path).status_code == 403
    assert u["walt"].post(f"{BASE}/{rid}/reject", json={"remarks": "no"}).status_code == 403
    assert u["ada"].get(f"{BASE}/999999").status_code == 404
    assert u["ada"].get(f"{BASE}?module=leave").status_code == 422
    assert u["ada"].get(f"{BASE}?status=DONE").status_code == 422
    assert u["ada"].post(f"{BASE}/{rid}/approve", json={"approved_by": 1}).status_code == 422


def test_ec_sec_05_inbox_and_counts_never_leak_other_scopes(world):
    cid, u, emp = world["cid"], world["u"], world["emp"]
    _chain(cid, "overtime", {"type": "primary_manager"})
    mine = _submit(cid, u["fay"].user_id, module="overtime", subject_type="entry", subject_id=31,
                   subject_employee_id=emp["walt"]["id"])
    _submit(cid, u["fay"].user_id, module="overtime", subject_type="entry", subject_id=32,
            subject_employee_id=emp["ben"]["id"])
    _submit(cid, u["fay"].user_id, module="compensation", subject_type="salary", subject_id=33,
            subject_employee_id=emp["walt"]["id"])

    for params in ({}, {"mine": "false"}, {"mine": "false", "module": "overtime"}):
        listing = _inbox(u["mona"], **params)
        assert listing["total"] == 1 and [i["id"] for i in listing["items"]] == [mine]
    assert _inbox(u["mona"], mine="false", module="compensation")["total"] == 0
    assert _inbox(u["ada"], mine="false", module="overtime")["total"] == 0
    assert _inbox(u["ada"], mine="false")["total"] == 1  # the compensation request only
    assert _inbox(u["admin"], mine="false", module="overtime")["total"] == 2  # company-wide overtime:approve
    assert _inbox(u["fay"], mine="false")["total"] == 3  # her own requests
    assert _inbox(u["fay"], mine="true")["total"] == 0
    assert _inbox(u["mona"], status="APPROVED") == {"total": 0, "items": []}
    page = _inbox(u["fay"], mine="false", limit=2)
    assert page["total"] == 3 and len(page["items"]) == 2
    assert _inbox(u["fay"], mine="false", skip=2, limit=2)["items"][0]["id"] == mine


def test_idempotency_key_replays_the_decision_once(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="compensation", subject_type="salary", subject_id=40,
                  subject_employee_id=world["emp"]["walt"]["id"])
    headers = {"Idempotency-Key": "approve-40-a"}
    first = u["ada"].post(f"{BASE}/{rid}/approve", json={"remarks": "fine"}, headers=headers)
    again = u["ada"].post(f"{BASE}/{rid}/approve", json={"remarks": "fine"}, headers=headers)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() and again.headers.get("Idempotent-Replayed") == "true"
    other = u["ada"].post(f"{BASE}/{rid}/approve", json={"remarks": "changed"}, headers=headers)
    assert other.status_code == 409
    assert [a for _, a, _, _ in _audit_rows(cid, rid)].count("APPROVAL_APPROVED") == 1


# ---------------------------------------------------------------------------
# submit() validation and system requests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"module": "leave"}, "INVALID_APPROVAL_MODULE"),
        ({"module": "bonus"}, "INVALID_APPROVAL_MODULE"),
        ({"amount": "10.00"}, "AMOUNT_CURRENCY_REQUIRED"),
        ({"currency": "INR"}, "AMOUNT_CURRENCY_REQUIRED"),
        ({"amount": 10.5, "currency": "INR"}, "FLOAT_NOT_ALLOWED"),
        ({"amount": "1.234", "currency": "INR"}, "AMOUNT_NOT_ROUNDED"),
        ({"amount": "1", "currency": "XXQ"}, None),
        ({"subject_type": ""}, "INVALID_APPROVAL_SUBJECT"),
    ],
)
def test_submit_rejects_invalid_requests(world, kwargs, code):
    cid, u = world["cid"], world["u"]
    params = {"module": "compensation", "subject_type": "salary", "subject_id": 1, **kwargs}
    with _session(cid, u["admin"].user_id) as (db, principal):
        with pytest.raises(AppError) as err:
            approvals.submit(db, principal, **params)
    assert err.value.status_code == 400
    if code:
        assert err.value.code == code


def test_submit_one_pending_request_per_subject_and_reserved_context(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="loan", subject_type="loan", subject_id=1,
                  context={"subject_employee_id": world["emp"]["ada"]["id"], "pending_reason": "x",
                           "principal_amount": Decimal("100.50"), "maker_user_ids": [3, 3, "x"]})
    with _session(cid, u["admin"].user_id) as (db, principal):
        with pytest.raises(AppError) as err:
            approvals.submit(db, principal, module="loan", subject_type="loan", subject_id=1)
        assert err.value.status_code == 409 and err.value.code == "APPROVAL_ALREADY_PENDING"
        request = approvals.pending_request(db, "loan", "loan", 1)
        assert request.id == rid and approvals.latest_request(db, "loan", "loan", 1).id == rid
        # Callers cannot pose as the subject employee or preset the engine's reason.
        assert request.context == {"principal_amount": "100.50", "maker_user_ids": [3],
                                   "pending_reason": approvals.NO_ELIGIBLE_APPROVER}
        with pytest.raises(TypeError):
            approvals.submit(db, principal, module="loan", subject_type="loan", subject_id=2, context={"x": object()})


def test_system_raised_request_has_no_requester_and_is_audited_as_system(world):
    cid, u = world["cid"], world["u"]
    with _session(cid) as (db, _):
        request = approvals.submit(db, None, module="wage_input", subject_type="arrears", subject_id=77,
                                   subject_employee_id=world["emp"]["walt"]["id"], amount="1200.00", currency="INR")
        db.commit()
        rid = request.id
        assert request.requested_by_user_id is None and request.status == "PENDING"
    module, action, actor_type, details = _audit_rows(cid, rid)[0]
    assert (module, action, actor_type) == ("Payroll Inputs", "APPROVAL_REQUESTED", "system")
    assert json.loads(json.dumps(details))["subject_employee_id"] == world["emp"]["walt"]["id"]
    # The founder (payroll manager) holds payroll:inputs:approve and may decide a system request.
    assert _approve(u["admin"], rid).json()["status"] == "APPROVED"


def test_subjects_without_an_employee_use_company_or_target_scope(world):
    cid, u = world["cid"], world["u"]
    rid = _submit(cid, u["admin"].user_id, module="payment_batch", subject_type="batch", subject_id=1,
                  amount="250000.00", currency="INR", context={"legal_entity_id": 1})
    assert _inbox(u["fay"])["items"][0]["id"] == rid
    assert _inbox(u["ada"], mine="false")["total"] == 0  # payroll approvers do not approve payments
    assert _approve(u["fay"], rid).json()["status"] == "APPROVED"


def test_permission_holders_step_type_is_accepted_only_on_request_steps(world):
    cid, u = world["cid"], world["u"]
    with pytest.raises(Exception):
        _chain(cid, "payroll_run", {"type": "permission_holders"})
    rid = _submit(cid, u["admin"].user_id, module="payroll_run", subject_type="run", subject_id=99)
    rows = _sql("SELECT approver_type FROM approval_request_steps WHERE request_id = :r ORDER BY step_order", r=rid)
    assert rows[0][0] == "permission_holders"
