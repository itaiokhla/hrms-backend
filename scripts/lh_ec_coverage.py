"""Leave-holiday edge-case coverage (LH 14 IG-08): see scripts/time_ec_coverage.py.

    python scripts/lh_ec_coverage.py --check --milestone LH-M0
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import time_ec_coverage as cov  # noqa: E402

if __name__ == "__main__":
    sys.exit(cov.run(cov.LH_SET, cov.lh_milestone_ids))
