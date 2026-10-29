"""JC-AS-10 (G-16, IG-AS-94): attendance-window time facts mapped onto pay-period dates (``cycle_ledger.map_window``).

GV-AS-PAY-03 shape: November period 1–30 Nov with a 26th–25th cycle (window 26 Oct – 25 Nov). 1–25 Nov keep their
own facts; 26–31 Oct are re-dated onto 26–30 Nov in order (5 period dates for 6 window dates: 31 Oct overflows);
with a shorter window head the remaining tail dates are presumed present; holidays and weekly offs are never
presumed worked.
"""

from datetime import date, timedelta

from app.payroll import ledger
from app.payroll.services.cycle_ledger import map_window, presumed


def day(d, **kw):
    return ledger.DayInput(date=d, **kw)


def absent(d):
    return day(d, attendance=ledger.AttendanceInput(status="ABSENT", rules_mode="STANDARD"))


def present(d):
    return day(d, attendance=ledger.AttendanceInput(status="PRESENT", rules_mode="STANDARD"))


def span(start, end):
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def test_identity_when_window_equals_period():
    dates = span(date(2026, 11, 1), date(2026, 11, 30))
    by_date = {d: present(d) for d in dates}
    mapping = map_window(by_date, (dates[0], dates[-1]), (dates[0], dates[-1]))
    assert [d.date for d in mapping.days] == dates and mapping.overflow == ()
    assert all(mapping.source_dates[d] == d for d in dates)


def test_gv_as_pay_03_26th_to_25th_cycle_redates_the_window_head_onto_the_period_tail():
    window = (date(2026, 10, 26), date(2026, 11, 25))
    period = (date(2026, 11, 1), date(2026, 11, 30))
    by_date = {d: present(d) for d in span(date(2026, 10, 26), date(2026, 11, 30))}
    by_date[date(2026, 10, 28)] = absent(date(2026, 10, 28))      # absent in the window head
    by_date[date(2026, 11, 27)] = absent(date(2026, 11, 27))      # December's cycle: not this run
    mapping = map_window(by_date, period, window)
    days = {d.date: d for d in mapping.days}
    assert sorted(days) == span(*period)
    assert mapping.source_dates[date(2026, 11, 26)] == date(2026, 10, 26)
    assert mapping.source_dates[date(2026, 11, 28)] == date(2026, 10, 28)
    assert days[date(2026, 11, 28)].attendance.status is ledger.AttendanceStatus.ABSENT  # the 28 Oct absence
    assert days[date(2026, 11, 27)].attendance.status is ledger.AttendanceStatus.PRESENT  # 27 Oct, not 27 Nov
    assert mapping.source_dates[date(2026, 11, 10)] == date(2026, 11, 10)
    assert mapping.overflow == (date(2026, 10, 31),)  # 6 window-head dates, 5 period-tail dates


def test_period_dates_keep_employment_and_segment_while_facts_move():
    window = (date(2026, 10, 30), date(2026, 11, 28))
    period = (date(2026, 11, 1), date(2026, 11, 30))
    by_date = {d: present(d) for d in span(date(2026, 10, 30), date(2026, 11, 30))}
    by_date[date(2026, 11, 29)] = day(date(2026, 11, 29), employed=False, segment="A1-C2")
    by_date[date(2026, 10, 30)] = day(date(2026, 10, 30), weekly_off=True)
    mapping = map_window(by_date, period, window)
    moved = {d.date: d for d in mapping.days}[date(2026, 11, 29)]
    assert moved.employed is False and moved.segment == "A1-C2" and moved.weekly_off is True  # facts of 30 Oct


def test_tail_without_window_dates_is_presumed_present_except_days_off():
    window = (date(2026, 10, 31), date(2026, 11, 27))
    period = (date(2026, 11, 1), date(2026, 11, 30))
    by_date = {d: absent(d) for d in span(date(2026, 10, 31), date(2026, 11, 30))}
    by_date[date(2026, 11, 29)] = day(date(2026, 11, 29), weekly_off=True)
    mapping = map_window(by_date, period, window)
    days = {d.date: d for d in mapping.days}
    assert mapping.source_dates[date(2026, 11, 28)] == date(2026, 10, 31)
    assert mapping.source_dates[date(2026, 11, 30)] is None
    assert days[date(2026, 11, 30)].attendance.status is ledger.AttendanceStatus.PRESENT
    assert days[date(2026, 11, 29)].attendance is None  # weekly off: not presumed worked
    assert mapping.overflow == ()


def test_presumed_clears_leave_lop_and_on_duty_of_the_next_cycle():
    d = date(2026, 11, 30)
    base = day(d, manual_lop=(ledger.ManualLop(source_id=1, portion="FULL"),))
    result = presumed(base)
    assert result.manual_lop == () and result.leaves == () and result.on_duty == ()
    assert result.attendance.status is ledger.AttendanceStatus.PRESENT
    holiday = presumed(day(d, holiday=ledger.HolidayInput(holiday_id=3, type="PUBLIC")))
    assert holiday.attendance is None


def test_as_pay_17_leap_february_26th_to_25th_window():
    """February 2028 period 1-29 Feb, window 26 Jan - 25 Feb: 29 Feb belongs to the March window; the 6 window-head
    dates (26-31 Jan) meet 4 period-tail dates (26-29 Feb), so 30 and 31 Jan overflow (reported, never dropped)."""
    window = (date(2028, 1, 26), date(2028, 2, 25))
    period = (date(2028, 2, 1), date(2028, 2, 29))
    by_date = {d: present(d) for d in span(date(2028, 1, 26), date(2028, 2, 29))}
    mapping = map_window(by_date, period, window)
    assert len(mapping.days) == 29 and mapping.days[-1].date == date(2028, 2, 29)
    assert mapping.source_dates[date(2028, 2, 29)] == date(2028, 1, 29)
    assert mapping.source_dates[date(2028, 2, 25)] == date(2028, 2, 25)
    assert mapping.overflow == (date(2028, 1, 30), date(2028, 1, 31))
