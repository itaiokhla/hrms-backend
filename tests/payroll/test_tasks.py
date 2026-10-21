"""Tasks module: lifecycle, assignees and splits, evidence, approval, scope, rate cards, payroll reads, and the
row-level isolation of every overtime/task table across three tenants (3.4 §10, 3.1 §5.6, 3.9 §3.5/§4).

EC-TSK-01, EC-TSK-02, EC-TSK-04, EC-TSK-05, EC-TSK-10, EC-TSK-12, EC-SEC-01, EC-SEC-05.
"""

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.payroll.services import tasks as task_service
from tests.helpers import create_employee, invite_and_activate, signup_company

BASE = "/api/v1/tasks"
RATE_CARDS = "/api/v1/payroll/task-rate-cards"
POLICIES = "/api/v1/payroll/policies"
VALUES = "/api/v1/payroll/settings/values"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
CHECK, UNIQUE, FOREIGN_KEY, DENIED = "23514", "23505", "23503", "42501"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


@contextmanager
def session(company_id):
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        yield db
    finally:
        db.close()


@contextmanager
def tenant(company_id):
    from app.database.db import engine

    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"),
                     {"c": "" if company_id is None else str(company_id)})
        yield conn


def expect_error(conn, code, sql_text, **params):
    with pytest.raises(DBAPIError) as info:
        with conn.begin_nested():
            conn.execute(text(sql_text), params)
    assert info.value.orig.pgcode == code, info.value.orig


def today_of(company_id):
    from app.core.clock import company_today

    with session(company_id) as db:
        return company_today(db, company_id)


def new_task(client, employees, **extra):
    assignees = [{"employee_id": e} if isinstance(e, int) else e for e in employees]
    body = {"title": "Install routers", "task_type": "SITE_INSTALL", "quantity": "12", "unit": "installations",
            "assignees": assignees, **extra}
    return client.post(BASE, json=body)


def created_task(client, employees, **extra):
    r = new_task(client, employees, **extra)
    assert r.status_code == 201, r.text
    return r.json()


def task(client, task_id):
    r = client.get(f"{BASE}/{task_id}")
    assert r.status_code == 200, r.text
    return r.json()


def upload(client, task_id, data=PDF, name="proof.pdf"):
    return client.post(f"{BASE}/{task_id}/evidence", files={"file": (name, data, "application/octet-stream")})


def submitted_task(client, employees, **extra):
    created = created_task(client, employees, **extra)
    assert upload(client, created["id"]).status_code == 201
    r = client.post(f"{BASE}/{created['id']}/submit")
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, task_id, **body):
    return client.post(f"{BASE}/{task_id}/approve", json=body or None)


def approved_rows(company_id, employee_id, start, end, attribution="APPROVAL_DATE"):
    with session(company_id) as db:
        return task_service.approved_tasks(db, company_id, employee_id, start, end, attribution)


def audit_actions(company_id, task_id):
    return [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = 'task' "
                              "AND entity_id = :e ORDER BY id", c=company_id, e=str(task_id))]


def task_policy(client, **fields):
    """A new active version of the starter task pay policy from today."""
    policy = next(p for p in client.get(POLICIES, params={"type": "TASK_PAY_POLICY"}).json())
    r = client.post(f"{POLICIES}/{policy['id']}/versions", json={"fields": fields})
    assert r.status_code == 201, r.text
    r = client.post(f"{POLICIES}/{policy['id']}/versions/{r.json()['version']}/activate")
    assert r.status_code == 200, r.text


@pytest.fixture
def world(app):
    """Acme: founder admin (task:manage); manager Mona -> Walt and Wendy; Lou outside the team. Beta: its admin."""
    admin = signup_company(app, "Acme Corp")
    beta = signup_company(app, "Beta Industries")
    mona = create_employee(admin, "Mona")
    walt = create_employee(admin, "Walt", manager_id=mona["id"])
    wendy = create_employee(admin, "Wendy", manager_id=mona["id"])
    lou = create_employee(admin, "Lou")
    users = {
        "admin": admin,
        "beta": beta,
        "mona": invite_and_activate(app, admin, mona, ["manager"]),
        "walt": invite_and_activate(app, admin, walt, ["employee"]),
        "lou": invite_and_activate(app, admin, lou, ["employee"]),
    }
    cid = admin.company_id
    return {"cid": cid, "today": today_of(cid), "u": users,
            "emp": {k: v["id"] for k, v in {"mona": mona, "walt": walt, "wendy": wendy, "lou": lou}.items()}}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def test_ec_tsk_12_task_lifecycle_with_evidence_partial_acceptance_and_snapshot_read(world):
    u, emp, cid, today = world["u"], world["emp"], world["cid"], world["today"]
    created = created_task(u["admin"], [emp["walt"]], rate={"amount": "800.00", "currency": "INR"},
                           external_ref="JOB-1", completed_on=today.isoformat())
    detail = task(u["admin"], created["id"])
    assert detail["status"] == "DRAFT" and detail["quantity"] == "12" and detail["rate"] == {"amount": "800.00",
                                                                                             "currency": "INR"}
    assert detail["assignees"][0]["split_percent"] == "100.00"
    r = u["admin"].post(f"{BASE}/{created['id']}/submit")
    assert r.status_code == 400 and r.json()["code"] == "EVIDENCE_REQUIRED"  # the starter policy requires evidence

    assert upload(u["admin"], created["id"], b"MZ\x90\x00 not a document", "x.exe").json()["code"] == \
        "UNSUPPORTED_FILE_TYPE"
    r = upload(u["admin"], created["id"])
    assert r.status_code == 201, r.text
    evidence_id = r.json()["id"]
    assert upload(u["admin"], created["id"]).json()["code"] == "DUPLICATE_EVIDENCE"
    assert upload(u["walt"], created["id"], PNG, "p.png").status_code == 403  # task:read only

    r = u["admin"].post(f"{BASE}/{created['id']}/submit")
    assert r.status_code == 200 and r.json()["status"] == "SUBMITTED", r.text
    assert u["admin"].patch(f"{BASE}/{created['id']}", json={"title": "Late edit"}).json()["code"] == \
        "TASK_NOT_EDITABLE"
    assert u["admin"].delete(f"{BASE}/{created['id']}/evidence/{evidence_id}").json()["code"] == "TASK_NOT_EDITABLE"
    assert approved_rows(cid, emp["walt"], today, today) == []  # submitted is not payable (EC-TSK-02)

    awaiting = u["mona"].get(BASE, params={"awaiting": True}).json()
    assert [t["id"] for t in awaiting["items"]] == [created["id"]]
    r = approve(u["mona"], created["id"], accepted_quantity="10", remarks="2 not done")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED" and r.json()["accepted_quantity"] == "10"

    rows = approved_rows(cid, emp["walt"], today, today)
    assert len(rows) == 1
    row = rows[0]
    assert (row["id"], row["status"], row["quantity"], row["accepted_quantity"], row["rate"], row["rate_currency"]) \
        == (created["id"], "APPROVED", "12.0000", "10.0000", "800.0000", "INR")
    assert row["assignees"] == [{"employee_id": str(emp["walt"]), "split_percent": "100.00"}]
    assert row["approved_on"] == today.isoformat() and row["evidence_count"] == 1 and len(row["content_hash"]) == 64
    assert approved_rows(cid, emp["walt"], today, today, "COMPLETION_DATE")[0]["attribution_date"] == today.isoformat()
    assert approved_rows(cid, emp["walt"], today + timedelta(days=1), today + timedelta(days=30)) == []
    assert approved_rows(cid, emp["lou"], today, today) == []

    r = u["walt"].get(f"{BASE}/{created['id']}/evidence/{evidence_id}")
    assert r.status_code == 200 and r.content == PDF and r.headers["content-type"] == "application/pdf"
    assert u["lou"].get(f"{BASE}/{created['id']}/evidence/{evidence_id}").status_code == 404
    assert u["beta"].get(f"{BASE}/{created['id']}/evidence/{evidence_id}").status_code == 404
    actions = audit_actions(cid, created["id"])
    assert {"TASK_CREATED", "TASK_EVIDENCE_ADDED", "TASK_SUBMITTED", "TASK_PARTIALLY_ACCEPTED", "TASK_APPROVED"} \
        <= set(actions)
    logged = sql("SELECT change, employee_id, date_from FROM source_change_log WHERE company_id = :c "
                 "AND record_type = 'task' AND record_id = :t", c=cid, t=created["id"])
    assert [tuple(r) for r in logged] == [("UPDATED", emp["walt"], today)]  # draft runs see the approval


def test_ec_tsk_04_accepted_quantity_never_exceeds_the_submitted_quantity(world):
    u, emp, cid = world["u"], world["emp"], world["cid"]
    submitted = submitted_task(u["admin"], [emp["walt"]])
    r = approve(u["mona"], submitted["id"], accepted_quantity="12.0001")
    assert r.status_code == 400 and r.json()["code"] == "ACCEPTED_EXCEEDS_SUBMITTED"
    assert approve(u["mona"], submitted["id"], accepted_quantity=10).status_code == 422  # JSON number
    assert approve(u["mona"], submitted["id"], accepted_quantity="-1").json()["code"] == "INVALID_QUANTITY"
    assert task(u["admin"], submitted["id"])["accepted_quantity"] is None
    with tenant(cid) as conn:
        expect_error(conn, CHECK, "UPDATE tasks SET accepted_quantity = quantity + 1 WHERE id = :i", i=submitted["id"])
        expect_error(conn, CHECK, "UPDATE tasks SET quantity = 0 WHERE id = :i", i=submitted["id"])
    r = approve(u["mona"], submitted["id"])
    assert r.status_code == 200 and r.json()["accepted_quantity"] == "12"  # nothing given: all accepted


def test_ec_tsk_02_rejected_task_is_never_paid_and_cannot_be_approved_later(world):
    u, emp, cid, today = world["u"], world["emp"], world["cid"], world["today"]
    submitted = submitted_task(u["admin"], [emp["walt"]])
    assert u["mona"].post(f"{BASE}/{submitted['id']}/reject", json={}).status_code == 422
    r = u["mona"].post(f"{BASE}/{submitted['id']}/reject", json={"remarks": "Wrong site"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    detail = task(u["walt"], submitted["id"])
    assert detail["status_reason"] == "Wrong site" and detail["approved_on"] is None
    assert approve(u["admin"], submitted["id"]).json()["code"] == "TASK_NOT_PENDING"
    assert approved_rows(cid, emp["walt"], today - timedelta(days=5), today) == []
    assert "TASK_REJECTED" in audit_actions(cid, submitted["id"])


def test_disputed_task_is_excluded_until_an_approver_resolves_it(world):
    u, emp, cid, today = world["u"], world["emp"], world["cid"], world["today"]
    submitted = submitted_task(u["admin"], [emp["walt"]])
    assert u["mona"].post(f"{BASE}/{submitted['id']}/dispute", json={}).status_code == 422
    assert u["walt"].post(f"{BASE}/{submitted['id']}/dispute", json={"reason": "x"}).status_code == 403
    assert u["lou"].post(f"{BASE}/{submitted['id']}/dispute", json={"reason": "x"}).status_code == 404
    r = u["mona"].post(f"{BASE}/{submitted['id']}/dispute", json={"reason": "Customer complaint"})
    assert r.status_code == 200 and r.json()["status"] == "DISPUTED"
    assert u["mona"].post(f"{BASE}/{submitted['id']}/dispute", json={"reason": "again"}).status_code == 409
    assert approved_rows(cid, emp["walt"], today, today) == []
    assert approve(u["mona"], submitted["id"]).json()["status"] == "APPROVED"
    assert [r["id"] for r in approved_rows(cid, emp["walt"], today, today)] == [submitted["id"]]
    assert "TASK_DISPUTED" in audit_actions(cid, submitted["id"])


def test_submitted_task_cannot_be_withdrawn_and_drafts_edit_with_row_versions(world):
    u, emp = world["u"], world["emp"]
    draft = created_task(u["admin"], [emp["walt"]])
    r = u["admin"].patch(f"{BASE}/{draft['id']}", json={"quantity": "8", "row_version": 7})
    assert r.status_code == 409 and r.json()["code"] == "STALE_ROW_VERSION"
    r = u["admin"].patch(f"{BASE}/{draft['id']}", json={"quantity": "8", "row_version": draft["row_version"]})
    assert r.status_code == 200 and r.json()["row_version"] == draft["row_version"] + 1
    assert task(u["admin"], draft["id"])["quantity"] == "8"
    assert u["admin"].patch(f"{BASE}/{draft['id']}", json={"status": "APPROVED"}).status_code == 422
    assert u["admin"].patch(f"{BASE}/{draft['id']}", json={"quantity": 8}).status_code == 422
    assert u["admin"].patch(f"{BASE}/{draft['id']}", json={"assignees": None}).status_code == 422

    submitted = submitted_task(u["admin"], [emp["walt"]])
    r = u["admin"].post(f"/api/v1/payroll/approvals/{submitted['approval_request_id']}/cancel")
    assert r.status_code == 409 and r.json()["code"] == "TASK_CANNOT_BE_WITHDRAWN"
    assert task(u["admin"], submitted["id"])["status"] == "SUBMITTED"
    assert u["admin"].post(f"{BASE}/{submitted['id']}/submit").status_code == 409


def test_paid_task_is_reversed_by_an_approver_never_by_the_assignee(world):
    u, emp, cid, today = world["u"], world["emp"], world["cid"], world["today"]
    submitted = submitted_task(u["admin"], [emp["walt"]])
    assert u["mona"].post(f"{BASE}/{submitted['id']}/reverse", json={"reason": "x"}).json()["code"] == \
        "INVALID_TRANSITION"  # not paid yet
    assert approve(u["mona"], submitted["id"]).status_code == 200
    with session(cid) as db:
        assert task_service.mark_tasks_paid(db, cid, [submitted["id"]], run_id=77) == 1
        assert task_service.mark_tasks_paid(db, cid, [submitted["id"]], run_id=77) == 0  # exactly once
        db.commit()
    detail = task(u["walt"], submitted["id"])
    assert detail["status"] == "PAID" and detail["paid_in_run_id"] == 77
    assert approved_rows(cid, emp["walt"], today, today) == []  # paid tasks are not paid again
    assert u["walt"].post(f"{BASE}/{submitted['id']}/reverse", json={"reason": "x"}).status_code == 403
    r = u["mona"].post(f"{BASE}/{submitted['id']}/reverse", json={"reason": "Customer refund"})
    assert r.status_code == 200 and r.json()["status"] == "REVERSED"
    assert {"TASK_PAID", "TASK_REVERSED"} <= set(audit_actions(cid, submitted["id"]))


# ---------------------------------------------------------------------------
# Rules: external references, splits, evidence, currency
# ---------------------------------------------------------------------------
def test_ec_tsk_01_external_ref_is_unique_per_tenant(world):
    u, emp = world["u"], world["emp"]
    created_task(u["admin"], [emp["walt"]], external_ref="IMPORT-42")
    r = new_task(u["admin"], [emp["wendy"]], external_ref="IMPORT-42")
    assert r.status_code == 409 and r.json()["code"] == "DUPLICATE_EXTERNAL_REF"
    beta_employee = create_employee(u["beta"], "Bea")
    created_task(u["beta"], [beta_employee["id"]], external_ref="IMPORT-42")  # another tenant may reuse it
    with tenant(world["cid"]) as conn:
        expect_error(conn, UNIQUE, "INSERT INTO tasks (company_id, title, task_type, quantity, unit, external_ref) "
                                   "VALUES (:c, 't', 'X', 1, 'u', 'IMPORT-42')", c=world["cid"])


def test_ec_tsk_05_split_percentages_must_total_exactly_100_when_splitting(world):
    u, emp, cid, today = world["u"], world["emp"], world["cid"], world["today"]
    r = new_task(u["admin"], [{"employee_id": emp["walt"], "split_percent": "50"},
                              {"employee_id": emp["wendy"], "split_percent": "50"}])
    assert r.status_code == 400 and r.json()["code"] == "SPLIT_NOT_ALLOWED"  # starter policy: no splitting
    r = new_task(u["admin"], [{"employee_id": emp["walt"], "split_percent": "90"}])
    assert r.status_code == 400 and r.json()["code"] == "INVALID_SPLIT"
    task_policy(u["admin"], allow_split=True, requires_evidence=False)
    for percents in (("60", "30"), ("60", "40.01"), ("33.333", "66.667"), ("0", "100")):
        r = new_task(u["admin"], [{"employee_id": emp["walt"], "split_percent": percents[0]},
                                  {"employee_id": emp["wendy"], "split_percent": percents[1]}])
        assert r.status_code == 400 and r.json()["code"] == "INVALID_SPLIT", (percents, r.text)
    r = new_task(u["admin"], [emp["walt"], emp["walt"]])
    assert r.status_code == 400 and r.json()["code"] == "INVALID_SPLIT"
    split = created_task(u["admin"], [{"employee_id": emp["walt"], "split_percent": "60"},
                                      {"employee_id": emp["wendy"], "split_percent": "40.00"}])
    assert u["admin"].post(f"{BASE}/{split['id']}/submit").status_code == 200  # no evidence needed now
    assert approve(u["mona"], split["id"]).status_code == 200
    logged = sql("SELECT employee_id FROM source_change_log WHERE company_id = :c AND record_type = 'task' "
                 "AND record_id = :t", c=cid, t=split["id"])
    assert sorted(r[0] for r in logged) == sorted([emp["walt"], emp["wendy"]])  # one row per assignee
    walt_row = approved_rows(cid, emp["walt"], today, today)[0]
    wendy_row = approved_rows(cid, emp["wendy"], today, today)[0]
    assert walt_row["split_percent"] == "60.00" and wendy_row["split_percent"] == "40.00"
    assert walt_row["assignees"] == wendy_row["assignees"]
    with tenant(cid) as conn:
        expect_error(conn, CHECK, "UPDATE task_assignees SET split_percent = 0 WHERE task_id = :t", t=split["id"])


def test_split_task_needs_a_final_approver_covering_every_assignee_and_no_assignee_approves(app, world):
    u, emp = world["u"], world["emp"]
    hana = invite_and_activate(app, u["admin"], create_employee(u["admin"], "Hana"), ["hr_admin"])
    task_policy(u["admin"], allow_split=True, requires_evidence=False)
    across = created_task(u["admin"], [{"employee_id": emp["walt"], "split_percent": "70"},
                                       {"employee_id": emp["lou"], "split_percent": "30"}])
    u["admin"].post(f"{BASE}/{across['id']}/submit")
    r = approve(u["mona"], across["id"])  # Mona manages Walt (primary assignee) but not Lou
    assert r.status_code == 403
    assert task(u["admin"], across["id"])["status"] == "SUBMITTED"
    assert approve(u["admin"], across["id"]).status_code == 403  # the submitter never approves
    assert approve(hana, across["id"]).json()["status"] == "APPROVED"  # task:approve for the whole company

    own = created_task(u["admin"], [{"employee_id": emp["mona"], "split_percent": "50"},
                                    {"employee_id": emp["walt"], "split_percent": "50"}])
    u["admin"].post(f"{BASE}/{own['id']}/submit")
    assert approve(u["mona"], own["id"]).status_code == 403  # an assignee never approves a task paying them
    assert u["mona"].post(f"{BASE}/{own['id']}/reject", json={"remarks": "x"}).status_code == 403
    assert u["mona"].post(f"{BASE}/{own['id']}/dispute", json={"reason": "x"}).status_code == 403
    assert task(u["admin"], own["id"])["status"] == "SUBMITTED"
    assert approve(hana, own["id"]).json()["status"] == "APPROVED"


def test_ec_tsk_10_task_rate_in_another_currency_than_the_pay_group_is_flagged(world):
    u, emp, cid = world["u"], world["emp"], world["cid"]
    currency = sql("SELECT pg.currency FROM employee_assignments a JOIN pay_groups pg ON pg.id = a.pay_group_id "
                   "WHERE a.company_id = :c AND a.employee_id = :e AND a.is_primary", c=cid, e=emp["walt"])
    if not currency:
        entity = sql("SELECT legal_entity_id FROM employee_assignments WHERE company_id = :c AND employee_id = :e",
                     c=cid, e=emp["walt"])[0][0]
        group = sql("INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency) "
                    "VALUES (:c, :le, 'TSK10', 'TSK10', 'INR', 'MONTHLY') RETURNING id", c=cid, le=entity)[0][0]
        sql("UPDATE employee_assignments SET pay_group_id = :g WHERE company_id = :c AND employee_id = :e",
            g=group, c=cid, e=emp["walt"])
        currency = [("INR",)]
    pay_currency = currency[0][0]
    other = "USD" if pay_currency != "USD" else "EUR"
    same = created_task(u["admin"], [emp["walt"]], rate={"amount": "10.00", "currency": pay_currency})
    foreign = created_task(u["admin"], [emp["walt"]], rate={"amount": "10.125", "currency": other})
    assert task(u["admin"], same["id"])["warnings"] == []
    warnings = task(u["admin"], foreign["id"])["warnings"]
    assert [(w["code"], w["employee_id"]) for w in warnings] == [("CURRENCY_MISMATCH", emp["walt"])]
    assert task(u["admin"], foreign["id"])["rate"] == {"amount": "10.125", "currency": other}
    for bad in ({"amount": 10, "currency": "USD"}, {"amount": "-1.00", "currency": "USD"},
                {"amount": "1.00", "currency": "XAU"}, {"amount": "1.00", "currency": "ZZZ"}):
        assert new_task(u["admin"], [emp["walt"]], rate=bad).status_code == 422, bad
    assert new_task(u["admin"], [emp["walt"]], rate={"amount": "1.00001", "currency": "USD"}).json()["code"] == \
        "AMOUNT_NOT_STORABLE"


# ---------------------------------------------------------------------------
# Switch, permissions and scope
# ---------------------------------------------------------------------------
def test_tasks_enabled_switch_gates_the_module(world):
    u, emp = world["u"], world["emp"]
    created = created_task(u["admin"], [emp["walt"]])
    r = u["admin"].put(VALUES, json={"changes": [
        {"key": "payroll.task_pay.enabled", "value": False, "row_version": 1},
        {"key": "tasks.enabled", "value": False, "row_version": 1}]})
    assert r.status_code == 200, r.text
    for response in (u["admin"].get(BASE), u["admin"].get(f"{BASE}/{created['id']}"),
                     new_task(u["admin"], [emp["walt"]]), u["admin"].post(f"{BASE}/{created['id']}/submit")):
        assert response.status_code == 403 and response.json()["code"] == "TASKS_DISABLED"


def test_ec_sec_05_scope_decides_who_sees_creates_and_approves_tasks(world):
    u, emp = world["u"], world["emp"]
    walt_task = created_task(u["admin"], [emp["walt"]])
    lou_task = created_task(u["admin"], [emp["lou"]])
    assert new_task(u["mona"], [emp["walt"]]).status_code == 403  # no task:manage
    assert new_task(u["walt"], [emp["walt"]]).status_code == 403
    assert new_task(u["admin"], [emp["walt"], 999999]).status_code == 404
    listing = u["mona"].get(BASE).json()
    assert {t["id"] for t in listing["items"]} == {walt_task["id"]} and listing["total"] == 1
    assert u["mona"].get(f"{BASE}/{lou_task['id']}").status_code == 404
    assert {t["id"] for t in u["walt"].get(BASE).json()["items"]} == {walt_task["id"]}
    assert u["walt"].get(f"{BASE}/{lou_task['id']}").status_code == 404
    assert u["lou"].get(BASE, params={"employee_id": emp["walt"]}).json()["total"] == 0
    assert u["beta"].get(BASE).json()["total"] == 0
    assert u["beta"].get(f"{BASE}/{walt_task['id']}").status_code == 404
    assert u["beta"].patch(f"{BASE}/{walt_task['id']}", json={"title": "x"}).status_code == 404
    assert u["beta"].post(f"{BASE}/{walt_task['id']}/submit").status_code == 404
    assert new_task(u["beta"], [emp["walt"]]).status_code == 404


# ---------------------------------------------------------------------------
# Rate cards
# ---------------------------------------------------------------------------
def test_rate_cards_are_dated_never_overlap_and_feed_the_snapshot(world):
    u, cid = world["u"], world["cid"]
    branch_code = sql("SELECT code FROM branches WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0][0]
    body = {"task_type": "SITE_INSTALL", "rate": {"amount": "800.00", "currency": "INR"},
            "effective_from": "2026-01-01"}
    r = u["admin"].post(RATE_CARDS, json=body)
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["rate"] == {"amount": "800.00", "currency": "INR"} and first["criteria"] == {}
    r = u["admin"].post(RATE_CARDS, json={**body, "effective_from": "2026-06-01"})
    assert r.status_code == 409 and r.json()["code"] == "RATE_CARD_OVERLAP"
    located = u["admin"].post(RATE_CARDS, json={**body, "criteria": {"location": branch_code},
                                                "rate": {"amount": "850.00", "currency": "INR"}})
    assert located.status_code == 201, located.text
    assert u["admin"].post(RATE_CARDS, json={**body, "criteria": {"grade": "NOPE"}}).json()["code"] == "UNKNOWN_GRADE"
    assert u["admin"].post(RATE_CARDS, json={**body, "criteria": {"colour": "red"}}).status_code == 422
    assert u["admin"].post(RATE_CARDS, json={**body, "rate": {"amount": 800, "currency": "INR"}}).status_code == 422
    assert u["admin"].post(RATE_CARDS, json={**body, "effective_to": "2025-01-01"}).json()["code"] == "INVALID_RANGE"

    r = u["admin"].patch(f"{RATE_CARDS}/{first['id']}", json={"effective_to": "2026-05-31"})
    assert r.status_code == 200 and r.json()["effective_to"] == "2026-05-31"
    assert u["admin"].patch(f"{RATE_CARDS}/{first['id']}", json={"effective_to": "2026-07-01"}).json()["code"] == \
        "RATE_CARD_EXTENSION"
    successor = u["admin"].post(RATE_CARDS, json={**body, "effective_from": "2026-06-01",
                                                  "rate": {"amount": "900.00", "currency": "INR"}})
    assert successor.status_code == 201, successor.text

    listing = u["admin"].get(RATE_CARDS, params={"task_type": "SITE_INSTALL", "on": "2026-06-15"}).json()["items"]
    assert sorted(i["rate"]["amount"] for i in listing) == ["850.00", "900.00"]
    with session(cid) as db:
        rows = task_service.rate_card_rows(db, cid, date(2026, 6, 1), date(2026, 6, 30), ["SITE_INSTALL"])
    assert sorted((r["rate"], r["location"]) for r in rows) == [("850.0000", branch_code), ("900.0000", None)]

    assert u["mona"].get(RATE_CARDS).status_code == 403
    assert u["walt"].post(RATE_CARDS, json=body).status_code == 403
    assert u["beta"].get(RATE_CARDS).json()["items"] == []
    assert u["beta"].patch(f"{RATE_CARDS}/{first['id']}", json={"effective_to": "2026-02-01"}).status_code == 404
    assert "TASK_RATE_CARD_ENDED" in [a for (a,) in sql(
        "SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = 'task_rate_card'", c=cid)]


# ---------------------------------------------------------------------------
# Row-level isolation of the overtime and task tables (three tenants)
# ---------------------------------------------------------------------------
TABLES = ("overtime_entries", "tasks", "task_assignees", "task_evidence", "task_rate_cards")


def _seed(conn, company_id, employee_id):
    def insert(table, **values):
        columns = ", ".join(values)
        marks = ", ".join(f":{k}" for k in values)
        return conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id"), values).scalar()

    ids = {"overtime_entries": insert("overtime_entries", company_id=company_id, employee_id=employee_id,
                                      date="2026-10-01", minutes_requested=60)}
    ids["tasks"] = insert("tasks", company_id=company_id, title="t", task_type="X", quantity=1, unit="u")
    ids["task_assignees"] = insert("task_assignees", company_id=company_id, task_id=ids["tasks"],
                                   employee_id=employee_id)
    ids["task_evidence"] = insert("task_evidence", company_id=company_id, task_id=ids["tasks"], file_name="f.pdf",
                                  storage_path=f"{company_id}/task-evidence/x.pdf", mime="application/pdf", bytes=10,
                                  sha256="a" * 64)
    ids["task_rate_cards"] = insert("task_rate_cards", company_id=company_id, task_type="X", rate=1, currency="INR",
                                    effective_from="2026-01-01")
    return ids


def test_ec_sec_01_overtime_and_task_tables_are_isolated_across_three_tenants(three_companies):
    from app.models import TENANT_TABLES

    assert set(TABLES) <= set(TENANT_TABLES)
    companies = [three_companies[k].company_id for k in ("A", "B", "C")]
    employees = {cid: create_employee(three_companies[k], "Iso")["id"]
                 for k, cid in zip(("A", "B", "C"), companies)}
    seeded = {}
    for cid in companies:
        with tenant(cid) as conn:
            seeded[cid] = _seed(conn, cid, employees[cid])

    with tenant(None) as conn:
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES} == {t: 0 for t in TABLES}
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in TABLES:
                visible = conn.execute(text(f"SELECT id, company_id FROM {table}")).fetchall()
                assert {owner for _, owner in visible} == {cid}, table
                for other in others:
                    foreign = seeded[other][table]
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"), {"i": foreign}).scalar() == 0
                    assert conn.execute(text(f"UPDATE {table} SET updated_at = now() WHERE id = :i"),
                                        {"i": foreign}).rowcount == 0, table
                    assert conn.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": foreign}).rowcount == 0, table
                expect_error(conn, DENIED, f"UPDATE {table} SET company_id = :o WHERE id = :i",
                             o=others[0], i=seeded[cid][table])
    with tenant(companies[0]) as conn:  # composite keys refuse references into another tenant even when RLS allows
        b = companies[1]
        expect_error(conn, FOREIGN_KEY, "INSERT INTO task_assignees (company_id, task_id, employee_id) "
                                        "VALUES (:c, :t, :e)", c=companies[0], t=seeded[b]["tasks"],
                     e=employees[companies[0]])
        expect_error(conn, FOREIGN_KEY, "INSERT INTO task_assignees (company_id, task_id, employee_id) "
                                        "VALUES (:c, :t, :e)", c=companies[0], t=seeded[companies[0]]["tasks"],
                     e=employees[b])
        expect_error(conn, FOREIGN_KEY, "INSERT INTO overtime_entries (company_id, employee_id, date, "
                                        "minutes_requested) VALUES (:c, :e, '2026-10-02', 10)",
                     c=companies[0], e=employees[b])
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        for cid in companies:
            for table in TABLES:
                assert conn.execute(text(f"SELECT company_id FROM {table} WHERE id = :i"),
                                    {"i": seeded[cid][table]}).scalar() == cid
