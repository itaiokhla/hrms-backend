"""Operational commands.

    python -m app.cli setup-db-roles --app-role hrms_app      (run as owner, after migrations)
    python -m app.cli create-platform-admin --email ops@example.com --name "Ops"
    python -m app.cli create-partner-admin --partner payease --email admin@payease.example --name "Partner Admin"
    python -m app.cli rls-status
    python -m app.cli attendance-sweep                         (schedule every ~15 minutes)
    python -m app.cli sync-tenant-hosts                        (after changing PLATFORM_BASE_DOMAIN)
    python -m app.cli domain-checks                            (schedule every ~5 minutes)
    python -m app.cli org-sync                                 (daily, and after any code rollback: repairs
                                                               missing structure, applies future-dated placements)
    python -m app.cli payroll-inventory [--company-id N] [--json]   (read-only legacy payroll inventory, payroll v2 M0)
"""

import argparse
import getpass
import json
import os
import re
import sys
from decimal import Decimal

from sqlalchemy import create_engine, text

from app.config import settings

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def grant_app_role(connection, role: str) -> None:
    """Least-privilege grants for the RLS-restricted application role."""
    from app.models import TENANT_TABLES

    if not _ROLE_NAME.match(role):
        raise ValueError("invalid role name")
    connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    for table in TENANT_TABLES:
        connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"))
    # Attendance events and loan ledgers are append-only (triggers also refuse changes).
    connection.execute(text(f"REVOKE UPDATE, DELETE ON attendance_events FROM {role}"))
    connection.execute(text(f"REVOKE UPDATE, DELETE ON loan_transactions FROM {role}"))
    # Bank accounts, their change requests and funding accounts are never deleted; verification events are
    # append-only (0021: the same privileges, and triggers also refuse).
    connection.execute(text(f"REVOKE DELETE ON bank_accounts, bank_account_change_requests, funding_accounts FROM {role}"))
    connection.execute(text(f"REVOKE UPDATE, DELETE ON bank_verifications FROM {role}"))
    # Issued documents are immutable; only the stored-file pointer may be filled in later.
    connection.execute(text(f"REVOKE UPDATE, DELETE ON issued_documents FROM {role}"))
    connection.execute(text(f"GRANT UPDATE (sha256, storage_path, updated_at) ON issued_documents TO {role}"))
    # Companies are created by the system role during signup; tenants may only
    # read and edit their own row (RLS restricts which row).
    connection.execute(text(f"REVOKE ALL ON companies FROM {role}"))
    connection.execute(text(f"GRANT SELECT, UPDATE ON companies TO {role}"))
    # Audit trail: append-only.
    connection.execute(text(f"REVOKE ALL ON audit_logs FROM {role}"))
    connection.execute(text(f"GRANT SELECT, INSERT ON audit_logs TO {role}"))
    # Platform principals, partners, platform/partner brands and login handoff codes are
    # invisible to the tenant application role (they are read through the system session).
    for table in (
        "platform_admins", "platform_sessions", "alembic_version", "partners", "commercial_brand_profiles",
        "commercial_brand_assets", "auth_handoff_codes",
    ):
        connection.execute(text(f"REVOKE ALL ON {table} FROM {role}"))
    # Payroll v2 platform tables (0009): read-only catalogue state; FX rates and jobs are written by
    # tenants for their own rows (fx_rates RLS) or through the jobs service, never deleted.
    for table in ("platform_setting_values", "country_capabilities"):
        connection.execute(text(f"REVOKE ALL ON {table} FROM {role}"))
        connection.execute(text(f"GRANT SELECT ON {table} TO {role}"))
    for table in ("fx_rates", "jobs"):
        connection.execute(text(f"REVOKE ALL ON {table} FROM {role}"))
        connection.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role}"))
    connection.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))


def setup_db_roles(app_role: str, password: str) -> None:
    if not _ROLE_NAME.match(app_role):
        raise SystemExit("Role names may contain lowercase letters, digits and underscores only.")
    owner = create_engine(settings.effective_migration_database_url, isolation_level="AUTOCOMMIT")
    with owner.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": app_role}).first()
        # Role names cannot be bound parameters; they are validated above.
        if exists:
            conn.execute(text(f"ALTER ROLE {app_role} WITH LOGIN PASSWORD :pw"), {"pw": password})
        else:
            conn.execute(text(f"CREATE ROLE {app_role} WITH LOGIN PASSWORD :pw"), {"pw": password})
        conn.execute(text(f"ALTER ROLE {app_role} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"))
        grant_app_role(conn, app_role)
    print(f"Role '{app_role}' is ready. Point DATABASE_URL at it and keep the owner URL in SYSTEM_DATABASE_URL.")


def create_platform_admin(email: str, name: str, password: str, partner_slug: str = None) -> None:
    """A platform operator, or with partner_slug a partner administrator (partner console only)."""
    from app.core.security import hash_password, password_problem
    from app.database.db import system_session
    from app.models.company import Partner, PlatformAdmin

    problem = password_problem(password, email)
    if problem:
        raise SystemExit(problem)
    with system_session() as db:
        email = email.strip().lower()
        if db.query(PlatformAdmin).filter(PlatformAdmin.email == email).first():
            raise SystemExit("An operator account with this email already exists.")
        partner_id = None
        if partner_slug:
            partner = db.query(Partner).filter(Partner.slug == partner_slug).first()
            if partner is None:
                raise SystemExit(f"No partner with slug {partner_slug!r}.")
            partner_id = partner.id
        db.add(PlatformAdmin(email=email, full_name=name, password_hash=hash_password(password), partner_id=partner_id))
        db.commit()
    print(f"{'Partner' if partner_slug else 'Platform'} administrator {email} created.")


def attendance_sweep() -> None:
    """Close shifts past their company's limit, record missed location slots, redact expired coordinates.

    Runs as the RLS-restricted application role, one company at a time, so a
    sweep can never touch another tenant's rows. It only detects what happened;
    it cannot obtain a position from a browser.
    """
    from app.database.db import SessionLocal, bind_tenant, system_session
    from app.models.company import Company
    from app.services import location_service

    with system_session() as db:
        company_ids = [cid for (cid,) in db.query(Company.id).filter(Company.status == "active").order_by(Company.id)]
    for company_id in company_ids:
        db = SessionLocal()
        try:
            bind_tenant(db, company_id)
            result = location_service.sweep(db, company_id)
            db.commit()
            print(result)
        except Exception as exc:  # one company's failure must not stop the others
            db.rollback()
            print(f"company {company_id}: sweep failed: {exc}", file=sys.stderr)
        finally:
            db.close()


def sync_tenant_hosts() -> None:
    from app.services import domain_service

    print(domain_service.sync_platform_subdomains())


def domain_checks() -> None:
    """DNS/TLS state machine for custom domains (pending, verifying, active re-checks, broken -> disabled)."""
    from app.services import domain_service

    for line in domain_service.run_checks():
        print(line)


def org_sync() -> None:
    """Create missing default structure (rows written by an older release), then copy each
    employee's current primary placement into the legacy employee columns."""
    from app.database.db import SessionLocal, bind_tenant, system_session
    from app.models.company import Company
    from app.services import company_service, org_structure_service

    with system_session() as db:
        company_ids = [cid for (cid,) in db.query(Company.id).order_by(Company.id)]
    for company_id in company_ids:
        db = SessionLocal()
        try:
            bind_tenant(db, company_id)
            repaired = company_service.repair_defaults(db, db.get(Company, company_id))
            changed = org_structure_service.sync_all_current(db, company_id)
            db.commit()
            created = ", ".join(f"{count} {kind}" for kind, count in repaired.items() if count)
            print(f"company {company_id}: {changed} employee(s) updated" + (f"; created {created}" if created else ""))
        except Exception as exc:
            db.rollback()
            print(f"company {company_id}: org sync failed: {exc}", file=sys.stderr)
        finally:
            db.close()


def _legacy_recompute(db, row, employee):
    """Today's legacy amounts for a stored row from today's data (payroll_service.calculate, audit 02 §2).

    Overtime hours and bonus are the stored inputs; attendance, holidays and structures are read now,
    so a difference is drift since generation, never a correction. None when no structure covers the month.
    """
    from app.models.attendance import Attendance
    from app.services import payroll_service as legacy

    year, month = row.salary_month.year, row.salary_month.month
    structure = legacy.active_structure(db, row.employee_id, year, month)
    if structure is None or employee is None:
        return None
    first, last = legacy._month_bounds(year, month)
    statuses = [s for (s,) in db.query(Attendance.status).filter(
        Attendance.employee_id == row.employee_id, Attendance.date >= first, Attendance.date <= last)]
    wdays = legacy.working_days(db, row.company_id, employee, year, month)
    gross = sum((legacy._money(getattr(structure, column)) for column in (
        "basic_salary", "house_rent_allowance", "dearness_allowance", "conveyance_allowance", "other_allowances")),
        Decimal("0"))
    per_day = gross / Decimal(wdays) if wdays else Decimal("0")
    deductions = (legacy._money(per_day * statuses.count("absent"))
                  + legacy._money(per_day * statuses.count("half-day") * Decimal("0.5"))
                  + legacy._money(Decimal(str(settings.late_penalty_amount)) * statuses.count("late")))
    hours_per_day = Decimal(str(settings.default_work_hours_per_day))
    overtime = legacy._money(per_day / hours_per_day * legacy._money(row.overtime_hours)) if hours_per_day else Decimal("0")
    net = gross + overtime + legacy._money(row.bonus) - deductions
    return {"gross_salary": gross, "total_deductions": deductions, "net_salary": legacy._money(max(net, Decimal("0")))}


def _company_payroll_inventory(db, company) -> dict:
    from collections import Counter

    from app.models.branding import IssuedDocument
    from app.models.employee import Employee
    from app.models.payroll import Payroll, SalaryStructure

    rows = db.query(Payroll).filter(Payroll.company_id == company.id).order_by(Payroll.salary_month, Payroll.id).all()
    issued = {source_id for (source_id,) in db.query(IssuedDocument.source_id).filter(
        IssuedDocument.company_id == company.id, IssuedDocument.document_type == "PAYSLIP")}
    structures = db.query(SalaryStructure.id, SalaryStructure.employee_id).filter(
        SalaryStructure.company_id == company.id).order_by(SalaryStructure.id).all()
    employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == company.id)}
    with_structure = {employee_id for _, employee_id in structures}

    by_month: dict = {}
    for row in rows:
        month = by_month.setdefault(row.salary_month.strftime("%Y-%m"), {})
        month[row.status] = month.get(row.status, 0) + 1
    clipped = []
    for row in rows:
        earnings = row.gross_salary + row.overtime_amount + row.bonus
        if row.net_salary == 0 and row.total_deductions > earnings:
            clipped.append({"payroll_id": row.id, "employee_id": row.employee_id,
                            "salary_month": row.salary_month.isoformat(), "currency": row.currency,
                            "earnings": str(earnings), "total_deductions": str(row.total_deductions),
                            "net_salary": str(row.net_salary), "shortfall": str(row.total_deductions - earnings)})
    drift = []
    for row in rows:
        recomputed = _legacy_recompute(db, row, employees.get(row.employee_id))
        stored = {k: getattr(row, k) for k in ("gross_salary", "total_deductions", "net_salary")}
        if recomputed != stored:
            drift.append({"payroll_id": row.id, "salary_month": row.salary_month.isoformat(),
                          "stored": {k: str(v) for k, v in stored.items()},
                          "recomputed": {k: str(v) for k, v in recomputed.items()} if recomputed else None})
    return {
        "company_id": company.id,
        "company_name": company.name,
        "slug": company.slug,
        "payroll_rows": {
            "total": len(rows),
            "by_status": dict(sorted(Counter(r.status for r in rows).items())),
            "by_currency": dict(sorted(Counter(r.currency for r in rows).items())),
            "by_month": {m: dict(sorted(s.items())) for m, s in by_month.items()},
        },
        # The legacy table has no currency column: every structure's currency is implied by its employee's entity.
        "salary_structures_without_currency": {"count": len(structures), "structure_ids": [s for s, _ in structures]},
        "clipped_net_rows": clipped,
        # "processed" only means "marked paid": no legacy row carries payment evidence.
        "processed_without_payment_evidence": [r.id for r in rows if r.status == "processed"],
        "finalized_without_issued_payslip": [r.id for r in rows if r.status != "draft" and r.id not in issued],
        "rows_without_legal_entity": [r.id for r in rows if r.legal_entity_id is None],
        "rows_with_overtime_or_bonus": [r.id for r in rows if r.overtime_amount or r.bonus],
        "employees_without_structure": [
            {"employee_id": e.id, "employee_code": e.employee_code, "is_active": e.is_active}
            for e in sorted(employees.values(), key=lambda e: e.employee_code) if e.id not in with_structure
        ],
        "recalculation_drift": drift,
    }


def payroll_inventory(company_id: int = None) -> list:
    """Per-tenant inventory of legacy payroll (payroll v2 M0), read through the system session in a
    READ ONLY transaction: nothing is written, fixed or migrated."""
    from app.database.db import system_session
    from app.models.company import Company

    with system_session() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        query = db.query(Company).order_by(Company.id)
        if company_id is not None:
            query = query.filter(Company.id == company_id)
        companies = query.all()
        if company_id is not None and not companies:
            raise SystemExit(f"No company with id {company_id}.")
        report = [_company_payroll_inventory(db, company) for company in companies]
        db.rollback()
    return report


def _ids(values) -> str:
    return ", ".join(f"#{v}" for v in values)


def print_payroll_inventory(report: list, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"companies": report}, indent=2, sort_keys=True))
        return
    for c in report:
        rows = c["payroll_rows"]
        statuses = ", ".join(f"{k} {v}" for k, v in rows["by_status"].items())
        print(f"Company {c['company_id']} - {c['company_name']} ({c['slug']})")
        print(f"  Legacy payroll rows: {rows['total']}" + (f" ({statuses})" if statuses else ""))
        for month, counts in rows["by_month"].items():
            print(f"    {month}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        if rows["by_currency"]:
            print("  By currency: " + ", ".join(f"{k} {v}" for k, v in rows["by_currency"].items()))
        print(f"  Salary structures without currency: {c['salary_structures_without_currency']['count']}"
              " (the legacy table has no currency column)")
        print(f"  Clipped net rows: {len(c['clipped_net_rows'])}")
        for r in c["clipped_net_rows"]:
            print(f"    #{r['payroll_id']} {r['salary_month'][:7]} {r['currency']}: deductions {r['total_deductions']} "
                  f"exceed earnings {r['earnings']} (net stored {r['net_salary']}, shortfall {r['shortfall']})")
        for label, key in (("Processed without payment evidence", "processed_without_payment_evidence"),
                           ("Approved/processed without issued payslip", "finalized_without_issued_payslip"),
                           ("Rows without legal entity", "rows_without_legal_entity"),
                           ("Rows with overtime or bonus", "rows_with_overtime_or_bonus")):
            print(f"  {label}: {len(c[key])}" + (f" ({_ids(c[key])})" if c[key] else ""))
        missing = c["employees_without_structure"]
        codes = ", ".join(e["employee_code"] + ("" if e["is_active"] else " (inactive)") for e in missing)
        print(f"  Employees without salary structure: {len(missing)}" + (f" ({codes})" if codes else ""))
        print(f"  Recalculation drift (report only): {len(c['recalculation_drift'])}")
        for d in c["recalculation_drift"]:
            now = d["recomputed"]
            print(f"    #{d['payroll_id']} {d['salary_month'][:7]}: stored net {d['stored']['net_salary']}, "
                  + (f"today {now['net_salary']}" if now else "no salary structure covers the month today"))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    roles = sub.add_parser("setup-db-roles", help="Create/refresh the RLS-restricted application role")
    roles.add_argument("--app-role", default="hrms_app")

    admin = sub.add_parser("create-platform-admin", help="Create a SaaS platform operator account")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)

    partner_admin = sub.add_parser("create-partner-admin", help="Create a partner administrator (partner console only)")
    partner_admin.add_argument("--partner", required=True, help="partner slug")
    partner_admin.add_argument("--email", required=True)
    partner_admin.add_argument("--name", required=True)

    sub.add_parser("rls-status", help="Show whether the app role is subject to row-level security")
    sub.add_parser("attendance-sweep", help="Detect expired shifts and missed location checks; apply retention")
    sub.add_parser("sync-tenant-hosts", help="Create <slug>.<PLATFORM_BASE_DOMAIN> hosts for every company")
    sub.add_parser("domain-checks", help="Run DNS/TLS verification for custom domains")
    sub.add_parser("org-sync", help="Apply effective-dated placements to the legacy employee columns")
    inventory = sub.add_parser("payroll-inventory", help="Read-only inventory of legacy payroll rows per company")
    inventory.add_argument("--company-id", type=int, default=None)
    inventory.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args(argv)
    if args.command == "setup-db-roles":
        password = os.environ.get("HRMS_APP_DB_PASSWORD") or getpass.getpass("Password for the app role: ")
        setup_db_roles(args.app_role, password)
    elif args.command == "create-platform-admin":
        password = os.environ.get("HRMS_PLATFORM_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        create_platform_admin(args.email, args.name, password)
    elif args.command == "create-partner-admin":
        password = os.environ.get("HRMS_PARTNER_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        create_platform_admin(args.email, args.name, password, partner_slug=args.partner)
    elif args.command == "rls-status":
        from app.database.db import rls_status

        print(rls_status())
    elif args.command == "attendance-sweep":
        attendance_sweep()
    elif args.command == "sync-tenant-hosts":
        sync_tenant_hosts()
    elif args.command == "domain-checks":
        domain_checks()
    elif args.command == "org-sync":
        org_sync()
    elif args.command == "payroll-inventory":
        print_payroll_inventory(payroll_inventory(args.company_id), as_json=args.json)


if __name__ == "__main__":
    main(sys.argv[1:])
