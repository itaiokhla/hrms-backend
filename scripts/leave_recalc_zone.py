"""Recount leave after a tzdata change of one zone (LH 02 §13 FM-16, LH-REL-12).

    venv/Scripts/python.exe scripts/leave_recalc_zone.py --zone Asia/Dubai --from 2027-03-01 --tzdata 2027a

Run by the deploy after a tzdata upgrade changed the zone's rules from the given date: for every active tenant with
employees whose work zone is that zone, one ``leave.recalculate`` job keyed ``recalc:tz:{zone}:{tzdata}`` recounts
approved leave from that date (a rerun enqueues nothing new).
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zone", required=True)
    parser.add_argument("--from", dest="date_from", required=True, type=date.fromisoformat)
    parser.add_argument("--tzdata", required=True, help="the tzdata release that changed the zone, e.g. 2027a")
    args = parser.parse_args()

    from app.database.db import SessionLocal, bind_tenant
    from app.models.company import Company
    from app.services import leave_jobs

    system = SessionLocal()
    try:
        tenants = [c for (c,) in system.query(Company.id).filter(Company.status == "active").order_by(Company.id)]
    finally:
        system.close()
    for company_id in tenants:
        db = SessionLocal()
        try:
            bind_tenant(db, company_id)
            result = leave_jobs.recount_zone(db, company_id, args.zone, args.date_from, tzdata_version=args.tzdata)
            db.commit()
            print(company_id, result)
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
