# D. Chrome testing evidence

All runs used installed Google Chrome 155.0.8059.80 through Playwright 1.52.0 (headed unless noted),
production builds of the Next.js frontend (15.5.18) and the FastAPI backend on embedded PostgreSQL
16.2 with the least-privilege `hrms_app` role (row-level security on). Windows 11, 7.5 GB RAM.
Evidence is in `hrms-frontend/test-evidence/2026-10-20-saas/` (git-ignored): screenshots, payslip
PDFs and rendered pages, per-person network and console logs, recorded attack responses, videos and
traces in the HTML reports.

## Three-company audit suite (`hrms-frontend/e2e-saas/`)

Final run on the final code: **29/29 passed in 9.5 minutes** (headed, started 15:25 UTC, 0 flaky,
0 skipped; `final-code-run-29-of-29/`), on a fresh seed whose 23 accounts and 2 partners
`verify_saas_scopes.py` checked first (0 mismatches, `saas-scope-matrix.json`). Earlier runs: 29/29 at
13:27 UTC before the location fix (`final-run-29-of-29/`), and the attack spec 3/3 after tightening one
probe.

| Spec | Test | What is checked |
|---|---|---|
| 01 | Each address paints its own brand; unknown, pending and removed addresses stay neutral | Raw HTML title, theme colour, "Powered by" rule, `private, no-store`; no other company named; Gamma's verified custom domain; neutral page for `people.beta-logistics.localhost` (pending), `old.alpha-people.localhost` (removed), `nobody.hrms.localhost` |
| 01 | Three administrators sign in on their own addresses | Branded login and dashboard screenshots; title and theme-colour meta; `auth/me` company; cookies host-only and HttpOnly; no cookies for the other hosts; other hosts answer 401; each browser contacted only its own host; no page errors |
| 01 | Gamma's custom domain signs in Gamma only | Alpha's administrator gets the generic error there; a Gamma employee signs in |
| 02 | ×3 Each administrator cannot reach the other two companies | 16 direct API attacks per victim (records, photos, placements, leave, payroll and payslips, entity, branch, department, manager, user status, assignments), search with forged `company_id`, foreign department filter, leave report, forged `company_id` body, the victim's logo on the attacker's host, the victim's host with the attacker's cookies; victim data unchanged afterwards. Responses recorded in `02-<company>-attack-responses.json` |
| 03 | Alpha / Beta / Gamma role scopes | Department manager = reporting tree, team lead = direct reports, employee = self with self-service menu and the access-denied page on six admin URLs, HR sees everyone but no salaries and no payroll, finance sees ₹ payroll; Beta Operations Manager sees Dubai staff only and gets 404 for Abu Dhabi; Beta payroll in AED |
| 04 | ×3 Legal entity, branches, org chart, roles | Entity name and currency, both branches, three management levels in the org chart, roles page; reporting cycle refused; manager from another company refused (404) |
| 05 | Gamma: draft, preview, publish, live update, rollback | Draft invisible to others; SVG and CSS-injection colour refused; partner-locked "Powered by" cannot be switched off; publish diff; first paint changes; Alpha and Beta untouched; rollback from History |
| 05 | Alpha: payslip reprinted byte for byte after a brand change | Same SHA-256 and bytes |
| 06 | ×3 Employee modules | Attendance history (≥ 5 seeded days), leave, holidays, assigned asset, payslip PDF in INR/AED with the right legal entity and no other company (checked with PyMuPDF); Gamma has no published payslip (draft payroll) |
| 06 | Team lead approves a pending leave in the browser; cannot approve another company's | Approval card, toast; 404 for the foreign request |
| 06 | Regional holidays follow the branch | Bengaluru employee gets Karnataka Rajyotsava, Delhi employee does not |
| 07 | Deep links, reload, back/forward, second tab after sign-out | Second tab is sent to sign-in |
| 07 | Crafted `?next=` never leaves the company's address | `/%5Cexample.com`, `/%5C%5Cexample.com`, `/%09/example.com`, `//example.com`, `https://example.com` all land on the tenant host |
| 07 | Silent refresh; revoked sessions and roles | Expired access cookie refreshes; revoked sessions return to sign-in; HR role removal shows access denied at once, restore works |
| 07 | Failed network call and malformed response | App stays usable, recovers after the fault is removed |
| 07 | ×3 Phone (390×844) and tablet (820×1180) | No sideways scrolling on dashboard, leave and payslips; phone menu drawer |
| 08 | Platform operator | Three tenants and partner assignment visible; 401 on HR routes and on the partner console |
| 08 | Partner administrators | Northwind sees Beta and Gamma only; Southern Cross sees the empty state; 401 on tenant and platform routes; publishing a support link reaches Beta, not Alpha; Beta keeps its own product name |
| 08 | Partner console on a tenant address | 404 page and 404 API |

### Defects found by the suite (fixed, see B)

* Regional holidays did not follow the branch for employees hired with a manager (spec 06, first run).
* `?next=` open redirect (found while extending spec 07).

### Test-only corrections (no product change)

* Spec 04 first expected the team lead on the org chart without expanding it; the chart loads one level
  at a time by design, so the test now expands two levels.
* Spec 02 sent `{"status": "inactive"}` to the user-status endpoint, which the schema rejects (422)
  before any ownership check, so that probe proved nothing. It now sends `suspended`, and only 403/404
  are accepted for every probe; the rerun returned 404 in all six directions.

## Existing suites rerun on the final code

| Suite | Result | Evidence |
|---|---|---|
| Existing HRMS journeys (`e2e/`, 35 tests, single-host stack) | First run 34/35: "Holiday calendars are per company and per location" failed. Root cause: a free-text location without a matching branch was overwritten with "Main" (B, defect 5). After the fix: **35/35 passed in 8.4 min** | `regression-35-run1-34-of-35/`, `regression-35-of-35/` |
| Attendance, lunch and GPS (`e2e-gps/`, 3 tests) | **3/3 passed in 25.3 min** on a fresh GPS seed | `gps-final/` |
| White label (`e2e-wl/`, 18 tests) | **18/18 passed in 5.2 min** on a fresh seed | `wl-final/` |

## Limits of this evidence

* Domains use a DNS fixture and simulated TLS; emails were read from the development outbox.
* Synthetic data only; one machine; Chrome only (no Firefox, Safari or physical phones); positions in
  the GPS suite come from Chrome's geolocation emulation.
* Screenshots illustrate; isolation is proven by the recorded API responses and the backend tests.
