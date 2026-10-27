"""LH-M0 golden baselines (WI-0.3; LH 13 §4.1 exit 1): generate or verify, through the serialized test runner.

    python scripts/lh_baseline.py            # run the goldens twice; both runs must pass (determinism)
    python scripts/lh_baseline.py --write    # regenerate tests/leave/goldens/*.json, then verify twice

Same mechanics as scripts/as_baseline.py, for the leave-holiday goldens of tests/leave/test_m0_goldens.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import as_baseline  # noqa: E402

if __name__ == "__main__":
    sys.exit(as_baseline.main("tests/leave/test_m0_goldens.py", as_baseline.ROOT / "tests" / "leave" / "goldens"))
