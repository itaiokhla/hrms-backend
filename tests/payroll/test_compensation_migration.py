"""Legacy salary_structure -> compensation v2 migration (3.3 §10, migration 0015, EC-MIG-01).

The test database is already at head, so these tests insert legacy rows and run the migration's
idempotent backfill (the same function the upgrade calls) on the owner connection.
"""

import importlib.util
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.helpers import create_employee, signup_company
from tests.payroll.test_compensation import employee_url
from tests.payroll.test_components import COMPONENTS, STRUCTURES, ok, seed
from tests.payroll.test_settings_engine import sql, today_of

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0015_compensation_v2.py"
COLUMNS = ("basic_salary", "house_rent_allowance", "dearness_allowance", "conveyance_allowance", "other_allowances")


def migration():
    spec = importlib.util.spec_from_file_location("migration_0015_compensation_v2", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backfill():
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        return migration().backfill(conn)


def legacy_row(company_id, employee_id, start, end=None, amounts=("30000", "12000", "3000", "1600", "2400")):
    sql("INSERT INTO salary_structure (company_id, employee_id, basic_salary, house_rent_allowance, dearness_allowance, "
        "conveyance_allowance, other_allowances, effective_from, effective_to, created_at, updated_at) "
        "VALUES (:c, :e, :b, :h, :d, :v, :o, :s, :t, now(), now())",
        c=company_id, e=employee_id, b=amounts[0], h=amounts[1], d=amounts[2], v=amounts[3], o=amounts[4], s=start,
        t=end)
    return sql("SELECT max(id) FROM salary_structure WHERE company_id = :c", c=company_id)[0][0]


def migrated(company_id):
    return {row[0]: row for row in sql(
        "SELECT legacy_salary_structure_id, id, status, reason, currency, pay_basis_amount, effective_from, "
        "effective_to, approved_at IS NOT NULL, approved_by_user_id FROM compensation_assignments "
        "WHERE company_id = :c AND legacy_salary_structure_id IS NOT NULL", c=company_id)}


def legacy_snapshot(company_id):
    return sql("SELECT id, employee_id, basic_salary, house_rent_allowance, dearness_allowance, conveyance_allowance, "
               "other_allowances, effective_from, effective_to, updated_at FROM salary_structure WHERE company_id = :c "
               "ORDER BY id", c=company_id)


@pytest.fixture
def acme(app):
    return signup_company(app, "Acme Corp")


def test_migration_creates_components_structure_and_one_assignment_per_row(acme):
    cid = acme.company_id
    worker = create_employee(acme, "Lee", "Legacy")
    row_id = legacy_row(cid, worker["id"], date(2024, 2, 1))
    before = legacy_snapshot(cid)
    base_before = sql("SELECT base_salary FROM employees WHERE id = :e", e=worker["id"])[0][0]
    summary = backfill()
    assert summary["assignments"] >= 1 and summary["templates"] >= 1
    components = {c["code"]: c for c in ok(acme.get(COMPONENTS))}
    for code in ("BASIC", "HRA", "DA", "CONVEYANCE", "OTHER_ALLOWANCE"):
        version = components[code]["current_version"]
        assert version["calc_method"] == "FIXED" and version["proration"] == "BY_PAID_DAYS", code
        assert version["tags"] == ["LOP_APPLICABLE"] and components[code]["kind"] == "EARNING", code
    structures = {s["code"]: s for s in ok(acme.get(STRUCTURES))}
    legacy = structures["LEGACY_FIXED"]
    assert legacy["name"] == "Legacy fixed structure" and legacy["type"] == "GROSS_BASED" and legacy["active_version"] == 1
    detail = ok(acme.get(f"{STRUCTURES}/{legacy['id']}"))
    lines = detail["versions"][0]["lines"]
    assert [line["component"] for line in lines] == ["BASIC", "HRA", "DA", "CONVEYANCE", "OTHER_ALLOWANCE"]
    assert all(line["method"] == "FIXED" and line["overridable"] and line["value"] is None for line in lines)
    row = migrated(cid)[row_id]
    _, assignment_id, status, reason, currency, amount, start, end, approved, approver = row
    assert (status, reason, currency, start, end) == ("ACTIVE", "MIGRATED", "INR", date(2024, 2, 1), None)
    assert amount == Decimal("49000") and approved and approver is None  # approved by the system
    overrides = ok(acme.get(f"/api/v1/payroll/compensation/{assignment_id}"))["overrides"]
    assert overrides == {"BASIC": {"amount": "30000"}, "HRA": {"amount": "12000"}, "DA": {"amount": "3000"},
                         "CONVEYANCE": {"amount": "1600"}, "OTHER_ALLOWANCE": {"amount": "2400"}}
    breakdown = ok(acme.get(employee_url(worker["id"])))["breakdown"]
    monthly = {line["component"]: line["monthly"]["amount"] for line in breakdown["lines"]}
    assert monthly == {"BASIC": "30000.00", "HRA": "12000.00", "DA": "3000.00", "CONVEYANCE": "1600.00",
                       "OTHER_ALLOWANCE": "2400.00"}
    assert breakdown["totals"]["gross"]["monthly"]["amount"] == "49000.00"
    # legacy tables and employees.base_salary are untouched
    assert legacy_snapshot(cid) == before
    assert sql("SELECT base_salary FROM employees WHERE id = :e", e=worker["id"])[0][0] == base_before
    note = sql("SELECT note FROM compensation_assignments WHERE id = :i", i=assignment_id)[0][0]
    assert "Currency assumed" in note


def test_migration_is_idempotent_and_keeps_existing_components(acme):
    cid = acme.company_id
    seed(acme)  # BASIC already exists with the seeded tags
    worker = create_employee(acme, "Ida", "Idempotent")
    legacy_row(cid, worker["id"], date(2024, 3, 1))
    first = backfill()
    counts = sql("SELECT (SELECT count(*) FROM pay_components WHERE company_id = :c), "
                 "(SELECT count(*) FROM salary_templates WHERE company_id = :c), "
                 "(SELECT count(*) FROM compensation_assignments WHERE company_id = :c), "
                 "(SELECT count(*) FROM compensation_assignment_lines WHERE company_id = :c)", c=cid)[0]
    again = backfill()
    assert again == {"components": 0, "templates": 0, "assignments": 0, "closed": 0, "superseded": 0, "skipped": 0}
    assert first["assignments"] == 1
    assert sql("SELECT (SELECT count(*) FROM pay_components WHERE company_id = :c), "
               "(SELECT count(*) FROM salary_templates WHERE company_id = :c), "
               "(SELECT count(*) FROM compensation_assignments WHERE company_id = :c), "
               "(SELECT count(*) FROM compensation_assignment_lines WHERE company_id = :c)", c=cid)[0] == counts
    basic = ok(acme.get(f"{COMPONENTS}/BASIC"))["current_version"]
    assert "OT_BASE" in basic["tags"]  # the existing component is not replaced


def test_overlapping_legacy_rows_resolve_as_the_legacy_engine_reads_them(acme):
    cid = acme.company_id
    one = create_employee(acme, "Ola", "Overlap")
    two = create_employee(acme, "Sam", "Same")
    early = legacy_row(cid, one["id"], date(2024, 2, 1))  # open-ended, never closed by the legacy update path
    later = legacy_row(cid, one["id"], date(2025, 4, 1), amounts=("40000", "16000", "0", "0", "0"))
    first = legacy_row(cid, two["id"], date(2024, 5, 1))
    same = legacy_row(cid, two["id"], date(2024, 5, 1), amounts=("35000", "0", "0", "0", "0"))
    summary = backfill()
    assert summary["closed"] == 1 and summary["superseded"] == 1
    rows = migrated(cid)
    assert rows[early][7] == date(2025, 3, 31) and rows[early][2] == "ACTIVE"  # closed the day before the later start
    assert rows[later][2] == "ACTIVE" and rows[later][7] is None
    assert rows[first][2] == "SUPERSEDED" and rows[same][2] == "ACTIVE"  # the same start: the later row wins
    current = ok(acme.get(employee_url(two["id"])))["current"]
    assert current["pay_basis_amount"] == {"amount": "35000.00", "currency": "INR"}


def test_future_legacy_rows_are_scheduled_and_rows_without_placement_are_skipped(acme):
    cid = acme.company_id
    future = create_employee(acme, "Fay", "Future")
    nowhere = create_employee(acme, "Nia", "Nowhere")
    start = today_of(cid) + timedelta(days=40)
    scheduled = legacy_row(cid, future["id"], start)
    sql("DELETE FROM employee_assignments WHERE company_id = :c AND employee_id = :e", c=cid, e=nowhere["id"])
    skipped = legacy_row(cid, nowhere["id"], date(2024, 2, 1))
    summary = backfill()
    rows = migrated(cid)
    assert rows[scheduled][2] == "SCHEDULED" and summary["skipped"] == 1 and skipped not in rows


def test_migrated_currency_is_the_entity_currency(app, acme):
    gulf = signup_company(app, "Gulf Trading")
    sql("UPDATE legal_entities SET base_currency = 'AED' WHERE company_id = :c", c=gulf.company_id)
    worker = create_employee(gulf, "Gus", "Gulf")
    row_id = legacy_row(gulf.company_id, worker["id"], date(2024, 2, 1))
    local = create_employee(acme, "Lia", "Local")
    local_id = legacy_row(acme.company_id, local["id"], date(2024, 2, 1))
    backfill()
    assert migrated(gulf.company_id)[row_id][4] == "AED"
    assert migrated(acme.company_id)[local_id][4] == "INR"
    # each tenant got its own catalogue and structure; nothing crosses tenants
    assert sql("SELECT count(DISTINCT company_id) FROM compensation_assignments WHERE legacy_salary_structure_id "
               "IN (:a, :b)", a=row_id, b=local_id)[0][0] == 2
    assert sql("SELECT count(*) FROM compensation_assignments c JOIN salary_template_versions v ON v.id = "
               "c.template_version_id WHERE c.company_id <> v.company_id")[0][0] == 0
