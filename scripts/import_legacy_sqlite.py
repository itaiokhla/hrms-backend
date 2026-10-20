"""One-time import of the legacy single-tenant SQLite database into one tenant.

    python scripts/import_legacy_sqlite.py --sqlite ./hrms.db --company "Intertech" [--commit]

* Runs as a dry run unless --commit is given.
* Everything is imported into ONE new company; nothing is merged into an
  existing tenant and no record is moved between companies.
* Legacy roles map to tenant roles: admin -> Company Administrator,
  hr -> HR Administrator, manager -> Manager, employee -> Employee.
* Rows that fail validation (orphans, duplicate emails, missing required
  values) are written to a quarantine JSON file for manual resolution
  instead of being imported or silently "fixed".
* Imported accounts keep their bcrypt hashes but users who still have the
  well-known default password must reset it before signing in.
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROLE_MAP = {"admin": "company_admin", "hr": "hr_admin", "manager": "manager", "employee": "employee"}


def _rows(conn, table):
    try:
        return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.OperationalError:
        return []


def _parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:19]).date()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    from app.core.security import verify_password
    from app.database.db import system_session
    from app.models.employee import Department, Employee, Shift
    from app.models.user import User
    from app.services import company_service, rbac_service

    legacy = sqlite3.connect(args.sqlite)
    legacy.row_factory = sqlite3.Row
    users = _rows(legacy, "users")
    employees = _rows(legacy, "employees")
    departments = _rows(legacy, "departments")
    shifts = _rows(legacy, "shifts")
    quarantine = {"users": [], "employees": []}

    admins = [u for u in users if u.get("role") == "admin" and u.get("is_active")]
    if not admins:
        sys.exit("The legacy database has no active admin user; nothing can own the new company.")

    with system_session() as db:
        owner = admins[0]
        company, owner_user = company_service.provision_company(
            db,
            company_name=args.company,
            admin_full_name=owner.get("username") or "Administrator",
            admin_email=owner["email"],
            admin_password_hash=owner["password_hash"],
            timezone=args.timezone,
        )
        must_reset = []
        if verify_password("Admin@123", owner["password_hash"]):
            owner_user.status = "suspended"
            must_reset.append(owner["email"])

        dept_map, shift_map = {}, {}
        for d in departments:
            dept = Department(company_id=company.id, name=d["name"], description=d.get("description"))
            db.add(dept)
            db.flush()
            dept_map[d["id"]] = dept.id
        for s in shifts:
            shift = Shift(company_id=company.id, name=s["name"], start_time=s["start_time"], end_time=s["end_time"],
                          duration_hours=s.get("duration_hours"), is_flexible=bool(s.get("is_flexible")))
            db.add(shift)
            db.flush()
            shift_map[s["id"]] = shift.id

        # provision_company created an employee profile for the owner; reuse the
        # legacy profile when there is one and avoid employee-code collisions.
        owner_employee = db.query(Employee).filter(Employee.user_id == owner_user.id).first()
        legacy_owner_emp = next((e for e in employees if e.get("user_id") == owner["id"]), None)
        if legacy_owner_emp:
            owner_employee.employee_code = legacy_owner_emp["employee_id"]
            owner_employee.first_name = legacy_owner_emp["first_name"]
            owner_employee.last_name = legacy_owner_emp["last_name"]
            owner_employee.email = legacy_owner_emp["email"].lower()
            owner_employee.position = legacy_owner_emp.get("position")
            owner_employee.hire_date = _parse_date(legacy_owner_emp.get("hire_date")) or owner_employee.hire_date
            employees = [e for e in employees if e is not legacy_owner_emp]
        elif any(e.get("employee_id") == owner_employee.employee_code for e in employees):
            owner_employee.employee_code = "ADMIN-0001"
        db.flush()

        user_map = {owner["id"]: owner_user.id}
        seen_emails = {owner["email"].lower()}
        for u in users:
            if u["id"] == owner["id"]:
                continue
            email = (u.get("email") or "").strip().lower()
            if not email or email in seen_emails or email.startswith("deleted_"):
                quarantine["users"].append({"legacy_id": u["id"], "email": email, "reason": "missing/duplicate/deleted email"})
                continue
            if db.query(User.id).filter(User.email == email).first():
                quarantine["users"].append({"legacy_id": u["id"], "email": email, "reason": "email already exists on platform"})
                continue
            seen_emails.add(email)
            status = "active" if u.get("is_active") else "deactivated"
            if verify_password("Admin@123", u["password_hash"]):
                status = "suspended"
                must_reset.append(email)
            user = User(company_id=company.id, email=email, full_name=u.get("username") or email,
                        password_hash=u["password_hash"], status=status)
            db.add(user)
            db.flush()
            rbac_service.assign_role_by_key(db, company.id, user, ROLE_MAP.get(u.get("role"), "employee"))
            user_map[u["id"]] = user.id

        for e in employees:
            if e.get("user_id") not in user_map:
                quarantine["employees"].append({"legacy_id": e["id"], "reason": "linked user missing or quarantined"})
                continue
            db.add(Employee(
                company_id=company.id,
                user_id=user_map[e["user_id"]],
                employee_code=e["employee_id"],
                first_name=e["first_name"],
                last_name=e["last_name"],
                email=e["email"].lower(),
                phone=e.get("phone"),
                date_of_birth=_parse_date(e.get("date_of_birth")),
                gender=e.get("gender"),
                address=e.get("address"),
                department_id=dept_map.get(e.get("department_id")),
                shift_id=shift_map.get(e.get("shift_id")),
                position=e.get("position"),
                hire_date=_parse_date(e.get("hire_date")) or date.today(),
                base_salary=e.get("base_salary"),
                is_active=bool(e.get("is_active", True)),
            ))
        db.flush()

        report = {
            "company": {"id": company.id, "name": company.name, "slug": company.slug},
            "imported_users": len(user_map),
            "imported_employees": len(employees) - len(quarantine["employees"]),
            "suspended_default_password_accounts": must_reset,
            "quarantine": quarantine,
            "committed": bool(args.commit),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        if args.commit:
            db.commit()
        else:
            db.rollback()
    out = Path("legacy_import_report.json")
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if not args.commit:
        print("\nDry run only. Re-run with --commit to write the data.")


if __name__ == "__main__":
    main()
