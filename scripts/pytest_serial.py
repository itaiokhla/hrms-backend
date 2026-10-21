"""Run pytest against the shared test database, one run at a time.

    python scripts/pytest_serial.py [pytest arguments ...]

Every database-backed test run uses the same embedded PostgreSQL server and the
same database (tests/conftest.py drops and recreates it), so two runs at once
corrupt each other. This waits for the machine-wide heavy-command lock
(scripts/run_exclusive.py), runs `python -m pytest` with the given arguments in
the backend directory, and releases the lock. Pure-logic runs
(HRMS_TEST_NO_DB=1) do not need it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_exclusive import run_exclusive  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.exit(run_exclusive([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=ROOT))
