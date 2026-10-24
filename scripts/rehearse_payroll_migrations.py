"""Rehearse the payroll v2 migrations (0008 .. head) on representative pre-payroll-v2 data.

    python scripts/rehearse_payroll_migrations.py [--admin-url URL] [--db-name NAME] [--keep] [--json PATH]

What it does, on a scratch database it creates and drops (never the demo stack's .local-postgres):

 1. ``alembic upgrade 0007_partner_admins`` (the last revision before payroll v2), then seeds legacy data as
    the old release wrote it (plain SQL, the ORM describes the newest schema): three tenants (INR, AED and an
    empty one), legal entities, branches, shifts, system and custom roles with legacy payroll codes, users
    (one with a plaintext TOTP secret), employees (a terminated one with a placement after the termination,
    one without a placement, one with a blank location), salary_structure rows (overlapping, same start,
    future, unplaceable), legacy payroll rows (draft, approved, processed, a clipped net) with issued payslips,
    company_settings (valid and invalid legacy values), attendance, leave types, leaves and holidays, and the
    privileges a 0007 deployment gave its RLS-restricted application role;
 2. ``alembic upgrade head`` and checks the legacy migration results: LEGACY_V1 profile (0011), permissions v2
    (0010), the compensation backfill (0015), terminated placements (0013), leave days (0016), encrypted TOTP
    secrets (0026), the overtime formula (0029), the grants the migrations give the existing application role,
    then setup-db-roles (``grant_app_role``) and row-level security as the application role;
 3. ``alembic downgrade 0027_payslip_queries``, seeds tasks the way 0017..0027 wrote them, and upgrades to head
    again (task backfill 0028, overtime formula revert/re-apply 0029);
 4. with a PAYROLL_PII data key present, ``alembic downgrade 0007_partner_admins`` must be refused by 0012 and
    leave the database exactly at head (one transaction); without it the downgrade runs, then
    ``alembic upgrade head`` once more.

At every stage the rows of every 0007 table are compared column by column with the seeded state. Legacy payroll
rows, salary structures, issued payslips, attendance, leave, employees and settings must be identical (sha256
per table in the report); the only differences allowed are listed in EXPECTED_* below and each one is asserted
exactly. The schema after the downgrade must equal the 0007 schema and the schema after the second upgrade the
first head schema (columns, constraints, indexes, triggers, policies, functions, row security).

Run it alone (it takes the machine-wide heavy-command lock when it starts its own server). Without
``--admin-url`` it uses the embedded PostgreSQL of the test suite (pgserver, data in the system temp directory).
tests/payroll/test_migration_rehearsal.py runs the same rehearsal inside the database test suite. Exit status 0
means every check passed; the JSON report lists every check and difference.
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LEGACY_REVISION = "0007_partner_admins"
BEFORE_TASK_BACKFILL = "0027_payslip_queries"
HEAD = "head"
DEFAULT_DB = "hrms_rehearsal_payroll_v2"
APP_ROLE = "hrms_rehearsal_app"
FORBIDDEN_DATA_DIR = ".local-postgres"  # the user's demo stack

# Frozen copies of the literals the migrations write (they must not follow later code changes).
OLD_OT_FORMULA = 'SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day'
NEW_OT_FORMULA = 'SUM_TAG_HOURLY("OT_BASE")'
OLD_ADMIN_DESCRIPTION = "Full control of this company's workspace. Only administrators can grant this role."
OLD_PAYROLL_MANAGER_DESCRIPTION = "Prepares, approves and pays payroll. Sees compensation for the whole company."
TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # RFC 6238 style example value, not a real account
FAKE_RUN_ID = 990001  # tasks.paid_in_run_id is not a foreign key (0017)

# Legacy tables whose rows must never change, in any stage, in any 0007 column.
FROZEN_TABLES = (
    "payroll", "salary_structure", "issued_documents", "attendance", "leaves", "leave_types", "leave_approvals",
    "employees", "company_settings", "holidays", "legal_entities", "branches", "departments", "shifts",
    "companies", "user_role_assignment_targets", "brand_profiles", "tenant_domains",
)


class RehearsalError(AssertionError):
    pass


# ----------------------------------------------------------------------------------------------- helpers
def _norm(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        text = format(value.normalize(), "f")
        return "0" if text in ("-0",) else text
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return format(Decimal(str(value)).normalize(), "f") if isinstance(value, float) else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return str(value)


def _sha(rows) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _alembic(url: str, env_extra: dict, *args) -> str:
    env = {**os.environ, **env_extra, "DATABASE_URL": url, "SYSTEM_DATABASE_URL": url, "MIGRATION_DATABASE_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True,
                            text=True)
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise RehearsalError(f"alembic {' '.join(args)} failed:\n{output[-6000:]}")
    return output


def _seed(conn, table, **values):
    """INSERT through plain SQL against the schema of the current revision (NOT NULL columns without a
    database default get a neutral value), RETURNING id."""
    from sqlalchemy import text

    kinds = dict(conn.execute(text("SELECT column_name, data_type FROM information_schema.columns "
                                   "WHERE table_schema = 'public' AND table_name = :t"), {"t": table}).fetchall())
    required = [name for (name,) in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t "
        "AND is_nullable = 'NO' AND column_default IS NULL"), {"t": table})]
    stamp = dt.datetime(2025, 3, 31, 12, 0, tzinfo=dt.timezone.utc)
    fill = {"boolean": False, "integer": 0, "jsonb": {}, "timestamp with time zone": stamp}
    row = {name: fill.get(kinds[name]) for name in required if name not in values}
    missing = [k for k, v in row.items() if v is None]
    if missing:
        raise RehearsalError(f"seed {table}: no value for {missing}")
    row.update(values)
    unknown = set(row) - set(kinds)
    if unknown:
        raise RehearsalError(f"seed {table}: unknown columns {sorted(unknown)}")
    row = {k: json.dumps(v) if kinds[k] == "jsonb" else v for k, v in row.items()}
    placeholders = ", ".join(f"CAST(:{k} AS jsonb)" if kinds[k] == "jsonb" else f":{k}" for k in row)
    return conn.execute(text(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({placeholders}) RETURNING id"),
                        row).scalar_one()


def _columns(conn) -> dict:
    from sqlalchemy import text

    out = {}
    for table, column in conn.execute(text(
            "SELECT c.table_name, c.column_name FROM information_schema.columns c JOIN information_schema.tables t "
            "ON t.table_schema = c.table_schema AND t.table_name = c.table_name WHERE c.table_schema = 'public' "
            "AND t.table_type = 'BASE TABLE' AND c.table_name <> 'alembic_version' "
            "ORDER BY c.table_name, c.ordinal_position")):
        out.setdefault(table, []).append(column)
    return out


def snapshot(conn, columns: dict) -> dict:
    """{table: {key: {column: normalized value}}} for the given columns of each table (key = id)."""
    from sqlalchemy import text

    out = {}
    for table, cols in columns.items():
        quoted = ", ".join(f'"{c}"' for c in cols)
        rows = conn.execute(text(f'SELECT {quoted} FROM "{table}"')).fetchall()
        data = {}
        for row in rows:
            record = {c: _norm(v) for c, v in zip(cols, row)}
            key = record["id"] if "id" in record else _sha(record)
            data[key] = record
        out[table] = data
    return out


def diff(before: dict, after: dict, tables=None) -> dict:
    """{table: {"added": [ids], "removed": [ids], "changed": {id: {column: [old, new]}}}} (only differences)."""
    out = {}
    for table in sorted(tables or before):
        old, new = before.get(table, {}), after.get(table, {})
        added = sorted(set(new) - set(old), key=str)
        removed = sorted(set(old) - set(new), key=str)
        changed = {}
        for key in set(old) & set(new):
            cols = {c: [old[key][c], new[key].get(c)] for c in old[key] if old[key][c] != new[key].get(c)}
            if cols:
                changed[key] = cols
        if added or removed or changed:
            out[table] = {"added": added, "removed": removed,
                          "changed": dict(sorted(changed.items(), key=lambda kv: str(kv[0])))}
    return out


def schema_fingerprint(conn) -> dict:
    from sqlalchemy import text

    def rows(sql):
        return sorted(tuple(_norm(v) for v in r) for r in conn.execute(text(sql)).fetchall())

    return {
        "columns": rows("SELECT table_name, column_name, data_type, character_maximum_length, numeric_precision, "
                        "numeric_scale, is_nullable, column_default FROM information_schema.columns "
                        "WHERE table_schema = 'public'"),
        "constraints": rows("SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE connamespace = 'public'::regnamespace"),
        "indexes": rows("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"),
        "triggers": rows("SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                         "WHERE NOT tgisinternal AND tgrelid IN (SELECT oid FROM pg_class "
                         "WHERE relnamespace = 'public'::regnamespace)"),
        "policies": rows("SELECT tablename, policyname, cmd, permissive, roles::text, qual, with_check "
                         "FROM pg_policies WHERE schemaname = 'public'"),
        "row_security": rows("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                             "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p')"),
        # Bodies compared without layout: 0008's downgrade re-creates the 0005 guard with other indentation.
        "functions": sorted((name, " ".join(body.split())) for name, body in rows(
            "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
            "WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f'")),
        "table_grants": rows(f"SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                             f"WHERE table_schema = 'public' AND grantee = '{APP_ROLE}'"),
    }


def schema_diff(a: dict, b: dict) -> dict:
    out = {}
    for part in a:
        left, right = set(map(tuple, a[part])), set(map(tuple, b[part]))
        if left != right:
            out[part] = {"only_before": sorted(left - right)[:40], "only_after": sorted(right - left)[:40]}
    return out


# ----------------------------------------------------------------------------------------------- legacy data
def _legacy_roles(conn, cid, *, with_payroll_manager: bool, admin_description: str) -> dict:
    roles = {}
    specs = [
        ("company_admin", "Company Administrator", admin_description, True, True, ()),
        ("hr_admin", "HR Administrator", "Manages people records.", False, False,
         (("employee:read", "COMPANY"), ("payroll:read", "COMPANY"), ("compensation:read", "COMPANY"))),
        ("manager", "Manager", "Manages a team.", False, False, (("employee:read", "DIRECT_REPORTS"),)),
        ("employee", "Employee", "Self service.", False, False,
         (("payroll:read", "SELF"), ("compensation:read", "SELF"), ("attendance:read", "SELF"))),
    ]
    if with_payroll_manager:
        specs.append(("payroll_manager", "Payroll Manager", OLD_PAYROLL_MANAGER_DESCRIPTION, False, False,
                      tuple((c, "COMPANY") for c in ("payroll:read", "payroll:prepare", "payroll:approve",
                                                     "payroll:process", "payroll:export", "compensation:read",
                                                     "compensation:manage"))))
    for key, name, description, full, protected, grants in specs:
        rid = _seed(conn, "roles", company_id=cid, name=name, description=description, system_key=key,
                    is_system_role=True, is_protected=protected, is_full_access=full)
        for code, scope in grants:
            _seed(conn, "role_permissions", company_id=cid, role_id=rid, permission_code=code, scope=scope)
        roles[key] = rid
    return roles


def _user(conn, cid, email, name, role_id, **extra):
    uid = _seed(conn, "users", company_id=cid, email=email, full_name=name, password_hash="!rehearsal-no-login",
                status="active", mfa_enabled=bool(extra.get("mfa_secret")), failed_login_count=0,
                permission_version=1, **extra)
    _seed(conn, "user_role_assignments", company_id=cid, user_id=uid, role_id=role_id,
          effective_from=dt.datetime(2024, 1, 10, tzinfo=dt.timezone.utc))
    return uid


def _payroll_row(conn, cid, entity, employee, month, status, parts, currency, *, bonus="0", ot_hours="0",
                 ot_amount="0", deductions="0", net=None, approver=None, wdays=21):
    parts = [Decimal(p) for p in parts]
    gross = sum(parts, Decimal("0"))
    net_value = Decimal(net) if net is not None else gross + Decimal(ot_amount) + Decimal(bonus) - Decimal(deductions)
    stamp = dt.datetime(month.year, month.month, 28, 10, 0, tzinfo=dt.timezone.utc)
    values = dict(company_id=cid, employee_id=employee, salary_month=month, basic_salary=parts[0],
                  house_rent_allowance=parts[1], dearness_allowance=parts[2], conveyance_allowance=parts[3],
                  other_allowances=parts[4], bonus=Decimal(bonus), gross_salary=gross, provident_fund=Decimal("0"),
                  insurance=Decimal("0"), professional_tax=Decimal("0"), income_tax=Decimal("0"),
                  other_deductions=Decimal("0"), total_deductions=Decimal(deductions),
                  overtime_hours=Decimal(ot_hours), overtime_amount=Decimal(ot_amount), net_salary=net_value,
                  working_days=wdays, present_days=wdays, absent_days=0, leave_days=0, status=status,
                  currency=currency, legal_entity_id=entity, created_at=stamp, updated_at=stamp,
                  remarks=f"legacy {status} row")
    if status in ("approved", "processed"):
        values.update(approved_by_user_id=approver, approved_at=stamp + dt.timedelta(hours=2))
    if status == "processed":
        values.update(processed_by_user_id=approver, processed_at=stamp + dt.timedelta(hours=4))
    return _seed(conn, "payroll", **values)


def seed_legacy(conn, today: dt.date) -> dict:
    """Pre-payroll-v2 data as the 0007 release wrote it. Returns the ids the checks need."""
    created = dt.datetime(2024, 1, 10, 8, 0, tzinfo=dt.timezone.utc)
    ids = {}

    # ---------------- Tenant A: India, INR, the full legacy payroll history
    a = _seed(conn, "companies", name="Rehearsal Legacy India", slug="rehearsal-legacy-india", status="active",
              subscription_status="active", subscription_plan="growth", timezone="Asia/Kolkata", currency="INR",
              country_code="IN", created_at=created, updated_at=created)
    le_a = _seed(conn, "legal_entities", company_id=a, code="LE-IN", legal_name="Rehearsal Legacy India Pvt Ltd",
                 country_code="IN", base_currency="INR", timezone="Asia/Kolkata", is_default=True)
    bom = _seed(conn, "branches", company_id=a, legal_entity_id=le_a, code="BOM", name="Mumbai", country_code="IN",
                timezone="Asia/Kolkata")
    pnq = _seed(conn, "branches", company_id=a, legal_entity_id=le_a, code="PNQ", name="Pune", country_code="IN",
                timezone="Asia/Kolkata")
    eng = _seed(conn, "departments", company_id=a, name="Engineering", is_active=True)
    shift = _seed(conn, "shifts", company_id=a, name="General", start_time="09:30", end_time="18:30",
                  duration_hours=9, is_flexible=False, is_active=True)
    roles_a = _legacy_roles(conn, a, with_payroll_manager=True, admin_description=OLD_ADMIN_DESCRIPTION)
    clerk_role = _seed(conn, "roles", company_id=a, name="Payroll Clerk", description="Custom legacy role",
                       is_system_role=False, is_protected=False, is_full_access=False)
    for code, scope in (("payroll:read", "COMPANY"), ("payroll:prepare", "COMPANY"), ("compensation:read", "SELF")):
        _seed(conn, "role_permissions", company_id=a, role_id=clerk_role, permission_code=code, scope=scope)
    admin_a = _user(conn, a, "admin@rehearsal-india.example", "Aarav Admin", roles_a["company_admin"],
                    mfa_secret=TOTP_SECRET)
    _user(conn, a, "hr@rehearsal-india.example", "Hema HR", roles_a["hr_admin"])
    clerk = _user(conn, a, "clerk@rehearsal-india.example", "Chetan Clerk", clerk_role)
    emp_user = _user(conn, a, "asha@rehearsal-india.example", "Asha Rao", roles_a["employee"])

    def employee(cid, code, first, last, *, location, hire, branch, entity, dept=None, user=None, active=True,
                 terminated=None, base="0", shift_id=None):
        eid = _seed(conn, "employees", company_id=cid, employee_code=code, first_name=first, last_name=last,
                    email=f"{first.lower()}.{last.lower()}@{'gulf' if cid != a else 'india'}.rehearsal.example",
                    hire_date=hire, location=location, department_id=dept, shift_id=shift_id, user_id=user,
                    is_active=active, termination_date=terminated, base_salary=Decimal(base),
                    created_at=created, updated_at=created)
        if branch is not None:
            _seed(conn, "employee_assignments", company_id=cid, employee_id=eid, legal_entity_id=entity,
                  branch_id=branch, department_id=dept, is_primary=True, reason="MIGRATION", effective_from=hire)
        return eid

    asha = employee(a, "IN001", "Asha", "Rao", location="Mumbai", hire=dt.date(2024, 1, 15), branch=bom, entity=le_a,
                    dept=eng, user=emp_user, base="49000", shift_id=shift)
    ravi = employee(a, "IN002", "Ravi", "Kumar", location="Pune", hire=dt.date(2024, 1, 15), branch=pnq, entity=le_a,
                    base="35000", shift_id=shift)
    meera = employee(a, "IN003", "Meera", "Iyer", location="", hire=dt.date(2024, 6, 1), branch=bom, entity=le_a,
                     base="30000")
    # Terminated on 15 Apr 2025 by the old release: its placement stays open and a transfer recorded earlier
    # starts after the termination (0013 D-4 repairs both).
    kabir = employee(a, "IN004", "Kabir", "Shah", location="Mumbai", hire=dt.date(2024, 1, 15), branch=None,
                     entity=le_a, active=False, terminated=dt.date(2025, 4, 15), base="20000")
    kabir_first = _seed(conn, "employee_assignments", company_id=a, employee_id=kabir, legal_entity_id=le_a,
                        branch_id=bom, is_primary=True, reason="MIGRATION", effective_from=dt.date(2024, 1, 15),
                        effective_to=dt.date(2025, 5, 31))
    kabir_future = _seed(conn, "employee_assignments", company_id=a, employee_id=kabir, legal_entity_id=le_a,
                         branch_id=pnq, is_primary=True, reason="TRANSFER", effective_from=dt.date(2025, 6, 1))
    nisha = employee(a, "IN005", "Nisha", "Paul", location="Pune", hire=dt.date(2024, 2, 1), branch=None,
                     entity=le_a, base="25000")  # no placement: her structure cannot be migrated

    def structure(cid, emp, start, parts, end=None):
        return _seed(conn, "salary_structure", company_id=cid, employee_id=emp, basic_salary=Decimal(parts[0]),
                     house_rent_allowance=Decimal(parts[1]), dearness_allowance=Decimal(parts[2]),
                     conveyance_allowance=Decimal(parts[3]), other_allowances=Decimal(parts[4]),
                     effective_from=start, effective_to=end, created_by_user_id=admin_a if cid == a else None,
                     created_at=created, updated_at=created)

    full = ("30000", "12000", "3000", "1600", "2400")
    s = ids["structures"] = {}
    s["asha_2024"] = structure(a, asha, dt.date(2024, 2, 1), full)  # open-ended, overlapped by the next row
    s["asha_2025"] = structure(a, asha, dt.date(2025, 4, 1), ("40000", "16000", "0", "0", "0"))
    s["ravi_first"] = structure(a, ravi, dt.date(2024, 5, 1), ("30000", "5000", "0", "0", "0"))
    s["ravi_same"] = structure(a, ravi, dt.date(2024, 5, 1), ("35000", "0", "0", "0", "0"))
    s["meera_future"] = structure(a, meera, today + dt.timedelta(days=40), ("100.10", "0", "0", "0", "0"))
    s["kabir"] = structure(a, kabir, dt.date(2024, 3, 1), ("20000", "0", "0", "0", "0"), end=dt.date(2025, 4, 15))
    s["nisha_unplaced"] = structure(a, nisha, dt.date(2024, 2, 1), ("25000", "0", "0", "0", "0"))

    march, april, may = dt.date(2025, 3, 1), dt.date(2025, 4, 1), dt.date(2025, 5, 1)
    p = ids["payroll"] = {}
    p["asha_mar"] = _payroll_row(conn, a, le_a, asha, march, "processed", full, "INR", bonus="1000.50",
                                 ot_hours="4.5", ot_amount="937.50", deductions="4466.66", approver=admin_a)
    p["ravi_mar"] = _payroll_row(conn, a, le_a, ravi, march, "processed", ("35000", "0", "0", "0", "0"), "INR",
                                 approver=admin_a)
    p["kabir_mar"] = _payroll_row(conn, a, le_a, kabir, march, "processed", ("20000", "0", "0", "0", "0"), "INR",
                                  approver=admin_a)
    p["asha_apr"] = _payroll_row(conn, a, le_a, asha, april, "approved", ("40000", "16000", "0", "0", "0"), "INR",
                                 approver=admin_a)
    # Absent on every day: deductions exceed gross and the old engine clipped net at zero (defect D2).
    p["kabir_apr"] = _payroll_row(conn, a, le_a, kabir, april, "approved", ("1000", "0", "0", "0", "0"), "INR",
                                  deductions="1400.00", net="0", approver=admin_a, wdays=20)
    p["asha_may"] = _payroll_row(conn, a, le_a, asha, may, "draft", ("40000", "16000", "0", "0", "0"), "INR")
    for key in ("asha_mar", "ravi_mar", "kabir_mar"):
        _seed(conn, "issued_documents", company_id=a, document_type="PAYSLIP", source_id=p[key],
              legal_entity_id=le_a, template_version="payslip-v1", snapshot={"payroll_id": p[key], "legacy": True},
              sha256=hashlib.sha256(str(p[key]).encode()).hexdigest(), storage_path=f"payslips/{p[key]}.pdf",
              issued_by_user_id=admin_a)

    for key, value in (("payrollSeparationOfDuties", "true"), ("graceTime", "10"), ("lunchIsPaid", "true"),
                       ("overtime", "1.5"), ("pf", "12.0"), ("tax", "10.0")):
        _seed(conn, "company_settings", company_id=a, key=key, value=value)

    for day, status in ((3, "present"), (4, "absent"), (5, "late"), (6, "half-day")):
        start = dt.datetime(2025, 3, day, 4, 15, tzinfo=dt.timezone.utc)
        _seed(conn, "attendance", company_id=a, employee_id=asha, date=dt.date(2025, 3, day), check_in_time=start,
              check_out_time=start + dt.timedelta(hours=9), shift_id=shift, is_manual=True, source="manual",
              status=status, recorded_by_user_id=admin_a)
    _seed(conn, "leave_types", company_id=a, code="CL", name="Casual leave", annual_quota=12, is_paid=True,
          is_unlimited=False, is_active=True)
    _seed(conn, "leave_types", company_id=a, code="LWP", name="Leave without pay", annual_quota=0, is_paid=False,
          is_unlimited=True, is_active=True)
    ids["approved_leave"] = _seed(conn, "leaves", company_id=a, employee_id=ravi, leave_type="CL",
                                  start_date=dt.date(2025, 3, 10), end_date=dt.date(2025, 3, 12), number_of_days=3,
                                  status="approved", decided_by_user_id=admin_a,
                                  decided_at=dt.datetime(2025, 3, 5, tzinfo=dt.timezone.utc))
    _seed(conn, "leaves", company_id=a, employee_id=asha, leave_type="LWP", start_date=dt.date(2025, 5, 20),
          end_date=dt.date(2025, 5, 20), number_of_days=1, status="pending")
    _seed(conn, "leaves", company_id=a, employee_id=ravi, leave_type="CL", start_date=dt.date(2025, 2, 3),
          end_date=dt.date(2025, 2, 4), number_of_days=2, status="rejected")
    _seed(conn, "holidays", company_id=a, name="Republic Day", date=dt.date(2025, 1, 26), is_optional=False)
    _seed(conn, "holidays", company_id=a, name="Ganesh Chaturthi", date=dt.date(2025, 8, 27), location="Mumbai",
          is_optional=True)
    _seed(conn, "audit_logs", company_id=a, actor_type="user", actor_id=admin_a, actor_email="admin@rehearsal-india.example",
          module="Payroll", action="APPROVED", entity_type="payroll", entity_id=str(p["asha_apr"]),
          description="Payroll approved (legacy release)")

    # ---------------- Tenant B: UAE, AED, customised admin role, payroll_manager deleted, invalid settings
    b = _seed(conn, "companies", name="Rehearsal Gulf Trading", slug="rehearsal-gulf-trading", status="active",
              subscription_status="trialing", subscription_plan="trial", timezone="Asia/Dubai", currency="AED",
              country_code="AE", created_at=created + dt.timedelta(days=30), updated_at=created)
    le_b = _seed(conn, "legal_entities", company_id=b, code="LE-AE", legal_name="Rehearsal Gulf Trading LLC",
                 country_code="AE", base_currency="AED", timezone="Asia/Dubai", is_default=True, country_confirmed=False)
    dxb = _seed(conn, "branches", company_id=b, legal_entity_id=le_b, code="DXB", name="Dubai", country_code="AE",
                timezone="Asia/Dubai")
    roles_b = _legacy_roles(conn, b, with_payroll_manager=False, admin_description="Our own administrator text")
    admin_b = _user(conn, b, "admin@rehearsal-gulf.example", "Bilal Admin", roles_b["company_admin"])
    omar = employee(b, "AE001", "Omar", "Haddad", location="Dubai", hire=dt.date(2024, 3, 1), branch=dxb, entity=le_b,
                    base="15000")
    layla = employee(b, "AE002", "Layla", "Nasser", location="Dubai", hire=dt.date(2024, 3, 1), branch=dxb,
                     entity=le_b, base="12000")
    s["omar"] = structure(b, omar, dt.date(2024, 3, 1), ("10000", "5000", "0", "0", "0"))
    s["layla"] = structure(b, layla, dt.date(2024, 3, 1), ("12000", "0", "0", "0", "0"))
    p["omar_mar"] = _payroll_row(conn, b, le_b, omar, march, "processed", ("10000", "5000", "0", "0", "0"), "AED",
                                 approver=admin_b, wdays=22)
    p["layla_mar"] = _payroll_row(conn, b, le_b, layla, march, "processed", ("12000", "0", "0", "0", "0"), "AED",
                                  approver=admin_b, wdays=22)
    for key, value in (("graceTime", '"abc"'), ("overtime", "20"), ("lunchIsPaid", "false")):
        _seed(conn, "company_settings", company_id=b, key=key, value=value)
    _seed(conn, "holidays", company_id=b, name="National Day", date=dt.date(2025, 12, 2), location="Dubai",
          is_optional=False)

    # ---------------- Tenant C: an empty workspace (signed up, nothing entered)
    c = _seed(conn, "companies", name="Rehearsal Empty Co", slug="rehearsal-empty-co", status="active",
              subscription_status="trialing", subscription_plan="trial", timezone="Asia/Kolkata", currency="INR",
              created_at=created + dt.timedelta(days=60), updated_at=created)
    le_c = _seed(conn, "legal_entities", company_id=c, code="LE01", legal_name="Rehearsal Empty Co",
                 country_code="IN", base_currency="INR", timezone="Asia/Kolkata", is_default=True)
    _seed(conn, "branches", company_id=c, legal_entity_id=le_c, code="MAIN", name="Main", country_code="IN",
          timezone="Asia/Kolkata")
    roles_c = _legacy_roles(conn, c, with_payroll_manager=True, admin_description=OLD_ADMIN_DESCRIPTION)
    admin_c = _user(conn, c, "admin@rehearsal-empty.example", "Chitra Admin", roles_c["company_admin"])

    ids.update(companies={"A": a, "B": b, "C": c}, entities={"A": le_a, "B": le_b, "C": le_c},
               admins={"A": admin_a, "B": admin_b, "C": admin_c}, clerk_user=clerk, clerk_role=clerk_role,
               roles={"A": roles_a, "B": roles_b, "C": roles_c},
               employees={"asha": asha, "ravi": ravi, "meera": meera, "kabir": kabir, "nisha": nisha, "omar": omar,
                          "layla": layla},
               kabir_first=kabir_first, kabir_future=kabir_future)
    return ids


def grant_0007_app_role(conn, role: str, columns: dict) -> None:
    """The privileges `setup-db-roles` gave the application role on a 0007 deployment (before payroll v2)."""
    from sqlalchemy import text

    tenant = sorted(t for t, cols in columns.items() if "company_id" in cols
                    and t not in ("companies", "audit_logs", "auth_handoff_codes"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    for table in tenant:
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"))
    conn.execute(text(f"REVOKE UPDATE, DELETE ON attendance_events FROM {role}"))
    conn.execute(text(f"REVOKE UPDATE, DELETE ON issued_documents FROM {role}"))
    conn.execute(text(f"GRANT UPDATE (sha256, storage_path, updated_at) ON issued_documents TO {role}"))
    conn.execute(text(f"GRANT SELECT, UPDATE ON companies TO {role}"))
    conn.execute(text(f"GRANT SELECT, INSERT ON audit_logs TO {role}"))
    conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))


def seed_tasks(conn, ids) -> dict:
    """Tasks as 0017..0027 stored them: one shared PAID task (one paid marker on the task) and one APPROVED,
    unpaid task."""
    a, asha, ravi = ids["companies"]["A"], ids["employees"]["asha"], ids["employees"]["ravi"]
    paid = _seed(conn, "tasks", company_id=a, title="Inventory count (shared)", task_type="PIECE_RATE",
                 quantity=Decimal("2"), unit="count", rate=Decimal("500"), rate_currency="INR", status="PAID",
                 approved_on=dt.date(2025, 3, 20), paid_in_run_id=FAKE_RUN_ID, accepted_quantity=Decimal("2"))
    open_task = _seed(conn, "tasks", company_id=a, title="Shelf audit", task_type="PIECE_RATE", quantity=Decimal("1"),
                      unit="count", rate=Decimal("300"), rate_currency="INR", status="APPROVED",
                      approved_on=dt.date(2025, 4, 2), accepted_quantity=Decimal("1"))
    _seed(conn, "task_assignees", company_id=a, task_id=paid, employee_id=asha, split_percent=Decimal("60"))
    _seed(conn, "task_assignees", company_id=a, task_id=paid, employee_id=ravi, split_percent=Decimal("40"))
    _seed(conn, "task_assignees", company_id=a, task_id=open_task, employee_id=asha, split_percent=Decimal("100"))
    return {"paid": paid, "open": open_task}


# ----------------------------------------------------------------------------------------------- checks
class Checks:
    def __init__(self, log):
        self.results = []
        self.log = log

    def check(self, name: str, ok: bool, detail=None) -> bool:
        self.results.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail is not None else {})})
        self.log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok or detail is None else f": {detail}"))
        return bool(ok)

    def equal(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, None if actual == expected else
                          {"actual": actual, "expected": expected})

    @property
    def failed(self):
        return [r for r in self.results if not r["ok"]]


def _one(conn, sql, **params):
    from sqlalchemy import text

    return conn.execute(text(sql), params).scalar()


def _all(conn, sql, **params):
    from sqlalchemy import text

    return [tuple(r) for r in conn.execute(text(sql), params).fetchall()]


def verify_head(conn, ids, checks: Checks, today: dt.date) -> None:
    """Legacy migration results at head (0008 .. 0029)."""
    A, B, C = (ids["companies"][k] for k in "ABC")
    s, e = ids["structures"], ids["employees"]

    # 0011 LEGACY_V1 profile (EC-MIG-03/04/05/06)
    checks.equal("0011 every pre-v2 tenant gets payroll profile LEGACY_V1",
                 _all(conn, "SELECT id, payroll_profile_version FROM companies ORDER BY id"),
                 [(A, "LEGACY_V1"), (B, "LEGACY_V1"), (C, "LEGACY_V1")])
    values = {(cid, key): value for cid, key, value in _all(
        conn, "SELECT company_id, key, value FROM setting_values WHERE source = 'LEGACY_MIGRATION' "
              "AND scope_type = 'TENANT' AND status = 'ACTIVE'")}
    checks.equal("0011 legacy company_settings carried over (EC-MIG-06)",
                 [values.get((A, k)) for k in ("payroll.approval.separation_of_duties", "attendance.grace_minutes",
                                               "attendance.lunch_is_paid")], [True, 10, True])
    checks.equal("0011 invalid legacy graceTime is skipped, valid lunchIsPaid kept (tenant B)",
                 [values.get((B, "attendance.grace_minutes")), values.get((B, "attendance.lunch_is_paid"))], [15, False])
    checks.equal("0011 overtime stays OFF and late penalty is not applied automatically (EC-MIG-03/04)",
                 sorted({values.get((cid, k)) for cid in (A, B, C)
                         for k in ("payroll.overtime.enabled", "payroll.late_penalty.enabled")}, key=str), [False])
    starts = dict(_all(conn, "SELECT company_id, min(effective_from) FROM setting_values "
                             "WHERE source = 'LEGACY_MIGRATION' GROUP BY company_id"))
    created = {cid: stamp.date() for cid, stamp in _all(conn, "SELECT id, created_at FROM companies")}
    checks.check("0011 settings effective from each company's creation date",
                 all(starts.get(cid) is not None and abs((starts[cid] - created[cid]).days) <= 1 for cid in (A, B, C)),
                 {"starts": {k: str(v) for k, v in starts.items()}})
    skipped = _one(conn, "SELECT details->'skipped_legacy_values' FROM audit_logs WHERE company_id = :c "
                         "AND action = 'PROFILE_APPLIED' ORDER BY id DESC LIMIT 1", c=B)
    checks.equal("0011 invalid legacy values reported in the audit row (tenant B)", sorted(skipped or {}),
                 ["graceTime", "overtime"])
    ot = dict(_all(conn, "SELECT p.company_id, v.fields->'buckets'->0->>'multiplier' FROM policies p JOIN "
                         "policy_versions v ON v.policy_id = p.id WHERE p.type = 'OVERTIME_POLICY' AND v.status = 'DRAFT'"))
    checks.check("0011 legacy overtime 1.5 copied into the draft overtime policy (tenant A)", ot.get(A) == "1.5",
                 {"multipliers": ot})
    checks.equal("0011 legacy pf/tax never become statutory settings (EC-MIG-05)",
                 _one(conn, "SELECT count(*) FROM setting_values WHERE key IN ('pf', 'tax')"), 0)

    # 0029 overtime formula on the LEGACY_V1 drafts
    formulas = _all(conn, "SELECT p.company_id, v.status, v.fields->>'rate_formula' FROM policies p JOIN "
                          "policy_versions v ON v.policy_id = p.id WHERE p.type = 'OVERTIME_POLICY' ORDER BY 1")
    checks.equal("0029 LEGACY_V1 draft overtime policies hold the new default formula",
                 formulas, [(A, "DRAFT", NEW_OT_FORMULA), (B, "DRAFT", NEW_OT_FORMULA), (C, "DRAFT", NEW_OT_FORMULA)])

    # 0010 permissions v2 (EC-MIG-07/08)
    holders = _all(conn, "SELECT u.company_id, u.id FROM users u JOIN user_role_assignments a ON a.user_id = u.id "
                         "JOIN roles r ON r.id = a.role_id WHERE r.system_key = 'payroll_manager' ORDER BY 1")
    checks.equal("0010 every Company Administrator holds payroll_manager (EC-MIG-08)", holders,
                 [(A, ids["admins"]["A"]), (B, ids["admins"]["B"]), (C, ids["admins"]["C"])])
    checks.equal("0010 a tenant that had deleted payroll_manager gets it back (tenant B)",
                 _one(conn, "SELECT count(*) FROM roles WHERE company_id = :c AND system_key = 'payroll_manager'", c=B), 1)
    clerk = {code for (code,) in _all(conn, "SELECT permission_code FROM role_permissions WHERE role_id = :r",
                                      r=ids["clerk_role"])}
    checks.check("0010 custom role with legacy payroll codes receives the v2 equivalents and keeps its codes",
                 {"payroll:read", "payroll:prepare", "payroll:run:read", "payroll:run:create",
                  "payroll:payslip:read", "payroll:compensation:read"} <= clerk, sorted(clerk))
    descriptions = dict(_all(conn, "SELECT company_id, description FROM roles WHERE system_key = 'company_admin'"))
    checks.check("0010 default admin description updated, customised one kept",
                 descriptions[A] != OLD_ADMIN_DESCRIPTION and descriptions[B] == "Our own administrator text",
                 descriptions)

    # 0015 compensation backfill (EC-MIG-01)
    rows = {r[0]: r[1:] for r in _all(
        conn, "SELECT legacy_salary_structure_id, company_id, status, reason, currency, pay_basis_amount, "
              "effective_from, effective_to FROM compensation_assignments WHERE legacy_salary_structure_id IS NOT NULL")}
    expected = {
        s["asha_2024"]: (A, "ACTIVE", "MIGRATED", "INR", Decimal("49000"), dt.date(2024, 2, 1), dt.date(2025, 3, 31)),
        s["asha_2025"]: (A, "ACTIVE", "MIGRATED", "INR", Decimal("56000"), dt.date(2025, 4, 1), None),
        s["ravi_first"]: (A, "SUPERSEDED", "MIGRATED", "INR", Decimal("35000"), dt.date(2024, 5, 1), None),
        s["ravi_same"]: (A, "ACTIVE", "MIGRATED", "INR", Decimal("35000"), dt.date(2024, 5, 1), None),
        s["meera_future"]: (A, "SCHEDULED", "MIGRATED", "INR", Decimal("100.10"), today + dt.timedelta(days=40), None),
        s["kabir"]: (A, "ACTIVE", "MIGRATED", "INR", Decimal("20000"), dt.date(2024, 3, 1), dt.date(2025, 4, 15)),
        s["omar"]: (B, "ACTIVE", "MIGRATED", "AED", Decimal("15000"), dt.date(2024, 3, 1), None),
        s["layla"]: (B, "ACTIVE", "MIGRATED", "AED", Decimal("12000"), dt.date(2024, 3, 1), None),
    }
    got = {k: (v[0], v[1], v[2], v[3], Decimal(v[4]).normalize(), v[5], v[6]) for k, v in rows.items()}
    want = {k: (v[0], v[1], v[2], v[3], v[4].normalize(), v[5], v[6]) for k, v in expected.items()}
    checks.equal("0015 one MIGRATED assignment per placeable salary_structure row (overlap closed, same start "
                 "superseded, future scheduled, entity currency)",
                 {str(k): [_norm(x) for x in v] for k, v in got.items()},
                 {str(k): [_norm(x) for x in v] for k, v in want.items()})
    checks.equal("0015 a structure of an employee without a placement is not migrated",
                 s["nisha_unplaced"] in rows, False)
    lines = dict(_all(conn, "SELECT c.legacy_salary_structure_id, sum(l.amount) FROM compensation_assignments c "
                            "JOIN compensation_assignment_lines l ON l.company_id = c.company_id "
                            "AND l.compensation_assignment_id = c.id WHERE c.legacy_salary_structure_id IS NOT NULL "
                            "GROUP BY 1"))
    checks.equal("0015 assignment lines (overrides) add up to the legacy monthly gross",
                 {str(k): _norm(v) for k, v in lines.items()}, {str(k): _norm(v[4]) for k, v in want.items()})
    templates = _all(conn, "SELECT company_id, count(*) FROM salary_templates WHERE code = 'LEGACY_FIXED' GROUP BY 1 "
                           "ORDER BY 1")
    checks.equal("0015 one LEGACY_FIXED structure in every company", templates, [(A, 1), (B, 1), (C, 1)])

    # 0013 D-4: terminated placements
    checks.equal("0013 placement starting after the termination removed, open placement closed on it",
                 _all(conn, "SELECT id, effective_to FROM employee_assignments WHERE employee_id = :e ORDER BY id",
                      e=e["kabir"]), [(ids["kabir_first"], dt.date(2025, 4, 15))])

    # 0008 org backfill (EC-MIG-09/10)
    checks.equal("0008 every placement has a pay group and employment type",
                 _one(conn, "SELECT count(*) FROM employee_assignments WHERE pay_group_id IS NULL "
                            "OR employment_type_id IS NULL"), 0)
    checks.equal("0008 every holiday sits in a calendar (location text kept, EC-MIG-10)",
                 _all(conn, "SELECT name, location, calendar_id IS NOT NULL FROM holidays ORDER BY name"),
                 [("Ganesh Chaturthi", "Mumbai", True), ("National Day", "Dubai", True), ("Republic Day", None, True)])
    checks.equal("0008 employee with a blank location keeps a placement (EC-MIG-09)",
                 _one(conn, "SELECT count(*) FROM employee_assignments WHERE employee_id = :e AND pay_group_id IS NOT NULL",
                      e=e["meera"]), 1)

    # 0016 leave days
    checks.equal("0016 approved legacy leave becomes FULL leave days and one USE transaction",
                 [_one(conn, "SELECT count(*) FROM leave_days WHERE leave_id = :l AND portion = 'FULL'",
                       l=ids["approved_leave"]),
                  _norm(_one(conn, "SELECT sum(delta) FROM leave_balance_transactions WHERE ref_type = 'leave' "
                                   "AND ref_id = :l", l=ids["approved_leave"]))], [3, "-3"])

    # 0026 TOTP secrets
    secret = _one(conn, "SELECT mfa_secret FROM users WHERE id = :u", u=ids["admins"]["A"])
    checks.check("0026 plaintext TOTP secret encrypted at rest", bool(secret) and secret.startswith("v1:")
                 and TOTP_SECRET not in secret)
    checks.equal("0026 one MFA_SECRET data key for the tenant with a secret",
                 _all(conn, "SELECT company_id FROM tenant_data_keys WHERE purpose = 'MFA_SECRET'"), [(A,)])

    # 0028 column present (the task backfill is exercised by the task stage)
    checks.equal("0028 task_assignees.paid_in_run_id exists",
                 _one(conn, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'task_assignees' "
                            "AND column_name = 'paid_in_run_id'"), 1)

    # Legacy payroll stays readable (EC-MIG-02)
    checks.equal("legacy payroll rows by status",
                 _all(conn, "SELECT status, count(*) FROM payroll GROUP BY 1 ORDER BY 1"),
                 [("approved", 2), ("draft", 1), ("processed", 5)])


def verify_security(conn, ids, checks: Checks, stage: str, *, before_setup: bool) -> None:
    """Grants and row-level security at head, as the RLS-restricted application role."""
    from sqlalchemy import text

    from app.models import TENANT_TABLES

    if before_setup:
        # Existing deployments: the migrations themselves grant the existing app role its reach on new tables
        # (0009/0012/0015/... grant_app_roles). setup-db-roles is still the documented step after migrating.
        missing = [t for t in TENANT_TABLES if not _one(
            conn, "SELECT has_table_privilege(:r, :t, 'SELECT')", r=APP_ROLE, t=t)]
        checks.check(f"{stage}: migrations grant the existing 0007 application role every new tenant table "
                     "(before setup-db-roles)", not missing, {"missing_select": missing})
        return
    from app.cli import grant_app_role

    grant_app_role(conn, APP_ROLE)
    rls = {name: enabled for name, enabled in _all(
        conn, "SELECT c.relname, c.relrowsecurity AND EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid "
              "AND (pg_get_expr(p.polqual, p.polrelid) LIKE '%app.company_id%')) FROM pg_class c "
              "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'")}
    checks.equal(f"{stage}: every tenant table has row security with an app.company_id policy",
                 [t for t in TENANT_TABLES if not rls.get(t)], [])
    checks.equal(f"{stage}: application role is NOSUPERUSER NOBYPASSRLS",
                 _all(conn, "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r", r=APP_ROLE),
                 [(False, False)])
    system_only = ("auth_handoff_codes", "platform_admins", "platform_sessions", "commercial_brand_profiles",
                   "commercial_brand_assets", "partners", "alembic_version")
    checks.equal(f"{stage}: system-only tables are not readable by the application role",
                 [t for t in system_only if _one(conn, "SELECT has_table_privilege(:r, :t, 'SELECT')", r=APP_ROLE, t=t)],
                 [])
    A, B = ids["companies"]["A"], ids["companies"]["B"]
    probe = ("payroll", "salary_structure", "compensation_assignments", "setting_values", "policies",
             "employee_assignments", "issued_documents", "leave_days", "users")
    seen = {}
    conn.execute(text("SAVEPOINT rls_probe"))
    try:
        conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        for label, tenant in (("A", A), ("B", B), ("none", "")):
            conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(tenant)})
            seen[label] = {t: _all(conn, f"SELECT DISTINCT company_id FROM {t} ORDER BY 1") for t in probe}
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(A)})
        moved = conn.execute(text("UPDATE payroll SET remarks = remarks WHERE company_id = :b"), {"b": B}).rowcount
    finally:
        conn.execute(text("ROLLBACK TO SAVEPOINT rls_probe"))
        conn.execute(text("RESET ROLE"))
    checks.equal(f"{stage}: as tenant A the application role sees only tenant A rows",
                 {t: v for t, v in seen["A"].items() if v not in ([(A,)], [])}, {})
    checks.equal(f"{stage}: as tenant B the application role sees only tenant B rows",
                 {t: v for t, v in seen["B"].items() if v not in ([(B,)], [])}, {})
    checks.equal(f"{stage}: without a tenant the application role sees no rows",
                 {t: v for t, v in seen["none"].items() if v}, {})
    checks.check(f"{stage}: tenant A sees its legacy payroll and migrated compensation (proof is not vacuous)",
                 seen["A"]["payroll"] == [(A,)] and seen["A"]["compensation_assignments"] == [(A,)])
    checks.equal(f"{stage}: tenant A cannot update tenant B legacy payroll rows", moved, 0)


def v2_content(conn) -> dict:
    """Content of the rows the migrations derive, without surrogate ids and timestamps, for re-upgrade parity."""
    queries = {
        "compensation": "SELECT c.company_id, c.legacy_salary_structure_id, c.status, c.reason, c.currency, "
                        "c.pay_basis_amount, c.effective_from, c.effective_to FROM compensation_assignments c",
        "setting_values": "SELECT company_id, key, value::text, effective_from, source, status FROM setting_values",
        "policies": "SELECT p.company_id, p.type, p.name, v.version, v.status, v.fields::text, v.policy_hash "
                    "FROM policies p JOIN policy_versions v ON v.policy_id = p.id",
        "roles": "SELECT company_id, name, system_key, description FROM roles",
        "role_permissions": "SELECT r.company_id, r.name, p.permission_code, p.scope FROM role_permissions p "
                            "JOIN roles r ON r.id = p.role_id",
        "role_holders": "SELECT u.email, r.system_key, r.name FROM user_role_assignments a JOIN users u "
                        "ON u.id = a.user_id JOIN roles r ON r.id = a.role_id",
        "pay_groups": "SELECT company_id, code, currency FROM pay_groups",
        "holiday_calendars": "SELECT company_id, code FROM holiday_calendars",
        "placements": "SELECT company_id, employee_id, effective_from, effective_to, fte FROM employee_assignments",
        "leave_days": "SELECT company_id, leave_id, date, portion, units FROM leave_days",
        "leave_balance_transactions": "SELECT company_id, employee_id, leave_type, delta, reason, ref_id "
                                      "FROM leave_balance_transactions",
        "pay_components": "SELECT company_id, code FROM pay_components",
        "salary_templates": "SELECT company_id, code, name FROM salary_templates",
    }
    from sqlalchemy import text

    out = {}
    for name, sql in queries.items():
        rows = sorted([_norm(v) for v in r] for r in conn.execute(text(sql)).fetchall())
        out[name] = {"count": len(rows), "sha256": _sha(rows)}
    return out


def _table_counts(conn, tables) -> dict:
    return {t: _one(conn, f'SELECT count(*) FROM "{t}"') for t in sorted(tables)}


# ----------------------------------------------------------------------------------------------- expectations
def check_legacy_diff(checks: Checks, name: str, d: dict, ids: dict, *, at_head: bool, base_audit_max: int) -> None:
    """Every difference between the seeded 0007 rows and a later stage, restricted to the 0007 columns."""
    problems = {}
    allowed = {"audit_logs", "users", "roles", "role_permissions", "user_role_assignments", "employee_assignments"}
    for table in set(d) - allowed:
        problems[table] = d[table]
    # audit_logs: append-only, old rows untouched
    audit = d.get("audit_logs")
    if audit and (audit["removed"] or audit["changed"] or any(int(i) <= base_audit_max for i in audit["added"])):
        problems["audit_logs"] = audit
    # users: permission_version bumps (sessions refresh, EC-SEC-14); TOTP secret encrypted only at head
    users = d.get("users")
    if users:
        user_cols = {"permission_version"} | ({"mfa_secret"} if at_head else set())
        bad = {k: v for k, v in users["changed"].items() if set(v) - user_cols}
        if users["added"] or users["removed"] or bad:
            problems["users"] = users
        if at_head and str(ids["admins"]["A"]) not in {str(k) for k in users["changed"]
                                                        if "mfa_secret" in users["changed"][k]}:
            problems["users.mfa_secret"] = "the plaintext secret was not encrypted"
    # roles: v2 roles added at head; default descriptions updated (head) / restored (after downgrade)
    roles = d.get("roles")
    if roles:
        bad = {k: v for k, v in roles["changed"].items()
               if set(v) - ({"description", "updated_at"} if at_head else {"updated_at"})}
        if roles["removed"] or bad or (roles["added"] and not at_head):
            problems["roles"] = roles
    for table in ("role_permissions", "user_role_assignments"):
        t = d.get(table)
        if t and (t["removed"] or t["changed"] or (t["added"] and not at_head)):
            problems[table] = t
    # employee_assignments: 0013 D-4 removes the placement that starts after the termination and closes the
    # open one on the termination date; downgrade keeps the repair (0013 downgrade changes nothing).
    placements = d.get("employee_assignments")
    expected = {"added": [], "removed": [str(ids["kabir_future"])],
                "changed": {str(ids["kabir_first"]): {"effective_to": ["2025-05-31", "2025-04-15"]}}}
    if placements != expected:
        problems["employee_assignments"] = {"actual": placements, "expected": expected}
    checks.check(name, not problems, problems or None)


# ----------------------------------------------------------------------------------------------- rehearsal
def rehearse(admin_url: str, db_name: str = DEFAULT_DB, *, keep: bool = False, log=print,
             master_key: str = None) -> dict:
    """Run the whole rehearsal on a scratch database of the server behind `admin_url` (a superuser URL).
    Returns the report; raises RehearsalError when a check fails (the report is attached as .report)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    if not db_name.replace("_", "").isalnum():
        raise ValueError("database names may contain letters, digits and underscores only")
    base = make_url(admin_url)
    url = base.set(database=db_name).render_as_string(hide_password=False)
    key = master_key or os.environ.get("PAYROLL_MASTER_KEY") or base64.b64encode(secrets.token_bytes(32)).decode()
    env = {"PAYROLL_MASTER_KEY": key, "ENVIRONMENT": os.environ.get("ENVIRONMENT", "test")}
    os.environ.setdefault("PAYROLL_MASTER_KEY", key)
    checks = Checks(log)
    report = {"database": db_name, "stages": {}, "checks": checks.results}
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"), {"d": db_name})
        conn.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
        conn.execute(text(f"CREATE DATABASE {db_name}"))
        if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}).first():
            conn.execute(text(f"CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    engine = create_engine(url)
    try:
        # ---------------- stage 0: legacy data at 0007
        log(f"[0] alembic upgrade {LEGACY_REVISION} and seed legacy data")
        _alembic(url, env, "upgrade", LEGACY_REVISION)
        with engine.begin() as conn:
            import pytz  # as 0015 does: the embedded Windows server ships no time zone files

            today = dt.datetime.now(dt.timezone.utc).astimezone(pytz.timezone("Asia/Kolkata")).date()
            ids = seed_legacy(conn, today)
            legacy_columns = _columns(conn)
            grant_0007_app_role(conn, APP_ROLE, legacy_columns)
        with engine.connect() as conn:
            s0 = snapshot(conn, legacy_columns)
            schema0 = schema_fingerprint(conn)
            audit_max0 = _one(conn, "SELECT coalesce(max(id), 0) FROM audit_logs")
        report["legacy_rows"] = {t: len(rows) for t, rows in s0.items() if rows}
        report["legacy_checksums"] = {t: _sha(s0[t]) for t in FROZEN_TABLES}
        log(f"    legacy rows: {sum(report['legacy_rows'].values())} in {len(report['legacy_rows'])} tables")

        # ---------------- stage 1: upgrade head
        log("[1] alembic upgrade head")
        out = _alembic(url, env, "upgrade", HEAD)
        report["stages"]["upgrade_1"] = [line for line in out.splitlines() if "Running upgrade" in line][-1:]
        with engine.begin() as conn:
            verify_security(conn, ids, checks, "head (1)", before_setup=True)
            verify_head(conn, ids, checks, today)
            verify_security(conn, ids, checks, "head (1)", before_setup=False)
        with engine.connect() as conn:
            s1 = snapshot(conn, legacy_columns)
            head_columns = _columns(conn)
            schema1 = schema_fingerprint(conn)
            v2_1 = v2_content(conn)
            counts1 = _table_counts(conn, set(head_columns) - set(legacy_columns) - {"audit_logs"})
        d1 = diff(s0, s1)
        check_legacy_diff(checks, "head (1): every 0007 row unchanged except the documented migration effects",
                          d1, ids, at_head=True, base_audit_max=audit_max0)
        for table in FROZEN_TABLES:
            checks.equal(f"head (1): {table} rows identical (sha256)", _sha(s1[table]), _sha(s0[table]))

        # ---------------- stage 2: task backfill (0028) and overtime formula revert/re-apply (0029)
        log(f"[2] alembic downgrade {BEFORE_TASK_BACKFILL}, seed tasks, upgrade head")
        _alembic(url, env, "downgrade", BEFORE_TASK_BACKFILL)
        with engine.begin() as conn:
            checks.equal("0029 downgrade restores the old formula on the drafts it changed",
                         sorted({f for (f,) in _all(conn, "SELECT v.fields->>'rate_formula' FROM policies p JOIN "
                                                         "policy_versions v ON v.policy_id = p.id WHERE "
                                                         "p.type = 'OVERTIME_POLICY'")}), [OLD_OT_FORMULA])
            tasks = seed_tasks(conn, ids)
        _alembic(url, env, "upgrade", HEAD)
        with engine.begin() as conn:
            paid = _all(conn, "SELECT task_id, employee_id, paid_in_run_id FROM task_assignees ORDER BY task_id, "
                              "employee_id")
            e = ids["employees"]
            checks.equal("0028 each assignee of a PAID task inherits the task's run; unpaid shares stay unpaid",
                         paid, sorted([(tasks["paid"], e["asha"], FAKE_RUN_ID), (tasks["paid"], e["ravi"], FAKE_RUN_ID),
                                       (tasks["open"], e["asha"], None)]))
            checks.equal("0029 re-applied after a revert: drafts hold the new formula again",
                         sorted({f for (f,) in _all(conn, "SELECT v.fields->>'rate_formula' FROM policies p JOIN "
                                                         "policy_versions v ON v.policy_id = p.id WHERE "
                                                         "p.type = 'OVERTIME_POLICY'")}), [NEW_OT_FORMULA])
        with engine.connect() as conn:
            checks.equal("partial down/up (0027 <-> head): schema identical to the first head schema",
                         schema_diff(schema1, schema_fingerprint(conn)), {})
            for table in FROZEN_TABLES:
                checks.equal(f"partial down/up: {table} rows identical (sha256)",
                             _sha(snapshot(conn, {table: legacy_columns[table]})[table]), _sha(s0[table]))

        # ---------------- stage 2b: a downgrade that would destroy encrypted payroll data is refused atomically
        log(f"[2b] alembic downgrade {LEGACY_REVISION} while a PAYROLL_PII data key exists (must be refused)")
        with engine.begin() as conn:
            head_revision = _one(conn, "SELECT version_num FROM alembic_version")
            pii_key = _seed(conn, "tenant_data_keys", company_id=ids["companies"]["A"], purpose="PAYROLL_PII",
                            key_id="rehearsal-pii-key", version=1, kek_id="rehearsal-kek",
                            wrapped_key="not-a-real-wrapped-key")
        try:
            _alembic(url, env, "downgrade", LEGACY_REVISION)
            refused = None
        except RehearsalError as error:
            refused = str(error)
        checks.check("0012 refuses the downgrade while data keys protect encrypted payroll data",
                     refused is not None and "Refusing to drop tenant_data_keys" in refused,
                     None if refused else "the downgrade ran")
        with engine.begin() as conn:
            checks.equal("refused downgrade is all-or-nothing: still at head", _one(
                conn, "SELECT version_num FROM alembic_version"), head_revision)
            checks.equal("refused downgrade is all-or-nothing: schema unchanged",
                         schema_diff(schema1, schema_fingerprint(conn)), {})
            checks.equal("refused downgrade is all-or-nothing: derived payroll v2 rows unchanged",
                         {k: v for k, v in v2_content(conn).items() if v != v2_1.get(k)}, {})
            conn.execute(text("DELETE FROM tenant_data_keys WHERE id = :i"), {"i": pii_key})

        # ---------------- stage 3: downgrade to 0007
        log(f"[3] alembic downgrade {LEGACY_REVISION}")
        _alembic(url, env, "downgrade", LEGACY_REVISION)
        with engine.connect() as conn:
            s3 = snapshot(conn, legacy_columns)
            schema3 = schema_fingerprint(conn)
            left = sorted(set(_columns(conn)) - set(legacy_columns))
            secret = _one(conn, "SELECT mfa_secret FROM users WHERE id = :u", u=ids["admins"]["A"])
        checks.equal("downgrade: no payroll v2 table is left", left, [])
        checks.equal("downgrade: schema identical to the 0007 schema (columns, constraints, indexes, triggers, "
                     "policies, functions, row security, grants)", schema_diff(schema0, schema3), {})
        checks.equal("downgrade: TOTP secret decrypted back to the original plaintext", secret, TOTP_SECRET)
        d3 = diff(s0, s3)
        check_legacy_diff(checks, "downgrade: every 0007 row unchanged except audit rows, permission_version and "
                          "the 0013 placement repair", d3, ids, at_head=False, base_audit_max=audit_max0)
        for table in FROZEN_TABLES:
            checks.equal(f"downgrade: {table} rows identical (sha256)", _sha(s3[table]), _sha(s0[table]))

        # ---------------- stage 4: upgrade head again
        log("[4] alembic upgrade head (again)")
        _alembic(url, env, "upgrade", HEAD)
        with engine.begin() as conn:
            verify_head(conn, ids, checks, today)
            verify_security(conn, ids, checks, "head (2)", before_setup=False)
        with engine.connect() as conn:
            s4 = snapshot(conn, legacy_columns)
            schema4 = schema_fingerprint(conn)
            v2_4 = v2_content(conn)
            counts4 = _table_counts(conn, set(head_columns) - set(legacy_columns) - {"audit_logs"})
        checks.equal("re-upgrade: schema identical to the first head schema", schema_diff(schema1, schema4), {})
        checks.equal("re-upgrade: derived payroll v2 rows identical to the first upgrade (content checksums)",
                     {k: v for k, v in v2_4.items() if v != v2_1.get(k)}, {})
        checks.equal("re-upgrade: row counts of every payroll v2 table identical to the first upgrade",
                     {t: [counts1.get(t), counts4.get(t)] for t in counts4 if counts1.get(t) != counts4.get(t)}, {})
        d4 = diff(s0, s4)
        check_legacy_diff(checks, "re-upgrade: every 0007 row unchanged except the documented migration effects",
                          d4, ids, at_head=True, base_audit_max=audit_max0)
        for table in FROZEN_TABLES:
            checks.equal(f"re-upgrade: {table} rows identical (sha256)", _sha(s4[table]), _sha(s0[table]))
        report["v2_rows_after_upgrade"] = {k: v["count"] for k, v in v2_1.items()}
        report["legacy_diff_at_head"] = {t: {"added": len(v["added"]), "removed": len(v["removed"]),
                                             "changed": len(v["changed"])} for t, v in d1.items()}
        report["legacy_diff_after_downgrade"] = {t: {"added": len(v["added"]), "removed": len(v["removed"]),
                                                     "changed": len(v["changed"])} for t, v in d3.items()}
    finally:
        engine.dispose()
        if not keep:
            with admin.connect() as conn:
                conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"),
                             {"d": db_name})
                conn.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
                still = conn.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE 'hrms_rehearsal%'")).scalar()
                if not still:
                    try:
                        conn.execute(text(f"DROP ROLE IF EXISTS {APP_ROLE}"))
                    except Exception:  # the role still holds privileges in another database
                        pass
        admin.dispose()
    report["passed"] = len(checks.results) - len(checks.failed)
    report["failed"] = len(checks.failed)
    if checks.failed:
        error = RehearsalError(f"{len(checks.failed)} rehearsal check(s) failed: "
                               + "; ".join(r["check"] for r in checks.failed))
        error.report = report
        raise error
    return report


def _default_admin_url():
    import pgserver

    data_dir = Path(tempfile.gettempdir()) / "hrms_pg_tests"
    if data_dir.name == FORBIDDEN_DATA_DIR:
        raise SystemExit("Refusing to use the demo stack's database directory")
    server = pgserver.get_server(str(data_dir), cleanup_mode="stop")
    return server, server.get_uri()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--admin-url", help="superuser URL of a scratch PostgreSQL server (default: the embedded "
                                            "test server)")
    parser.add_argument("--db-name", default=DEFAULT_DB)
    parser.add_argument("--keep", action="store_true", help="keep the scratch database for inspection")
    parser.add_argument("--json", help="write the report to this file")
    args = parser.parse_args(argv)
    if args.admin_url and FORBIDDEN_DATA_DIR in args.admin_url and "payroll" not in args.admin_url:
        raise SystemExit("Refusing a URL that points at the demo stack")
    if not args.admin_url and os.environ.get("HRMS_REHEARSAL_LOCKED") != "1":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from run_exclusive import run_exclusive

        os.environ["HRMS_REHEARSAL_LOCKED"] = "1"
        return run_exclusive([sys.executable, str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])],
                             cwd=ROOT)
    server = None
    admin_url = args.admin_url
    if not admin_url:
        server, admin_url = _default_admin_url()
    from sqlalchemy.engine import make_url

    # The checks import the application (grant_app_role, TENANT_TABLES): point every URL at the scratch
    # database so nothing reads the developer's .env database.
    scratch = make_url(admin_url).set(database=args.db_name).render_as_string(hide_password=False)
    for name in ("DATABASE_URL", "SYSTEM_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        os.environ[name] = scratch
    os.environ.setdefault("ENVIRONMENT", "test")
    try:
        report = rehearse(admin_url, args.db_name, keep=args.keep)
        status = 0
    except RehearsalError as error:
        report = getattr(error, "report", {"error": str(error)})
        print(str(error), file=sys.stderr)
        status = 1
    finally:
        if server is not None:
            server.cleanup()
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Rehearsal {'PASSED' if status == 0 else 'FAILED'}: {report.get('passed', 0)} checks passed, "
          f"{report.get('failed', '?')} failed")
    return status


if __name__ == "__main__":
    sys.exit(main())
