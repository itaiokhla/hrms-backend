# HRMS Backend (multi-tenant SaaS)

FastAPI + SQLAlchemy 2 + PostgreSQL (Neon). Each company is an isolated tenant:
companies sign up, invite their people, define roles, and run attendance,
leave, payroll, assets and reports without ever seeing another company's data.

## Architecture in one page

| Concern | Where |
|---|---|
| Tenant boundary | `company_id` on every tenant table, composite `(company_id, id)` foreign keys, ORM guards in `app/database/db.py`, PostgreSQL RLS (`alembic/versions/0001_saas_baseline.py`) |
| Identity | `app/core/principal.py` resolves the company **only** from the server-side session (HttpOnly cookies + CSRF, or Bearer) |
| Permissions | `app/core/permission_registry.py` — single registry of `resource:action` codes and allowed scopes (frontend constants are generated from it) |
| Data scopes | `app/core/authz.py` turns SELF / DIRECT_REPORTS / REPORTING_TREE / DEPARTMENT / ASSIGNED_GROUP / COMPANY into SQL sub-selects applied to every query |
| Hierarchy | `employees.manager_id` (primary line), `secondary_reporting_lines`, `approval_delegations`; cycles blocked in the service and by a DB trigger |
| Delegated admin | `app/services/rbac_service.py` — no privilege escalation, protected Company Administrator role, last-admin protection |
| Platform operators | Separate principal (`platform_admins`), separate cookies, `/api/v1/platform/*`, no access to tenant HR records |

## Setup with Neon

1. Create a Neon project. Copy the **owner** connection string (role `neondb_owner`).
2. Configure `.env` (see `.env.example`):
   ```
   MIGRATION_DATABASE_URL=<owner, direct host>
   SYSTEM_DATABASE_URL=<owner, pooled host>
   SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
   ```
3. Install and migrate:
   ```bash
   python -m venv venv && venv\Scripts\activate      # Windows
   pip install -r requirements.txt
   alembic upgrade head
   ```
4. Create the least-privilege application role (RLS is enforced for it):
   ```bash
   python -m app.cli setup-db-roles --app-role hrms_app
   ```
   Then set `DATABASE_URL` to the **pooled** host with user `hrms_app`.
   Re-run this command after future migrations to refresh grants.
5. Check isolation: `python -m app.cli rls-status` should print `rls_enforced: True`.
6. (Optional) create a platform operator: `python -m app.cli create-platform-admin --email ops@you.com --name "Ops"`.
7. Run: `uvicorn app.main:app --reload --port 8000`.

No Neon yet? `python scripts/local_postgres.py` starts a local PostgreSQL 16
and prints an owner URL you can use for steps 2–5.

### Importing the legacy single-tenant database

```bash
python scripts/import_legacy_sqlite.py --sqlite ./hrms.db --company "Your Company"          # dry run
python scripts/import_legacy_sqlite.py --sqlite ./hrms.db --company "Your Company" --commit
```
Invalid rows are quarantined in `legacy_import_report.json`; accounts still using
the old default password are suspended until reset.

## Tests

The suite runs against a real PostgreSQL 16 (embedded via `pgserver`) with the
application connected as a non-superuser role, so row-level security is
exercised exactly as in production.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Demo stack with seeded companies

A complete local stack for trying the product or running the browser tests
(`hrms-frontend/e2e`). It uses an embedded PostgreSQL 16, not Neon:

```bash
python scripts/demo_stack.py --fresh   # PostgreSQL + migrations + hrms_app role + platform operator + API on :8000
python scripts/seed_demo.py            # Nimbus Technologies and Saffron Foods, created through the public API
```

The stack runs with `ENVIRONMENT=development` and no SMTP: one-time codes and
invitation links are written to `logs/demo-backend.log` (lines marked
`[DEV ONLY]`) instead of being emailed. Run `--fresh` again to reset to a clean
database. The browser tests change data, so reset and reseed before each full run.

Every seeded account uses the password `Demo@12345`. The platform console
(`/platform/login`) is `ops@hrms-platform.io` / `Platform@12345`.

**Nimbus Technologies** (`@nimbustech.io`, IT services, five reporting levels)

| Account | Roles | Position, reports to |
|---|---|---|
| `ananya.rao` | Company Administrator | CEO |
| `vikram.shah` | HR Administrator | Head of People → Ananya |
| `pooja.sharma` | Recruiter (custom) | Talent Acquisition → Vikram |
| `priya.nair` | Payroll Manager | Finance Controller → Ananya |
| `karan.mehta` | Payroll Manager | Payroll Analyst → Priya |
| `neha.joshi` | Auditor, Employee | Internal Auditor → Ananya |
| `rohan.gupta` | Manager | VP Engineering → Ananya |
| `sneha.iyer` | Manager | Engineering Manager → Rohan |
| `arjun.das` | Employee, Team Lead (custom) | Tech Lead → Sneha |
| `divya.menon`, `farhan.ali` | Employee | Software Engineers → Arjun |
| `meera.pillai` | Employee | QA Engineer → Sneha; project line to Tara |
| `nikhil.jain` | Employee | DevOps Engineer → Rohan |
| `tara.kapoor` | Manager | Sales Director → Ananya |
| `rahul.verma`, `isha.bose` | Employee | Account Executives → Tara |

Sameer Khan (intern → Arjun) is an employee record without a login.

**Saffron Foods** (`@saffronfoods.in`, manufacturing, shifts, button check-in)

| Account | Roles | Position, reports to |
|---|---|---|
| `rajesh.kumar` | Company Administrator | Managing Director |
| `kavita.menon` | HR Administrator | HR Manager → Rajesh |
| `suresh.pillai` | Manager | Plant Head → Rajesh |
| `lakshmi.narayanan` | Manager, Team Lead (custom, different permissions from Nimbus's) | Shift Supervisor → Suresh |
| `ramesh.babu`, `ganesh.murthy` | Employee | Machine Operators (morning / evening shift) → Lakshmi |
| `pooja.sharma` | Employee | Quality Analyst → Suresh (same name as Nimbus's recruiter) |
| `deepa.raghavan`, `anil.kumar` | Payroll Manager | Accounts → Rajesh / Deepa |

**Orbit Logistics** (`@orbitlogistics.io`) is not seeded: the first browser test
signs it up in the UI (`daniel.dsouza`, then invites `fatima.khan` as Manager and
`joseph.mathew` as Employee).

## Keeping the frontend in sync

After changing the permission registry:
```bash
python scripts/export_permissions.py   # writes hrms-frontend/src/lib/permissions.generated.ts
```
