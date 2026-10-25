"""The legacy Chrome suite's seed (scripts/seed_demo.py) must follow the role registry (pure).

gate-legacy-e2e (25 Oct): after payroll v2 the Employee role gained SELF-scoped payroll self-service grants
(3.9 §5.3), but seed_demo's custom "Recruiter" role still copied the 2026.10.3 self-service list. The
anti-escalation rule (rbac_service.can_delegate: a non-administrator may only give grants they hold) then
disabled the Employee checkbox for Pooja and e2e/04 "A recruiter can onboard people…" failed. The rule is
right; the fixture had drifted. This test keeps the copy equal to the registry.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.core.permission_registry import DEFAULT_ROLES_BY_KEY, SELF_SERVICE, grants_cover

SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_demo_under_test", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # module level only defines constants and classes
    return module


def _as_strings(grants):
    return {(code, getattr(scope, "value", scope)) for code, scope in grants}


def test_seed_self_service_equals_registry_self_service():
    seed = _seed_module()
    assert _as_strings(seed.SELF_SERVICE) == _as_strings(SELF_SERVICE)


def test_seed_recruiter_covers_every_employee_grant():
    """Pooja (custom Recruiter) may grant the Employee role: she holds each of its grants at a covering scope."""
    seed = _seed_module()
    recruiter = _as_strings(seed.NIMBUS["custom_roles"]["Recruiter"])
    employee = _as_strings(DEFAULT_ROLES_BY_KEY["employee"].grants)
    missing = sorted((code, scope) for code, scope in employee if not grants_cover(recruiter, code, scope))
    assert missing == []
