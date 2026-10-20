# A. SaaS architecture audit

Audited 20 October 2026 on branch `feature/saas-hardening` (both repositories), against the master
prompt and the reference documents `docs/payroll/01-white-label.md` and `02-multi-level-company.md`
(HRMS root). Evidence order: running system, migrations, FastAPI code, Next.js code, tests, documents.

## Two hierarchies

| Hierarchy | Levels in the code | Where |
|---|---|---|
| Commercial | Platform → Partner → Company (tenant); direct customers have no partner | `companies.partner_id`, `partners` (migration 0005); platform and partner operators in `platform_admins` (0007) |
| Organisational | Company → Legal entity → Branch → Department → Team (child department) → Employee; reporting lines separate | `legal_entities`, `branches`, `departments.parent_department_id`, `employee_assignments`, `employees.manager_id` (0005, 0001) |

The company is the security boundary. A legal entity is the employer, currency and document identity
inside one company; it is never a tenant.

## Components and evidence

| Area | Current implementation | Evidence |
|---|---|---|
| Authentication | Email + password (+ TOTP MFA), HttpOnly host-only cookies, refresh rotation with reuse detection, CSRF double submit plus exact `Origin` check, sessions bound to the host they were created on | `app/routes/auth.py`, `app/services/auth_service.py`, `app/core/security.py`, `app/core/principal.py` |
| Tenant context | Company comes only from the server-side session; `bind_tenant` sets `app.company_id` per transaction; ORM guards stamp/filter `company_id`; PostgreSQL RLS on every tenant table; least-privilege `hrms_app` role | `app/database/db.py`, `app/models/base.py` (`TenantMixin`), migration 0001, `tests/test_platform_contracts.py` |
| Same-tenant references | Composite `(company_id, id)` foreign keys between tenant tables | `tests/test_platform_contracts.py::test_foreign_keys_between_tenant_tables_include_the_company` (none missing) |
| RBAC | 52 permissions with scopes SELF, DIRECT_REPORTS, REPORTING_TREE, DEPARTMENT (incl. child teams), ASSIGNED_GROUP, COMPANY, LEGAL_ENTITY, BRANCH; built-in and custom roles; per-assignment targets for entity/branch scopes; permission version invalidates live sessions | `app/core/permission_registry.py`, `app/core/authz.py`, `app/routes/users.py` |
| Hierarchy | Primary manager tree with cycle prevention (advisory lock + DB trigger), secondary lines, delegations with expiry | `app/services/org_service.py`, `app/core/hierarchy.py` |
| Hosts and branding | Platform hosts, `<slug>.<base domain>`, verified custom domains; proxy secret boundary; brand cascade platform → partner (locks) → tenant → legal-entity documents; server-rendered first paint | `app/core/hosts.py`, `app/services/brand_service.py`, `hrms-frontend/src/middleware.ts`, `src/lib/brand-server.ts` |
| Platform operator | Separate principal, cookies and sessions; aggregate counts only | `app/routes/platform.py` |
| Partner administrator | Added in this audit: `platform_admins.partner_id`; own console, own clients (counts), own brand | `app/routes/partner.py`, `hrms-frontend/src/app/partner/` |
| Settings | Per-company key/value (`company_settings`), attendance policy, leave types, approval chain; brand has its own cascade | `app/models/system_settings.py`, `app/services/settings_service.py` |
| Files | `UPLOAD_DIR/<company_id>/<category>/…`, resolved per company with traversal checks; served only through authorised routes; brand assets content-addressed and host-checked | `app/core/storage.py`, `app/routes/employees.py` (photo), `app/routes/branding.py` |
| Email | Sandboxed Jinja templates per tenant; development outbox; SMTP unchanged | `app/services/email_templates.py`, `email_service.py` |
| Documents | Payslips issued once (snapshot, bytes, SHA-256, immutability trigger) with legal-entity identity; Unicode fonts | `app/services/document_service.py`, `app/utils/pdf_generator.py` |
| Background jobs | CLI jobs bind one company at a time as the RLS-restricted role (`attendance-sweep`, `org-sync`, `domain-checks`) | `app/cli.py`, `tests/test_tenant_isolation.py::test_background_job_requires_explicit_tenant` |
| Reports and exports | Every report query goes through the caller's scopes; Excel/PDF exports per company | `app/services/reports_service.py` |
| Realtime | **None.** No WebSocket or server-sent events exist, so there is no channel to subscribe to. | `grep -r WebSocket app/` (no matches) |

## Modules present (and absent)

Present and tested: authentication/MFA/reset/invitations, users and roles, employees, organisation
chart (secondary lines, delegations), legal entities and branches, attendance (face, GPS, lunch,
kiosk), leave and approval chains, holidays (company and regional), payroll and payslips, assets,
reports and exports, audit log, notifications, dashboard, branding and domains, platform console,
partner console.

**Not present in the code** (nothing to test, and no screens were added for them): recruitment,
performance, tasks, helpdesk, general document management, realtime messaging.

## Gaps against the target architecture

| Requirement | State |
|---|---|
| General configuration cascade EMPLOYEE → PAY_GROUP → BRANCH → LEGAL_ENTITY → TENANT → PARTNER → COUNTRY_PACK → PLATFORM | **Not implemented.** Only the brand cascade exists. Company settings are flat per company; there are no country packs, effective dating or locks for settings. |
| PAY_GROUP scope and pay groups, cost centres | **Not implemented.** |
| Payroll and compensation permissions scoped to a legal entity | **Not implemented** (company or self only). |
| Partner bureau grants, platform support-access grants | **Not implemented.** Partners and operators have no route to HR data at all. |
| Partner-managed domains | **Not implemented** (domains belong to tenants). |
| Email verification flow for new accounts | **Not present.** Sign-up and invitations rely on the invitation link and reset codes. |
| Tenant sending domains (SPF/DKIM) | **Not implemented.** |
| Multi-instance cache invalidation | Per-process caches with short lifetimes (brand 60 s, hosts 10 s, frontend 5 s). |
| Statutory payroll per country | **Not implemented** (country metadata only). |
