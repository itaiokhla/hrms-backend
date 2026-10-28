"""Give the embedded PostgreSQL (pgserver) the IANA time-zone database it ships without.

pgserver's build has ``share/postgresql/timezonesets`` but no ``share/postgresql/timezone``, so SQL such as
``now() AT TIME ZONE 'Asia/Kolkata'`` fails with "time zone not recognized" on the test and demo databases while it
works on Neon. PostgreSQL reads compiled TZif files from that directory on demand; pytz bundles the same files
(``pytz/zoneinfo``), so copying them once makes local databases behave like production. Idempotent and cheap.
"""

from __future__ import annotations

import os
import shutil


def ensure_timezone_data() -> str | None:
    """Copy pytz's zoneinfo into pgserver's share directory when missing; returns the directory used (or None)."""
    try:
        import pgserver
        import pytz
    except ImportError:
        return None
    share = os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "share", "postgresql")
    target = os.path.join(share, "timezone")
    if not os.path.isdir(share):
        return None
    if os.path.exists(os.path.join(target, "Asia", "Kolkata")):
        return target
    source = os.path.join(os.path.dirname(pytz.__file__), "zoneinfo")
    shutil.copytree(source, target, dirs_exist_ok=True)
    return target


if __name__ == "__main__":
    print(ensure_timezone_data() or "pgserver or pytz not installed")
