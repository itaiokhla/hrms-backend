"""Generic explicit-only permission mechanism (WI-AS-0.4; spine A-09, LH V3-073; no new codes in G-01)."""

from dataclasses import replace

from app.core import permission_registry as pr

# The payroll-confidential set before the mechanism was generalised (registry 2026.10.4).
# Explicit-only codes of other modules (roster:exempt, registry 2026.10.6) are not part of it.
PAYROLL_CONFIDENTIAL_2026_10_4 = sorted(p.code for p in pr.PERMISSIONS
                                        if p.full_access_scopes is not None and p.module in pr.PAYROLL_MODULES)


def test_roster_exempt_is_explicit_only_but_not_payroll_confidential():
    exempt = pr.PERMISSIONS_BY_CODE["roster:exempt"]
    assert exempt.explicit_only and not exempt.payroll_confidential
    assert not pr.is_payroll_grant("roster:exempt", "COMPANY")


def test_payroll_confidential_set_is_unchanged():
    assert sorted(p.code for p in pr.PERMISSIONS if p.payroll_confidential) == PAYROLL_CONFIDENTIAL_2026_10_4
    assert all(p.module in pr.PAYROLL_MODULES for p in pr.PERMISSIONS if p.payroll_confidential)


def test_explicit_only_is_independent_of_the_payroll_module():
    attendance = pr.PermissionDef("roster:exempt", "Attendance", "Exempt", "", (pr.Scope.COMPANY,),
                                  "restricted", full_access_scopes=())
    assert attendance.explicit_only and not attendance.payroll_confidential
    assert attendance.implied_scopes == ()
    plain = replace(attendance, full_access_scopes=None)
    assert not plain.explicit_only and plain.implied_scopes == (pr.Scope.COMPANY,)


def test_explicit_access_grant_covers_payroll_grants():
    for code in PAYROLL_CONFIDENTIAL_2026_10_4:
        for scope in pr.PERMISSIONS_BY_CODE[code].scopes:
            assert pr.is_explicit_access_grant(code, scope.value) == pr.is_payroll_grant(code, scope.value)
    assert not pr.is_explicit_access_grant("attendance:read", "COMPANY")
    assert not pr.is_explicit_access_grant("no:such_code", "COMPANY")
