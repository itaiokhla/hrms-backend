"""The API process installs every "until pay runs exist" hook at startup (EC-CFG-16, FOLLOW-UPS F-14, F-35,
F-36, F-37). Checked in a fresh interpreter that only imports ``app.main``: inside the test session other
modules (the snapshot builder among them) are already imported, which hid a missing wiring where
``policies.policy_in_use`` stayed "False until runs exist" in the API process, so a policy used by a frozen
run snapshot could still be edited or deleted there.

No database is touched: importing the application never connects.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROBE = """
import json, sys
import app.main  # noqa: F401  (what the API process imports)
from app.payroll.services import compensation, components, loans, policies, settings, wage_inputs
print(json.dumps({
    "policy_in_use": policies.policy_in_use.__module__,
    "pack_constraints": settings.pack_constraints.__module__,
    "mark_runs_stale": settings.mark_runs_stale.__module__,
    "finalized_period_overlaps": settings.finalized_period_overlaps.__module__,
    "period_is_open": wage_inputs.period_is_open.__module__,
    "finalized_periods": compensation.finalized_periods.__module__,
    "component_in_runs": components.component_in_runs.__module__,
    "pay_figures": loans._figures_provider.__module__,
}))
"""


def _probe() -> dict:
    env = {**os.environ, "ENVIRONMENT": "test", "HRMS_TEST_NO_DB": "1",
           # A PostgreSQL URL is required by the settings; nothing connects to it during the import.
           "DATABASE_URL": "postgresql://probe:probe@127.0.0.1:1/probe"}
    done = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_ec_cfg_16_api_process_installs_the_snapshot_and_run_hooks_at_startup():
    wired = _probe()
    assert wired["policy_in_use"] == "app.payroll.services.snapshot"       # EC-CFG-16
    assert wired["pack_constraints"] == "app.payroll.services.snapshot"    # F-14
    assert wired["pay_figures"] == "app.payroll.services.snapshot"         # F-36
    for hook in ("mark_runs_stale", "finalized_period_overlaps", "period_is_open", "finalized_periods",
                 "component_in_runs"):
        assert wired[hook] == "app.payroll.services.runs", (hook, wired[hook])
