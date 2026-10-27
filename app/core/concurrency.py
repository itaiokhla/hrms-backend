"""Optimistic concurrency and employee locks shared by every module (LH-M0 WI-0.4; LH 14 §2.4, §2.9; LCK-02).

* ``if_match`` / ``check_row_version``: the expected ``row_version`` from ``If-Match`` (``W/"7"`` or ``7``) or the
  body, compared with the row -> 409 ``ROW_VERSION_CONFLICT``; the caller then bumps ``row.row_version``.
* ``lock_employees``: the per-employee attendance advisory lock for several employees in ascending id order, after
  ``SET LOCAL lock_timeout = '5s'``; a lock timeout or deadlock becomes 409 ``LOCK_TIMEOUT`` (retryable).
"""

from typing import Any, Iterable, Optional

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.errors import conflict

LOCK_TIMEOUT = "5s"
_RETRYABLE_SQLSTATES = ("55P03", "40P01")  # lock_not_available, deadlock_detected


def if_match(request: Request, body_version: Optional[int]) -> Optional[int]:
    """Expected row_version from If-Match (W/"7" or 7), else the body field."""
    header = request.headers.get("if-match")
    if header:
        value = header.strip().removeprefix("W/").strip('"')
        if value.isdigit():
            return int(value)
    return body_version


def check_row_version(row: Any, expected: Optional[int]) -> None:
    """ROW_VERSION_CONFLICT (409) when an expected version is given and differs from the row's."""
    if expected is not None and expected != row.row_version:
        raise conflict("This record was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")


def lock_employees(db: Session, employee_ids: Iterable[int]) -> None:
    """LCK-02: the attendance lock of each employee in ascending id order, waiting at most LOCK_TIMEOUT."""
    from app.services.attendance_service import lock_employee

    db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        for employee_id in sorted(set(employee_ids)):
            lock_employee(db, employee_id)
    except DBAPIError as exc:
        if getattr(getattr(exc, "orig", None), "pgcode", None) in _RETRYABLE_SQLSTATES:
            raise conflict("Someone else is changing these records. Try again in a moment.", "LOCK_TIMEOUT") from exc
        raise
