"""Day ledger (3.4 §4, §5.3, §6, §7, §11, §17.5; 3.1 §5.2-§5.4 and §6.1)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll import ledger as L
from app.payroll.ledger import (
    AttendanceInput,
    AttendanceStatus,
    Classification as C,
    DayInput,
    ExceptionCode,
    HolidayInput,
    HolidayType,
    LeavePortion,
    LedgerConfig,
    LedgerConfigError,
    LedgerInputError,
    ManualLop,
    PayTreatment,
    Portion,
    Severity,
    Slot,
    build_ledger,
)
from app.payroll.workweek import MON_SAT, dates_between, is_weekly_off

D = Decimal
H8 = 8 * 3600
MON = dt.date(2026, 2, 2)  # February 2026: Monday 2nd .. Sunday 8th
ON = LedgerConfig(attendance_enabled=True)
ON_ON = LedgerConfig(attendance_enabled=True, leave_enabled=True)
LEAVE_ONLY = LedgerConfig(leave_enabled=True)


def d(offset: int) -> dt.date:
    return MON + dt.timedelta(days=offset)


def present(worked: int = H8, **kwargs: object) -> AttendanceInput:
    return AttendanceInput(AttendanceStatus.PRESENT, session_id=kwargs.pop("session_id", "s"), worked_seconds=worked,
                           **kwargs)  # type: ignore[arg-type]


def absent() -> AttendanceInput:
    return AttendanceInput(AttendanceStatus.ABSENT, session_id="a")


def half() -> AttendanceInput:
    return AttendanceInput(AttendanceStatus.HALF_DAY, session_id="h", worked_seconds=4 * 3600)


def leave(code: str = "casual", portion: Portion = Portion.FULL, treatment: PayTreatment = PayTreatment.FULL,
          **kwargs: object) -> LeavePortion:
    return LeavePortion(kwargs.pop("leave_id", f"lv-{code}"), code, portion, treatment, **kwargs)  # type: ignore[arg-type]


def holiday(kind: HolidayType = HolidayType.PUBLIC) -> HolidayInput:
    return HolidayInput("hol-1", kind, "Founders Day")


def one(cfg: LedgerConfig, **kwargs: object) -> L.DayLedger:
    return build_ledger([DayInput(date=kwargs.pop("date", MON), **kwargs)], cfg)  # type: ignore[arg-type]


def kinds(record: L.DayRecord) -> tuple[C, C]:
    return record.am.classification, record.pm.classification


def codes(ledger: L.DayLedger) -> list[ExceptionCode]:
    return [item.code for item in ledger.exceptions]


def week_17_5(sandwich: L.SandwichRule = L.SandwichRule.OFF) -> L.DayLedger:
    cfg = dataclasses.replace(ON_ON, sandwich_rule=sandwich)
    return build_ledger([
        DayInput(d(0), attendance=present()),
        DayInput(d(1), leaves=(leave(portion=Portion.FIRST_HALF),), attendance=present(4 * 3600)),
        DayInput(d(2), holiday=holiday()),
        DayInput(d(3), attendance=absent()),
        DayInput(d(4), leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),)),
        DayInput(d(5), attendance=half()),
        DayInput(d(6), weekly_off=True),
    ], cfg)


# --- 3.4 §17.5 worked example ----------------------------------------------------------------------------------------


def test_doc_3_4_17_5_week_ledger() -> None:
    """3.4 §17.5: attendance ON, leave ON, Monday-Saturday week; paid 4.5 and LOP 2.5."""
    ledger = week_17_5()
    expected = [
        ((C.PRESENT, C.PRESENT), "1", "0"),
        ((C.LEAVE, C.PRESENT), "1", "0"),
        ((C.HOLIDAY, C.HOLIDAY), "1", "0"),
        ((C.ABSENT, C.ABSENT), "0", "1"),
        ((C.LEAVE, C.LEAVE), "0", "1"),
        ((C.HALF_DAY, C.PRESUMED_PRESENT), "0.5", "0.5"),
        ((C.WEEKLY_OFF, C.WEEKLY_OFF), "1", "0"),
    ]
    assert [(kinds(r), str(r.paid), str(r.lop)) for r in ledger.days] == expected
    totals = ledger.totals
    assert (totals.paid_days, totals.lop_days, totals.employment_days) == (D("4.5"), D("2.5"), D(7))
    assert (totals.scheduled_working_days, totals.lop_days_on_working_days) == (D(5), D("2.5"))
    assert (totals.present_days, totals.half_days, totals.holidays, totals.weekly_offs) == (D(2), D(1), D(1), D(1))
    assert (totals.leave_days_paid, totals.leave_days_unpaid, totals.absent_days) == (D("0.5"), D(1), D(1))
    assert totals.worked_hours == D(16) and ledger.exceptions == ()
    tuesday = ledger.days[1]
    assert tuesday.am.source == L.SourceRef("leave", "lv-casual") and tuesday.pm.source.type == "attendance"
    assert ledger.days[2].am.source == L.SourceRef("holiday", "hol-1") and ledger.days[2].am.detail == "Founders Day"


def test_doc_3_4_17_5_sunday_stays_paid_with_weekly_off_sandwich() -> None:
    """3.4 §17.5: with WEEKLY_OFFS sandwich Sunday stays paid; next Monday (outside the ledger) would decide."""
    ledger = week_17_5(L.SandwichRule.WEEKLY_OFFS)
    assert kinds(ledger.days[6]) == (C.WEEKLY_OFF, C.WEEKLY_OFF) and ledger.totals.sandwich_lop_days == 0


# --- precedence (§4.2) and switch combinations (3.1 §6.1) -----------------------------------------------------------


def test_every_switch_off_pays_every_employed_day() -> None:
    """3.4 §1 / 3.1 §6.1 OFF×OFF: a salaried employee is paid for every employed day; punches and leave never matter."""
    days = [DayInput(day, weekly_off=is_weekly_off(MON_SAT, day), attendance=absent(),
                     leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),) if day == d(1) else ())
            for day in dates_between(d(-1), d(26))]
    ledger = build_ledger(days, LedgerConfig())
    assert ledger.totals.paid_days == 28 and ledger.totals.lop_days == 0 and ledger.exceptions == ()
    assert ledger.days[2].am.classification is C.PRESUMED_PRESENT
    assert ledger.totals.payable_hours == 24 * 8 and ledger.totals.worked_hours == 0


def test_manual_lop_reduces_pay_with_every_switch_off() -> None:
    """3.1 §6.1 OFF×OFF: paid unless a manual LOP input covers it (rule 6)."""
    ledger = one(LedgerConfig(), manual_lop=(ManualLop("m1", Portion.SECOND_HALF),))
    assert kinds(ledger.days[0]) == (C.PRESUMED_PRESENT, C.MANUAL_LOP)
    assert ledger.days[0].pm.source == L.SourceRef("manual_lop", "m1") and ledger.totals.lop_days == D("0.5")


def test_suspension_is_lop_and_beats_leave_and_holiday() -> None:
    ledger = one(ON_ON, suspended_unpaid=True, suspension_id=7, leaves=(leave(),), holiday=holiday())
    assert kinds(ledger.days[0]) == (C.SUSPENDED_UNPAID, C.SUSPENDED_UNPAID)
    assert ledger.totals.lop_days == 1 and ledger.totals.suspended_days == 1
    assert ledger.days[0].am.source == L.SourceRef("suspension", 7)


def test_holiday_beats_weekly_off_and_attendance_never_reclassifies_it() -> None:
    """3.4 §4.2 notes: work on a holiday keeps the HOLIDAY classification; the hours are recorded for premiums."""
    ledger = one(ON_ON, holiday=holiday(), attendance=present(6 * 3600))
    assert kinds(ledger.days[0]) == (C.HOLIDAY, C.HOLIDAY) and ledger.totals.worked_hours == 6
    off = one(ON_ON, weekly_off=True, attendance=present(5 * 3600))
    assert kinds(off.days[0]) == (C.WEEKLY_OFF, C.WEEKLY_OFF) and off.totals.worked_hours == 5


def test_unpaid_holiday_types_are_working_days() -> None:
    """HOLIDAY_PAY_POLICY.paid_holiday_types: an optional holiday not availed is an ordinary working day."""
    ledger = one(ON, holiday=holiday(HolidayType.OPTIONAL_NOT_AVAILED), attendance=present())
    assert kinds(ledger.days[0]) == (C.PRESENT, C.PRESENT) and ledger.days[0].scheduled_working_day
    company_unpaid = LedgerConfig(paid_holiday_types=frozenset({HolidayType.PUBLIC}))
    assert one(company_unpaid, holiday=holiday(HolidayType.COMPANY)).days[0].am.classification is C.PRESUMED_PRESENT


def test_on_duty_counts_as_present_per_policy() -> None:
    duty = (L.OnDuty("od-1"),)
    assert kinds(one(ON, on_duty=duty).days[0]) == (C.PRESENT, C.PRESENT)
    held = one(dataclasses.replace(ON, count_on_duty_as_present=False), on_duty=duty)
    assert kinds(held.days[0]) == (C.UNMARKED, C.UNMARKED)


@pytest.mark.parametrize(("value", "paid", "lop"), [("0", "0", "1"), ("0.5", "0.5", "0.5"), ("1", "1", "0")])
def test_half_day_value_zero_half_or_one(value: str, paid: str, lop: str) -> None:
    """3.1 §5.2 half_day_value 0 / 0.5 / 1: a half day pays zero slots, one slot or both."""
    ledger = one(dataclasses.replace(ON, half_day_value=D(value)), attendance=half())
    assert (ledger.totals.paid_days, ledger.totals.lop_days) == (D(paid), D(lop))
    assert ledger.totals.half_days == 1


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(9, (C.PRESENT, C.PRESENT)), (8, (C.PRESENT, C.PRESENT)), (5, (C.HALF_DAY, C.PRESUMED_PRESENT)),
     (3, (C.ABSENT, C.ABSENT))],
)
def test_derive_status_from_hours(hours: int, expected: tuple[C, C]) -> None:
    """3.4 §6: net ≥ full_day_min_hours → present; ≥ half_day_min_hours → half day; otherwise absent."""
    cfg = dataclasses.replace(ON, derive_status_from_hours=True)
    assert kinds(one(cfg, attendance=present(hours * 3600)).days[0]) == expected
    marked_half = AttendanceInput(AttendanceStatus.HALF_DAY, session_id="h", worked_seconds=hours * 3600)
    assert kinds(one(cfg, attendance=marked_half).days[0]) == expected
    assert kinds(one(ON, attendance=present(3 * 3600)).days[0]) == (C.PRESENT, C.PRESENT)


def test_late_status_maps_to_present() -> None:
    """3.4 §6 status mapping: late → PRESENT (lateness only feeds §11)."""
    late = AttendanceInput(AttendanceStatus.LATE, session_id="s", worked_seconds=H8)
    ledger = one(ON, attendance=late)
    assert kinds(ledger.days[0]) == (C.PRESENT, C.PRESENT) and ledger.totals.late_marks == 1


# --- edge cases ----------------------------------------------------------------------------------------------------


def test_ec_att_01_unmarked_day_is_a_review_exception_not_a_deduction() -> None:
    """EC-ATT-01: a working day with no attendance and no leave → EXCEPTION by default, slots pending, no LOP."""
    ledger = one(ON)
    assert kinds(ledger.days[0]) == (C.UNMARKED, C.UNMARKED) and all(s.pending for s in ledger.days[0].slots)
    assert (ledger.totals.paid_days, ledger.totals.lop_days, ledger.totals.pending_days) == (0, 0, 1)
    assert [(e.code, e.severity) for e in ledger.exceptions] == [(ExceptionCode.UNMARKED_DAY, Severity.BLOCKING)]
    assert not ledger.guard.triggered


@pytest.mark.parametrize(("policy", "kind", "lop"), [("ABSENT", C.ABSENT, 1), ("PRESENT", C.PRESENT, 0)])
def test_ec_att_01_unmarked_day_follows_absent_or_present_policy(policy: str, kind: C, lop: int) -> None:
    ledger = one(dataclasses.replace(ON, unmarked_day=L.UnmarkedDayPolicy(policy)))
    assert kinds(ledger.days[0]) == (kind, kind) and ledger.totals.lop_days == lop
    assert codes(ledger) == ([ExceptionCode.ZERO_PAID_WORKING_DAYS] if lop else [])
    assert ledger.days[0].am.source == L.SourceRef("policy", "ATTENDANCE_PAY_POLICY.unmarked_day")


def test_ec_att_01_review_override_resolves_the_day_and_is_the_source() -> None:
    days = [DayInput(MON), DayInput(d(1))]
    ledger = build_ledger(days, ON, {MON: "TREAT_AS_PRESENT", d(1): L.LedgerOverride("TREAT_AS_ABSENT", "rv-9")})
    assert kinds(ledger.days[0]) == (C.PRESENT, C.PRESENT) and kinds(ledger.days[1]) == (C.ABSENT, C.ABSENT)
    assert ledger.days[1].am.source == L.SourceRef("override", "rv-9") and ledger.exceptions == ()
    half_override = build_ledger([DayInput(MON)], ON, {MON: "TREAT_AS_HALF_DAY"})
    assert half_override.totals.lop_days == D("0.5")


def test_ec_att_01_unused_overrides_are_reported() -> None:
    ledger = build_ledger([DayInput(MON, weekly_off=True)], ON, {MON: "TREAT_AS_ABSENT", d(5): "TREAT_AS_PRESENT"})
    assert codes(ledger) == [ExceptionCode.OVERRIDE_NOT_APPLIED] * 2 + [ExceptionCode.ZERO_PAID_WORKING_DAYS]
    assert ledger.guard.reason is L.ZeroPayReason.NO_WORKING_DAYS_IN_EMPLOYMENT
    assert kinds(ledger.days[0]) == (C.WEEKLY_OFF, C.WEEKLY_OFF)
    with pytest.raises(LedgerInputError) as caught:
        build_ledger([DayInput(MON)], ON, {MON: "TREAT_AS_LATE"})
    assert caught.value.code == "INVALID_OVERRIDE"


def test_ec_att_02_missing_punch_out_is_an_exception_and_hours_are_never_invented() -> None:
    """EC-ATT-02: a session closed without a punch-out → EXCEPTION by default; it carries no hours."""
    open_session = AttendanceInput(AttendanceStatus.PRESENT, session_id="s-7", closed_without_punch_out=True)
    ledger = one(ON, attendance=open_session)
    assert kinds(ledger.days[0]) == (C.MISSING_PUNCH_OUT, C.MISSING_PUNCH_OUT)
    assert codes(ledger) == [ExceptionCode.MISSING_PUNCH_OUT] and ledger.blocking
    assert ledger.totals.worked_hours == 0 and ledger.totals.lop_days == 0
    assert ledger.exceptions[0].sources == (L.SourceRef("attendance", "s-7"),)


@pytest.mark.parametrize(
    ("policy", "paid", "lop"), [("HALF_DAY", "0.5", "0.5"), ("FULL_DAY", "1", "0"), ("ABSENT", "0", "1")]
)
def test_ec_att_02_missing_punch_out_policies(policy: str, paid: str, lop: str) -> None:
    open_session = AttendanceInput(AttendanceStatus.PRESENT, session_id="s", closed_without_punch_out=True)
    ledger = one(dataclasses.replace(ON, missing_punch_out=L.MissingPunchOutPolicy(policy)), attendance=open_session)
    assert (ledger.totals.paid_days, ledger.totals.lop_days) == (D(paid), D(lop))
    assert codes(ledger) == ([ExceptionCode.ZERO_PAID_WORKING_DAYS] if paid == "0" else [])
    assert ledger.totals.worked_hours == 0


def test_ec_att_02_an_open_session_cannot_carry_hours() -> None:
    with pytest.raises(LedgerInputError) as caught:
        AttendanceInput(AttendanceStatus.PRESENT, closed_without_punch_out=True, worked_seconds=H8)
    assert caught.value.code == "INCONSISTENT_ATTENDANCE"


def test_ec_att_03_half_day_leave_plus_half_day_attendance_has_no_lop() -> None:
    """EC-ATT-03: one slot leave, one slot present → no LOP."""
    ledger = one(ON_ON, leaves=(leave(portion=Portion.FIRST_HALF),), attendance=half())
    assert kinds(ledger.days[0]) == (C.LEAVE, C.HALF_DAY)
    assert (ledger.totals.paid_days, ledger.totals.lop_days) == (1, 0)
    second = one(ON_ON, leaves=(leave(portion=Portion.SECOND_HALF),), attendance=half())
    assert kinds(second.days[0]) == (C.HALF_DAY, C.LEAVE) and second.totals.lop_days == 0


def test_ec_att_03_half_day_attendance_alone_loses_half_a_day() -> None:
    """EC-ATT-03 (negative): without the half-day leave, the uncovered half is LOP."""
    ledger = one(ON_ON, attendance=half())
    assert ledger.totals.lop_days == D("0.5") and ledger.days[0].pm.classification is C.PRESUMED_PRESENT


def test_ec_att_04_attendance_on_leave_off_approved_leave_is_paid_excused() -> None:
    """EC-ATT-04: attendance ON, leave OFF, approved (even unpaid) leave → LEAVE_EXCUSED, paid, never absent."""
    ledger = one(ON, leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),))
    assert kinds(ledger.days[0]) == (C.LEAVE_EXCUSED, C.LEAVE_EXCUSED) and ledger.days[0].am.rule == "3b"
    assert (ledger.totals.paid_days, ledger.totals.lop_days, ledger.totals.leave_days_excused) == (1, 0, 1)
    assert ledger.exceptions == ()


def test_ec_att_04_leave_off_treat_as_absent_is_lop() -> None:
    """EC-ATT-04 (negative): when_disabled_treat_approved_leave_as = ABSENT turns the leave day into LOP."""
    cfg = dataclasses.replace(ON, when_disabled_treat_approved_leave_as=L.LeaveDisabledTreatment.ABSENT)
    ledger = one(cfg, leaves=(leave(),))
    assert kinds(ledger.days[0]) == (C.ABSENT, C.ABSENT) and ledger.totals.lop_days == 1


def test_ec_att_07_overnight_session_counts_on_its_start_date() -> None:
    """EC-ATT-07: a 22:00-06:00 session dated on its local start date pays and records hours on that date only."""
    night = present(H8, night_seconds=7 * 3600, session_id="night")
    ledger = build_ledger([DayInput(MON, attendance=night), DayInput(d(1), attendance=present())], ON)
    assert ledger.days[0].worked_seconds == H8 and ledger.days[0].night_seconds == 7 * 3600
    assert ledger.days[1].night_seconds == 0 and ledger.totals.night_hours == 7 and ledger.totals.worked_hours == 16


def test_ec_att_08_location_has_no_input_at_all() -> None:
    """EC-ATT-08: GPS gaps, accuracy or movement flags cannot reach the ledger: there is no field for them."""
    names = {f.name for cls in (DayInput, AttendanceInput, LedgerConfig) for f in dataclasses.fields(cls)}
    assert not {n for n in names if any(word in n for word in ("gps", "location", "geo", "accuracy", "movement"))}
    with pytest.raises(TypeError):
        AttendanceInput(AttendanceStatus.PRESENT, worked_seconds=H8, gps_flags=("LOW_ACCURACY",))  # type: ignore[call-arg]


def _working_days_february() -> list[dt.date]:
    return [day for day in dates_between(dt.date(2026, 2, 1), dt.date(2026, 2, 28)) if not is_weekly_off(MON_SAT, day)]


def _february(attendance_for: object, holidays: frozenset[dt.date] = frozenset(),
              cfg: LedgerConfig = ON) -> L.DayLedger:
    return build_ledger([
        DayInput(day, weekly_off=is_weekly_off(MON_SAT, day),
                 holiday=holiday() if day in holidays else None,
                 attendance=None if is_weekly_off(MON_SAT, day) or day in holidays else attendance_for(day))  # type: ignore[operator]
        for day in dates_between(dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    ], cfg)


def test_ec_att_09_absent_every_working_day_triggers_the_zero_pay_guard() -> None:
    """EC-ATT-09 / 3.4 §5.3: no paid working day → BLOCKING ZERO_PAID_WORKING_DAYS ("confirm before paying")."""
    ledger = _february(lambda day: absent())
    assert ledger.guard == L.ZeroPayGuard(True, L.ZeroPayReason.NO_PAID_WORKING_DAYS, False)
    [item] = ledger.blocking
    assert item.code is ExceptionCode.ZERO_PAID_WORKING_DAYS and "confirm before paying" in item.message
    assert ledger.totals.paid_days == 4  # the Sundays: why CALENDAR_DAYS needs the guard


def test_ec_att_09_pay_zero_policy_is_informational_and_one_paid_day_clears_the_guard() -> None:
    """EC-ATT-09 (negative/boundary): PAY_ZERO records the decision; a single paid working day means no guard."""
    pay_zero = _february(lambda day: absent(), cfg=dataclasses.replace(ON, zero_paid_days=L.ZeroPaidDaysPolicy.PAY_ZERO))
    assert pay_zero.guard.pay_zero and not pay_zero.blocking
    assert [e.severity for e in pay_zero.exceptions] == [Severity.INFO]
    first = _working_days_february()[0]
    one_day = _february(lambda day: present() if day == first else absent())
    assert not one_day.guard.triggered and one_day.exceptions == ()
    unmarked = _february(lambda day: None)
    assert not unmarked.guard.triggered and ExceptionCode.ZERO_PAID_WORKING_DAYS not in codes(unmarked)


def test_ec_att_10_holiday_calendar_covering_every_working_day_hits_the_guard() -> None:
    """EC-ATT-10: same guard; the message points at the holiday calendar."""
    ledger = _february(lambda day: present(), holidays=frozenset(_working_days_february()))
    assert ledger.guard.reason is L.ZeroPayReason.HOLIDAYS_COVER_ALL_WORKING_DAYS
    assert "holiday calendar" in ledger.blocking[0].message and ledger.totals.paid_days == 28


def test_ec_att_10_one_working_day_outside_the_calendar_clears_the_guard() -> None:
    ledger = _february(lambda day: present(), holidays=frozenset(_working_days_february()[1:]))
    assert not ledger.guard.triggered and ledger.totals.scheduled_working_days == 1


def test_ec_att_13_joiner_days_before_hire_are_not_employed_never_lop() -> None:
    """EC-ATT-13: dates before hire are NOT_EMPLOYED: they reduce employment days, not LOP."""
    days = [DayInput(day, employed=day >= d(3), attendance=present(), assignment_id=4) for day in dates_between(MON, d(6))]
    ledger = build_ledger(days, ON)
    assert kinds(ledger.days[0]) == (C.NOT_EMPLOYED, C.NOT_EMPLOYED)
    assert ledger.days[0].am.source == L.SourceRef("assignment", 4)
    totals = ledger.totals
    assert (totals.employment_days, totals.paid_days, totals.lop_days, totals.unpaid_days) == (4, 4, 0, 0)
    assert not ledger.period.employed_all_period and ledger.period.days == 7


def test_ec_att_13_inputs_outside_employment_are_reported_and_ignored() -> None:
    """EC-ATT-13 / EC-LV-13 (negative): attendance or leave dated after exit never changes pay."""
    ledger = one(ON_ON, employed=False, attendance=absent(), leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),))
    assert ledger.totals.lop_days == 0 and ledger.totals.employment_days == 0
    assert codes(ledger) == [ExceptionCode.INPUT_OUTSIDE_EMPLOYMENT]
    assert not ledger.guard.triggered


def _sandwich_week(rule: L.SandwichRule, saturday: AttendanceInput, monday: AttendanceInput) -> L.DayLedger:
    cfg = dataclasses.replace(ON, sandwich_rule=rule)
    return build_ledger([
        DayInput(d(5), attendance=saturday),
        DayInput(d(6), weekly_off=True, work_week_id="ww-1"),
        DayInput(d(7), attendance=monday),
    ], cfg)


def test_ec_att_15_sandwich_on_turns_the_weekly_off_between_two_lop_days_into_lop() -> None:
    """EC-ATT-15 / 3.4 §4.3: WEEKLY_OFFS and BOTH reclassify the Sunday between two full LOP days."""
    for rule in (L.SandwichRule.WEEKLY_OFFS, L.SandwichRule.BOTH):
        ledger = _sandwich_week(rule, absent(), absent())
        sunday = ledger.days[1]
        assert kinds(sunday) == (C.SANDWICH_LOP, C.SANDWICH_LOP) and sunday.am.rule == "S"
        assert sunday.am.source == L.SourceRef("sandwich", "2026-02-07/2026-02-09")
        assert ledger.totals.lop_days == 3 and ledger.totals.sandwich_lop_days == 1
        assert ledger.totals.lop_days_on_working_days == 2


def test_ec_att_15_sandwich_off_or_partial_lop_keeps_the_weekly_off_paid() -> None:
    """EC-ATT-15 (negative): OFF keeps it paid; so do HOLIDAYS-only, and a half day on either side."""
    assert _sandwich_week(L.SandwichRule.OFF, absent(), absent()).totals.lop_days == 2
    assert _sandwich_week(L.SandwichRule.HOLIDAYS, absent(), absent()).totals.sandwich_lop_days == 0
    assert _sandwich_week(L.SandwichRule.BOTH, half(), absent()).totals.sandwich_lop_days == 0


def test_ec_att_15_holiday_sandwich_and_ledger_edges() -> None:
    cfg = dataclasses.replace(ON, sandwich_rule=L.SandwichRule.HOLIDAYS)
    ledger = build_ledger([DayInput(MON, attendance=absent()), DayInput(d(1), holiday=holiday()),
                           DayInput(d(2), attendance=absent())], cfg)
    assert kinds(ledger.days[1]) == (C.SANDWICH_LOP, C.SANDWICH_LOP)
    edge = build_ledger([DayInput(MON, holiday=holiday()), DayInput(d(1), attendance=absent())], cfg)
    assert edge.totals.sandwich_lop_days == 0


def test_ec_att_16_manual_lop_on_a_leave_or_holiday_is_rejected_on_entry() -> None:
    """EC-ATT-16: the entry-time check names the classification that already holds the slot."""
    on_holiday = L.manual_lop_conflicts(DayInput(MON, holiday=holiday()), Portion.FULL, ON_ON)
    assert [(e.code, e.detail, e.slots) for e in on_holiday] == [
        (ExceptionCode.MANUAL_LOP_CONFLICT, "HOLIDAY", (Slot.AM, Slot.PM))
    ]
    on_leave = L.manual_lop_conflicts(DayInput(MON, leaves=(leave(portion=Portion.FIRST_HALF),)), "FULL", ON_ON)
    assert [(e.detail, e.slots) for e in on_leave] == [("LEAVE", (Slot.AM,))]
    assert L.manual_lop_conflicts(DayInput(MON, leaves=(leave(portion=Portion.FIRST_HALF),)), "SECOND_HALF", ON_ON) == ()


def test_ec_att_16_manual_lop_on_a_working_day_is_accepted_and_ignored_when_it_collides() -> None:
    """EC-ATT-16 (negative): a free working day accepts it; a stale input on a holiday is ignored and reported."""
    assert L.manual_lop_conflicts(DayInput(MON, attendance=present()), Portion.FULL, ON) == ()
    ledger = build_ledger([DayInput(MON, holiday=holiday(), manual_lop=(ManualLop("m-3"),)),
                           DayInput(d(1), attendance=present())], ON_ON)
    assert kinds(ledger.days[0]) == (C.HOLIDAY, C.HOLIDAY) and ledger.totals.lop_days == 0
    [item] = ledger.exceptions
    assert item.code is ExceptionCode.MANUAL_LOP_IGNORED and item.sources == (L.SourceRef("manual_lop", "m-3"),)


def test_ec_att_17_lunch_is_subtracted_once_through_canonical_net_hours() -> None:
    """EC-ATT-17: worked_seconds is canonical net (gross − unpaid breaks); the ledger never subtracts lunch again."""
    unpaid_lunch = one(ON, attendance=present(8 * 3600))
    paid_lunch = one(ON, attendance=present(9 * 3600))
    assert unpaid_lunch.totals.payable_hours == 8 and paid_lunch.totals.payable_hours == 9


def test_ec_att_18_attendance_off_leave_on_only_unpaid_or_partial_leave_reduces_pay() -> None:
    """EC-ATT-18 / 3.1 §6.1 OFF×ON: FULL leave and punches change nothing; UNPAID and PARTIAL leave do."""
    ledger = build_ledger([
        DayInput(MON, leaves=(leave(),), attendance=absent()),
        DayInput(d(1), attendance=absent()),
        DayInput(d(2), leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),)),
        DayInput(d(3), leaves=(leave("sick", treatment=PayTreatment.PARTIAL, partial_percent=D(50)),)),
    ], LEAVE_ONLY)
    assert [r.lop for r in ledger.days] == [0, 0, 1, D("0.5")]
    assert kinds(ledger.days[1]) == (C.PRESUMED_PRESENT, C.PRESUMED_PRESENT)
    assert ledger.days[3].am.detail == "PARTIAL at 50%" and ledger.totals.leave_days_unpaid == D("1.5")


def test_ec_att_19_and_ec_lv_01_unpaid_leave_and_absence_on_one_day_count_once() -> None:
    """EC-ATT-19 / EC-LV-01: leave beats absence for the same slot: one LOP day, not two."""
    ledger = one(ON_ON, leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),), attendance=absent())
    assert kinds(ledger.days[0]) == (C.LEAVE, C.LEAVE)
    assert (ledger.totals.lop_days, ledger.totals.absent_days, ledger.totals.leave_days_unpaid) == (1, 0, 1)


def test_ec_lv_01_half_unpaid_leave_and_absence_split_the_day() -> None:
    """EC-LV-01 (boundary): a half-day unpaid leave and an absence: AM leave LOP + PM absent LOP = one day."""
    ledger = one(ON_ON, leaves=(leave("unpaid", Portion.FIRST_HALF, PayTreatment.UNPAID),), attendance=absent())
    assert kinds(ledger.days[0]) == (C.LEAVE, C.ABSENT) and ledger.totals.lop_days == 1


def test_ec_att_23_late_marks_never_change_pay_without_penalty_input() -> None:
    """EC-ATT-23 / 3.4 §11: late marks are counted per day, never reclassify; penalty OFF → no effect."""
    late = present(late=True, early_exit=True)
    ledger = build_ledger([DayInput(MON, attendance=late), DayInput(d(1), attendance=late)], ON)
    assert ledger.totals.late_marks == 2 and ledger.totals.early_exits == 2
    assert all(kinds(r) == (C.PRESENT, C.PRESENT) for r in ledger.days) and ledger.totals.lop_days == 0


def test_ec_att_23_penalty_lop_is_added_separately_and_capped() -> None:
    """EC-ATT-23 / 3.4 §11 & §17.4: penalty LOP (1.0 day) is an input added to LOP; LOP never exceeds the paid days."""
    days = [DayInput(day, attendance=present(late=True)) for day in dates_between(MON, d(4))]
    ledger = build_ledger(days, ON, penalty_lop_days=D("1.0"))
    totals = ledger.totals
    assert (totals.paid_days, totals.lop_days, totals.penalty_lop_days, totals.paid_days_after_penalty) == (5, 1, 1, 4)
    assert totals.lop_days_on_working_days == 1 and totals.unit_facts().lop_days == 1
    capped = build_ledger(days, ON, penalty_lop_days="7")
    assert capped.totals.penalty_lop_days == 5 and codes(capped) == [ExceptionCode.PENALTY_LOP_CAPPED]
    assert ledger.with_penalty_lop(0).totals.lop_days == 0
    with pytest.raises(LedgerInputError):
        build_ledger(days, ON, penalty_lop_days=-1)


def test_ec_att_24_absent_days_carry_no_late_mark_and_regularised_marks_are_counted() -> None:
    """EC-ATT-24: a regularised late mark is reported as such (no penalty downstream); an absent day has no mark."""
    ledger = build_ledger([
        DayInput(MON, attendance=present(late=True, regularised=True)),
        DayInput(d(1), attendance=AttendanceInput(AttendanceStatus.ABSENT, session_id="x", late=True)),
        DayInput(d(2), attendance=present(late=False)),
    ], ON)
    assert (ledger.totals.late_marks, ledger.totals.regularised_late_marks) == (1, 1)
    assert ledger.days[1].late_mark is False


def test_ec_att_12_daylight_saving_hours_are_exact_seconds() -> None:
    """EC-ATT-12: a 25-hour local day is 90,000 UTC seconds; hours are exact Decimals."""
    ledger = one(ON, attendance=present(25 * 3600 - 1800))
    assert ledger.totals.worked_hours == D("24.5") and isinstance(ledger.totals.worked_hours, Decimal)


# --- leave pay treatments (§7) ---------------------------------------------------------------------------------------

SICK_TIERS = (L.Tier(1, 10, D(100)), L.Tier(11, 30, D(50)))


def _sick(days_before: str, rolls: tuple[tuple[dt.date, Decimal], ...] = ()) -> LeavePortion:
    return leave("sick", treatment=PayTreatment.TIERED, tiered=L.TieredPay(SICK_TIERS, D(days_before), rolls))


def test_ec_lv_05_tiered_sick_pay_counts_days_across_periods() -> None:
    """EC-LV-05: 9 days already in the rolling window → day 10 at 100%, days 11-12 at 50%."""
    ledger = build_ledger([DayInput(d(i), leaves=(_sick("9"),)) for i in range(3)], LEAVE_ONLY)
    assert [r.paid for r in ledger.days] == [1, D("0.5"), D("0.5")]
    assert ledger.days[1].am.detail == "TIERED day 11 of the rolling window at 50%"
    assert (ledger.totals.leave_days_paid, ledger.totals.leave_days_unpaid) == (2, 1)


def test_ec_lv_05_days_rolling_off_the_window_restore_full_pay() -> None:
    """EC-LV-05 (boundary): 1 earlier day leaves the window on day 2, so day 2 is position 10 (100%), day 3 is 11."""
    rolls = ((d(1), D(1)),)
    ledger = build_ledger([DayInput(d(i), leaves=(_sick("9", rolls),)) for i in range(3)], LEAVE_ONLY)
    assert [r.paid for r in ledger.days] == [1, 1, D("0.5")]
    fresh = build_ledger([DayInput(d(i), leaves=(_sick("0"),)) for i in range(3)], LEAVE_ONLY)
    assert fresh.totals.lop_days == 0
    beyond = build_ledger([DayInput(MON, leaves=(_sick("30"),))], LEAVE_ONLY)
    assert beyond.totals.lop_days == 1 and beyond.days[0].am.detail.endswith("at 0%")


def test_tiered_half_days_advance_the_position_by_half() -> None:
    half_sick = leave("sick", Portion.FIRST_HALF, PayTreatment.TIERED, tiered=L.TieredPay(SICK_TIERS, D("9.5")))
    ledger = one(ON_ON, leaves=(half_sick,), attendance=half())
    assert ledger.days[0].am.paid == 1 and ledger.days[0].am.detail == "TIERED day 10 of the rolling window at 100%"


def test_partial_leave_pays_p_and_loses_one_minus_p() -> None:
    """3.4 §7.2 PARTIAL: paid fraction p, LOP 1 − p."""
    ledger = one(LEAVE_ONLY, leaves=(leave("sick", treatment=PayTreatment.PARTIAL, partial_percent="75"),))
    assert (ledger.totals.paid_days, ledger.totals.lop_days) == (D("0.75"), D("0.25"))
    [by_type] = ledger.totals.leave_by_type
    assert (by_type.leave_type_code, by_type.paid_days, by_type.unpaid_days) == ("sick", D("0.75"), D("0.25"))


def test_ec_lv_12_statutory_leave_is_not_salary_and_is_flagged_for_the_pack() -> None:
    """EC-LV-12 / 3.4 §7.2 STATUTORY: no salary for the day (no double pay); the pack pays it (INFO item)."""
    ledger = one(ON_ON, leaves=(leave("maternity", treatment=PayTreatment.STATUTORY),))
    assert ledger.totals.leave_days_paid == 0 and ledger.totals.leave_days_statutory == 1
    assert ledger.totals.leave_days_unpaid == 0
    [item] = [e for e in ledger.exceptions if e.code is not ExceptionCode.ZERO_PAID_WORKING_DAYS]
    assert ledger.guard.triggered
    assert item.code is ExceptionCode.STATUTORY_LEAVE and item.detail == "maternity" and item.severity is Severity.INFO


def test_ec_lv_06_half_day_leave_on_a_holiday_consumes_no_leave() -> None:
    """EC-LV-06: the day stays a holiday unless the leave type counts non-working days."""
    ledger = one(ON_ON, holiday=holiday(), leaves=(leave(portion=Portion.FIRST_HALF),))
    assert kinds(ledger.days[0]) == (C.HOLIDAY, C.HOLIDAY) and ledger.totals.leave_days_paid == 0
    assert codes(ledger) == [ExceptionCode.LEAVE_ON_NON_WORKING_DAY, ExceptionCode.ZERO_PAID_WORKING_DAYS]
    counted = one(ON_ON, holiday=holiday(), leaves=(leave(portion=Portion.FIRST_HALF, counts_non_working_days=True),))
    assert kinds(counted.days[0]) == (C.LEAVE, C.HOLIDAY)
    assert codes(counted) == [ExceptionCode.ZERO_PAID_WORKING_DAYS]


def test_ec_lv_10_unpaid_leave_across_a_weekly_off_follows_counts_non_working_days() -> None:
    """EC-LV-10: Saturday-Monday unpaid leave: Sunday is LOP only when the type counts non-working days."""
    def span(counts: bool) -> L.DayLedger:
        unpaid = leave("unpaid", treatment=PayTreatment.UNPAID, counts_non_working_days=counts)
        return build_ledger([DayInput(d(5), leaves=(unpaid,)), DayInput(d(6), weekly_off=True, leaves=(unpaid,)),
                             DayInput(d(7), leaves=(unpaid,))], ON_ON)
    assert span(False).totals.lop_days == 2 and kinds(span(False).days[1]) == (C.WEEKLY_OFF, C.WEEKLY_OFF)
    assert span(True).totals.lop_days == 3


def test_ec_lv_09_leave_spanning_two_periods_is_split_by_date() -> None:
    """EC-LV-09: each period's ledger counts only its own dates of the leave."""
    unpaid = leave("unpaid", treatment=PayTreatment.UNPAID, leave_id=55)
    january = build_ledger([DayInput(dt.date(2026, 1, 30), leaves=(unpaid,)),
                            DayInput(dt.date(2026, 1, 31), leaves=(unpaid,))], LEAVE_ONLY)
    february = build_ledger([DayInput(MON, leaves=(unpaid,)), DayInput(d(1))], LEAVE_ONLY)
    assert (january.totals.lop_days, february.totals.lop_days) == (2, 1)


def test_ec_hol_01_holiday_on_a_weekly_off_follows_policy() -> None:
    """EC-HOL-01: nothing, a substitute day or an extra day's pay."""
    for policy, extra, substitute in (("NONE", 0, 0), ("SUBSTITUTE_DAY", 0, 1), ("EXTRA_DAY_PAY", 1, 0)):
        cfg = dataclasses.replace(ON, holiday_on_weekly_off=L.HolidayOnWeeklyOff(policy))
        ledger = one(cfg, weekly_off=True, holiday=holiday())
        assert kinds(ledger.days[0]) == (C.HOLIDAY, C.HOLIDAY) and ledger.days[0].holiday_on_weekly_off
        assert (ledger.totals.extra_day_pay_days, ledger.totals.substitute_days) == (extra, substitute)
        assert ledger.totals.paid_days == 1 and ledger.totals.holidays_on_weekly_off == 1


def test_ec_hol_01_holiday_on_a_working_day_is_not_on_a_weekly_off() -> None:
    cfg = dataclasses.replace(ON, holiday_on_weekly_off=L.HolidayOnWeeklyOff.EXTRA_DAY_PAY)
    ledger = one(cfg, holiday=holiday())
    assert ledger.totals.extra_day_pay_days == 0 and not ledger.days[0].holiday_on_weekly_off


# --- hourly and daily workers (§8) --------------------------------------------------------------------------------


def test_non_salaried_rest_days_unpaid_and_holiday_pay_by_adjacent_day_rule() -> None:
    """3.4 §8 / EC-HOL-03: hourly workers earn holiday pay only if eligible; weekly offs are neither paid nor LOP."""
    cfg = LedgerConfig(attendance_enabled=True, employee_is_salaried=False,
                       non_salaried_holiday_pay=L.NonSalariedHolidayPay.STANDARD_HOURS,
                       holiday_eligibility_adjacent_days=L.AdjacentDays.BOTH)
    def week(after: AttendanceInput) -> L.DayLedger:
        return build_ledger([DayInput(MON, attendance=present()), DayInput(d(1), holiday=holiday()),
                             DayInput(d(2), attendance=after), DayInput(d(3), weekly_off=True)], cfg)
    eligible = week(present())
    assert eligible.days[1].holiday_pay_eligible and eligible.days[1].paid == 1
    assert eligible.totals.paid_holiday_hours == 8 and eligible.totals.payable_hours == 24
    assert eligible.days[3].paid == 0 and eligible.days[3].lop == 0 and eligible.totals.unpaid_days == 1
    not_eligible = week(absent())
    assert not_eligible.days[1].holiday_pay_eligible is False and not_eligible.days[1].paid == 0
    with pytest.raises(LedgerConfigError) as caught:
        LedgerConfig(holiday_eligibility_adjacent_days=L.AdjacentDays.BOTH)
    assert caught.value.code == "ADJACENT_DAYS_NEED_ATTENDANCE"


# --- outputs, segments, validation ---------------------------------------------------------------------------------


def test_outputs_serialize_with_every_slot_source() -> None:
    """3.4 §4.4: the ledger with every slot's classification and deciding record is stored in the snapshot."""
    data = week_17_5().to_dict()
    json.dumps(data)
    assert data["days"][1]["slots"][0] == {
        "slot": "AM", "classification": "LEAVE", "rule": "3", "paid": "1", "lop": "0", "pending": False,
        "source": {"type": "leave", "id": "lv-casual", "content_hash": None}, "detail": None,
        "leave_type_code": "casual", "pay_treatment": "FULL",
    }
    assert data["totals"]["paid_days"] == "4.5" and data["period"]["days"] == 7
    assert week_17_5().lop_dates() == (d(3), d(4), d(5))


def test_segments_total_separately_and_feed_proration() -> None:
    days = [DayInput(day, attendance=absent() if day == d(4) else present(), segment="A" if day < d(3) else "B")
            for day in dates_between(MON, d(5))]
    ledger = build_ledger(days, ON)
    assert ledger.segment_keys == ("A", "B")
    assert (ledger.segment("A").employment_days, ledger.segment("B").lop_days) == (3, 1)
    assert [f.employment_days for f in ledger.segment_facts()] == [3, 3]
    with pytest.raises(LedgerInputError):
        ledger.segment("C")


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: build_ledger([DayInput(MON), DayInput(MON)], ON), "DUPLICATE_DATE"),
        (lambda: build_ledger([DayInput(MON), DayInput(d(2))], ON), "NON_CONTIGUOUS_DATES"),
        (lambda: build_ledger([], ON), "EMPTY_LEDGER"),
        (lambda: DayInput(MON, leaves=(leave(), leave("sick", Portion.FIRST_HALF))), "OVERLAPPING_LEAVE"),
        (lambda: DayInput(dt.datetime(2026, 2, 2, 9)), "INVALID_DATE"),
        (lambda: leave(treatment=PayTreatment.PARTIAL), "INVALID_LEAVE_PAY"),
        (lambda: leave(treatment=PayTreatment.PARTIAL, partial_percent=D(101)), "INVALID_PERCENT"),
        (lambda: leave(partial_percent=D(50)), "INVALID_LEAVE_PAY"),
        (lambda: leave(treatment=PayTreatment.TIERED), "INVALID_LEAVE_PAY"),
        (lambda: leave(treatment="HALF"), "INVALID_PAY_TREATMENT"),
        (lambda: L.TieredPay((L.Tier(1, 10, D(100)), L.Tier(5, 20, D(50)))), "INVALID_TIERS"),
        (lambda: L.TieredPay(SICK_TIERS, D(1), ((MON, D(2)),)), "INVALID_TIERED_HISTORY"),
        (lambda: L.TieredPay(SICK_TIERS, D("0.3")), "INVALID_DAYS"),
        (lambda: AttendanceInput(AttendanceStatus.PRESENT, worked_seconds=-1), "INVALID_SECONDS"),
        (lambda: AttendanceInput("ON_TIME"), "INVALID_CHOICE"),
        (lambda: AttendanceInput(AttendanceStatus.PRESENT, night_seconds=60), "INCONSISTENT_ATTENDANCE"),
        (lambda: DayInput(MON, standard_hours=8.0), "INVALID_NUMBER"),
    ],
)
def test_malformed_input_raises_typed_errors(make: object, code: str) -> None:
    with pytest.raises(LedgerInputError) as caught:
        make()  # type: ignore[operator]
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"half_day_value": D("0.25")}, "INVALID_HALF_DAY_VALUE"),
        ({"half_day_value": 0.5}, "INVALID_NUMBER"),
        ({"attendance_enabled": 1}, "INVALID_FLAG"),
        ({"unmarked_day": "IGNORE"}, "INVALID_CHOICE"),
        ({"full_day_min_hours": D(4), "half_day_min_hours": D(5)}, "INVALID_HOURS"),
        ({"standard_hours_per_day": D(0)}, "INVALID_HOURS"),
        ({"paid_holiday_types": frozenset({"OPTIONAL_NOT_AVAILED"})}, "INVALID_CHOICE"),
    ],
)
def test_invalid_config_raises_typed_errors(kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(LedgerConfigError) as caught:
        LedgerConfig(**kwargs)  # type: ignore[arg-type]
    assert caught.value.code == code


def test_half_day_value_is_canonical() -> None:
    assert LedgerConfig(half_day_value="0.50").half_day_value == D("0.5")
    assert str(LedgerConfig(half_day_value=1).half_day_value) == "1"


# --- properties ------------------------------------------------------------------------------------------------------

_treatments = st.sampled_from([PayTreatment.FULL, PayTreatment.UNPAID, PayTreatment.PARTIAL, PayTreatment.TIERED,
                               PayTreatment.STATUTORY])


_TIERED_HISTORY = L.TieredPay(SICK_TIERS, D(9), ((dt.date(2026, 2, 12), D(3)),))


@st.composite
def _leave(draw: st.DrawFn) -> LeavePortion:
    treatment = draw(_treatments)
    extra: dict[str, object] = {}
    if treatment is PayTreatment.PARTIAL:
        extra["partial_percent"] = D(draw(st.integers(0, 100)))
    if treatment is PayTreatment.TIERED:
        extra["tiered"] = _TIERED_HISTORY  # one leave type has one rolling-window history per ledger
    code = f"type-{treatment.value}"
    return LeavePortion(code, code, draw(st.sampled_from(list(Portion))), treatment,
                        counts_non_working_days=draw(st.booleans()), **extra)  # type: ignore[arg-type]


@st.composite
def _attendance(draw: st.DrawFn) -> AttendanceInput | None:
    status = draw(st.sampled_from([None, *AttendanceStatus]))
    if status is None:
        return None
    if draw(st.booleans()) and status is not AttendanceStatus.ABSENT:
        return AttendanceInput(status, session_id="s", closed_without_punch_out=True)
    return AttendanceInput(status, session_id="s", worked_seconds=draw(st.integers(0, 12 * 3600)),
                           late=draw(st.booleans()), early_exit=draw(st.booleans()))


@st.composite
def _day(draw: st.DrawFn, when: dt.date) -> DayInput:
    leaves = draw(st.lists(_leave(), max_size=1))
    return DayInput(
        when,
        employed=draw(st.booleans()) or draw(st.booleans()),
        suspended_unpaid=draw(st.integers(0, 9)) == 0,
        leaves=tuple(leaves),
        holiday=draw(st.sampled_from([None, None, *(HolidayInput(1, t) for t in HolidayType)])),
        weekly_off=draw(st.integers(0, 6)) == 0,
        manual_lop=tuple(draw(st.lists(st.sampled_from([ManualLop("m", p) for p in Portion]), max_size=1))),
        attendance=draw(_attendance()),
        on_duty=tuple(draw(st.lists(st.sampled_from([L.OnDuty("o", p) for p in Portion]), max_size=1))),
        segment=draw(st.sampled_from(["A", "B"])),
    )


_configs = st.builds(
    LedgerConfig,
    attendance_enabled=st.booleans(),
    leave_enabled=st.booleans(),
    when_disabled_treat_approved_leave_as=st.sampled_from(list(L.LeaveDisabledTreatment)),
    half_day_value=st.sampled_from([D(0), D("0.5"), D(1)]),
    unmarked_day=st.sampled_from(list(L.UnmarkedDayPolicy)),
    missing_punch_out=st.sampled_from(list(L.MissingPunchOutPolicy)),
    derive_status_from_hours=st.booleans(),
    count_on_duty_as_present=st.booleans(),
    sandwich_rule=st.sampled_from(list(L.SandwichRule)),
    zero_paid_days=st.sampled_from(list(L.ZeroPaidDaysPolicy)),
    employee_is_salaried=st.booleans(),
    non_salaried_holiday_pay=st.sampled_from(list(L.NonSalariedHolidayPay)),
)


@st.composite
def _ledger_inputs(draw: st.DrawFn) -> tuple[list[DayInput], LedgerConfig, Decimal]:
    length = draw(st.integers(1, 21))
    days = sorted((draw(_day(d(i))) for i in range(length)), key=lambda day: day.date)
    # Segments must be contiguous blocks in date order: A first, then B.
    split = draw(st.integers(0, length))
    days = [dataclasses.replace(day, segment="A" if index < split else "B") for index, day in enumerate(days)]
    return days, draw(_configs), D(draw(st.integers(0, 12))) / 2


@settings(max_examples=250, deadline=None)
@given(_ledger_inputs())
def test_property_each_slot_is_classified_exactly_once(inputs: tuple[list[DayInput], LedgerConfig, Decimal]) -> None:
    """Contract §21.2: every date has exactly an AM and a PM slot with one classification; nothing counts twice."""
    days, cfg, penalty = inputs
    ledger = build_ledger(days, cfg, penalty_lop_days=penalty)
    assert [r.date for r in ledger.days] == [day.date for day in days]
    for record, day in zip(ledger.days, days):
        assert (record.am.slot, record.pm.slot) == (Slot.AM, Slot.PM)
        for slot in record.slots:
            assert isinstance(slot.classification, C)
            assert slot.paid >= 0 and slot.lop >= 0 and slot.paid + slot.lop <= 1
            assert (slot.classification is C.NOT_EMPLOYED) == (not day.employed)
            if slot.pending:
                assert slot.paid == slot.lop == 0
            elif day.employed and cfg.employee_is_salaried:
                assert slot.paid + slot.lop == 1
            assert slot.source is not None
    totals = ledger.totals
    assert totals.paid_days + totals.slot_lop_days + totals.pending_days + totals.unpaid_days == totals.employment_days
    assert totals.paid_days + totals.lop_days <= totals.employment_days + penalty
    assert totals.penalty_lop_days <= penalty and totals.penalty_lop_days <= totals.paid_working_days
    for name in ("employment_days", "paid_days", "lop_days", "pending_days", "worked_hours", "payable_hours",
                 "worked_seconds", "night_hours", "late_marks"):
        assert sum((getattr(seg, name) for seg in ledger.segments.values()), D(0)) == getattr(totals, name), name
    assert ledger.guard.triggered == any(e.code is ExceptionCode.ZERO_PAID_WORKING_DAYS for e in ledger.exceptions)
    json.dumps(ledger.to_dict())
    ledger.unit_facts()


# --- review additions: negative and boundary cases per edge case, regressions ------------------------------------------


def test_ec_att_02_closed_session_marked_half_day_still_follows_the_missing_punch_out_policy() -> None:
    """EC-ATT-02: a session closed without a punch-out follows missing_punch_out whatever status it carries."""
    open_half = AttendanceInput(AttendanceStatus.HALF_DAY, session_id="s-8", closed_without_punch_out=True)
    ledger = one(ON, attendance=open_half)
    assert kinds(ledger.days[0]) == (C.MISSING_PUNCH_OUT, C.MISSING_PUNCH_OUT)
    assert codes(ledger) == [ExceptionCode.MISSING_PUNCH_OUT] and ledger.totals.lop_days == 0
    full = one(dataclasses.replace(ON, missing_punch_out=L.MissingPunchOutPolicy.FULL_DAY), attendance=open_half)
    assert kinds(full.days[0]) == (C.PRESENT, C.PRESENT) and full.totals.paid_days == 1


def test_ec_att_02_completed_half_day_session_is_not_a_missing_punch_out() -> None:
    """EC-ATT-02 (negative): a punched-out half-day session is an ordinary half day, no review item."""
    ledger = one(ON, attendance=half())
    assert kinds(ledger.days[0]) == (C.HALF_DAY, C.PRESUMED_PRESENT) and ledger.exceptions == ()


def test_ec_att_03_half_day_value_zero_still_pays_the_attended_half_next_to_leave() -> None:
    """EC-ATT-03 (boundary): even with half_day_value = 0, one slot leave + one slot attended → no LOP."""
    cfg = dataclasses.replace(ON_ON, half_day_value=D(0))
    ledger = one(cfg, leaves=(leave(portion=Portion.FIRST_HALF),), attendance=half())
    assert kinds(ledger.days[0]) == (C.LEAVE, C.HALF_DAY)
    assert (ledger.totals.paid_days, ledger.totals.lop_days) == (1, 0)
    alone = one(cfg, attendance=half())
    assert (alone.totals.paid_days, alone.totals.lop_days) == (0, 1)


def test_ec_att_03_half_day_next_to_manual_lop_loses_only_the_lop_half() -> None:
    """EC-ATT-03 (negative): the other half is manual LOP, so exactly half a day is LOP, never more."""
    ledger = one(dataclasses.replace(ON, half_day_value=D(0)), attendance=half(),
                 manual_lop=(ManualLop("m-1", Portion.SECOND_HALF),))
    assert kinds(ledger.days[0]) == (C.HALF_DAY, C.MANUAL_LOP) and ledger.totals.lop_days == D("0.5")


def test_ec_att_07_overnight_session_on_a_weekly_off_start_date_stays_a_weekly_off() -> None:
    """EC-ATT-07 (boundary): a Sunday 22:00 start belongs to Sunday: hours recorded there, Monday untouched."""
    ledger = build_ledger([DayInput(d(6), weekly_off=True, attendance=present(H8, night_seconds=6 * 3600)),
                           DayInput(d(7), attendance=present())], ON)
    assert kinds(ledger.days[0]) == (C.WEEKLY_OFF, C.WEEKLY_OFF) and ledger.days[0].night_seconds == 6 * 3600
    assert ledger.days[1].worked_seconds == H8 and ledger.days[1].night_seconds == 0


def test_ec_att_08_sessions_differing_only_in_identity_pay_the_same() -> None:
    """EC-ATT-08 (negative): sessions flagged differently upstream reach the ledger identically and pay the same."""
    flagged = AttendanceInput(AttendanceStatus.PRESENT, session_id="gps-gap", worked_seconds=H8, content_hash="h1")
    clean = AttendanceInput(AttendanceStatus.PRESENT, session_id="clean", worked_seconds=H8, content_hash="h2")
    a, b = one(ON, attendance=flagged), one(ON, attendance=clean)
    assert a.totals == b.totals and kinds(a.days[0]) == kinds(b.days[0])


def test_ec_att_12_twenty_three_hour_day_and_seconds_are_kept_exactly() -> None:
    """EC-ATT-12 (boundary): a 23-hour DST day is exact; odd seconds keep their exact integer count."""
    short = one(ON, attendance=present(23 * 3600))
    assert short.totals.worked_hours == 23 and short.totals.worked_seconds == 23 * 3600
    odd = one(ON, attendance=present(1))
    assert odd.totals.worked_seconds == 1 and odd.totals.worked_hours == L.hours_of(1) == D("0.0002777778")


def test_ec_att_17_zero_net_seconds_are_not_replaced_by_presumed_hours() -> None:
    """EC-ATT-17 (negative): a completed session with zero net time pays zero hours; standard hours are not presumed."""
    ledger = one(ON, attendance=present(0))
    assert ledger.days[0].hours_recorded and ledger.totals.payable_hours == 0 and ledger.totals.presumed_hours == 0


def test_ec_att_18_punches_never_change_pay_when_attendance_is_off() -> None:
    """EC-ATT-18 / 3.4 §6 When OFF: session hours do not reach worked or payable hours; presence is presumed."""
    ledger = build_ledger([DayInput(MON, attendance=present(3 * 3600, night_seconds=3600)),
                           DayInput(d(1), attendance=present(11 * 3600))], LEAVE_ONLY)
    assert (ledger.totals.worked_hours, ledger.totals.night_hours) == (0, 0)
    assert ledger.totals.payable_hours == 16 and ledger.totals.presumed_hours == 16
    assert not any(record.hours_recorded for record in ledger.days)


def test_ec_att_18_leave_off_too_means_unpaid_leave_changes_nothing() -> None:
    """EC-ATT-18 (negative) / 3.1 §6.1 OFF×OFF: with leave OFF as well, unpaid leave does not reduce pay."""
    ledger = one(LedgerConfig(), leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),))
    assert kinds(ledger.days[0]) == (C.PRESUMED_PRESENT, C.PRESUMED_PRESENT) and ledger.totals.lop_days == 0


def test_ec_att_19_absence_and_unpaid_leave_on_different_days_count_separately() -> None:
    """EC-ATT-19 (negative): on two different days (3.4 §17.5 Thursday/Friday) each counts once: 2 LOP days."""
    ledger = build_ledger([DayInput(MON, attendance=absent()),
                           DayInput(d(1), leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),))], ON_ON)
    assert ledger.totals.lop_days == 2 and (ledger.totals.absent_days, ledger.totals.leave_days_unpaid) == (1, 1)


def test_ec_att_24_late_within_grace_carries_no_mark_and_an_unregularised_mark_stays_chargeable() -> None:
    """EC-ATT-24: within grace the session is not late (no mark); an unregularised late mark is not excused."""
    ledger = build_ledger([DayInput(MON, attendance=present(late=False)),
                           DayInput(d(1), attendance=present(late=True))], ON)
    assert (ledger.totals.late_marks, ledger.totals.regularised_late_marks) == (1, 0)
    assert ledger.days[0].late_mark is False and ledger.days[1].late_mark_regularised is False


def test_ec_lv_06_half_day_leave_on_a_working_day_is_consumed() -> None:
    """EC-LV-06 (negative): the same half-day leave on a working day is a LEAVE slot."""
    ledger = one(ON_ON, leaves=(leave(portion=Portion.FIRST_HALF),), attendance=half())
    assert ledger.days[0].am.classification is C.LEAVE and ledger.totals.leave_days_paid == D("0.5")
    assert ExceptionCode.LEAVE_ON_NON_WORKING_DAY not in codes(ledger)


def test_ec_lv_09_leave_on_the_last_day_of_a_period_counts_only_there() -> None:
    """EC-LV-09 (boundary): a leave day dated on the period's last date stays in that period only."""
    unpaid = leave("unpaid", treatment=PayTreatment.UNPAID, leave_id=56)
    january = build_ledger([DayInput(dt.date(2026, 1, 31), leaves=(unpaid,))], LEAVE_ONLY)
    february = build_ledger([DayInput(dt.date(2026, 2, 2))], LEAVE_ONLY)
    assert (january.totals.lop_days, february.totals.lop_days) == (1, 0)


def test_ec_lv_10_paid_leave_across_a_weekly_off_does_not_consume_the_sunday() -> None:
    """EC-LV-10 (negative): FULL leave Saturday-Monday: Sunday stays a paid weekly off and uses no leave."""
    casual = leave()
    ledger = build_ledger([DayInput(d(5), leaves=(casual,)), DayInput(d(6), weekly_off=True, leaves=(casual,)),
                           DayInput(d(7), leaves=(casual,))], LEAVE_ONLY)
    assert kinds(ledger.days[1]) == (C.WEEKLY_OFF, C.WEEKLY_OFF)
    assert (ledger.totals.leave_days_paid, ledger.totals.weekly_offs, ledger.totals.lop_days) == (2, 1, 0)


def test_ec_lv_12_full_pay_leave_is_salary_and_raises_no_statutory_item() -> None:
    """EC-LV-12 (negative): an ordinary FULL leave is paid in salary; no statutory line is requested."""
    ledger = one(ON_ON, leaves=(leave("earned"),))
    assert ledger.totals.leave_days_paid == 1 and ledger.totals.leave_days_statutory == 0
    assert ExceptionCode.STATUTORY_LEAVE not in codes(ledger) and not ledger.guard.triggered


def test_ec_lv_13_leave_after_the_exit_date_is_reported_and_never_paid_or_deducted() -> None:
    """EC-LV-13: leave dated after exit is outside employment: reported, no leave days, no LOP."""
    days = [DayInput(MON, attendance=present()),
            DayInput(d(1), employed=False, leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),)),
            DayInput(d(2), employed=False, leaves=(leave(),))]
    ledger = build_ledger(days, ON_ON)
    assert [kinds(r) for r in ledger.days[1:]] == [(C.NOT_EMPLOYED, C.NOT_EMPLOYED)] * 2
    assert (ledger.totals.leave_days_paid, ledger.totals.leave_days_unpaid, ledger.totals.lop_days) == (0, 0, 0)
    assert codes(ledger) == [ExceptionCode.INPUT_OUTSIDE_EMPLOYMENT] * 2
    assert L.manual_lop_conflicts(days[1], Portion.FULL, ON_ON)[0].detail == "NOT_EMPLOYED"


def test_ec_lv_13_leave_on_the_exit_date_itself_counts() -> None:
    """EC-LV-13 (boundary): the exit date is still employed, so leave on it is an ordinary leave day."""
    ledger = build_ledger([DayInput(MON, leaves=(leave("unpaid", treatment=PayTreatment.UNPAID),)),
                           DayInput(d(1), employed=False)], ON_ON)
    assert ledger.totals.leave_days_unpaid == 1 and codes(ledger) == [ExceptionCode.ZERO_PAID_WORKING_DAYS]


def test_segments_must_be_contiguous_blocks() -> None:
    """3.4 §5.4: a segment is one date range; a label that resumes after another segment is refused."""
    days = [DayInput(day, segment=label) for day, label in zip(dates_between(MON, d(2)), ("A", "B", "A"))]
    with pytest.raises(LedgerInputError) as caught:
        build_ledger(days, LedgerConfig())
    assert caught.value.code == "NON_CONTIGUOUS_SEGMENT"
