"""WI-AS-1.5 / EC AS-RBAC-05: the registry rows of the attendance-shift codes equal AS 06 §3 (spine §7.1).

The table grows with each milestone's codes: AS-M1 adds ``shift:*`` and ``roster:*``. Team scopes (TM, TT, AT) join the
``_T`` bundles with X-M1; until then ``_ALL_T`` = ``_ALL`` and ``_TEAM_T`` = ``_TEAM``.
"""

from app.core import permission_registry as pr

S, DR, RT, D, AG, LE, BR, C = ("SELF", "DIRECT_REPORTS", "REPORTING_TREE", "DEPARTMENT", "ASSIGNED_GROUP",
                               "LEGAL_ENTITY", "BRANCH", "COMPANY")
ALL_T = (S, DR, RT, D, AG, LE, BR, C)
TEAM_T = (DR, RT, D, AG, LE, BR, C)
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
