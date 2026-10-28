"""``DAY_RESULT_TO_LEDGER`` (JC-AS-01; mapping owner AS 05 §5, ledger rules payroll 3.4 §4.2 rules 7–8, §6).

Pure (L0): one stored ``attendance_days`` evaluation of an attendance v2 tenant -> at most one
``ledger.AttendanceInput``. Attendance publishes facts (day result, halves, marks, net and night seconds); this
module only translates them into the ledger's input and the ledger turns them into money. LEGACY_V1 rows are not
mapped here: their input is today's session input exactly (AS 05 §5.1, zero diff), built by
``time_inputs._attendance_inputs``.

Rows give no input (the ledger's own rules decide the date) when: the row is OPEN (provisional figures, payroll never
reads them — they are the AS-M4 pending item ``attendance_day_pending``), the day is UNMARKED / PENDING / OFF, or the
only presence is an approved on-duty without a session (the ledger's on-duty path).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from app.payroll import ledger

# day_result -> AttendanceInput.status (§5.2); None = no input.
DAY_RESULT_TO_LEDGER: Mapping[str, Optional[str]] = {
    "PRESENT": "PRESENT",  # LATE when the mark stands (late_mark, excused or not; B-05)
    "HALF_DAY": "HALF_DAY",
    "ABSENT": "ABSENT",
    "MISSING_PUNCH": "PRESENT",  # with closed_without_punch_out -> ATTENDANCE_PAY_POLICY.missing_punch_out
    "UNMARKED": None,  # ledger unmarked_day
    "PENDING": None,  # leave pending -> unapproved_at_cutoff; other reasons -> attendance_day_pending (AS-M4)
    "OFF": None,  # day facts decide (holiday, weekly off, leave)
}
COVERAGE_ONLY = ("ON_DUTY", "REMOTE_WORK")


def _get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def lineage(day_id: int, version: int) -> str:
    """The snapshot source id of an evaluation (JC-AS-13, INV-21)."""
    return f"attendance_day:{day_id}:v{version}"


def parse_lineage(session_id: Any) -> Optional[tuple[int, int]]:
    if not isinstance(session_id, str) or not session_id.startswith("attendance_day:"):
        return None
    _, day_id, version = session_id.split(":")
    return int(day_id), int(version.lstrip("v"))


def _other_half(row: Any, present: str) -> str:
    other = "second_half_result" if present == "FIRST_HALF" else "first_half_result"
    result = _get(row, other)
    if result == "UNMARKED":
        return "UNMARKED"
    if result == "PENDING" and "LEAVE_PENDING" in (_get(row, "pending_reasons") or ()):
        return "PENDING_LEAVE"
    return "ABSENT"


def to_attendance_input(row: Any) -> Optional[ledger.AttendanceInput]:
    """The ledger input of one STANDARD evaluation (AS 05 §5.2), or None when the day gives no input."""
    if _get(row, "rules_mode") != "STANDARD":
        raise ValueError("DAY_RESULT_TO_LEDGER maps STANDARD evaluations only; LEGACY_V1 uses the session input")
    if _get(row, "evaluation_state") == "OPEN":
        return None
    result = _get(row, "day_result")
    status = DAY_RESULT_TO_LEDGER.get(result)
    if status is None:
        return None
    sessions = int(_get(row, "session_count") or 0)
    basis = _get(row, "presence_basis")
    if result in ("PRESENT", "HALF_DAY") and basis in COVERAGE_ONLY and sessions == 0:
        return None  # GV-AS-PAY-06: the ledger's on-duty path pays the covered halves
    net = _get(row, "net_seconds")
    worked = int(net) if net is not None else None
    closed = result == "MISSING_PUNCH"
    if closed or status == "ABSENT":
        worked = None
    late = bool(_get(row, "late_mark"))
    early = bool(_get(row, "early_mark"))
    late_excused = bool(_get(row, "late_excused")) and late
    early_excused = bool(_get(row, "early_excused")) and early
    present_portion = other_half = None
    if result == "HALF_DAY":
        present_portion = "FIRST_HALF" if _get(row, "first_half_result") == "PRESENT" else "SECOND_HALF"
        other_half = _other_half(row, present_portion)
    elif result == "PRESENT" and late:
        status = "LATE"
    night = int(_get(row, "night_minutes") or 0) * 60 if worked is not None else 0
    return ledger.AttendanceInput(
        status=status, session_id=lineage(int(_get(row, "id")), int(_get(row, "version") or 1)),
        closed_without_punch_out=closed, worked_seconds=worked, late=late, early_exit=early,
        regularised=late_excused or early_excused, night_seconds=night, content_hash=_get(row, "content_hash"),
        rules_mode="STANDARD", present_portion=present_portion, other_half=other_half,
        late_regularised=late_excused, early_exit_regularised=early_excused)
