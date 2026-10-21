"""L18: `python -m app.cli payroll-inventory` (README §8 M0, audit 02 §11, EC-MIG-02).

A read-only, per-tenant inventory of legacy payroll before the v2 migration: rows by status,
month and currency; structures without currency (all of them); clipped nets (D2); processed rows
without payment evidence (all of them, D1); approved rows without an issued payslip (D14); rows
without a legal entity; OT/bonus rows; employees without a structure; recalculation drift.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.attendance_helpers import owner_rows
from tests.helpers import create_employee
from tests.payroll.legacy_fixtures import calc_url, insert_rows, mark, payroll_tenant, structure

COUNTED_TABLES = ("payroll", "issued_documents", "audit_logs", "salary_structure", "employees", "attendance")


def _generate(client, employee_id, year, month, ot="0", bonus="0") -> int:
    r = client.post(calc_url(employee_id, year, month, ot, bonus, action="generate"))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _approve(client, payroll_id) -> None:
    r = client.post(f"/api/v1/payroll/{payroll_id}/approve")
    assert r.status_code == 200, r.text


def _counts() -> dict:
    return {t: owner_rows(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in COUNTED_TABLES}


def _run(capsys, *args) -> str:
    from app.cli import main

    capsys.readouterr()
    main(["payroll-inventory", *args])
    return capsys.readouterr().out


@pytest.fixture
def estate(app):
    """Tenant A with one row of every kind the inventory reports; tenant B with a single draft."""
    a = payroll_tenant(app, "Inventory Alpha")
    b = payroll_tenant(app, "Inventory Beta")
    staff = {name: create_employee(a.admin, name) for name in ("Clipped", "Paid", "Overtime", "Legacy", "Drift")}
    for name, emp in staff.items():
        structure(a.admin, emp["id"], basic_salary="100" if name == "Clipped" else "20000")
    leaver = create_employee(a.admin, "Leaver")
    assert a.admin.post(f"/api/v1/employees/{leaver['id']}/deactivate").status_code == 200

    for day in ("2025-03-03", "2025-03-04"):  # 2 x 100 late penalty > 100 gross -> net clipped to 0
        mark(a.admin, staff["Clipped"]["id"], day, "late")
    ids = {"clipped": _generate(a.preparer, staff["Clipped"]["id"], 2025, 3)}
    ids["processed"] = _generate(a.preparer, staff["Paid"]["id"], 2025, 3)
    _approve(a.approver, ids["processed"])
    assert a.approver.post(f"/api/v1/payroll/{ids['processed']}/process").status_code == 200
    ids["overtime"] = _generate(a.preparer, staff["Overtime"]["id"], 2025, 4, ot="2", bonus="150")
    _approve(a.approver, ids["overtime"])
    ids["legacy_approved"] = _generate(a.preparer, staff["Legacy"]["id"], 2025, 4)
    from app.database.db import system_engine

    with system_engine.begin() as conn:  # approved by a release that predates issued payslips
        conn.execute(text("UPDATE payroll SET status = 'approved' WHERE id = :p"), {"p": ids["legacy_approved"]})
    ids["drift"] = _generate(a.preparer, staff["Drift"]["id"], 2025, 4)
    mark(a.admin, staff["Drift"]["id"], "2025-04-07", "absent")  # attendance corrected after generation
    # A row written without currency or entity for a month before the hire placement (trigger path).
    ids["no_entity"] = insert_rows(a.company_id, [{
        "employee_id": staff["Paid"]["id"], "salary_month": date(2023, 12, 1), "basic_salary": Decimal("20000"),
        "gross_salary": Decimal("20000"), "net_salary": Decimal("20000")}])[0]

    other = create_employee(b.admin, "Elsewhere")
    structure(b.admin, other["id"], basic_salary="5000")
    ids["beta"] = _generate(b.preparer, other["id"], 2025, 3)
    return {"a": a, "b": b, "staff": staff, "leaver": leaver, "ids": ids}


def test_L18_inventory_json_reports_every_legacy_category_EC_MIG_02(estate, capsys):
    """Per tenant: rows by status/month/currency, structures without currency (all), clipped nets (D2),
    processed rows without payment evidence (all, D1), approved rows without a payslip (D14), rows without
    a legal entity, OT/bonus rows, employees without a structure, recalculation drift (report only)."""
    a, b, ids, staff = estate["a"], estate["b"], estate["ids"], estate["staff"]
    report = json.loads(_run(capsys, "--json"))
    companies = {c["company_id"]: c for c in report["companies"]}
    assert set(companies) == {a.company_id, b.company_id}
    alpha = companies[a.company_id]
    assert (alpha["company_name"], alpha["slug"]) == ("Inventory Alpha", "inventory-alpha")

    assert alpha["payroll_rows"] == {
        "total": 6,
        "by_status": {"approved": 2, "draft": 3, "processed": 1},
        "by_currency": {"INR": 6},
        "by_month": {"2023-12": {"draft": 1}, "2025-03": {"draft": 1, "processed": 1},
                     "2025-04": {"approved": 2, "draft": 1}},
    }
    structures = owner_rows("SELECT id FROM salary_structure WHERE company_id = :c ORDER BY id", c=a.company_id)
    assert alpha["salary_structures_without_currency"] == {"count": 5, "structure_ids": [s["id"] for s in structures]}
    assert alpha["clipped_net_rows"] == [{
        "payroll_id": ids["clipped"], "employee_id": staff["Clipped"]["id"], "salary_month": "2025-03-01",
        "currency": "INR", "earnings": "100.00", "total_deductions": "200.00", "net_salary": "0.00",
        "shortfall": "100.00"}]
    assert alpha["processed_without_payment_evidence"] == [ids["processed"]]
    assert alpha["finalized_without_issued_payslip"] == [ids["legacy_approved"]]
    assert alpha["rows_without_legal_entity"] == [ids["no_entity"]]
    assert alpha["rows_with_overtime_or_bonus"] == [ids["overtime"]]
    missing = {e["employee_id"]: e["is_active"] for e in alpha["employees_without_structure"]}
    assert missing == {a.admin.me()["employee"]["id"]: True, a.preparer.employee["id"]: True,
                       a.approver.employee["id"]: True, estate["leaver"]["id"]: False}

    drift = {d["payroll_id"]: d for d in alpha["recalculation_drift"]}
    # The corrected absence (20000 / 22 working days) and the hand-inserted row both differ from today's result.
    assert set(drift) == {ids["drift"], ids["no_entity"]}
    assert drift[ids["drift"]]["stored"] == {"gross_salary": "20000.00", "total_deductions": "0.00",
                                             "net_salary": "20000.00"}
    assert drift[ids["drift"]]["recomputed"] == {"gross_salary": "20000.00", "total_deductions": "909.09",
                                                 "net_salary": "19090.91"}
    assert drift[ids["no_entity"]]["recomputed"] is None  # no structure covers December 2023

    beta = companies[b.company_id]
    assert beta["payroll_rows"]["total"] == 1 and beta["payroll_rows"]["by_status"] == {"draft": 1}
    assert beta["clipped_net_rows"] == [] and beta["processed_without_payment_evidence"] == []
    assert beta["recalculation_drift"] == [] and beta["salary_structures_without_currency"]["count"] == 1
    alpha_ids = {ids[k] for k in ids if k != "beta"}
    for key in ("processed_without_payment_evidence", "finalized_without_issued_payslip", "rows_without_legal_entity",
                "rows_with_overtime_or_bonus"):
        assert set(beta[key]).isdisjoint(alpha_ids) and ids["beta"] not in alpha[key]


def test_L18_inventory_is_read_only_and_filters_one_company(estate, capsys):
    """The inventory writes nothing (READ ONLY transaction through the system session); --company-id limits
    the output to that tenant; an unknown id is an error, never another tenant's data."""
    a, b = estate["a"], estate["b"]
    before = _counts()
    only = json.loads(_run(capsys, "--company-id", str(b.company_id), "--json"))
    assert [c["company_id"] for c in only["companies"]] == [b.company_id]
    _run(capsys)
    assert _counts() == before
    assert owner_rows("SELECT status FROM payroll WHERE id = :p", p=estate["ids"]["legacy_approved"])[0]["status"] == "approved"

    with pytest.raises(SystemExit) as stop:
        _run(capsys, "--company-id", "999999")
    assert "No company with id 999999" in str(stop.value)

    from app.cli import payroll_inventory

    assert [c["company_id"] for c in payroll_inventory(a.company_id)] == [a.company_id]


def test_L18_inventory_refuses_writes_inside_its_transaction(estate, monkeypatch):
    """A write attempted inside the inventory transaction fails: PostgreSQL rejects it as read-only."""
    import app.cli as cli

    def tamper(db, company):
        db.execute(text("UPDATE payroll SET remarks = 'tampered' WHERE company_id = :c"), {"c": company.id})

    monkeypatch.setattr(cli, "_company_payroll_inventory", tamper)
    with pytest.raises(Exception, match="read-only transaction"):
        cli.payroll_inventory(estate["a"].company_id)
    assert owner_rows("SELECT count(*) AS n FROM payroll WHERE remarks IS NOT NULL")[0]["n"] == 0


def test_L18_inventory_text_output(estate, capsys):
    """Human-readable output: one block per tenant with every category and the clipped-row explanation."""
    a, ids = estate["a"], estate["ids"]
    out = _run(capsys, "--company-id", str(a.company_id))
    assert out.startswith(f"Company {a.company_id} - Inventory Alpha (inventory-alpha)\n")
    for line in (
        "  Legacy payroll rows: 6 (approved 2, draft 3, processed 1)",
        "    2025-03: draft 1, processed 1",
        "    2025-04: approved 2, draft 1",
        "  By currency: INR 6",
        "  Salary structures without currency: 5 (the legacy table has no currency column)",
        "  Clipped net rows: 1",
        f"    #{ids['clipped']} 2025-03 INR: deductions 200.00 exceed earnings 100.00 (net stored 0.00, shortfall 100.00)",
        f"  Processed without payment evidence: 1 (#{ids['processed']})",
        f"  Approved/processed without issued payslip: 1 (#{ids['legacy_approved']})",
        f"  Rows without legal entity: 1 (#{ids['no_entity']})",
        f"  Rows with overtime or bonus: 1 (#{ids['overtime']})",
        "  Recalculation drift (report only): 2",
        f"    #{ids['drift']} 2025-04: stored net 20000.00, today 19090.91",
        f"    #{ids['no_entity']} 2023-12: stored net 20000.00, no salary structure covers the month today",
    ):
        assert line + "\n" in out, line
    assert "Employees without salary structure: 4 (" in out and "(inactive)" in out
    assert "Inventory Beta" not in out


def test_L18_inventory_of_a_tenant_without_payroll(app, capsys):
    """A tenant that never ran payroll is listed with zero counts (no division, no error)."""
    fresh = payroll_tenant(app, "Inventory Empty")
    report = json.loads(_run(capsys, "--company-id", str(fresh.company_id), "--json"))["companies"][0]
    assert report["payroll_rows"] == {"total": 0, "by_status": {}, "by_currency": {}, "by_month": {}}
    assert report["clipped_net_rows"] == [] and report["recalculation_drift"] == []
    assert report["salary_structures_without_currency"] == {"count": 0, "structure_ids": []}
    assert len(report["employees_without_structure"]) == 3
    out = _run(capsys, "--company-id", str(fresh.company_id))
    assert "  Legacy payroll rows: 0\n" in out and "By currency" not in out
