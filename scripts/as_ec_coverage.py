"""Attendance-shift / cross-module edge-case coverage (AS 14 IG-AS-07): see scripts/time_ec_coverage.py.

    python scripts/as_ec_coverage.py --check --milestone AS-M0
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import time_ec_coverage as cov  # noqa: E402

if __name__ == "__main__":
    sys.exit(cov.run(cov.AS_SET, cov.as_milestone_ids))
