"""Audit-log module names of payroll events.

Rows whose module is listed here are payroll-confidential: GET /api/v1/audit-logs returns them only
to holders of payroll:audit:read (3.9 §5.1, §8). Payroll services must write their events under one
of these modules, never under a generic one.
"""

LEGACY_PAYROLL = "Payroll"
PAYROLL_SETTINGS = "Payroll Settings"
PAYROLL_POLICIES = "Payroll Policies"
COMPENSATION = "Compensation"
PAYROLL_INPUTS = "Payroll Inputs"
PAYROLL_RUNS = "Payroll Runs"
PAYSLIPS = "Payslips"
BANK_ACCOUNTS = "Bank Accounts"
PAYMENTS = "Payments"
COUNTRY_PACKS = "Country Packs"
PAYROLL_ROLES = "Payroll Roles"
LOANS = "Loans"
REIMBURSEMENTS = "Reimbursements"
LEGAL_HOLDS = "Legal Holds"
TAX_PROFILES = "Tax Profiles"

PAYROLL_AUDIT_MODULES = (
    LEGACY_PAYROLL,
    PAYROLL_SETTINGS,
    PAYROLL_POLICIES,
    COMPENSATION,
    PAYROLL_INPUTS,
    PAYROLL_RUNS,
    PAYSLIPS,
    BANK_ACCOUNTS,
    PAYMENTS,
    COUNTRY_PACKS,
    PAYROLL_ROLES,
    LOANS,
    REIMBURSEMENTS,
    LEGAL_HOLDS,
    TAX_PROFILES,
)

PAYROLL_AUDIT_PERMISSION = "payroll:audit:read"

# Payroll Roles actions.
ROLE_GRANTED = "ROLE_GRANTED"
ROLE_REVOKED = "ROLE_REVOKED"
ROLE_SELF_GRANTED = "ROLE_SELF_GRANTED"
ROLE_SELF_REVOKED = "ROLE_SELF_REVOKED"
