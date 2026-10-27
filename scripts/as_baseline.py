"""AS-M0 golden baselines (WI-AS-0.3; AS 13 §4.1 exit 1): generate or verify, through the serialized test runner.

    python scripts/as_baseline.py            # run the goldens twice; both runs must pass (determinism)
    python scripts/as_baseline.py --write    # regenerate tests/attendance/goldens/*.json, then verify twice

The goldens are read-only captures of today's behaviour on the time-stack dataset (tests/time_goldens.py); the
script never changes business data. Commit the JSON files after a deliberate ``--write`` only.
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_exclusive import run_exclusive  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODULE = "tests/attendance/test_m0_goldens.py"
GOLDENS = ROOT / "tests" / "attendance" / "goldens"


def fingerprint(folder: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(folder.glob("*.json")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def pytest_run(module: str, write: bool) -> int:
    env = dict(os.environ, HRMS_GOLDEN_WRITE="1" if write else "0")
    os.environ.update(env)
    return run_exclusive([sys.executable, "-m", "pytest", module, "-q", "-p", "no:cacheprovider"], cwd=ROOT)


def main(module: str = MODULE, goldens: Path = GOLDENS, runs: int = 2) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="regenerate the golden files first")
    parser.add_argument("--runs", type=int, default=runs, help="verification runs (default 2)")
    args = parser.parse_args()
    if args.write:
        code = pytest_run(module, write=True)
        if code:
            print(f"[baseline] writing the goldens failed (exit {code})")
            return code
    before = fingerprint(goldens)
    for n in range(1, args.runs + 1):
        code = pytest_run(module, write=False)
        print(f"[baseline] verification run {n}/{args.runs}: {'PASS' if code == 0 else f'FAIL (exit {code})'}")
        if code:
            return code
    if fingerprint(goldens) != before:
        print("[baseline] the golden files changed during verification")
        return 1
    print(f"[baseline] {module}: green {args.runs}x on unchanged goldens ({before[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
