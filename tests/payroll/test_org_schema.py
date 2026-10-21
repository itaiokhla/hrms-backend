"""Payroll organisation schema (migration 0008): tenant isolation, composite keys, guards, backfill, audit details."""

import importlib.util
import json
import logging
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.helpers import create_employee, signup_company

NEW_TABLES = ("work_week_patterns", "holiday_calendars", "grades", "employment_types", "cost_centers", "designations",
              "pay_groups")
INSERTS = {
    "work_week_patterns": "INSERT INTO work_week_patterns (company_id, code, name, weekly_off_days) "
                          "VALUES (:c, :code, :code, CAST('[5, 6]' AS jsonb)) RETURNING id",
    "holiday_calendars": "INSERT INTO holiday_calendars (company_id, code, name) VALUES (:c, :code, :code) RETURNING id",
    "grades": "INSERT INTO grades (company_id, code, name) VALUES (:c, :code, :code) RETURNING id",
    "employment_types": "INSERT INTO employment_types (company_id, code, name, worker_class) "
                        "VALUES (:c, :code, :code, 'EMPLOYEE') RETURNING id",
    "cost_centers": "INSERT INTO cost_centers (company_id, code, name) VALUES (:c, :code, :code) RETURNING id",
    "designations": "INSERT INTO designations (company_id, code, name) VALUES (:c, :code, :code) RETURNING id",
    "pay_groups": "INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency) "
                  "VALUES (:c, :entity, :code, :code, 'INR', 'MONTHLY') RETURNING id",
}
MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0008_payroll_org.py"


@contextmanager
def as_tenant(company_id):
    """A transaction of the RLS-restricted application role, bound to one tenant (or none)."""
    from app.database.db import engine

    with engine.begin() as conn:
        if company_id is not None:
            conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(company_id)})
        yield conn


@contextmanager
def as_owner():
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        yield conn


def _entity_id(conn, company_id, code="LE01"):
    return conn.execute(text("SELECT id FROM legal_entities WHERE company_id = :c AND code = :code"),
                        {"c": company_id, "code": code}).scalar_one()


def _insert(conn, table, company_id, code, entity=None):
    if table == "pay_groups" and entity is None:
        entity = _entity_id(conn, company_id)
    return conn.execute(text(INSERTS[table]), {"c": company_id, "code": code, "entity": entity}).scalar_one()


def _rows(company_id, code):
    """One row per new table for a tenant, written as that tenant: {table: id}."""
    with as_tenant(company_id) as conn:
        return {table: _insert(conn, table, company_id, code) for table in NEW_TABLES}


def _fails(statement, params=None, company_id="owner", match=None):
    """The statement is rejected (in its own transaction, so nothing of it is kept)."""
    context = as_owner() if company_id == "owner" else as_tenant(company_id)
    with pytest.raises(DBAPIError) as err:
        with context as conn:
            conn.execute(text(statement), params or {})
    if match:
        assert match in str(err.value), str(err.value)
    return err.value


def _assignment(conn, company_id):
    return conn.execute(text(
        "SELECT id, employee_id, legal_entity_id, branch_id FROM employee_assignments WHERE company_id = :c ORDER BY id LIMIT 1"
    ), {"c": company_id}).one()


@pytest.fixture
def tenants(app):
    return {key: signup_company(app, name) for key, name in (("A", "Acme Corp"), ("B", "Beta Industries"),
                                                              ("C", "Cobalt Labs"))}


# ---------------------------------------------------------------------------
# Row-level security (EC-ORG-01, EC-ORG-02)
# ---------------------------------------------------------------------------
def test_ec_org_01_every_new_table_is_isolated_across_three_tenants(tenants):
    """Identical codes in three tenants coexist; no tenant reads, changes, deletes or plants another's rows."""
    ids = {key: tenants[key].company_id for key in tenants}
    rows = {key: _rows(cid, "SHARED") for key, cid in ids.items()}

    for viewer, viewer_cid in ids.items():
        for owner, owner_cid in ids.items():
            if owner == viewer:
                continue
            for table in NEW_TABLES:
                target = {"id": rows[owner][table]}
                with as_tenant(viewer_cid) as conn:
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :id"), target).scalar() == 0
                    assert conn.execute(text(f"UPDATE {table} SET name = 'hijacked' WHERE id = :id"), target).rowcount == 0
                    assert conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), target).rowcount == 0
                    assert {c for (c,) in conn.execute(text(f"SELECT DISTINCT company_id FROM {table}"))} == {viewer_cid}
                # Writing a row for another tenant, or moving one's own row there, is refused.
                with as_owner() as conn:
                    entity = _entity_id(conn, viewer_cid)
                _fails(INSERTS[table], {"c": owner_cid, "code": f"PLANT-{viewer}", "entity": entity}, company_id=viewer_cid)
                _fails(f"UPDATE {table} SET company_id = :o WHERE id = :id", {"o": owner_cid, "id": rows[viewer][table]},
                       company_id=viewer_cid)

    with as_tenant(None) as conn:  # no tenant bound: nothing at all
        for table in NEW_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0
    with as_owner() as conn:
        for table in NEW_TABLES:
            # Signup also provisions starter rows (MONTHLY, PERMANENT, MON_FRI, COMPANY); check the rows planted here.
            stored = conn.execute(text(
                f"SELECT company_id, code, name FROM {table} WHERE code = 'SHARED' ORDER BY company_id"
            )).fetchall()
            assert [tuple(r) for r in stored] == [(cid, "SHARED", "SHARED") for cid in sorted(ids.values())], table
            hijacked = conn.execute(text(f"SELECT count(*) FROM {table} WHERE name = 'hijacked' OR code LIKE 'PLANT-%'"))
            assert hijacked.scalar() == 0, table


def test_ec_org_01_codes_are_unique_per_tenant_only(tenants):
    a = tenants["A"].company_id
    _rows(a, "DUP")
    _rows(tenants["B"].company_id, "DUP")
    for table in NEW_TABLES:
        with as_owner() as conn:
            entity = _entity_id(conn, a)
        _fails(INSERTS[table], {"c": a, "code": "DUP", "entity": entity}, company_id=a, match="uq_")


def test_orm_models_match_the_tables_and_stay_tenant_scoped(tenants):
    from app.database.db import SessionLocal, bind_tenant
    from app.models.company import Company
    from app.models.holiday import Holiday
    from app.models.org import Branch, EmployeeAssignment, LegalEntity
    from app.models.pay_org import (
        CostCenter, Designation, EmploymentType, Grade, HolidayCalendar, PayGroup, WorkWeekPattern,
        calendar_lineage_select,
    )

    a, b = tenants["A"].company_id, tenants["B"].company_id
    _rows(b, "B-ONLY")
    db = SessionLocal()
    try:
        bind_tenant(db, a)
        entity = db.query(LegalEntity).filter(LegalEntity.code == "LE01").one()
        week = WorkWeekPattern(company_id=a, code="MON_SAT_2_4", name="2nd and 4th Saturday off", weekly_off_days=[6],
                               nth_weekday_offs=[{"weekday": 5, "nth": [2, 4]}])
        national = HolidayCalendar(company_id=a, code="IN", name="India", country_code="IN")
        db.add_all([week, national])
        db.flush()
        state = HolidayCalendar(company_id=a, code="IN-MH", name="Maharashtra", country_code="IN",
                                subdivision_code="IN-MH", parent_calendar_id=national.id)
        grade = Grade(company_id=a, code="G1", name="Grade 1", rank=1,
                      salary_ranges=[{"currency": "INR", "min": "300000.00", "mid": "450000", "max": "600000.00"}])
        db.add_all([state, grade])
        db.flush()
        city = HolidayCalendar(company_id=a, code="PNQ", name="Pune", parent_calendar_id=state.id)
        group = PayGroup(company_id=a, legal_entity_id=entity.id, code="IN-M", name="India monthly", currency="INR",
                         pay_frequency="MONTHLY", calendar_rules={"monthly_start_day": 1}, country_pack_code="IN")
        kind = EmploymentType(company_id=a, code="FT", name="Full time", worker_class="EMPLOYEE")
        center = CostCenter(company_id=a, code="CC1", name="Engineering", legal_entity_id=entity.id)
        title = Designation(company_id=a, code="ENG", name="Engineer", grade_id=grade.id)
        db.add_all([city, group, kind, center, title])
        db.flush()

        assert [r for (r,) in db.execute(calendar_lineage_select(a, city.id))] == [city.id, state.id, national.id]
        branch = db.query(Branch).filter(Branch.legal_entity_id == entity.id).first()
        branch.holiday_calendar_id, branch.work_week_pattern_id = city.id, week.id
        entity.default_work_week_pattern_id = week.id
        placement = db.query(EmployeeAssignment).one()
        assert placement.fte == Decimal("1")  # rows written by today's code get the default
        placement.pay_group_id, placement.grade_id, placement.employment_type_id = group.id, grade.id, kind.id
        placement.designation_id, placement.cost_center_id, placement.work_week_pattern_id = title.id, center.id, week.id
        placement.fte = Decimal("0.5")
        db.add(Holiday(company_id=a, name="Ganesh Chaturthi", date=date(2026, 9, 14), calendar_id=state.id,
                       holiday_type="PUBLIC"))
        company = db.get(Company, a)
        company.reporting_currency, company.data_region = "USD", "ap-south-1"
        db.commit()

        bind_tenant(db, a)
        for model in (PayGroup, Grade, CostCenter, EmploymentType, Designation, WorkWeekPattern, HolidayCalendar):
            assert {row.company_id for row in db.query(model)} == {a}, model.__tablename__
        stored = db.query(EmployeeAssignment).one()
        assert (stored.pay_group_id, stored.fte) == (group.id, Decimal("0.5000"))
        assert db.query(Holiday).one().holiday_type == "PUBLIC"
        assert db.get(Company, a).reporting_currency == "USD"
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Composite foreign keys (cross-tenant references)
# ---------------------------------------------------------------------------
def test_ec_org_02_references_to_another_tenants_rows_are_rejected(tenants):
    """Even the owner role (no RLS) cannot link a row to another tenant's dimension."""
    a, b = tenants["A"].company_id, tenants["B"].company_id
    theirs = _rows(b, "THEIRS")
    ours = _rows(a, "OURS")
    with as_owner() as conn:
        their_entity = _entity_id(conn, b)
        placement = _assignment(conn, a)
        branch = conn.execute(text("SELECT id FROM branches WHERE company_id = :c LIMIT 1"), {"c": a}).scalar_one()
        our_entity = _entity_id(conn, a)
        holiday = conn.execute(text(
            "INSERT INTO holidays (company_id, name, date, is_optional, holiday_type, created_at, updated_at) "
            "VALUES (:c, 'Founders Day', '2099-02-01', false, 'COMPANY', now(), now()) RETURNING id"), {"c": a}).scalar_one()

    _fails(INSERTS["pay_groups"], {"c": a, "code": "X", "entity": their_entity}, match="fk_pay_groups_legal_entity")
    _fails("INSERT INTO cost_centers (company_id, code, name, legal_entity_id) VALUES (:c, 'X', 'X', :e)",
           {"c": a, "e": their_entity}, match="fk_cost_centers_legal_entity")
    _fails("INSERT INTO designations (company_id, code, name, grade_id) VALUES (:c, 'X', 'X', :g)",
           {"c": a, "g": theirs["grades"]}, match="fk_designations_grade")
    _fails("UPDATE holiday_calendars SET parent_calendar_id = :p WHERE id = :id",
           {"p": theirs["holiday_calendars"], "id": ours["holiday_calendars"]}, match="fk_holiday_calendars_parent")
    _fails("UPDATE branches SET holiday_calendar_id = :v WHERE id = :id", {"v": theirs["holiday_calendars"], "id": branch},
           match="fk_branches_holiday_calendar")
    _fails("UPDATE branches SET work_week_pattern_id = :v WHERE id = :id", {"v": theirs["work_week_patterns"], "id": branch},
           match="fk_branches_work_week")
    _fails("UPDATE legal_entities SET default_work_week_pattern_id = :v WHERE id = :id",
           {"v": theirs["work_week_patterns"], "id": our_entity}, match="fk_legal_entities_work_week")
    _fails("UPDATE holidays SET calendar_id = :v WHERE id = :id", {"v": theirs["holiday_calendars"], "id": holiday},
           match="fk_holidays_calendar")
    for column, table in (("grade_id", "grades"), ("employment_type_id", "employment_types"),
                          ("designation_id", "designations"), ("cost_center_id", "cost_centers"),
                          ("work_week_pattern_id", "work_week_patterns")):
        _fails(f"UPDATE employee_assignments SET {column} = :v WHERE id = :id", {"v": theirs[table], "id": placement.id},
               match="fk_employee_assignments_")
    # Another tenant's pay group is caught by the guard before the composite key.
    _fails("UPDATE employee_assignments SET pay_group_id = :v WHERE id = :id", {"v": theirs["pay_groups"], "id": placement.id})
    with as_owner() as conn:
        shift = conn.execute(text(
            "INSERT INTO shifts (company_id, name, start_time, end_time, is_flexible, is_active, created_at, updated_at) "
            "VALUES (:c, 'Night', '22:00', '06:00', false, true, now(), now()) RETURNING id"), {"c": b}).scalar_one()
    _fails("UPDATE employee_assignments SET shift_id = :v WHERE id = :id", {"v": shift, "id": placement.id},
           match="fk_employee_assignments_shift")

    # The same references within the tenant are accepted.
    with as_owner() as conn:
        conn.execute(text(
            "UPDATE employee_assignments SET grade_id = :g, employment_type_id = :t, designation_id = :d, cost_center_id = :cc, "
            "work_week_pattern_id = :w, pay_group_id = :pg WHERE id = :id"),
            {"g": ours["grades"], "t": ours["employment_types"], "d": ours["designations"], "cc": ours["cost_centers"],
             "w": ours["work_week_patterns"], "pg": ours["pay_groups"], "id": placement.id})
        conn.execute(text("UPDATE holidays SET calendar_id = :v WHERE id = :id"), {"v": ours["holiday_calendars"], "id": holiday})


# ---------------------------------------------------------------------------
# employee_assignments guard (EC-ORG-11, EC-ORG-26) and FTE
# ---------------------------------------------------------------------------
def _make_entity(client, code, name, country, currency, tz):
    r = client.post("/api/v1/legal-entities", json={"code": code, "legal_name": name, "country_code": country,
                                                    "base_currency": currency, "timezone": tz})
    assert r.status_code == 200, r.text
    return r.json()


def _make_branch(client, entity_id, code, name):
    r = client.post("/api/v1/branches", json={"legal_entity_id": entity_id, "code": code, "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def test_ec_org_11_guard_requires_the_pay_group_of_the_rows_entity_and_keeps_every_0005_rule(app):
    acme = signup_company(app, "Acme Corp")
    a = acme.company_id
    gulf = _make_entity(acme, "AE01", "Acme Gulf FZ-LLC", "AE", "AED", "Asia/Dubai")
    dubai = _make_branch(acme, gulf["id"], "DXB", "Dubai")
    with as_tenant(a) as conn:
        india_group = _insert(conn, "pay_groups", a, "IN-MONTHLY")
        gulf_group = _insert(conn, "pay_groups", a, "AE-MONTHLY", entity=gulf["id"])
        placement = _assignment(conn, a)

    params = {"id": placement.id}
    _fails("UPDATE employee_assignments SET pay_group_id = :pg WHERE id = :id", {**params, "pg": gulf_group}, company_id=a,
           match="The pay group does not belong to the legal entity")
    _fails("INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, pay_group_id, is_primary, "
           "reason, effective_from) VALUES (:c, :e, :le, :b, :pg, false, 'CONCURRENT_START', '2030-01-01')",
           {"c": a, "e": placement.employee_id, "le": placement.legal_entity_id, "b": placement.branch_id, "pg": gulf_group},
           company_id=a, match="The pay group does not belong to the legal entity")
    with as_tenant(a) as conn:
        conn.execute(text("UPDATE employee_assignments SET pay_group_id = :pg WHERE id = :id"), {**params, "pg": india_group})
    # Moving a pay group that placements use to another entity would break them: refused by the composite key.
    _fails("UPDATE pay_groups SET legal_entity_id = :le WHERE id = :id", {"le": gulf["id"], "id": india_group}, company_id=a,
           match="fk_employee_assignments_pay_group")

    # 0005 rules still hold: the branch belongs to the entity, primary windows never overlap.
    _fails("INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, is_primary, reason, "
           "effective_from) VALUES (:c, :e, :le, :b, true, 'TRANSFER', '2030-01-01')",
           {"c": a, "e": placement.employee_id, "le": placement.legal_entity_id, "b": dubai["id"]}, company_id=a,
           match="The branch does not belong to the legal entity")
    _fails("INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, pay_group_id, is_primary, "
           "reason, effective_from) VALUES (:c, :e, :le, :b, :pg, true, 'CORRECTION', '2030-01-01')",
           {"c": a, "e": placement.employee_id, "le": gulf["id"], "b": dubai["id"], "pg": gulf_group}, company_id=a,
           match="Primary assignments of one employee cannot overlap")
    _fails("INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, is_primary, reason, "
           "effective_from) VALUES (:c, :e, :le, NULL, false, 'CONCURRENT_START', '2030-01-01')",
           {"c": a, "e": placement.employee_id, "le": placement.legal_entity_id}, company_id=a)  # EC-ORG-26: branch required

    # Today's placement service still writes rows without the new columns.
    emp = create_employee(acme, "Mover", hire_date="2024-01-15")
    r = acme.post(f"/api/v1/employees/{emp['id']}/assignments",
                  json={"branch_id": dubai["id"], "effective_from": "2024-06-01", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    with as_owner() as conn:
        rows = conn.execute(text("SELECT fte FROM employee_assignments WHERE employee_id = :e"), {"e": emp["id"]}).scalars()
        assert list(rows) == [Decimal("1")] * 2


@pytest.mark.parametrize("fte, ok", [("1", True), ("0.5", True), ("0.0001", True), ("0", False), ("-0.5", False),
                                     ("1.0001", False)])
def test_fte_must_be_above_zero_and_at_most_one(app, fte, ok):
    acme = signup_company(app, "Acme Corp")
    statement = "UPDATE employee_assignments SET fte = CAST(:f AS numeric) WHERE company_id = :c"
    params = {"f": fte, "c": acme.company_id}
    if ok:
        with as_tenant(acme.company_id) as conn:
            assert conn.execute(text(statement), params).rowcount == 1
    else:
        _fails(statement, params, company_id=acme.company_id, match="ck_employee_assignments_fte")


# ---------------------------------------------------------------------------
# Holiday calendars, work weeks, grades, holidays
# ---------------------------------------------------------------------------
def test_holiday_calendar_inheritance_cycles_are_rejected(app):
    a = signup_company(app, "Acme Corp").company_id
    with as_tenant(a) as conn:
        root = _insert(conn, "holiday_calendars", a, "ROOT")
        mid = conn.execute(text("INSERT INTO holiday_calendars (company_id, code, name, parent_calendar_id) "
                                "VALUES (:c, 'MID', 'MID', :p) RETURNING id"), {"c": a, "p": root}).scalar_one()
        leaf = conn.execute(text("INSERT INTO holiday_calendars (company_id, code, name, parent_calendar_id) "
                                 "VALUES (:c, 'LEAF', 'LEAF', :p) RETURNING id"), {"c": a, "p": mid}).scalar_one()
    update = "UPDATE holiday_calendars SET parent_calendar_id = :p WHERE id = :id"
    _fails(update, {"p": leaf, "id": root}, company_id=a, match="would create a cycle")
    _fails(update, {"p": mid, "id": root}, company_id=a, match="would create a cycle")
    _fails(update, {"p": root, "id": root}, company_id=a, match="cannot inherit from itself")
    with as_tenant(a) as conn:  # re-parenting without a cycle is fine
        conn.execute(text(update), {"p": root, "id": leaf})
        conn.execute(text(update), {"p": None, "id": mid})


@pytest.mark.parametrize("off_days, nth, ok", [
    ("[5, 6]", "[]", True),
    ("[]", "[]", True),
    ("[6]", '[{"weekday": 5, "nth": [2, 4]}]', True),
    ("[4]", '[{"weekday": 5, "nth": [-1]}]', True),
    ("[0, 1, 2, 3, 4, 5, 6]", "[]", False),   # no working day left
    ("[5, 5]", "[]", False),
    ("[7]", "[]", False),
    ('["5"]', "[]", False),
    ("[5.0]", "[]", False),
    ('{"off": [5]}', "[]", False),
    ("[6]", '[{"weekday": 5, "nth": []}]', False),
    ("[6]", '[{"weekday": 5, "nth": [6]}]', False),
    ("[6]", '[{"weekday": 9, "nth": [1]}]', False),
    ("[6]", '[{"nth": [1]}]', False),
])
def test_work_week_patterns_accept_iso_weekdays_and_nth_weekday_rules_only(app, off_days, nth, ok):
    a = signup_company(app, "Acme Corp").company_id
    statement = ("INSERT INTO work_week_patterns (company_id, code, name, weekly_off_days, nth_weekday_offs) "
                 "VALUES (:c, 'W', 'W', CAST(:off AS jsonb), CAST(:nth AS jsonb))")
    params = {"c": a, "off": off_days, "nth": nth}
    if ok:
        with as_tenant(a) as conn:
            conn.execute(text(statement), params)
    else:
        _fails(statement, params, company_id=a, match="ck_work_week_patterns_rules")


@pytest.mark.parametrize("ranges, ok", [
    ("[]", True),
    ('[{"currency": "INR", "min": "300000.00", "max": "600000.00"}, {"currency": "AED", "min": "0", "max": "1"}]', True),
    ('[{"currency": "INR", "min": "1", "mid": "2", "max": "3"}]', True),
    ('[{"currency": "INR", "min": 300000, "max": 600000}]', False),  # JSON numbers are never money
    ('[{"currency": "INR", "min": "600000", "max": "300000"}]', False),
    ('[{"currency": "INR", "min": "1", "mid": "5", "max": "3"}]', False),
    ('[{"currency": "INR", "min": "1", "max": "2"}, {"currency": "INR", "min": "3", "max": "4"}]', False),
    ('[{"currency": "inr", "min": "1", "max": "2"}]', False),
    ('[{"currency": "INR", "min": "-1", "max": "2"}]', False),
    ('[{"currency": "INR", "min": "1e3", "max": "2000"}]', False),
    ('{"INR": {"min": "1", "max": "2"}}', False),
])
def test_grade_salary_ranges_are_decimal_strings_one_band_per_currency(app, ranges, ok):
    a = signup_company(app, "Acme Corp").company_id
    statement = "INSERT INTO grades (company_id, code, name, salary_ranges) VALUES (:c, 'G', 'G', CAST(:r AS jsonb))"
    if ok:
        with as_tenant(a) as conn:
            conn.execute(text(statement), {"c": a, "r": ranges})
    else:
        _fails(statement, {"c": a, "r": ranges}, company_id=a, match="ck_grades_salary_ranges")


def test_check_constraints_on_pay_groups_employment_types_calendars_and_companies(app):
    a = signup_company(app, "Acme Corp").company_id
    with as_owner() as conn:
        entity = _entity_id(conn, a)
    pay_group = ("INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency, calendar_rules) "
                 "VALUES (:c, :e, :code, 'P', :cur, :freq, CAST(:rules AS jsonb))")
    good = {"c": a, "e": entity, "code": "P", "cur": "INR", "freq": "MONTHLY", "rules": "{}"}
    _fails(pay_group, {**good, "freq": "FORTNIGHTLY"}, company_id=a, match="ck_pay_groups_frequency")
    _fails(pay_group, {**good, "cur": "inr"}, company_id=a, match="ck_pay_groups_currency")
    _fails(pay_group, {**good, "rules": "[]"}, company_id=a, match="ck_pay_groups_calendar_rules")
    with as_tenant(a) as conn:
        for n, freq in enumerate(("MONTHLY", "SEMI_MONTHLY", "BI_WEEKLY", "WEEKLY", "DAILY")):
            conn.execute(text(pay_group), {**good, "code": f"P{n}", "freq": freq})
    _fails("UPDATE pay_groups SET status = 'ARCHIVED' WHERE company_id = :c", {"c": a}, company_id=a,
           match="ck_pay_groups_status")
    _fails("INSERT INTO employment_types (company_id, code, name, worker_class) VALUES (:c, 'X', 'X', 'FREELANCER')",
           {"c": a}, company_id=a, match="ck_employment_types_worker_class")
    _fails("INSERT INTO holiday_calendars (company_id, code, name, country_code, subdivision_code) "
           "VALUES (:c, 'X', 'X', 'IN', 'AE-DU')", {"c": a}, company_id=a, match="ck_holiday_calendars_subdivision")
    _fails("INSERT INTO holiday_calendars (company_id, code, name, country_code) VALUES (:c, 'X', 'X', 'India')",
           {"c": a}, company_id=a)
    _fails("UPDATE companies SET reporting_currency = 'us' WHERE id = :c", {"c": a}, company_id=a,
           match="ck_companies_reporting_currency")


def test_holidays_keep_todays_api_and_gain_a_type_and_calendar(app):
    acme = signup_company(app, "Acme Corp")
    a = acme.company_id
    assert acme.post("/api/v1/holidays", json={"name": "Founders Day", "date": "2099-02-01"}).status_code == 200
    with as_tenant(a) as conn:
        row = conn.execute(text("SELECT holiday_type, location FROM holidays WHERE company_id = :c"), {"c": a}).one()
        assert tuple(row) == ("COMPANY", None)
        calendar = _insert(conn, "holiday_calendars", a, "COMPANY-2099")
        conn.execute(text("UPDATE holidays SET calendar_id = :cal, holiday_type = 'PUBLIC' WHERE company_id = :c"),
                     {"cal": calendar, "c": a})
    _fails("UPDATE holidays SET holiday_type = 'BANK' WHERE company_id = :c", {"c": a}, company_id=a, match="ck_holidays_type")
    assert [h["name"] for h in acme.get("/api/v1/holidays?year=2099").json()] == ["Founders Day"]


# ---------------------------------------------------------------------------
# PAY_GROUP scope
# ---------------------------------------------------------------------------
def test_pay_group_scope_is_accepted_by_role_grants_and_assignment_targets(app):
    from app.models.org import TARGET_SCOPES

    assert "PAY_GROUP" in TARGET_SCOPES
    a = signup_company(app, "Acme Corp").company_id
    with as_owner() as conn:
        role = conn.execute(text("SELECT id FROM roles WHERE company_id = :c ORDER BY id LIMIT 1"), {"c": a}).scalar_one()
        assignment = conn.execute(text("SELECT id FROM user_role_assignments WHERE company_id = :c LIMIT 1"),
                                  {"c": a}).scalar_one()
    with as_tenant(a) as conn:
        group = _insert(conn, "pay_groups", a, "PG")
        conn.execute(text("INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
                          "VALUES (:c, :r, 'test:pay_group_scope', 'PAY_GROUP', now(), now())"), {"c": a, "r": role})
        conn.execute(text("INSERT INTO user_role_assignment_targets (company_id, user_role_assignment_id, scope, target_id, "
                          "created_at, updated_at) VALUES (:c, :u, 'PAY_GROUP', :t, now(), now())"),
                     {"c": a, "u": assignment, "t": group})
    _fails("INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
           "VALUES (:c, :r, 'test:bad_scope', 'PAY_GROUPS', now(), now())", {"c": a, "r": role}, company_id=a,
           match="ck_role_permissions_scope")
    _fails("INSERT INTO user_role_assignment_targets (company_id, user_role_assignment_id, scope, target_id, "
           "created_at, updated_at) VALUES (:c, :u, 'DEPARTMENT', 1, now(), now())", {"c": a, "u": assignment}, company_id=a,
           match="ck_role_assignment_targets_scope")


# ---------------------------------------------------------------------------
# Audit details (EC-SEC-19)
# ---------------------------------------------------------------------------
def test_ec_sec_19_audit_details_round_trip_masked_and_append_only(app):
    from app.database.db import SessionLocal, bind_tenant
    from app.services.audit_log_service import AuditLogService

    a = signup_company(app, "Acme Corp").company_id
    details = {
        "before": {"base_salary": Decimal("50000.00"), "bank": {"account_number": "123456789012", "ifsc": "HDFC0001234"}},
        "after": {"base_salary": Decimal("55000.50"), "effective_from": date(2026, 11, 1), "tax_id": "ABCDE1234F"},
        "reason": "Annual revision",
        "password": "hunter2",
        "verified": True,
        "previous_iban": None,
    }
    db = SessionLocal()
    try:
        bind_tenant(db, a)
        full = AuditLogService.log(db, None, "payroll", "COMPENSATION_CHANGED", "Salary revised", entity_type="employee",
                                   entity_id=7, company_id=a, details=details)
        masked = AuditLogService.log(db, None, "payroll", "COMPENSATION_CHANGED", "Salary revised", company_id=a,
                                     details=details, mask_amounts=True)
        legacy = AuditLogService.log(db, None, "employee", "UPDATE", "No details", company_id=a)
        db.commit()
        ids = (full.id, masked.id, legacy.id)
    finally:
        db.close()

    with as_tenant(a) as conn:
        stored = dict(conn.execute(text("SELECT id, details FROM audit_logs WHERE id = ANY(:ids)"), {"ids": list(ids)}).fetchall())
    assert stored[ids[0]] == {
        "before": {"base_salary": "50000.00", "bank": {"account_number": "****9012", "ifsc": "HDFC0001234"}},
        "after": {"base_salary": "55000.50", "effective_from": "2026-11-01", "tax_id": "****234F"},
        "reason": "Annual revision", "password": "****", "verified": True, "previous_iban": None,
    }
    assert stored[ids[1]]["before"]["base_salary"] == "****" and stored[ids[1]]["after"]["base_salary"] == "****"
    assert stored[ids[2]] is None
    assert "123456789012" not in json.dumps(stored) and "hunter2" not in json.dumps(stored)
    _fails("UPDATE audit_logs SET details = '{}'::jsonb WHERE id = :id", {"id": ids[2]}, company_id=a)
    other = signup_company(app, "Beta Industries").company_id
    with as_tenant(other) as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_logs WHERE id = ANY(:ids)"), {"ids": list(ids)}).scalar() == 0


def test_mask_sensitive_matches_key_words_at_any_depth():
    from app.services.audit_log_service import MASK, mask_sensitive

    masked = mask_sensitive({
        "bankAccountNumber": "000123456789", "account_number_last4": "6789", "iban": "GB82WEST12345698765432",
        "items": [{"amount": "10.00", "currency": "INR"}, {"net": Decimal("5")}], "pin_code": "411001", "pin": "1234",
        "api_key": "k", "tax_id_type": "PAN", "short_tin": "123", "total": 3,
    })
    assert masked == {
        "bankAccountNumber": "****6789", "account_number_last4": "6789", "iban": "****5432",
        "items": [{"amount": MASK, "currency": "INR"}, {"net": MASK}], "pin_code": "411001", "pin": MASK,
        "api_key": MASK, "tax_id_type": "PAN", "short_tin": MASK, "total": 3,
    }
    assert mask_sensitive({"amount": Decimal("1.50")}, amounts=False) == {"amount": "1.50"}
    assert mask_sensitive(None) is None
    with pytest.raises(TypeError):
        mask_sensitive(["not", "a", "mapping"])
    with pytest.raises(TypeError):
        mask_sensitive({"value": object()})


# ---------------------------------------------------------------------------
# Backfill (EC-MIG-09, EC-MIG-10, EC-MIG-14)
# ---------------------------------------------------------------------------
def _migration():
    spec = importlib.util.spec_from_file_location("migration_0008_payroll_org", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_tenant(name, slug, *, with_second_entity):
    """A tenant as an older release left it: entities, branches, placements and location-text holidays only."""
    from app.database.db import SessionLocal, bind_tenant, system_session
    from app.models.company import Company
    from app.models.employee import Employee, Shift
    from app.models.holiday import Holiday
    from app.models.org import Branch, EmployeeAssignment, LegalEntity

    with system_session() as db:
        company = Company(name=name, slug=slug, currency="INR", timezone="Asia/Kolkata")
        db.add(company)
        db.commit()
        cid = company.id
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        india = LegalEntity(company_id=cid, code="LE01", legal_name=name, country_code="IN", base_currency="INR",
                            timezone="Asia/Kolkata", is_default=True)
        db.add(india)
        db.flush()
        main = Branch(company_id=cid, legal_entity_id=india.id, code="MAIN", name="Main", country_code="IN",
                      timezone="Asia/Kolkata")
        db.add(main)
        db.flush()
        out ={"company": cid, "india": india.id, "main": main.id}
        if with_second_entity:
            gulf = LegalEntity(company_id=cid, code="AE01", legal_name=f"{name} Gulf", country_code="AE",
                               base_currency="AED", timezone="Asia/Dubai", status="INACTIVE")
            db.add(gulf)
            db.flush()
            pune = Branch(company_id=cid, legal_entity_id=india.id, code="PNQ", name="Pune", country_code="IN",
                          subdivision_code="IN-MH", timezone="Asia/Kolkata")
            dubai = Branch(company_id=cid, legal_entity_id=gulf.id, code="PNQ", name="Dubai", country_code="AE",
                           timezone="Asia/Dubai")
            shift = Shift(company_id=cid, name="General", start_time="09:00", end_time="18:00")
            db.add_all([pune, dubai, shift])
            db.flush()
            mover = Employee(company_id=cid, employee_code="E1", first_name="Asha", last_name="Rao",
                             email=f"asha@{slug}.example", hire_date=date(2024, 1, 1), location="Pune", shift_id=shift.id)
            future = Employee(company_id=cid, employee_code="E2", first_name="Ben", last_name="Ng",
                              email=f"ben@{slug}.example", hire_date=date(2024, 1, 1), location="Main")
            gulf_emp = Employee(company_id=cid, employee_code="E3", first_name="Layla", last_name="Haddad",
                                email=f"layla@{slug}.example", hire_date=date(2024, 1, 1), location="Dubai",
                                shift_id=shift.id)
            db.add_all([mover, future, gulf_emp])
            db.flush()

            def place(emp, entity, branch, start, end=None, primary=True, reason="MIGRATION"):
                row = EmployeeAssignment(company_id=cid, employee_id=emp.id, legal_entity_id=entity.id, branch_id=branch.id,
                                         is_primary=primary, reason=reason, effective_from=start, effective_to=end)
                db.add(row)
                db.flush()
                return row.id

            out.update(
                gulf=gulf.id, pune=pune.id, dubai=dubai.id, shift=shift.id,
                mover_old=place(mover, india, main, date(2024, 1, 1), date(2024, 12, 31)),
                mover_now=place(mover, india, pune, date(2025, 1, 1), reason="TRANSFER"),
                mover_side=place(mover, gulf, dubai, date(2025, 3, 1), primary=False, reason="CONCURRENT_START"),
                future_now=place(future, india, main, date(2024, 1, 1), date(2099, 12, 31)),
                future_next=place(future, india, pune, date(2100, 1, 1), reason="TRANSFER"),
                gulf_now=place(gulf_emp, gulf, dubai, date(2024, 1, 1)),
            )
            for holiday, location, optional in (("New Year", None, False), ("Diwali", "Pune", True),
                                                ("National Day", "Dubai", False), ("Typo Day", "pune ", False),
                                                ("Old Office Day", "Old Office", True)):
                db.add(Holiday(company_id=cid, name=holiday, date=date(2026, 11, 1), location=location,
                               is_optional=optional))
        db.commit()
        return out
    finally:
        db.close()


def _snapshot(conn):
    tables = (*NEW_TABLES, "employee_assignments", "branches", "legal_entities", "holidays")
    return {t: [tuple(r) for r in conn.execute(text(f"SELECT * FROM {t} ORDER BY id"))] for t in tables}


def test_ec_mig_10_ec_mig_14_backfill_builds_payroll_dimensions_once(app, caplog):
    migration = _migration()
    x = _legacy_tenant("Old Release Trading", "old-release-trading", with_second_entity=True)
    y = _legacy_tenant("Quiet Tenant", "quiet-tenant", with_second_entity=False)
    with caplog.at_level(logging.WARNING, logger="alembic.0008"):
        with as_owner() as conn:
            summary = migration.backfill(conn)
    assert summary["pay_groups"] == 3 and summary["employment_types"] == 2 and summary["work_week_patterns"] == 2
    assert summary["holiday_calendars"] == 2 + 4 + 1  # COMPANY x2, a calendar per branch, UNMATCHED for x only
    assert [r.getMessage() for r in caplog.records] == [
        f"Company {x['company']}: 2 holiday(s) name a location that matches no branch; they are in the holiday "
        "calendar UNMATCHED for review."]

    with as_owner() as conn:
        def one(sql, **params):
            return conn.execute(text(sql), params).one()

        groups = {(r.company_id, r.code): r for r in conn.execute(text("SELECT * FROM pay_groups"))}
        assert set(groups) == {(x["company"], "MONTHLY"), (x["company"], "MONTHLY-AE01"), (y["company"], "MONTHLY")}
        india, gulf = groups[(x["company"], "MONTHLY")], groups[(x["company"], "MONTHLY-AE01")]
        assert (india.legal_entity_id, india.currency, india.pay_frequency, india.status, india.name) == (
            x["india"], "INR", "MONTHLY", "ACTIVE", "Monthly")
        assert india.calendar_rules == {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}
        assert (gulf.legal_entity_id, gulf.currency, gulf.status) == (x["gulf"], "AED", "INACTIVE")

        permanent = one("SELECT id, worker_class, name FROM employment_types WHERE company_id = :c AND code = 'PERMANENT'",
                        c=x["company"])
        assert tuple(permanent)[1:] == ("EMPLOYEE", "Permanent")
        placements = {r.id: r for r in conn.execute(text("SELECT * FROM employee_assignments WHERE company_id = :c"),
                                                    {"c": x["company"]})}
        for key in ("mover_old", "mover_now", "future_now", "future_next"):
            assert placements[x[key]].pay_group_id == india.id, key
        assert placements[x["gulf_now"]].pay_group_id == gulf.id
        assert placements[x["mover_side"]].pay_group_id is None  # concurrent rows are not primary placements
        assert {r.employment_type_id for r in placements.values()} == {permanent.id}
        # The shift of the placement in effect today only; history and future rows have none.
        assert {k: placements[x[k]].shift_id for k in ("mover_old", "mover_now", "future_now", "future_next", "gulf_now")} == {
            "mover_old": None, "mover_now": x["shift"], "future_now": None, "future_next": None, "gulf_now": x["shift"]}
        assert {r.fte for r in placements.values()} == {Decimal("1")}

        week = one("SELECT id, weekly_off_days, nth_weekday_offs FROM work_week_patterns WHERE company_id = :c "
                   "AND code = 'MON_FRI'", c=x["company"])
        assert (week.weekly_off_days, week.nth_weekday_offs) == ([5, 6], [])
        assert {r for (r,) in conn.execute(text("SELECT default_work_week_pattern_id FROM legal_entities WHERE company_id = :c"),
                                           {"c": x["company"]})} == {week.id}

        calendars = {r.code: r for r in conn.execute(text("SELECT * FROM holiday_calendars WHERE company_id = :c"),
                                                     {"c": x["company"]})}
        assert set(calendars) == {"COMPANY", "BR-MAIN", "BR-LE01-PNQ", "BR-AE01-PNQ", "UNMATCHED"}
        company_cal = calendars["COMPANY"].id
        assert calendars["COMPANY"].parent_calendar_id is None and calendars["UNMATCHED"].parent_calendar_id is None
        assert {calendars[c].parent_calendar_id for c in ("BR-MAIN", "BR-LE01-PNQ", "BR-AE01-PNQ")} == {company_cal}
        assert (calendars["BR-LE01-PNQ"].country_code, calendars["BR-LE01-PNQ"].subdivision_code) == ("IN", "IN-MH")
        assert (calendars["BR-AE01-PNQ"].country_code, calendars["BR-AE01-PNQ"].name) == ("AE", "Dubai holidays")
        branches = {r.id: r for r in conn.execute(text("SELECT * FROM branches WHERE company_id = :c"), {"c": x["company"]})}
        assert {branches[x[k]].holiday_calendar_id: k for k in ("main", "pune", "dubai")} == {
            calendars["BR-MAIN"].id: "main", calendars["BR-LE01-PNQ"].id: "pune", calendars["BR-AE01-PNQ"].id: "dubai"}
        assert {b.work_week_pattern_id for b in branches.values()} == {week.id}
        assert calendars["UNMATCHED"].id not in {b.holiday_calendar_id for b in branches.values()}

        holidays = {r.name: r for r in conn.execute(text("SELECT * FROM holidays WHERE company_id = :c"), {"c": x["company"]})}
        assert {name: (h.calendar_id, h.holiday_type, h.location, h.is_optional) for name, h in holidays.items()} == {
            "New Year": (company_cal, "COMPANY", None, False),
            "Diwali": (calendars["BR-LE01-PNQ"].id, "OPTIONAL", "Pune", True),
            "National Day": (calendars["BR-AE01-PNQ"].id, "COMPANY", "Dubai", False),
            "Typo Day": (calendars["UNMATCHED"].id, "COMPANY", "pune ", False),
            "Old Office Day": (calendars["UNMATCHED"].id, "OPTIONAL", "Old Office", True),
        }
        # Pune's effective holidays: its own calendar plus the inherited company calendar.
        lineage = conn.execute(text(
            "WITH RECURSIVE up(id) AS (SELECT CAST(:cal AS integer) UNION ALL "
            "SELECT c.parent_calendar_id FROM holiday_calendars c JOIN up ON c.id = up.id WHERE c.parent_calendar_id IS NOT NULL) "
            "SELECT h.name FROM holidays h WHERE h.calendar_id IN (SELECT id FROM up) ORDER BY h.name"),
            {"cal": calendars["BR-LE01-PNQ"].id}).scalars().all()
        assert lineage == ["Diwali", "New Year"]

        quiet = {r for (r,) in conn.execute(text("SELECT code FROM holiday_calendars WHERE company_id = :c"), {"c": y["company"]})}
        assert quiet == {"COMPANY", "BR-MAIN"}  # EC-MIG-09: no UNMATCHED calendar when every holiday matched
        before = _snapshot(conn)

    with as_owner() as conn:  # EC-MIG-14: a second run adds and changes nothing
        again = migration.backfill(conn)
        assert set(again.values()) == {0}, again
        assert _snapshot(conn) == before


def test_ec_mig_14_backfill_never_overwrites_values_set_after_the_migration(app):
    migration = _migration()
    x = _legacy_tenant("Old Release Trading", "old-release-trading", with_second_entity=True)
    with as_owner() as conn:
        migration.backfill(conn)
        weekly = conn.execute(text("INSERT INTO work_week_patterns (company_id, code, name, weekly_off_days) "
                                   "VALUES (:c, 'SUN', 'Sunday off', CAST('[6]' AS jsonb)) RETURNING id"),
                              {"c": x["company"]}).scalar_one()
        conn.execute(text("UPDATE branches SET work_week_pattern_id = :w WHERE id = :b"), {"w": weekly, "b": x["pune"]})
        conn.execute(text("UPDATE holidays SET holiday_type = 'PUBLIC' WHERE name = 'Diwali'"))
        conn.execute(text("UPDATE employee_assignments SET shift_id = NULL WHERE id = :id"), {"id": x["mover_now"]})
        # A branch and a holiday created later by today's code, without the new columns.
        conn.execute(text("INSERT INTO branches (company_id, legal_entity_id, code, name, country_code, timezone, address, status) "
                          "VALUES (:c, :le, 'BOM', 'Mumbai', 'IN', 'Asia/Kolkata', '{}', 'ACTIVE')"),
                     {"c": x["company"], "le": x["india"]})
        conn.execute(text("INSERT INTO holidays (company_id, name, date, location, is_optional, created_at, updated_at) "
                          "VALUES (:c, 'Mumbai Day', '2026-12-01', 'Mumbai', true, now(), now())"), {"c": x["company"]})
        summary = migration.backfill(conn)
        assert (summary["holiday_calendars"], summary["calendar_holidays"], summary["branch_work_weeks"]) == (1, 1, 1)
        assert conn.execute(text("SELECT work_week_pattern_id FROM branches WHERE id = :b"), {"b": x["pune"]}).scalar() == weekly
        assert conn.execute(text("SELECT holiday_type FROM holidays WHERE name = 'Diwali'")).scalar() == "PUBLIC"
        mumbai = conn.execute(text(
            "SELECT h.holiday_type, c.code, c.parent_calendar_id IS NOT NULL FROM holidays h "
            "JOIN holiday_calendars c ON c.id = h.calendar_id WHERE h.name = 'Mumbai Day'")).one()
        assert tuple(mumbai) == ("OPTIONAL", "BR-BOM", True)
        # The shift fill only touches NULL rows, so a cleared shift is filled again from the employee.
        assert conn.execute(text("SELECT shift_id FROM employee_assignments WHERE id = :id"),
                            {"id": x["mover_now"]}).scalar() == x["shift"]


# ---------------------------------------------------------------------------
# Upgrade, rollback and re-upgrade on a scratch database (real migration run)
# ---------------------------------------------------------------------------
def _alembic(url, *args):
    import os
    import subprocess
    import sys

    env = {**os.environ, "MIGRATION_DATABASE_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=MIGRATION.parents[2], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def _seed(conn, table, **values):
    """Insert a row through plain SQL (the ORM models describe the newest schema), filling the
    NOT NULL columns that only have Python-side defaults."""
    from datetime import datetime, timezone

    kinds = dict(conn.execute(text("SELECT column_name, data_type FROM information_schema.columns "
                                   "WHERE table_schema = 'public' AND table_name = :t"), {"t": table}).fetchall())
    required = [name for (name,) in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t "
        "AND is_nullable = 'NO' AND column_default IS NULL"), {"t": table})]
    fill = {"boolean": False, "integer": 0, "jsonb": {}, "timestamp with time zone": datetime.now(timezone.utc)}
    row = {name: fill.get(kinds[name]) for name in required if name not in values}
    assert None not in row.values(), (table, [k for k, v in row.items() if v is None])
    row.update(values)
    row = {k: json.dumps(v) if kinds[k] == "jsonb" else v for k, v in row.items()}
    placeholders = ", ".join(f"CAST(:{k} AS jsonb)" if kinds[k] == "jsonb" else f":{k}" for k in row)
    return conn.execute(text(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({placeholders}) RETURNING id"), row).scalar_one()


def test_upgrade_downgrade_and_upgrade_again_with_legacy_data(app):
    """The real migration on a populated 0007 database: backfill, then a rollback that keeps every
    legacy row and deletes PAY_GROUP grants, then a second upgrade (rollback rehearsal)."""
    import os

    from sqlalchemy import create_engine, inspect
    from sqlalchemy.engine import make_url

    owner_url = make_url(os.environ["MIGRATION_DATABASE_URL"])
    scratch = "hrms_test_m0008"
    admin = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
            conn.execute(text(f"CREATE DATABASE {scratch}"))
    except DBAPIError:
        admin.dispose()
        pytest.skip("the test server does not allow creating a scratch database")
    url = owner_url.set(database=scratch).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        _alembic(url, "upgrade", "0007_partner_admins")
        with engine.begin() as conn:
            cid = _seed(conn, "companies", name="Legacy Co", slug="legacy-co", status="active", currency="AED",
                        timezone="Asia/Dubai", subscription_status="active", subscription_plan="trial")
            entity = _seed(conn, "legal_entities", company_id=cid, code="LE01", legal_name="Legacy Co", country_code="AE",
                           base_currency="AED", timezone="Asia/Dubai", fiscal_year_start="01-01", status="ACTIVE",
                           is_default=True, country_confirmed=False)
            branch = _seed(conn, "branches", company_id=cid, legal_entity_id=entity, code="DXB", name="Dubai",
                           country_code="AE", timezone="Asia/Dubai", status="ACTIVE")
            shift = _seed(conn, "shifts", company_id=cid, name="Day", start_time="09:00", end_time="18:00",
                          is_flexible=False, is_active=True)
            employee = _seed(conn, "employees", company_id=cid, employee_code="E1", first_name="Layla", last_name="Haddad",
                             email="layla@legacy.example", hire_date=date(2024, 1, 1), location="Dubai", shift_id=shift)
            placement = _seed(conn, "employee_assignments", company_id=cid, employee_id=employee, legal_entity_id=entity,
                              branch_id=branch, is_primary=True, reason="MIGRATION", effective_from=date(2024, 1, 1))
            _seed(conn, "holidays", company_id=cid, name="National Day", date=date(2026, 12, 2), location="Dubai",
                  is_optional=True)
            _seed(conn, "holidays", company_id=cid, name="New Year", date=date(2027, 1, 1), is_optional=False)

        _alembic(url, "upgrade", "0008_payroll_org")
        with engine.begin() as conn:
            row = conn.execute(text("SELECT g.code, g.currency, a.shift_id, a.fte, t.code FROM employee_assignments a "
                                    "JOIN pay_groups g ON g.id = a.pay_group_id JOIN employment_types t "
                                    "ON t.id = a.employment_type_id WHERE a.id = :id"), {"id": placement}).one()
            assert tuple(row) == ("MONTHLY", "AED", shift, Decimal("1"), "PERMANENT")
            holidays = conn.execute(text("SELECT h.name, c.code, h.holiday_type FROM holidays h "
                                         "JOIN holiday_calendars c ON c.id = h.calendar_id ORDER BY h.name")).fetchall()
            assert [tuple(h) for h in holidays] == [("National Day", "BR-DXB", "OPTIONAL"), ("New Year", "COMPANY", "COMPANY")]
            role = _seed(conn, "roles", company_id=cid, name="Pay group clerk", is_system_role=False, is_protected=False,
                         is_full_access=False)
            _seed(conn, "role_permissions", company_id=cid, role_id=role, permission_code="employee:read", scope="PAY_GROUP")
            _seed(conn, "role_permissions", company_id=cid, role_id=role, permission_code="employee:read", scope="COMPANY")

        _alembic(url, "downgrade", "0007_partner_admins")
        inspector = inspect(engine)
        assert not set(NEW_TABLES) & set(inspector.get_table_names())
        assert "pay_group_id" not in {c["name"] for c in inspector.get_columns("employee_assignments")}
        assert "details" not in {c["name"] for c in inspector.get_columns("audit_logs")}
        assert "holiday_type" not in {c["name"] for c in inspector.get_columns("holidays")}
        with engine.begin() as conn:
            assert conn.execute(text("SELECT scope FROM role_permissions")).scalars().all() == ["COMPANY"]
            assert tuple(conn.execute(text("SELECT location, is_optional FROM holidays WHERE name = 'National Day'")).one()) == (
                "Dubai", True)
            assert conn.execute(text("SELECT count(*) FROM employee_assignments")).scalar() == 1
        # The 0005 guard is back: overlapping primary placements are still refused.
        with pytest.raises(DBAPIError, match="cannot overlap"):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, "
                                  "is_primary, reason, effective_from) VALUES (:c, :e, :le, :b, true, 'CORRECTION', "
                                  "'2025-01-01')"), {"c": cid, "e": employee, "le": entity, "b": branch})

        _alembic(url, "upgrade", "0008_payroll_org")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM pay_groups")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM holidays WHERE calendar_id IS NULL")).scalar() == 0
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"), {"d": scratch})
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        admin.dispose()
