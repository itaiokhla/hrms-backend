"""WI-AS-1.5 / EC AS-RBAC-05: the registry rows of the attendance-shift codes equal AS 06 §3 (spine §7.1).

The table grows with each milestone's codes: AS-M1 adds ``shift:*`` and ``roster:*``; X-M1 (G-05) adds the team scopes
(TM, TT, AT) to the ``_T`` bundles, the ``team:*`` codes (X 01 §10.3), ``attendance:read`` at ``_ALL_WITH_PG_T`` and
the JC-AS-24 ``overtime:*`` scopes.
"""

from app.core import permission_registry as pr

S, DR, RT, D, AG, LE, BR, C = ("SELF", "DIRECT_REPORTS", "REPORTING_TREE", "DEPARTMENT", "ASSIGNED_GROUP",
                               "LEGAL_ENTITY", "BRANCH", "COMPANY")
TM, TT, AT, PG = "TEAM", "TEAM_TREE", "ASSIGNED_TEAMS", "PAY_GROUP"
ALL_T = (S, DR, RT, TM, TT, D, AG, AT, LE, BR, C)
TEAM_T = (DR, RT, TM, TT, D, AG, AT, LE, BR, C)
ALL_WITH_PG_T = (S, DR, RT, TM, TT, D, AG, AT, LE, BR, PG, C)
ORG_UNITS = (LE, BR, C)
PLANNING = (D, LE, BR, C)

# code -> (module, scopes, sensitivity, explicit_only)
SPINE = {
    "shift:read": ("Shifts", ALL_T, "standard", False),
    "shift:manage": ("Shifts", ORG_UNITS, "standard", False),
    "shift:assign": ("Shifts", TEAM_T, "standard", False),
    "roster:read": ("Shifts", ALL_T, "standard", False),
    "roster:plan": ("Shifts", PLANNING, "standard", False),
    "roster:publish": ("Shifts", PLANNING, "standard", False),
    "roster:request": ("Shifts", (S,), "standard", False),
    "roster:approve": ("Shifts", TEAM_T, "standard", False),
    "roster:exempt": ("Shifts", ORG_UNITS, "restricted", True),
    "location:review": ("Attendance", (DR, RT, D, AG, LE, BR, C), "confidential", False),
    "attendance:read": ("Attendance", ALL_WITH_PG_T, "standard", False),
    "overtime:request": ("Overtime", (S, DR, RT, D, AG, C, TM, TT, AT), "standard", False),
    "overtime:approve": ("Overtime", TEAM_T, "standard", False),
    # LH-M2 (LH SPINE §9): leave:read gains PAY_GROUP (payroll managers read leave of their pay groups).
    "leave:read": ("Leave", ALL_WITH_PG_T, "standard", False),
    "leave:approve": ("Leave", TEAM_T, "standard", False),
    "team:read": ("People", ALL_T, "standard", False),
    "team:manage": ("People", ORG_UNITS, "standard", False),
    "team:membership:manage": ("People", (TT, AT, LE, BR, C), "standard", False),
    # AS-M2 capture-side codes (spine §7.1 rows; 11 §11.2 note C-3; SP-AS-169, -220).
    "attendance:review": ("Attendance", TEAM_T, "standard", False),
    "attendance:evidence:read": ("Attendance", TEAM_T, "confidential", True),
    "attendance:manage_policy": ("Attendance", (LE, C), "standard", False),
    "attendance:manage_devices": ("Attendance", ORG_UNITS, "restricted", False),
    "attendance:manage_sites": ("Attendance", ORG_UNITS, "standard", False),
}


def test_as_rbac_05_registry_rows_equal_the_spine():
    for code, (module, scopes, sensitivity, explicit_only) in SPINE.items():
        perm = pr.PERMISSIONS_BY_CODE[code]
        assert perm.module == module, code
        assert tuple(s.value for s in perm.scopes) == scopes, code
        assert perm.sensitivity == sensitivity, code
        assert perm.explicit_only is explicit_only, code


def test_roster_exempt_is_never_implied():
    # Never partner-delegable too: tests/shifts/test_shift_routes.py (needs the app config).
    assert pr.PERMISSIONS_BY_CODE["roster:exempt"].implied_scopes == ()
    assert not any(code == "roster:exempt" for role in pr.DEFAULT_ROLES for code, _ in role.grants)


def test_default_grants_of_as_06_section_4_1():
    grants = {r.system_key: {(c, s.value) for c, s in r.grants} for r in pr.DEFAULT_ROLES}
    assert {("shift:read", S), ("roster:read", S), ("roster:request", S)} <= grants["employee"]
    assert {("shift:read", RT), ("roster:read", RT), ("roster:approve", RT)} <= grants["manager"]
    assert {("shift:read", C), ("shift:manage", C), ("shift:assign", C), ("roster:plan", C), ("roster:publish", C),
            ("roster:approve", C)} <= grants["hr_admin"]
    assert {("shift:read", C), ("roster:read", C)} <= grants["auditor"]
    assert not any(c.startswith(("shift:manage", "shift:assign")) for c, _ in grants["manager"])


def test_x_m1_default_grants_and_jc_as_24():
    grants = {r.system_key: {(c, s.value) for c, s in r.grants} for r in pr.DEFAULT_ROLES}
    assert ("team:read", S) in grants["employee"] and ("team:read", RT) in grants["manager"]
    assert {("team:read", C), ("team:manage", C), ("team:membership:manage", C)} <= grants["hr_admin"]
    assert ("attendance:read", PG) in grants["payroll_manager"] and ("attendance:read", PG) in grants["payroll_processor"]
    assert pr.scope_covers([TT], TM) and not pr.scope_covers([TM], TT) and not pr.scope_covers([TT], AT)
    assert pr.Scope.ASSIGNED_TEAMS in pr.TARGETED_SCOPES and pr.Scope.ASSIGNED_TEAMS not in pr.TARGET_ASSIGNMENT_COLUMNS


def test_as_m2_default_grants_and_evidence_is_explicit_only():
    """SP-AS-169 / -220: hr_admin holds the four capture-side codes and manage_policy at COMPANY; the evidence code
    has no default grant and is never implied by full access (07 §8 E3; EC AS-SEC-13 (b))."""
    grants = {r.system_key: {(c, s.value) for c, s in r.grants} for r in pr.DEFAULT_ROLES}
    assert {("attendance:review", C), ("attendance:manage_devices", C), ("attendance:manage_sites", C),
            ("attendance:manage_policy", C)} <= grants["hr_admin"]
    assert not any(code == "attendance:evidence:read" for role in pr.DEFAULT_ROLES for code, _ in role.grants)
    assert pr.PERMISSIONS_BY_CODE["attendance:evidence:read"].implied_scopes == ()
    assert not pr.implied_by_full_access("attendance:evidence:read", C)
    assert pr.implied_by_full_access("attendance:manage_sites", C)
    assert not any(c.startswith("attendance:manage") for c, _ in grants["manager"] | grants["employee"])
