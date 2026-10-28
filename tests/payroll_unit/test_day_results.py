"""``DAY_RESULT_TO_LEDGER`` (JC-AS-01, -03 fields, -04, -05, -06, -13): AS 05 §5 and its vectors GV-AS-PAY-01, -04..-07.

Pure: stored-evaluation rows (as mappings) -> ``AttendanceInput`` -> ``build_ledger``. The snapshot round trip keeps
v1 inputs byte-identical (golden T-3) and carries the STANDARD fields.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from app.payroll import day_results as dr
from app.payroll import ledger as L
from app.payroll.engine import snapshot as snap
from app.payroll.ledger import AttendanceInput, AttendanceStatus, Classification as C, DayInput, LedgerConfig, Slot

MON = dt.date(2026, 11, 2)
ON = LedgerConfig(attendance_enabled=True)
DERIVE = dataclasses.replace(ON, derive_status_from_hours=True, full_day_min_hours=Decimal(8),
                             half_day_min_hours=Decimal(4))
HASH = "a" * 64


def row(**fields) -> dict:
    base = dict(id=700104, version=1, content_hash=HASH, rules_mode="STANDARD", evaluation_state="FINAL",
                day_result="PRESENT", first_half_result="PRESENT", second_half_result="PRESENT",
                presence_basis="SESSION", session_count=1, net_seconds=28800, night_minutes=0, late_mark=False,
                late_excused=False, early_mark=False, early_excused=False, pending_reasons=[])
    return base | fields


def ledger_of(attendance: AttendanceInput | None, cfg: LedgerConfig = ON, **day) -> L.DayLedger:
    return L.build_ledger([DayInput(date=MON, attendance=attendance, **day)], cfg)


def test_gv_as_pay_01_late_excused_by_policy():
    att = dr.to_attendance_input(row(late_mark=True, late_excused=True, net_seconds=30000))
    assert (att.status, att.late, att.late_regularised, att.early_exit, att.worked_seconds) == (
        AttendanceStatus.LATE, True, True, False, 30000)
    assert att.session_id == "attendance_day:700104:v1" and att.source_type == "attendance_day"
    record = ledger_of(att).days[0]
    assert record.late_mark and record.late_mark_regularised and not record.early_exit_regularised


def test_gv_as_pay_04_half_day_other_half_unmarked_follows_unmarked_day():
    att = dr.to_attendance_input(row(day_result="HALF_DAY", second_half_result="UNMARKED", net_seconds=4 * 3600))
    assert (att.status, att.present_portion, att.other_half) == (AttendanceStatus.HALF_DAY, L.Portion.FIRST_HALF,
                                                                 "UNMARKED")
    record = ledger_of(att).days[0]
    assert (record.am.classification, record.pm.classification) == (C.HALF_DAY, C.UNMARKED)
    assert record.pm.pending
    by_policy = ledger_of(att, dataclasses.replace(ON, unmarked_day=L.UnmarkedDayPolicy.ABSENT)).days[0]
    assert (by_policy.am.classification, by_policy.pm.classification) == (C.HALF_DAY, C.ABSENT)


def test_gv_as_pay_05_missing_punch():
    att = dr.to_attendance_input(row(day_result="MISSING_PUNCH", first_half_result="MISSING_PUNCH",
                                     second_half_result="MISSING_PUNCH", net_seconds=None))
    assert (att.status, att.closed_without_punch_out, att.worked_seconds) == (AttendanceStatus.PRESENT, True, None)
    assert ledger_of(att).days[0].am.classification is C.MISSING_PUNCH_OUT


def test_gv_as_pay_06_on_duty_without_session_gives_no_input():
    assert dr.to_attendance_input(row(presence_basis="ON_DUTY", session_count=0, net_seconds=None)) is None


def test_gv_as_pay_07_late_beyond_converted_second_half():
    att = dr.to_attendance_input(row(day_result="HALF_DAY", first_half_result="ABSENT", net_seconds=20700,
                                     night_minutes=340))
    assert (att.status, att.present_portion, att.other_half, att.late, att.worked_seconds, att.night_seconds) == (
        AttendanceStatus.HALF_DAY, L.Portion.SECOND_HALF, "ABSENT", False, 20700, 20400)
    record = ledger_of(att).days[0]
    assert record.pm.classification is C.HALF_DAY and record.am.classification is C.PRESUMED_PRESENT
    assert record.am.paid == 0 and record.pm.paid == 1 and not record.late_mark  # deducted once (INV-18)
    assert record.night_seconds == 20400  # JC-AS-06


@pytest.mark.parametrize("result, reasons", [("UNMARKED", []), ("PENDING", ["LEAVE_PENDING"]), ("OFF", [])])
def test_days_without_input(result, reasons):
    assert dr.to_attendance_input(row(day_result=result, pending_reasons=reasons)) is None


def test_open_rows_are_never_read():
    assert dr.to_attendance_input(row(evaluation_state="OPEN")) is None


def test_pending_leave_other_half():
    att = dr.to_attendance_input(row(day_result="HALF_DAY", first_half_result="PENDING",
                                     pending_reasons=["LEAVE_PENDING"]))
    assert att.present_portion is L.Portion.SECOND_HALF and att.other_half == "PENDING_LEAVE"


def test_standard_status_is_never_rederived_from_hours():
    """JC-AS-04: 5 h is a half day for payroll's own thresholds, but attendance said PRESENT."""
    att = dr.to_attendance_input(row(net_seconds=5 * 3600))
    assert ledger_of(att, DERIVE).days[0].am.classification is C.PRESENT
    legacy = AttendanceInput(AttendanceStatus.PRESENT, session_id=9, worked_seconds=5 * 3600)
    assert ledger_of(legacy, DERIVE).days[0].pm.classification is not C.PRESENT


def test_legacy_rows_are_not_mapped_here():
    with pytest.raises(ValueError):
        dr.to_attendance_input(row(rules_mode="LEGACY_V1"))


def test_expected_none_worked_day_and_absent():
    worked = dr.to_attendance_input(row(first_half_result="OFF", second_half_result="OFF", presence_basis=None,
                                        net_seconds=29100))
    assert (worked.status, worked.worked_seconds) == (AttendanceStatus.PRESENT, 29100)  # GV-AS-INT-01
    absent = dr.to_attendance_input(row(day_result="ABSENT", first_half_result="ABSENT", second_half_result="ABSENT",
                                        net_seconds=7200, night_minutes=30))
    assert (absent.status, absent.worked_seconds, absent.night_seconds) == (AttendanceStatus.ABSENT, None, 0)


def test_snapshot_round_trip_v1_bytes_unchanged_and_standard_fields_kept():
    legacy = DayInput(date=MON, attendance=AttendanceInput(AttendanceStatus.LATE, session_id=12, worked_seconds=100,
                                                           late=True, content_hash=HASH))
    body = snap.day_to_dict(legacy)
    assert set(body["attendance"]) == {"status", "session_id", "closed_without_punch_out", "worked_seconds", "late",
                                       "early_exit", "regularised", "night_seconds", "content_hash"}
    assert snap.day_from_dict(body, "d") == legacy
    standard = DayInput(date=MON, attendance=dr.to_attendance_input(row(day_result="HALF_DAY",
                                                                        first_half_result="ABSENT")))
    again = snap.day_from_dict(snap.day_to_dict(standard), "d")
    assert again == standard and again.attendance.present_portion is L.Portion.SECOND_HALF


def test_lineage_parsing():
    assert dr.parse_lineage("attendance_day:700104:v3") == (700104, 3)
    assert dr.parse_lineage(12) is None and dr.parse_lineage("attendance:12") is None


def test_present_portion_needs_a_standard_half_day():
    with pytest.raises(L.LedgerInputError):
        AttendanceInput(AttendanceStatus.PRESENT, present_portion="FIRST_HALF", rules_mode="STANDARD")
    with pytest.raises(L.LedgerInputError):
        AttendanceInput(AttendanceStatus.HALF_DAY, present_portion="FIRST_HALF")  # LEGACY_V1
    assert Slot.PM in L.Portion.SECOND_HALF.slots
