# C. Three-company test environment

A deterministic, synthetic environment on its own database (`.local-postgres-saas`), API port 8300
and frontend port 3300. It never touches the development stack (`.local-postgres`, ports 8000/3000)
or any real data.

## Start and seed

```bash
# hrms-backend
export $(grep -v '^#' .local-secrets/wl-stack.env | xargs)      # PLATFORM_HOSTS, PLATFORM_BASE_DOMAIN, fixture DNS, local TLS
export PROXY_SHARED_SECRET="$(cat .local-secrets/wl-proxy-secret)" \
       DOMAIN_DNS_FIXTURE_FILE=logs/saas-dns.json FRONTEND_URL=http://localhost:3300
python scripts/demo_stack.py --fresh --data-dir .local-postgres-saas --port 8300
python scripts/seed_saas_three_companies.py --api http://localhost:8300/api/v1 --frontend http://localhost:3300
python scripts/verify_saas_scopes.py            # every account's real scope through Next.js; exits 1 on a mismatch

# hrms-frontend (same PROXY_SHARED_SECRET)
NEXT_DIST_DIR=.next-saas BACKEND_URL=http://localhost:8300 PLATFORM_HOSTS=localhost,127.0.0.1 npx next build
NEXT_DIST_DIR=.next-saas BACKEND_URL=http://localhost:8300 PLATFORM_HOSTS=localhost,127.0.0.1 npx next start -p 3300
npx playwright test -c playwright.saas.config.ts
```

Re-seeding needs `--fresh` (the seed creates companies through the public API). Seeding takes about
two minutes.

## Credentials

One random password per run, never committed:

* `.local-secrets/saas-e2e.json`: machine-readable (ids, emails, the run password, hosts).
* `.local-secrets/saas-test-accounts.md`: readable table with Company, Test login URL, Role, Test email,
  Credential reference (`SAAS_E2E` = the `password` field of the JSON), Expected permissions and
  Expected data scope.

Both are under the git-ignored `.local-secrets/` folder. The seed prints only their paths.

## What is seeded

| | Alpha Technologies | Beta Logistics | Gamma Solutions |
|---|---|---|---|
| Country, currency, time zone | IN, INR, Asia/Kolkata | AE, AED, Asia/Dubai | US, USD, America/New_York |
| Commercial | Direct customer, Enterprise | Partner Northwind Partners, Professional | Partner Northwind Partners, Standard |
| Brand | Alpha People, indigo on white (light sidebar, Poppins) | Beta Workforce, teal on charcoal (pill corners, Roboto) | Gamma HR Cloud, orange and navy (sharp corners, Inter) |
| Legal entity | Alpha Technologies Pvt Ltd (CIN, PAN) | Beta Logistics LLC (trade licence) | Gamma Solutions Inc. (EIN) |
| Branches | Bengaluru, Delhi | Dubai, Abu Dhabi | New York, California |
| Departments (+ team) | HR, Engineering (+ Platform Team), Finance, Operations | HR, Operations, Fleet (+ Dispatch Team), Finance | Administration, Engineering (+ Web Team), Sales, Finance |
| People | Company admin, HR admin, Finance manager, Engineering manager, Team lead, 3 employees | Company admin, HR manager, Payroll manager, Operations manager, 2 employees | Company admin, HR admin, Engineering manager, Team lead, 2 employees |
| Reporting depth | Admin → Engineering manager → Team lead → 2 engineers; Admin → Finance manager → analyst | Admin → Operations manager → dispatcher; Admin → supervisor (Abu Dhabi) | Admin → Engineering manager → Team lead → 2 engineers |
| Special scopes | Team Lead role (direct reports) | Operations Manager role, BRANCH-scoped to Dubai | Team Lead role (direct reports) |
| Payroll | Last month, approved (issued payslips) | Last month, approved | Last month, **draft** (see below) |
| Also | A week of attendance with 40-minute lunches, one late arrival; approved and pending leave; company and regional holidays; assigned assets; published brand with logos | same | same; verified custom domain `hr.gamma-solutions.localhost` |

Partners: **Northwind Partners** (Beta and Gamma; locks the "Powered by" line on) and **Southern
Cross Resellers** (no clients), each with a partner administrator. Domains: Beta's
`people.beta-logistics.localhost` is pending (no DNS records) and Alpha's `old.alpha-people.localhost`
was added and removed; both show the neutral unknown-address page.

Gamma's specified roles include no payroll user besides the administrator, so its payroll stays a
draft: separation of duties refuses approval by the person who prepared it. That is the product
behaving correctly, not a seeding failure.

## Expected scopes (checked by `verify_saas_scopes.py`)

| Role | Sees in Employees | Payroll | Users/roles | Branding |
|---|---|---|---|---|
| Company administrator | Whole company | All rows, salaries | Yes | Yes |
| HR administrator / manager | Whole company, no salaries | Refused (403) | Yes | Email wording only |
| Payroll / finance manager | Whole company with salaries | All rows | Refused | Refused |
| Engineering (department) manager | Self and reporting tree | Refused | Refused | Refused |
| Team lead | Self and direct reports | Refused | Refused | Refused |
| Beta Operations manager | Staff placed in Dubai (not Abu Dhabi) | Refused | Refused | Refused |
| Employee | Self | Own payslips | Refused | Refused |
| Partner administrator | No HR data (401 on tenant routes) | — | — | Own partner brand |
| Platform operator | No HR data (401 on tenant routes) | — | — | Platform and partner brands |
