"""Computed day label (AS 04-attendance/03 §11; spine §5.8; D-LH-15): never stored, one function for the API, exports
and the calendar. Pure: the caller passes the stored day outputs and the expectation reasons of
``time_inputs.expected_presence`` for the same date.

Until G-10 (LH-M4) leave is shown with the generic masked label only ("On leave"); no leave type name is read, so
SENSITIVE types cannot leak (LH D-LH-17). ``attendance_days.day_label`` is the public entry point.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

PENDING_TEXT = {"SHIFT_IN_PROGRESS": "shift in progress", "DEVICE_SYNC": "awaiting device sync",
                "REQUEST_PENDING": "request pending", "LEAVE_PENDING": "leave pending",
                "NOT_EVALUATED": "not evaluated yet"}
RESULT_LABEL = {"PRESENT": ("Present", "P"), "HALF_DAY": ("Half day", "½P"), "ABSENT": ("Absent", "A"),
                "UNMARKED": ("Not marked", "NM"), "MISSING_PUNCH": ("Missing punch", "MP"), "OFF": ("Off", None),
                "PENDING": ("Pending", "…")}
HALF_TEXT = {"ABSENT": "absent", "UNMARKED": "not marked", "MISSING_PUNCH": "missing punch", "PENDING": "pending",
             "OFF": "off", "PRESENT": "present"}


def _get(day: Any, name: str, default: Any = None) -> Any:
    return day.get(name, default) if isinstance(day, Mapping) else getattr(day, name, default)


def _late_text(day: Any) -> Optional[str]:
    minutes = _get(day, "late_minutes")
    return f"late by {minutes} min" if minutes else None


def badges(day: Any) -> list[str]:
    out: list[str] = []
    if _get(day, "late_mark") and not _get(day, "late_excused"):
        out.append("Late")
    if _get(day, "early_mark") and not _get(day, "early_excused"):
        out.append("Early")
    if _get(day, "short_mark"):
        out.append("Short")
    if (_get(day, "ot_candidate_minutes") or 0) > 0:
        out.append("OT")
    if _get(day, "late_excused") or _get(day, "early_excused"):
        out.append("Excused")
    if "EVIDENCE_FLAGGED" in (_get(day, "exception_codes") or ()):
        out.append("Evidence")
    return out


def _half_day_detail(day: Any, reasons: Sequence[str]) -> Optional[str]:
    """The other half's reason: "First half: absent, late by 150 min" (GV-AS-EVL-02)."""
    first, second = _get(day, "first_half_result"), _get(day, "second_half_result")
    if first == "PRESENT" and second != "PRESENT":
        half, result, extra = "Second half", second, None
        if _get(day, "early_converted") and _get(day, "early_minutes"):
            extra = f"early by {_get(day, 'early_minutes')} min"
    elif second == "PRESENT" and first != "PRESENT":
        half, result, extra = "First half", first, _late_text(day) if _get(day, "late_converted") else None
    else:
        return None
    text = "on leave" if result == "OFF" and "LEAVE" in reasons else HALF_TEXT.get(result, str(result).lower())
    return f"{half}: {text}" + (f", {extra}" if extra else "")


def day_label(day: Any, reasons: Sequence[str] = ()) -> dict:
    """``{label, detail, code, badges}`` for one ``attendance_days`` row (or its outputs dict); first match of the
    §11 table wins."""
    reasons = tuple(reasons or ())
    result = _get(day, "day_result")
    worked = (_get(day, "session_count") or 0) > 0 or (_get(day, "net_minutes") or 0) > 0
    expected_none = _get(day, "expected_portion") == "NONE"
    rostered_off = _get(day, "work_source") == "ROSTER"
    label, detail, code = None, None, None
    if "NOT_EMPLOYED" in reasons:
        label, code = "Not employed", "–"
    elif result == "PENDING":
        texts = [PENDING_TEXT.get(r, r.lower()) for r in (_get(day, "pending_reasons") or ())]
        label, detail, code = "Pending", ", ".join(texts) or None, "…"
    elif worked and expected_none and "LEAVE" in reasons:
        label, code = "Worked on leave", "P"
    elif worked and expected_none and ("HOLIDAY" in reasons or "WEEKLY_OFF" in reasons):
        label = ("Worked on holiday" if "HOLIDAY" in reasons
                 else "Worked on off day" if rostered_off else "Worked on weekly off")
        code = "P"
    elif expected_none and "LEAVE" in reasons:
        label = "On leave"
    elif expected_none and "HOLIDAY" in reasons:
        label, code = "Holiday", "H"
    elif expected_none and "WEEKLY_OFF" in reasons:
        label, code = ("Off (rostered)", "W") if rostered_off else ("Weekly off", "W")
    elif _get(day, "presence_basis") in ("ON_DUTY", "REMOTE_WORK") and result in ("PRESENT", "HALF_DAY"):
        label, code = ("On duty", "OD") if _get(day, "presence_basis") == "ON_DUTY" else ("Remote", "RW")
    elif result == "PRESENT" and _get(day, "late_mark") and not _get(day, "late_excused"):
        label, detail, code = "Late", _late_text(day), "LT"
    else:
        label, code = RESULT_LABEL.get(result, (str(result).replace("_", " ").capitalize(), None))
        if result == "HALF_DAY":
            detail = _half_day_detail(day, reasons)
    return {"label": label, "detail": detail, "code": code, "badges": badges(day)}
