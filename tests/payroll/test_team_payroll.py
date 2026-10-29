"""Team payroll (managers prepare and submit, payroll commits), contractor invoices from approved timesheets and
payroll-cost invoices (owner decisions 2026-10-29).

* Mona (manager role, no ``payroll:`` codes) sees only her reports, never amounts; prepares a batch with a file,
  cannot include herself or people outside her team, cannot decide it; payroll returns it, she edits and resubmits,
  payroll approves -> each line is an APPROVED wage input.
* Mona drafts an invoice for her contractor Carl from his approved week (hours × rate); issuing stays with payroll.
* Finance prepares a cost invoice of a finalized run (fee + tax); another finance user issues it (gapless number);
  PDF; void.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.helpers import create_employee, invite_and_activate
from tests.payroll.test_invoices import inv_world  # noqa: F401 - fixture
from tests.payroll.test_payslips_v2 import docs_world, finalized_run  # noqa: F401 - fixture
from tests.payroll.test_wage_inputs import inputs_world, place, sql, world  # noqa: F401 - fixtures

TEAM = "/api/v1/payroll/team"
BATCHES = f"{TEAM}/batches"
COST = "/api/v1/payroll/cost-invoices"


@pytest.fixture
def team_world(app, inputs_world):
    w = inputs_world
    acme = w["u"]["founder"]
    mona = create_employee(acme, "Mona", hire_date="2024-01-15")
    rita = create_employee(acme, "Rita", manager_id=mona["id"], hire_date="2024-01-15")
    for person in (mona, rita):
        place(person["id"], w["le01"]["id"], w["groups"]["IN"])
    w["mona_id"], w["rita_id"] = mona["id"], rita["id"]
    w["mona"] = invite_and_activate(app, acme, mona, ["employee", "manager"])
    return w


def _batch(client, w, lines, title="March incentives"):
    start, end = w["period"]
    return client.post(BATCHES, json={"title": title, "period_start": start.isoformat(),
                                      "period_end": end.isoformat(), "lines": lines})


def test_manager_prepares_payroll_commits_with_return_and_resubmit(team_world):
    w = team_world
    mona, founder, emma = w["mona"], w["u"]["founder"], w["u"]["emma"]
    # Managers hold no payroll-confidential code (EC-SEC-09): the payroll screens stay closed.
    assert mona.get("/api/v1/payroll/wage-inputs").status_code == 403
    team = mona.get(TEAM)
    assert team.status_code == 200, team.text
    assert [i["name"] for i in team.json()["items"]] == ["Rita Test"]
    assert team.json()["can_submit"] is True and team.json()["amounts"] is False
    assert emma.get(TEAM).status_code == 403
    assert "BONUS" in {c["code"] for c in mona.get(f"{TEAM}/components").json()["items"]}
    line = {"employee_id": w["rita_id"], "component_code": "BONUS", "amount": "2500.00", "reason": "Quarter target"}
    assert _batch(mona, w, [{**line, "employee_id": w["emp"]["Ivan"]}]).status_code == 404  # not her report
    assert _batch(mona, w, [{**line, "employee_id": w["mona_id"]}]).status_code == 403  # own pay
    assert _batch(mona, w, [{**line, "component_code": "BASIC"}]).json()["code"] == "COMPONENT_NOT_FOUND"
    r = _batch(mona, w, [line])
    assert r.status_code == 201 and r.json()["status"] == "DRAFT", r.text
    batch = r.json()
    r = mona.post(f"{BATCHES}/{batch['id']}/files", files={"file": ("proof.pdf", b"%PDF-1.4 proof", "application/pdf")})
    assert r.status_code == 201, r.text
    file_id = r.json()["id"]
    assert mona.get(f"{BATCHES}/{batch['id']}/files/{file_id}").content == b"%PDF-1.4 proof"
    assert mona.post(f"{BATCHES}/{batch['id']}/files",
                     files={"file": ("x.exe", b"MZ", "application/x-msdownload")}).json()["code"] == "FILE_TYPE"
    r = mona.post(f"{BATCHES}/{batch['id']}/submit")
    assert r.status_code == 200 and r.json()["status"] == "SUBMITTED", r.text
    assert mona.post(f"{BATCHES}/{batch['id']}/approve", json={}).status_code == 403  # never her own
    assert mona.patch(f"{BATCHES}/{batch['id']}", json={"notes": "x"}).status_code == 409  # locked while submitted
    queue = founder.get(BATCHES, params={"box": "approvals"}).json()["items"]
    assert [b["id"] for b in queue] == [batch["id"]] and queue[0]["can"]["decide"] is True
    r = founder.post(f"{BATCHES}/{batch['id']}/return", json={"note": "Use the revised target"})
    assert r.status_code == 200 and r.json()["status"] == "RETURNED", r.text
    assert r.json()["decision_note"] == "Use the revised target"
    r = mona.patch(f"{BATCHES}/{batch['id']}", json={"lines": [{**line, "amount": "3000.00"}]})
    assert r.status_code == 200 and r.json()["total_amount"] == "3000.00", r.text
    assert mona.post(f"{BATCHES}/{batch['id']}/submit").json()["status"] == "SUBMITTED"
    r = founder.post(f"{BATCHES}/{batch['id']}/approve", json={"note": "OK"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    inputs = sql("SELECT status, amount, component_code FROM wage_inputs WHERE external_ref LIKE :p",
                 p=f"team-batch-{batch['id']}-%")
    assert [(s, Decimal(a), c) for s, a, c in inputs] == [("APPROVED", Decimal("3000.00"), "BONUS")]
    assert r.json()["lines"][0]["wage_input_id"]
    assert founder.get(BATCHES, params={"box": "approvals"}).json()["items"] == []
    notes = sql("SELECT title FROM notifications WHERE user_id = :u ORDER BY id", u=mona.me()["user"]["id"])
    assert ("Team payroll batch approved",) in [tuple(n) for n in notes]


def test_withdraw_and_reject_need_the_right_person(team_world):
    w = team_world
    mona, founder = w["mona"], w["u"]["founder"]
    line = {"employee_id": w["rita_id"], "component_code": "BONUS", "amount": "100.00", "reason": "Spot award"}
    first = _batch(mona, w, [line], title="Spot awards").json()
    mona.post(f"{BATCHES}/{first['id']}/submit")
    assert founder.post(f"{BATCHES}/{first['id']}/withdraw").status_code == 403
    r = mona.post(f"{BATCHES}/{first['id']}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text
    second = _batch(mona, w, [line], title="Spot awards 2").json()
    mona.post(f"{BATCHES}/{second['id']}/submit")
    r = founder.post(f"{BATCHES}/{second['id']}/reject", json={"note": "Budget closed"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED", r.text
    assert sql("SELECT count(*) FROM wage_inputs WHERE external_ref LIKE 'team-batch-%'")[0][0] == 0


def _contractor(w, name="Carl"):
    acme = w["u"]["founder"]
    carl = create_employee(acme, name, manager_id=w["mona_id"], hire_date="2024-01-15")
    place(carl["id"], w["le01"]["id"], w["groups"]["IN"])
    found = sql("SELECT id FROM employment_types WHERE company_id = :c AND code = 'CONTRACTOR'", c=w["cid"])
    type_id = found[0][0] if found else sql(
        "INSERT INTO employment_types (company_id, code, name, worker_class, status, created_at, updated_at) "
        "VALUES (:c, 'CONTRACTOR', 'Contractor', 'CONTRACTOR', 'ACTIVE', now(), now()) RETURNING id", c=w["cid"])[0][0]
    sql("UPDATE employee_assignments SET employment_type_id = :t WHERE employee_id = :e", t=type_id, e=carl["id"])
    return carl


def _approved_week(cid, employee_id, monday, minutes_per_day=(480, 480)):
    sheet = sql("INSERT INTO attendance_timesheet_approvals (company_id, employee_id, period_kind, period_start, "
                "period_end, status, version, row_version, created_at, updated_at) VALUES (:c, :e, 'WEEK', :s, :t, "
                "'APPROVED', 1, 1, now(), now()) RETURNING id", c=cid, e=employee_id, s=monday,
                t=monday + timedelta(days=6))[0][0]
    for offset, minutes in enumerate(minutes_per_day):
        sql("INSERT INTO attendance_days (company_id, employee_id, shift_date, work_timezone, evaluation_state, "
            "rules_mode, inputs_hash, content_hash, expected_portion, net_minutes, first_half_result, "
            "second_half_result, day_result, evaluated_at, trigger, created_at, updated_at) VALUES (:c, :e, :d, "
            "'Asia/Kolkata', 'FINAL', 'STANDARD', :h, :h, 'FULL', :m, 'PRESENT', 'PRESENT', 'PRESENT', now(), "
            "'PUNCH', now(), now())", c=cid, e=employee_id, d=monday + timedelta(days=offset), h="a" * 64, m=minutes)
    return sheet


def test_manager_drafts_a_contractor_invoice_from_approved_timesheets(team_world):
    w = team_world
    mona, founder = w["mona"], w["u"]["founder"]
    carl = _contractor(w)
    monday = w["period"][0] - timedelta(days=w["period"][0].weekday())
    sheet = _approved_week(w["cid"], carl["id"], monday)
    listed = mona.get(f"{TEAM}/contractors/{carl['id']}/timesheets")
    assert listed.status_code == 200 and [s["id"] for s in listed.json()["items"]] == [sheet], listed.text
    assert mona.get(f"{TEAM}/contractors/{w['emp']['Ivan']}/timesheets").status_code == 404
    body = {"mode": "INVOICE", "payee_employee_id": carl["id"], "timesheet_ids": [sheet], "hourly_rate": "500",
            "currency": "INR", "external_number": "CARL-1"}
    r = mona.post("/api/v1/payroll/invoices", json=body)
    assert r.status_code == 201, r.text
    invoice = r.json()
    assert invoice["status"] == "DRAFT" and invoice["lines"][0]["quantity"] == "16"
    assert invoice["total"]["amount"] == "8000.00"
    again = mona.post("/api/v1/payroll/invoices", json={**body, "external_number": "CARL-2"})
    assert again.status_code == 409 and again.json()["code"] == "TIMESHEET_ALREADY_INVOICED"
    # Employees are never invoiced; managers never issue.
    r = mona.post("/api/v1/payroll/invoices", json={**body, "payee_employee_id": w["rita_id"]})
    assert r.status_code in (404, 409), r.text
    assert mona.post(f"/api/v1/payroll/invoices/{invoice['id']}/issue", json={}).status_code == 403
    assert [i["id"] for i in mona.get(f"{TEAM}/invoices").json()["items"]] == [invoice["id"]]
    assert w["u"]["emma"].get(f"{TEAM}/invoices").status_code == 403


def test_cost_invoice_maker_checker_pdf_and_void(inv_world):
    w = inv_world
    fin, fay = w["fin"], w["fay"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    run_id, _ = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 8, 1), [emma, ivan])
    draft_run, _ = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 9, 1), [emma], status="APPROVED")
    assert fin.get(f"{COST}/preview", params={"run_id": draft_run}).json()["code"] == "RUN_NOT_FINALIZED"
    preview = fin.get(f"{COST}/preview", params={"run_id": run_id, "group_by": "COMPANY"})
    assert preview.status_code == 200, preview.text
    [group] = preview.json()["groups"]
    assert group["employees"] == 2 and group["cost"] == "143600.00"
    r = fin.post(COST, json={"run_id": run_id, "group_by": "COMPANY", "fee_percent": "10",
                             "taxes": [{"label": "GST", "rate_percent": "18"}],
                             "bill_to": {"name": "Acme Holdings", "reference": "PO-9"}})
    assert r.status_code == 201, r.text
    invoice = r.json()
    assert (invoice["fee_total"], invoice["subtotal"], invoice["tax_total"], invoice["total"]) == (
        "14360.00", "157960.00", "28432.80", "186392.80")
    assert invoice["status"] == "DRAFT" and invoice["number"] is None and len(invoice["lines"]) == 2
    assert fin.post(f"{COST}/{invoice['id']}/issue", json={}).status_code == 403  # the preparer never issues
    issued = fay.post(f"{COST}/{invoice['id']}/issue", json={})
    assert issued.status_code == 200, issued.text
    assert issued.json()["status"] == "ISSUED" and issued.json()["number"].endswith("-0001")
    assert fay.post(f"{COST}/{invoice['id']}/issue", json={}).status_code == 409
    pdf = fay.get(f"{COST}/{invoice['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert fay.post(f"{COST}/{invoice['id']}/void", json={"reason": "no"}).status_code == 422
    voided = fay.post(f"{COST}/{invoice['id']}/void", json={"reason": "Billed twice"})
    assert voided.status_code == 200 and voided.json()["status"] == "VOID", voided.text
    assert [i["id"] for i in fin.get(COST, params={"run_id": run_id}).json()["items"]] == [invoice["id"]]
    assert w["logins"]["Emma"].get(COST).status_code == 403
