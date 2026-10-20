# B. Security findings and fixes

Found while seeding three companies and running the attack, contract and Chrome suites on
20 October 2026. Each has a regression test that fails without the fix.

| # | Severity | Finding | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | Medium | **Open redirect after sign-in.** `/login?next=/%5Cexample.com` or `/login?next=/%09/example.com` on a tenant address sent the user to another site once signed in (reproduced in Chrome). | `safeNext` only rejected `//…`; Chrome normalises `/\` and `/<tab>/` into a protocol-relative URL. | Resolve `next` with the URL parser and use it only when it stays on the same origin (`src/lib/safe-redirect.ts`). Frontend `17ffe47`. | `src/lib/__tests__/safe-redirect.test.ts` (13 cases); Chrome `e2e-saas/07` (crafted `next` values never leave the host) |
| 2 | Medium (integrity) | **Employees hired with a manager lost their branch location**, so their branch's regional holidays never applied and branch-based columns were empty. | `record_hire` copied the branch name without flushing; `assign_manager` then refreshed the employee and discarded it. | `_sync_legacy` flushes. Existing rows are repaired by `python -m app.cli org-sync` (17 of 20 seeded employees repaired in the run). Backend `c91a08d`. | `tests/test_org_documents.py::test_hire_with_manager_keeps_branch_location_for_regional_holidays`; Chrome `e2e-saas/06` regional holidays |
| 3 | Missing control | **No partner identity.** Partners existed only as records; a reseller could not be given access without a platform-operator account, which sees every tenant. | Not built. | Partner administrators (`platform_admins.partner_id`, migration 0007): partner console only, own clients as counts, own brand; platform and tenant routes refuse them; the account row decides the kind, so re-scoping or suspending takes effect at once. Backend `eb65b33`, frontend `3f14348`. | `tests/test_partner_admins.py` (5); Chrome `e2e-saas/08` |
| 4 | Data integrity during rollback | Payroll written by the previous release after a code rollback would have been labelled INR whatever the company's currency. | Static column default. | Trigger fills currency and entity like the current release (found earlier today by the rollback rehearsal, backend `b5e3c7f`). | `tests/test_org_documents.py::test_rows_written_by_an_older_release_are_completed` |

## Attacks that were refused (no defect)

All 20 attack simulations of the master prompt were run or are covered (see D for the Chrome
evidence and `tests/test_cross_tenant_attacks.py`, `tests/test_tenant_isolation.py`,
`tests/test_hosts_domains.py`, `tests/test_white_label.py`):

| # | Attack | Result |
|---|---|---|
| 1 | Another tenant's employee id in reads, updates, photos, placements, leave balances | 404 |
| 2 | Forged `company_id` in bodies | 422 (unknown fields refused) |
| 3 | Forged `company_id`/`tenant_id` query parameters | Ignored; own data only |
| 4 | Nested references (department, manager, asset holder, secondary line, holiday) | 400/404, nothing written |
| 5 | Bulk update with one foreign id | Whole request refused (404) |
| 6 | Another tenant's export or report filter | Own rows only (checked inside the Excel files) |
| 7 | Another tenant's payslip (JSON and PDF) | 404 |
| 8 | Another tenant's logo / brand asset / document brand | 404 on the wrong host |
| 9 | Manager from another company | 404 |
| 10 | Role assignment with another tenant's role | 404 |
| 11 | Search for another tenant's people | No results |
| 12 | Pagination and filters with foreign ids | Empty |
| 13 | Another company's realtime channel | Not applicable: no realtime features exist |
| 14 | Stale cache: hostname moved between tenants; privilege downgrade on a live session | New owner's brand within 10 s; downgrade applies on the next request |
| 15 | Cookies or tokens replayed on another tenant's host | 401 (host-only cookies, host-bound sessions) |
| 16 | Tenant data through platform or partner endpoints | No such endpoint; tenant routes refuse operator sessions (401) |
| 17 | Cross-tenant file access | 404; files resolve inside the caller's company folder only |
| 18 | Report aggregates across companies | Own totals only |
| 19 | Concurrent interleaved requests from three tenants | Each always sees only its own rows |
| 20 | Background job without or with another tenant context | Refused by the ORM guard and RLS |
