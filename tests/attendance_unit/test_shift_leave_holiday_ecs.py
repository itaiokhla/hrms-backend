"""Shift-owned halves of the leave / holiday cross-module edge cases (pure; no database).

* AS-INT-07 — holiday calendar by ``shift_ids`` vs HOLIDAY_RULE OFF entries: the shift the LH criterion matches on
  (``shift_resolution.criterion_shift_id``, 03-shifts/02 §8). The matcher itself is LH's (04-holiday/01 §4.4.1).
* AS-INT-18 — holiday added over a published WORK entry: the JC-AS-08 approval predicate and the publish preview
  (``roster_rules`` HOLIDAY_WORK: REQUIRE_REASON for planned holiday work, WARN for an unreviewed published entry,
  nothing for one decided at its own publication). Payroll's use of the predicate is payroll's.
* X-INT-58 — Ramadan hours: ``RAMADAN_HOURS`` WARN above the pack's Ramadan normal day (AE 6 h).

DB-level halves: tests/shifts/test_shift_leave_holiday_ecs.py.
"""

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from app.services import roster_rules as rr
from app.services import shift_engine as se
from app.services import shift_resolution

D = dt.date
IST, DUBAI = "Asia/Kolkata", "Asia/Dubai"
SHIFT_A, SHIFT_C = 201, 203
C = se.ShiftVersionData(start_time="22:00", end_time="06:00", shift_id=SHIFT_C)
REPUBLIC_DAY = D(2027, 1, 26)


def entry(kind, shift_id=None, source="MANUAL", planned_shift_id=None):
    return SimpleNamespace(kind=kind, shift_id=shift_id, source=source, planned_shift_id=planned_shift_id)


def work(version, day, zone=IST, **flags):
    return rr.from_shift_instance(se.build_instance(version, day, zone), **flags)


def holiday_work(*instances, mode=rr.HOLIDAY_OFF):
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, IST, tuple(instances)),))
    results = rr.validate(schedule, rr.EmployeeLimits(holiday_mode=mode), rr.RuleContext(holidays={REPUBLIC_DAY}))
    return [(r.severity, r.message_key) for r in results if r.rule_key == rr.HOLIDAY_WORK]


# ------------------------------------------------------------------------------------------------ AS-INT-07
def test_as_int_07_a_holiday_rule_off_matches_shift_ids_on_the_shift_it_replaced():
    """EC AS-INT-07 (shift-owned half): Ravi's rotation token N -> shift C on Republic Day becomes an OFF with source
    HOLIDAY_RULE and planned_shift_id C; the night calendar's ``shift_ids [C]`` keeps matching on C, so publishing the
    OFF never removes its own holiday (no flip-flop). A MANUAL / DAY_OVERRIDE / rotation OFF never matches."""
    placement_shift = SHIFT_A
    assert shift_resolution.criterion_shift_id([entry("OFF", source="HOLIDAY_RULE", planned_shift_id=SHIFT_C)],
                                               placement_shift) == SHIFT_C
    for source in ("MANUAL", "DAY_OVERRIDE", "ROTATION", "SUBSTITUTION"):
        assert shift_resolution.criterion_shift_id([entry("OFF", source=source)], placement_shift) is None
    # Order 1: the published WORK entry on C is the resolved shift (the holiday resolves, preview HOLIDAY_WORK).
    assert shift_resolution.criterion_shift_id([entry("WORK", SHIFT_C)], placement_shift) == SHIFT_C
    # A split day: the first WORK entry decides; custom times without a shift match nothing.
    assert shift_resolution.criterion_shift_id([entry("WORK", None), entry("WORK", SHIFT_C)], 1) is None
    # No entry: the rule / placement shift (steps 2-3); a legacy HOLIDAY_RULE OFF without a planned shift reads as
    # if it were not there (shift_for(..., ignore_sources=[HOLIDAY_RULE])).
    assert shift_resolution.criterion_shift_id([], placement_shift) == placement_shift
    legacy = [entry("OFF", source="HOLIDAY_RULE")]
    assert shift_resolution.criterion_shift_id(legacy, placement_shift) == placement_shift


# ------------------------------------------------------------------------------------------------ AS-INT-18
def test_as_int_18_a_work_entry_published_before_its_holiday_is_not_approved_holiday_work():
    """EC AS-INT-18 (shift-owned predicate of the amended JC-AS-08): Ravi's WORK entry for 18 Nov was published on
    15 Oct; the COMPANY closure became known on 23 Nov, so the entry is not the approval of holiday work. An entry
    published once the holiday was known is (a re-publication after it counts too)."""
    # Golden vector GV-AS-INT-06 of AS 05 §5.3.
    published = dt.datetime(2026, 10, 15, 6, 30, tzinfo=dt.timezone.utc)
    known = dt.datetime(2026, 11, 23, 5, 0, tzinfo=dt.timezone.utc)
    assert rr.entry_approves_holiday_work(published, known) is False
    assert rr.entry_approves_holiday_work(known, known) is True
    assert rr.entry_approves_holiday_work(known + dt.timedelta(minutes=1), known) is True
    assert rr.entry_approves_holiday_work(None, known) is False
    assert rr.entry_approves_holiday_work(published, None) is True


def test_as_int_18_the_preview_shows_holiday_work_until_the_planner_decides():
    """EC AS-INT-18 (03-shifts/02 §8): under HOLIDAY_OFF a WORK entry this publication plans on a full holiday needs
    a reason; a published entry the holiday arrived after shows HOLIDAY_WORK as a WARN (nobody decided it yet, and
    an unrelated publication is not forced to answer for it); an entry decided at its own publication is not asked
    again. WORK_THROUGH never raises it."""
    planned = work(C, REPUBLIC_DAY, changed=True)
    unchanged = work(C, REPUBLIC_DAY)
    assert holiday_work(planned) == [(rr.REQUIRE_REASON, "roster.rule.holiday_work")]
    assert holiday_work(replace(unchanged, holiday_unreviewed=True)) == [
        (rr.WARN, "roster.rule.holiday_work.unreviewed")]
    assert holiday_work(unchanged) == []
    assert holiday_work(replace(unchanged, holiday_unreviewed=True), mode=rr.WORK_THROUGH) == []
    # A raised severity still applies (SCHEDULING_POLICY.rule_severity raises only).
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, IST, (replace(unchanged, holiday_unreviewed=True),)),))
    raised = rr.EmployeeLimits(rule_severity={rr.HOLIDAY_WORK: rr.REQUIRE_REASON})
    assert [r.severity for r in rr.validate(schedule, raised, rr.RuleContext(holidays={REPUBLIC_DAY}))
            if r.rule_key == rr.HOLIDAY_WORK] == [rr.REQUIRE_REASON]


# ------------------------------------------------------------------------------------------------ X-INT-58
RAMADAN = rr.ReducedHoursWindow("RAMADAN_2027", D(2027, 2, 8), D(2027, 3, 9), Decimal(6), verified=False)
BC12N = se.ShiftVersionData(start_time="19:00", end_time="07:00", shift_id=321)  # 12 h, paid rest
BD9_RAMADAN = se.ShiftVersionData(start_time="10:00", end_time="16:00", shift_id=311)  # no break: 360


def ramadan_results(*instances):
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, DUBAI, tuple(instances)),))
    return [r for r in rr.validate(schedule, rr.EmployeeLimits(reduced_hours=(RAMADAN,)), rr.RuleContext())
            if r.rule_key == rr.RAMADAN_HOURS]


def test_x_int_58_twelve_hours_inside_ramadan_warn_ramadan_hours():
    """EC X-INT-58 (shift-owned half): Maria on BC12N (12 h) inside the AE pack's Ramadan dates raises RAMADAN_HOURS
    WARN (scheduled 12 h above the Ramadan normal 8 - 2 = 6 h; the DRAFT pack reads "not verified"); Layla on the
    Ramadan version 10:00-16:00 (360) does not, nor does a 12-h night before the window."""
    # Golden vector GV-AS-CTY-06 of appendix-a.
    maria = ramadan_results(work(BC12N, D(2027, 2, 10), DUBAI, changed=True))
    assert [(r.severity, r.dates, r.measured, r.limit, r.unit, r.details) for r in maria] == [
        (rr.WARN, (D(2027, 2, 10),), Decimal("12.00"), Decimal(6), "h", {"window": "RAMADAN_2027"})]
    assert maria[0].message_key == "roster.rule.ramadan_hours.not_verified"
    assert ramadan_results(work(BD9_RAMADAN, D(2027, 2, 10), DUBAI, changed=True)) == []
    assert ramadan_results(work(BC12N, D(2027, 2, 7), DUBAI, changed=True)) == []
    assert ramadan_results(work(BC12N, D(2027, 3, 9), DUBAI, changed=True)) != []  # the last date is inside


def test_x_int_58_the_ae_pack_ramadan_window_reaches_the_rule():
    """EC X-INT-58: ``roster_validation`` turns the AE pack's dated reduced-hours window into the RAMADAN_HOURS input
    (normal day 6 h, WARN while the pack is DRAFT); a pack without one (IN) gives none."""
    from app.services import roster_validation
    from app.services.working_time_packs import PACKS

    assert roster_validation._reduced_hours(PACKS["AE"], False) == (
        rr.ReducedHoursWindow("RAMADAN_2027", D(2027, 2, 8), D(2027, 3, 9), Decimal(6), verified=False),)
    assert roster_validation._reduced_hours(PACKS["IN"], False) == ()
