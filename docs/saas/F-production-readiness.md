# F. Production readiness

**Deployment readiness: not ready for production; ready for a staging deployment.** Tenant isolation,
authentication, RBAC, hierarchy scopes, white-label branding and the partner/platform boundaries
held under every test that was run, including real Chrome sessions for three companies. Release still
depends on items that could not be verified here (real DNS/TLS and mail delivery), on features that
do not exist (configuration cascade, pay groups, bureau grants), and on a rehearsal with production
data. The detailed reasons are below.

## Status

### Implemented and verified (automated tests and real Chrome on the final code)

* Tenant isolation across employees, organisation, attendance, leave, payroll and payslips, assets,
  holidays, reports and exports, branding, files, audit, settings; 20 attack simulations (B).
* Server-side tenant context only; ORM guards; PostgreSQL RLS on every tenant table; composite keys;
  connection-pool context safety; contract tests for every route and table (E).
* Authentication: tenant-restricted sign-in, host-only HttpOnly cookies, host-bound sessions, MFA,
  refresh rotation, revocation, CSRF origin check, safe post-sign-in redirects (fixed today).
* RBAC and scopes: SELF, DIRECT_REPORTS, REPORTING_TREE, DEPARTMENT (+ child teams), COMPANY,
  LEGAL_ENTITY, BRANCH; privilege changes apply to live sessions; salary and confidential fields
  masked for HR administrators and managers (C).
* Organisation: one legal entity and two branches per company, departments and teams, reporting trees
  three levels deep, cycle prevention, cross-tenant hierarchy rejection, effective-dated placements.
* White label: brand per host at first paint, platform → partner (locks) → tenant → legal-entity
  documents, draft/publish/rollback, issued payslips reprinted byte for byte, neutral fallback for
  unknown, pending and removed addresses, partner brand reaching only its tenants.
* Platform operator and partner administrator identities, each limited to its own console.
* Existing modules: the 35 existing HRMS Chrome journeys, the 3 attendance/GPS journeys and the 18
  white-label journeys all pass on the final code.

### Implemented, verified only against local stand-ins

* Custom-domain lifecycle against a DNS fixture and simulated TLS. Real DNS lookups and certificates:
  **blocked** (no staging domain or certificate authority available).
* Branded email rendering into a development outbox. Delivery through a real provider: **not tested**.

### Partially implemented

* Legal-entity scoping covers employee, attendance, location, leave, asset and report permissions,
  not payroll or compensation.
* Cache invalidation is per process with short lifetimes; no shared invalidation for several instances.

### Not implemented

General configuration inheritance (EMPLOYEE → PAY_GROUP → BRANCH → LEGAL_ENTITY → TENANT → PARTNER →
COUNTRY_PACK → PLATFORM) with locks and effective dates; country packs and statutory rules; pay
groups and the PAY_GROUP scope; partner bureau grants and platform support-access grants;
partner-managed domains; tenant sending domains (SPF/DKIM); email verification of new accounts;
realtime features (there is no realtime channel, so nothing to secure); recruitment, performance,
tasks and helpdesk modules; translated interface text.

### Blocked

Real DNS, certificate issuance and email delivery (no provider credentials or staging domain).

## Remaining risks

1. Infrastructure paths above were never exercised against real providers.
2. The proxy shared secret is a single point of trust: keep FastAPI private and rotate the secret.
3. Per-process caches and rate limits need shared storage before running several instances.
4. Schema rollback loses brand history, domains and issued-document records; restoring a backup is the
   safe rollback and has not been rehearsed with production data.
5. Testing used synthetic data on one Windows laptop with Chrome only; no load test, no Firefox/Safari,
   no real phones; the security review was done by the same agent that wrote the code.

## Changes in this audit

Backend (`feature/saas-hardening`): partner administrators and console API with migration
`0007_partner_admins` (`eb65b33`); cross-tenant attack tests (`f371433`); platform contract tests
(`b53971f`); branch-location fixes (`c91a08d`, `a8283c6`); three-company seed, scope verifier and these documents
(`e78914c`). Frontend: open-redirect fix (`17ffe47`); partner console (`3f14348`); Chrome suite
`e2e-saas/` (`2a3c543`).

Migration `0007_partner_admins` adds a nullable `platform_admins.partner_id` (foreign key to
`partners`, index). Downgrade deletes partner-administrator accounts and drops the column.

## Test commands and counts (20 October 2026)

| Suite | Command | Result |
|---|---|---|
| Backend | `python -m pytest` | 240 passed in 11 min (0 failed) |
| Frontend unit | `npm test` | 43/43 passed (5 suites) |
| Three-company scope check | `python scripts/verify_saas_scopes.py` | 23 accounts and 2 partners, 0 mismatches |
| Chrome, three-company audit | `npx playwright test -c playwright.saas.config.ts` | 29/29 passed in 9.5 min (headed, final code) |
| Chrome, existing HRMS | `npx playwright test` | 35/35 passed in 8.4 min (first run 34/35, fixed: B defect 5) |
| Chrome, white label | `npx playwright test -c playwright.wl.config.ts` | 18/18 passed in 5.2 min |
| Chrome, attendance and GPS | `npx playwright test -c playwright.gps.config.ts` | 3/3 passed in 25.3 min |

Failures found and fixed during these runs are in B; test-only corrections (the org chart loads one
level at a time; an attack probe whose body was invalid) are described in D.
