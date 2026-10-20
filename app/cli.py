"""Operational commands.

    python -m app.cli setup-db-roles --app-role hrms_app      (run as owner, after migrations)
    python -m app.cli create-platform-admin --email ops@example.com --name "Ops"
    python -m app.cli rls-status
    python -m app.cli attendance-sweep                         (schedule every ~15 minutes)
    python -m app.cli sync-tenant-hosts                        (after changing PLATFORM_BASE_DOMAIN)
    python -m app.cli domain-checks                            (schedule every ~5 minutes)
    python -m app.cli org-sync                                 (daily: applies future-dated placements)
"""

import argparse
import getpass
import os
import re
import sys

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
    # Attendance events are append-only (a trigger also refuses changes).
    connection.execute(text(f"REVOKE UPDATE, DELETE ON attendance_events FROM {role}"))
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


def create_platform_admin(email: str, name: str, password: str) -> None:
    from app.core.security import hash_password, password_problem
    from app.database.db import system_session
    from app.models.company import PlatformAdmin

    problem = password_problem(password, email)
    if problem:
        raise SystemExit(problem)
    with system_session() as db:
        email = email.strip().lower()
        if db.query(PlatformAdmin).filter(PlatformAdmin.email == email).first():
            raise SystemExit("A platform administrator with this email already exists.")
        db.add(PlatformAdmin(email=email, full_name=name, password_hash=hash_password(password)))
        db.commit()
    print(f"Platform administrator {email} created.")


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
    """Copy each employee's current primary placement into the legacy employee columns."""
    from app.database.db import SessionLocal, bind_tenant, system_session
    from app.models.company import Company
    from app.services import org_structure_service

    with system_session() as db:
        company_ids = [cid for (cid,) in db.query(Company.id).order_by(Company.id)]
    for company_id in company_ids:
        db = SessionLocal()
        try:
            bind_tenant(db, company_id)
            changed = org_structure_service.sync_all_current(db, company_id)
            db.commit()
            print(f"company {company_id}: {changed} employee(s) updated")
        except Exception as exc:
            db.rollback()
            print(f"company {company_id}: org sync failed: {exc}", file=sys.stderr)
        finally:
            db.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    roles = sub.add_parser("setup-db-roles", help="Create/refresh the RLS-restricted application role")
    roles.add_argument("--app-role", default="hrms_app")

    admin = sub.add_parser("create-platform-admin", help="Create a SaaS platform operator account")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)

    sub.add_parser("rls-status", help="Show whether the app role is subject to row-level security")
    sub.add_parser("attendance-sweep", help="Detect expired shifts and missed location checks; apply retention")
    sub.add_parser("sync-tenant-hosts", help="Create <slug>.<PLATFORM_BASE_DOMAIN> hosts for every company")
    sub.add_parser("domain-checks", help="Run DNS/TLS verification for custom domains")
    sub.add_parser("org-sync", help="Apply effective-dated placements to the legacy employee columns")

    args = parser.parse_args(argv)
    if args.command == "setup-db-roles":
        password = os.environ.get("HRMS_APP_DB_PASSWORD") or getpass.getpass("Password for the app role: ")
        setup_db_roles(args.app_role, password)
    elif args.command == "create-platform-admin":
        password = os.environ.get("HRMS_PLATFORM_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        create_platform_admin(args.email, args.name, password)
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


if __name__ == "__main__":
    main(sys.argv[1:])
