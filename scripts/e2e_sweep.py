"""Run the scheduled attendance sweep against a local demo cluster (browser tests).

    python scripts/e2e_sweep.py [--data-dir .local-postgres-e2e]

Uses the same RLS-restricted application role as the running API, so it is
exactly what a cron job running `python -m app.cli attendance-sweep` does.
"""

import argparse
import os
import sys
from pathlib import Path

from pgserver.utils import PostmasterInfo
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=".local-postgres")
    args = parser.parse_args()
    data_dir = (ROOT / args.data_dir).resolve()
    info = PostmasterInfo.read_from_pgdata(data_dir)
    if info is None or not info.is_running():
        raise SystemExit(f"No running database in {data_dir}")
    owner = info.get_uri(database="hrms")
    app_url = make_url(owner).set(username="hrms_app", password=(data_dir / ".app_password").read_text().strip())
    os.environ.update({
        "DATABASE_URL": app_url.render_as_string(hide_password=False),
        "SYSTEM_DATABASE_URL": owner,
        "SECRET_KEY": (data_dir / ".secret_key").read_text().strip(),
        "ENVIRONMENT": "development",
        # Same accelerated test policy floor as the API started by demo_stack.py.
        "LOCATION_TRACKING_MIN_INTERVAL_MINUTES": os.environ.get("LOCATION_TRACKING_MIN_INTERVAL_MINUTES", "1"),
    })
    sys.path.insert(0, str(ROOT))
    from app.cli import attendance_sweep

    attendance_sweep()


if __name__ == "__main__":
    main()
