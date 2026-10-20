# E. Building a new module on the SaaS foundation

Every new HRMS resource (recruitment, performance, helpdesk, …) reuses the same building blocks.
The contract tests in `tests/test_platform_contracts.py` fail automatically when a new route or
table skips them.

## Building blocks to reuse

| Need | Use | Never |
|---|---|---|
| Who is calling | `Depends(require("<module>:<action>"))` or `get_principal` from `app/core/principal.py` | Read a company, role or user id from the request body, query, header or host |
| Tenant context | `get_db` (bound to the principal's company); background work: `SessionLocal()` + `bind_tenant(db, company_id)` one company at a time | Use `system_session()` for tenant data |
| Tenant ownership | Models inherit `TenantMixin`; the migration enables RLS with the standard `tenant_isolation` policy and grants the app role | Add `company_id` by hand or skip RLS |
| References between tenant rows | Composite foreign keys `(company_id, x_id) → (company_id, id)` | Single-column foreign keys between tenant tables |
| Permissions | Register codes and allowed scopes in `app/core/permission_registry.py`; regenerate `permissions.generated.ts` | Check role names (`if role == "hr_admin"`) |
| Data scopes | `authz` helpers (`ensure_employee_access`, scoped queries) for SELF, DIRECT_REPORTS, REPORTING_TREE, DEPARTMENT, ASSIGNED_GROUP, COMPANY, LEGAL_ENTITY, BRANCH | Filter in the frontend only |
| Sensitive fields | `FieldAccess` / confidential permissions (`employee:read_confidential`, `compensation:read`) | Return salary, bank or identity data to anyone who can see the employee |
| Missing or foreign ids | Return 404 for "not yours" exactly like "does not exist" | Return 403 that confirms another tenant's record exists |
| Files | `app/core/storage.py` (`save_bytes(company_id, …)`, `resolve(company_id, …)`), served through an authorised route | Public URLs or paths taken from the request |
| Brand on screens, emails, PDFs | `brand_service.effective_for_company`, `email_templates`, `document_service` | Hard-code product names, colours or logos |
| Currency, time zone, country | The employee's legal entity (`org_structure_service.entity_for_employee_on`), company settings | Assume INR, IST or India |
| Audit | `AuditLogService.log` for every sensitive change | Log secrets or salary values |
| Caches | Key by company (and host for branding); bounded lifetime; clear on change | Global keys or values shared between tenants |
| Exports and reports | Build from the same scoped queries as the list endpoints | Separate unscoped report queries |

## Checklist for a new resource

1. Model with `TenantMixin`, migration with RLS policy and grants, composite foreign keys.
2. Permissions registered with explicit allowed scopes; default role grants decided deliberately.
3. Routes depend on `require(...)`; ids resolved inside the caller's company; foreign ids give 404.
4. Entity, branch, department, team and reporting scopes applied where the data concerns employees.
5. Sensitive fields masked unless a confidential permission is held.
6. Brand, currency and locale taken from the shared services.
7. Audit entries for changes; no secrets or salary amounts in notifications.
8. Tests: positive per role, and negative cross-tenant tests with the `three_companies` fixture
   (copy the patterns in `tests/test_cross_tenant_attacks.py`).
9. Browser coverage in `hrms-frontend/e2e-saas/` (role menus, access-denied page, one cross-company probe).
10. Backwards compatible: additive migrations, existing API fields unchanged.

## What the contract tests enforce today

* Every `/api/*` route has an authenticated principal unless it is on the reviewed public list
  (sign-in, sign-up, reset, invitations, public brand).
* Platform and partner routes only answer on platform hosts.
* Every tenant table has RLS and a policy; the app role is neither superuser nor `BYPASSRLS` and
  cannot read system-only tables (operators, sessions, commercial brands, handoff codes, partners).
* A table with `company_id` that is not a tenant table must be listed as a reviewed exception.
* Foreign keys between tenant tables always include `company_id`.
