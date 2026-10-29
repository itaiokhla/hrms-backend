"""Computed day label (AS 04-attendance/03 §11): first match of the table, badges, the §11 examples. Pure."""

import pytest

from app.services.attendance_day_label import day_label

BASE = {"day_result": "PRESENT", "first_half_result": "PRESENT", "second_half_result": "PRESENT",
        "expected_portion": "FULL", "session_count": 1, "net_minutes": 480, "late_mark": False, "late_excused": False,
        "early_mark": False, "early_excused": False, "short_mark": False, "ot_candidate_minutes": 0,
        "presence_basis": "SESSION", "pending_reasons": [], "exception_codes": [], "work_source": "WORK_WEEK"}


def label(reasons=(), **changes):
    return day_label({**BASE, **changes}, reasons)


def test_gv_examples_of_section_11():
    # GV-AS-EVL-01: late present -> "Late", badge Late.
    assert label(late_mark=True, late_minutes=20) == {"label": "Late", "detail": "late by 20 min", "code": "LT",
                                                      "badges": ["Late"]}
    # GV-AS-EVL-02: late beyond -> the first half is absent.
    half = label(day_result="HALF_DAY", first_half_result="ABSENT", late_converted=True, late_minutes=150)
    assert (half["label"], half["detail"], half["code"]) == ("Half day", "First half: absent, late by 150 min", "½P")
    # GV-AS-EVL-07: worked on leave.
    assert label(("LEAVE",), expected_portion="NONE")["label"] == "Worked on leave"
    # GV-AS-EVL-09: pending leave.
    pending = label(("LEAVE_PENDING",), day_result="PENDING", pending_reasons=["LEAVE_PENDING"], session_count=0,
                    net_minutes=0)
    assert (pending["label"], pending["detail"]) == ("Pending", "leave pending")
    # GV-AS-LAT-02: an excused mark is "Present" with badge Excused, never "Late".
    excused = label(late_mark=True, late_excused=True, late_minutes=12)
    assert (excused["label"], excused["badges"]) == ("Present", ["Excused"])


@pytest.mark.parametrize("reasons, changes, expected", [
    (("NOT_EMPLOYED",), {"day_result": "OFF", "expected_portion": "NONE", "session_count": 0}, ("Not employed", "–")),
    (("HOLIDAY",), {"expected_portion": "NONE"}, ("Worked on holiday", "P")),
    (("WEEKLY_OFF",), {"expected_portion": "NONE"}, ("Worked on weekly off", "P")),
    (("WEEKLY_OFF",), {"expected_portion": "NONE", "work_source": "ROSTER"}, ("Worked on off day", "P")),
    (("LEAVE",), {"day_result": "OFF", "expected_portion": "NONE", "session_count": 0, "net_minutes": 0},
     ("On leave", None)),
    (("HOLIDAY",), {"day_result": "OFF", "expected_portion": "NONE", "session_count": 0, "net_minutes": 0},
     ("Holiday", "H")),
    (("WEEKLY_OFF",), {"day_result": "OFF", "expected_portion": "NONE", "session_count": 0, "net_minutes": 0,
                       "work_source": "ROSTER"}, ("Off (rostered)", "W")),
    ((), {"presence_basis": "ON_DUTY", "session_count": 0}, ("On duty", "OD")),
    ((), {"presence_basis": "REMOTE_WORK", "session_count": 0}, ("Remote", "RW")),
    ((), {}, ("Present", "P")),
    ((), {"day_result": "ABSENT", "session_count": 0, "net_minutes": 0}, ("Absent", "A")),
    ((), {"day_result": "UNMARKED", "session_count": 0, "net_minutes": 0}, ("Not marked", "NM")),
    ((), {"day_result": "MISSING_PUNCH"}, ("Missing punch", "MP")),
])
def test_first_match_of_the_table(reasons, changes, expected):
    got = label(reasons, **changes)
    assert (got["label"], got["code"]) == expected


def test_half_day_names_the_leave_half_and_badges_collect():
    half = label(("LEAVE",), day_result="HALF_DAY", expected_portion="FIRST_HALF", second_half_result="OFF")
    assert half["detail"] == "Second half: on leave"
    busy = label(early_mark=True, short_mark=True, ot_candidate_minutes=30, exception_codes=["EVIDENCE_FLAGGED"])
    assert busy["badges"] == ["Early", "Short", "OT", "Evidence"]
