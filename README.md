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
| Attendance, lunch & location | `app/services/attendance_service.py` (state machine), `attendance_hours.py` (the only hours calculator), `location_service.py` (location evidence and the periodic schedule), `geo.py` (WGS-84 geodesic distances); see "Attendance, lunch and location" below |

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

## Attendance, lunch and location

* **State machine** (server-enforced): `NOT_PUNCHED_IN → WORKING → ON_LUNCH → WORKING → … → PUNCHED_OUT`
  (Punch In `POST /attendance/me/check-in`, Lunch In `…/lunch-in`, Lunch Out `…/lunch-out`, Punch Out `…/check-out`).
  Each action takes a per-employee transaction lock, is written to the append-only `attendance_events`
  table with the server's time, and accepts an `idempotency_key` (retries return the first result).
  One *live* session per employee is enforced by a partial unique index, so overnight shifts punch out
  the next day. A shift left open past `maxShiftHours` is closed **without** inventing a punch-out and
  flagged for an HR correction. Punch-out during lunch asks for confirmation (or is blocked, by policy).
* **Hours**: `gross = out − in`, `net = gross − unpaid lunch`, whole UTC seconds, computed only by
  `attendance_hours.compute`. Views, reports, Excel/PDF exports and the payroll run snapshot
  (`payroll.net_work_seconds`) all use it, so they reconcile to the second. Pay amounts are unchanged.
* **Location** (off until a Company Administrator enables it under *Company settings → Attendance &
  location*): per-event policy OFF / OPTIONAL / REQUIRED for punches and for lunch; REQUIRED can only
  be bypassed through an explicit, manager-reviewed exception. Readings are the browser's estimate with
  its accuracy radius; failures are stored as failures and never carry coordinates. Movement is a
  straight-line distance from the punch-in reading, flagged for review only beyond both accuracy radii.
  Coordinates need the separate `location:read` permission (managers: direct reports) and every view by
  someone else is audited. Coordinates are redacted after `locationRetentionDays`.
* **Periodic checks** (best effort, web only): due every `trackingIntervalMinutes` (default 120) after
  the latest reading or lunch end; paused at lunch; stopped at punch-out, suspension, role removal or
  when disabled. A slot with no reading by due + grace is recorded as MISSED and the next reading is
  marked as a recovery; nothing is back-filled. Browsers pause hidden or closed pages, so schedule the
  sweep that detects missed slots (it cannot obtain positions):
  ```bash
  python -m app.cli attendance-sweep      # e.g. every 15 minutes (also applies location retention)
  ```
* `LOCATION_TRACKING_MIN_INTERVAL_MINUTES` (default 15) is the shortest interval a company may choose;
  values below 15 are refused in staging and production and exist only for accelerated browser tests.
* Migration `0004_attendance_gps_tracking` is additive. After upgrading, re-run
  `python -m app.cli setup-db-roles` so the application role gets the (append-only) grants.

### Three-company location browser tests

```bash
python scripts/demo_stack.py --fresh --data-dir .local-postgres-e2e --port 8100   # isolated stack
python scripts/seed_gps_e2e.py --api http://localhost:8100/api/v1                  # Alpha, Beta, Gamma
```
The seed generates a random password per run and writes it, with the account list, to
`.local-secrets/gps-e2e.json` (git-ignored). See the frontend README to run the Chrome suite.

## White label and organisation structure

Design: `docs/payroll/01-white-label.md` and `02-multi-level-company.md` (repository root).

* **Brands** are data: `PLATFORM -> PARTNER (may lock fields) -> TENANT -> LEGAL_ENTITY`
  (documents only), each with a draft, one published version and archived history.
  Fields resolve independently (lock, then entitled tenant value, then partner, then
  platform). Colours become accessible light/dark tokens on the server; images are decoded,
  stripped of metadata, re-encoded and stored as `UPLOAD_DIR/brand/<sha256>.<ext>`.
* **Hosts**: the Next.js middleware sends the original host with `PROXY_SHARED_SECRET`;
  without a matching secret every request counts as the platform host. Tenant hosts
  (`<slug>.<PLATFORM_BASE_DOMAIN>` and verified custom domains) only accept their own
  tenant's users, sessions are bound to the host they were created on, and the platform
  console answers only on `PLATFORM_HOSTS`. Unknown hosts get a neutral page.
* **Documents**: payslips are issued (snapshot + PDF bytes + brand version) when payroll is
  approved; downloads return the stored bytes. PDFs embed Noto Sans (app/assets/fonts, OFL).
* **Organisation**: legal entities, branches, effective-dated placements
  (`employee_assignments`), teams (child departments), `LEGAL_ENTITY`/`BRANCH` role scopes
  with targets per role assignment. Payroll rows carry the entity's currency.

### Rollout

1. Back up the database (Neon branch or `pg_dump`) and `UPLOAD_DIR`.
2. `pip install -r requirements.txt` (adds jinja2, uharfbuzz, publicsuffixlist, dnspython, pycountry).
3. `alembic upgrade head` (owner role) — `0005_org_structure` and `0006_white_label` only add
   tables/columns and backfill: one unconfirmed default legal entity per company, branches from
   `employees.location`, one placement per employee, payroll currency, a v1 brand per company
   with its existing logo re-processed, and the platform brand.
4. `python -m app.cli setup-db-roles` (grants the new tenant tables; issued documents become
   immutable for the app role).
5. Deploy backend and frontend. Without `PLATFORM_BASE_DOMAIN` everything stays single-host.
6. To enable tenant hosts: wildcard DNS + wildcard TLS for the base domain, set
   `PLATFORM_HOSTS`, `PLATFORM_BASE_DOMAIN`, `PROXY_SHARED_SECRET` (both apps),
   `CUSTOM_DOMAIN_CNAME_TARGET`; keep FastAPI private; leave `COOKIE_DOMAIN` unset.
   `python -m app.cli sync-tenant-hosts` (also runs at startup).
7. Schedule `python -m app.cli domain-checks` every ~5 minutes and `python -m app.cli org-sync` daily.

### Rollback

Code: redeploy the previous release; it runs on the new schema (rehearsed with `811f561`).
Rows it writes lack the new structure: payroll currency and entity are filled by a trigger,
and `python -m app.cli org-sync` creates the missing entity, branch, placements and brand
once the new release is back. Run it after every roll-forward.
Schema: `alembic downgrade 0004_attendance_gps_tracking` drops the new tables and columns
(brand history, partners, domains, issued-document records, placements, entity details and
entity/branch-scoped grants are lost; payroll, attendance, leave and users are untouched), so
prefer restoring the backup taken in step 1. Upgrading again re-runs the backfill (version-1
brands, one placement per employee) and payslips are re-issued with the current brand.
Brand files under `UPLOAD_DIR/brand` can stay; they are only content-addressed images.

### Not implemented (see the report for the full list)

Custom email sending domains (no provider with domain-identity APIs configured), partner
bureau grants, platform support-access grants, pay groups and the `PAY_GROUP` scope,
concurrent assignments, the general 8-level settings cascade (only the brand cascade and its
explain view exist), SVG logos, translated UI strings.

### Partner administrators

Resellers get their own console at `/partner` on platform hosts: their client companies as
aggregate counts and their partner brand (with the fields they lock for clients). They never
see tenant HR data and cannot use the platform console. Create one as a platform operator
(`POST /api/v1/platform/partners/{id}/admins`) or with
`python -m app.cli create-partner-admin --partner <slug> --email … --name …`
(password from `HRMS_PARTNER_ADMIN_PASSWORD` or a prompt). Migration `0007_partner_admins`.

## Multi-tenant audit and the three-company environment

`docs/saas/` holds the audit, security findings, the three-company seed
(`scripts/seed_saas_three_companies.py`, checked by `scripts/verify_saas_scopes.py`), the Chrome
evidence, the rules for new modules and the readiness report. `tests/test_platform_contracts.py`
enforces the tenancy and authorization rules for every route and table, including future ones.

## Keeping the frontend in sync

After changing the permission registry:
```bash
python scripts/export_permissions.py   # writes hrms-frontend/src/lib/permissions.generated.ts
```
