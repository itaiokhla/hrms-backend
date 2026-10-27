"""Holiday recurrence engine (04-holiday/02 §3-§7): golden vectors GV-REC-* of §11 and edge cases LH-REC-*, LH-HOL-*."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from app.services import holiday_recurrence as R
from app.services.holiday_recurrence import Occurrence, RecurrenceError, RuleSpec

D = dt.date
SAT_SUN = frozenset({5, 6})
SUN = frozenset({6})
FRI_SAT = frozenset({4, 5})
FRI_SUN = frozenset({4, 5, 6})
MON, TUE, THU = 0, 1, 3


def rule(kind: str, params: dict, name: str = "Holiday", rid: int | None = 1, **fields) -> RuleSpec:
    fields.setdefault("valid_from_year", 2020)
    return RuleSpec(kind=kind, params=params, name=name, id=rid, **fields)


def fixed(month: int, day: int, name: str = "Holiday", rid: int = 1, **fields) -> RuleSpec:
    return rule("FIXED_DATE", {"month": month, "day": day}, name, rid, **fields)


def gen(rules, year, weekend=SAT_SUN, fixed_rows=(), expected=None, **kw) -> R.GenerationResult:
    return R.generate_year(rules, year, weekend, fixed_rows, expected, **kw)


def rows_of(result: R.GenerationResult) -> list[tuple[dt.date, str, str]]:
    return [(r.date, r.observance, r.status) for r in result.rows]


def codes(issues) -> list[str]:
    return [i.code for i in issues]


def errors(issues) -> list[R.Issue]:
    return [i for i in issues if i.severity == "error"]


# --------------------------------------------------------------------------- golden vectors (§11)


def test_gv_rec_01_fixed_date_republic_day() -> None:
    assert rows_of(gen([fixed(1, 26, "Republic Day")], 2027, SUN)) == [(D(2027, 1, 26), "ORIGINAL", "CONFIRMED")]


def test_gv_rec_02_feb_29_skip() -> None:
    leap = rule("FIXED_DATE", {"month": 2, "day": 29, "missing_date": "SKIP"}, "Leap day party")
    common = gen([leap], 2027)
    assert common.rows == ()
    assert [n.message for n in common.notes if n.code == "DATE_SKIPPED"] == ["No 29 Feb in 2027"]
    assert rows_of(gen([leap], 2028)) == [(D(2028, 2, 29), "ORIGINAL", "CONFIRMED")]


def test_gv_rec_03_feb_29_last_day_of_month() -> None:
    leap = rule("FIXED_DATE", {"month": 2, "day": 29, "missing_date": "LAST_DAY_OF_MONTH"})
    assert [r.date for r in gen([leap], 2027).rows] == [D(2027, 2, 28)]


def test_gv_rec_04_feb_29_next_day() -> None:
    leap = rule("FIXED_DATE", {"month": 2, "day": 29, "missing_date": "NEXT_DAY"})
    assert [r.date for r in gen([leap], 2027).rows] == [D(2027, 3, 1)]


def test_gv_rec_05_fifth_monday() -> None:
    fifth = rule("NTH_WEEKDAY", {"month": 3, "weekday": MON, "nth": 5}, "Spring team day")
    assert [r.date for r in gen([fifth], 2027).rows] == [D(2027, 3, 29)]
    empty = gen([fifth], 2028)
    assert empty.rows == ()
    assert "DATE_SKIPPED" in codes(empty.notes)
    assert "RULE_NTH_5" in codes(R.validate(fifth))


def test_gv_rec_06_nth_weekday_with_offset() -> None:
    day_after = rule("NTH_WEEKDAY", {"month": 11, "weekday": THU, "nth": 4, "offset_days": 1}, "Day after Thanksgiving")
    assert [r.date for r in gen([day_after], 2027).rows] == [D(2027, 11, 26)]


UK_XMAS = [
    fixed(12, 25, "Christmas Day", 1, observed_rule="NEXT_WORKDAY"),
    fixed(12, 26, "Boxing Day", 2, observed_rule="NEXT_WORKDAY"),
]


def test_gv_rec_07_uk_christmas_boxing_chain() -> None:
    result = gen(UK_XMAS, 2027)
    assert rows_of(result) == [
        (D(2027, 12, 25), "ORIGINAL", "CONFIRMED"),
        (D(2027, 12, 26), "ORIGINAL", "CONFIRMED"),
        (D(2027, 12, 27), "OBSERVED", "CONFIRMED"),
        (D(2027, 12, 28), "OBSERVED", "CONFIRMED"),
    ]
    observed = {r.date: r.observed_from_date for r in result.rows if r.observance == "OBSERVED"}
    assert observed == {D(2027, 12, 27): D(2027, 12, 25), D(2027, 12, 28): D(2027, 12, 26)}


def test_gv_rec_08_us_nearest_workday() -> None:
    rules = [
        fixed(6, 19, "Juneteenth", 1, observed_rule="NEAREST_WORKDAY"),
        fixed(7, 4, "Independence Day", 2, observed_rule="NEAREST_WORKDAY"),
    ]
    assert rows_of(gen(rules, 2027)) == [
        (D(2027, 6, 18), "OBSERVED", "CONFIRMED"),
        (D(2027, 6, 19), "ORIGINAL", "CONFIRMED"),
        (D(2027, 7, 4), "ORIGINAL", "CONFIRMED"),
        (D(2027, 7, 5), "OBSERVED", "CONFIRMED"),
    ]


def test_gv_rec_09_one_day_weekend_moves_forward() -> None:
    rows = gen([fixed(8, 15, "Independence Day", observed_rule="NEAREST_WORKDAY")], 2027, SUN).rows
    assert [(r.date, r.observance) for r in rows] == [(D(2027, 8, 15), "ORIGINAL"), (D(2027, 8, 16), "OBSERVED")]


def test_gv_rec_10_friday_saturday_weekend() -> None:
    rules = [fixed(9, 24, "A", 1, observed_rule="NEAREST_WORKDAY"), fixed(9, 25, "B", 2, observed_rule="NEAREST_WORKDAY")]
    assert [(r.date, r.observance) for r in gen(rules, 2027, FRI_SAT).rows] == [
        (D(2027, 9, 23), "OBSERVED"),
        (D(2027, 9, 24), "ORIGINAL"),
        (D(2027, 9, 25), "ORIGINAL"),
        (D(2027, 9, 26), "OBSERVED"),
    ]


NEW_YEAR = fixed(1, 1, "New Year's Day", 7, observed_rule="NEAREST_WORKDAY")


def test_gv_rec_11_new_year_observed_in_previous_year() -> None:
    y2027 = gen([NEW_YEAR], 2027)
    assert [(r.date, r.observance, r.rule_year) for r in y2027.rows] == [
        (D(2027, 1, 1), "ORIGINAL", 2027),
        (D(2027, 12, 31), "OBSERVED", 2028),
    ]
    assert y2027.cross_year_items == ()
    y2028 = gen([NEW_YEAR], 2028)
    assert [(r.date, r.observance) for r in y2028.rows] == [(D(2028, 1, 1), "ORIGINAL")]


def test_gv_rec_12_orthodox_good_friday() -> None:
    good_friday = rule("EASTER_OFFSET", {"offset_days": -2, "tradition": "ORTHODOX"}, "Orthodox Good Friday")
    assert [r.date for r in gen([good_friday], 2027).rows] == [D(2027, 4, 30)]


def test_gv_rec_13_shrove_tuesday_on_leap_day() -> None:
    shrove = rule("EASTER_OFFSET", {"offset_days": -47, "tradition": "WESTERN"}, "Shrove Tuesday")
    assert [r.date for r in gen([shrove], 2028).rows] == [D(2028, 2, 29)]


def test_gv_rec_14_weekday_strictly_after() -> None:
    election = rule("WEEKDAY_RELATIVE", {"month": 11, "day": 1, "weekday": TUE, "direction": "AFTER"}, "Election Day")
    assert [r.date for r in gen([election], 2028).rows] == [D(2028, 11, 7)]
    assert [r.date for r in gen([election], 2033).rows] == [D(2033, 11, 8)]


def test_gv_rec_15_weekday_strictly_before() -> None:
    victoria = rule("WEEKDAY_RELATIVE", {"month": 5, "day": 25, "weekday": MON, "direction": "BEFORE"}, "Victoria Day")
    assert [r.date for r in gen([victoria], 2026).rows] == [D(2026, 5, 18)]


def test_gv_rec_16_one_off_shutdown_block() -> None:
    shutdown = rule(
        "ONE_OFF", {"date": "2027-12-27"}, "Annual shutdown", duration_days=5, category="SHUTDOWN",
        valid_from_year=2027, valid_to_year=2027,
    )
    rows = gen([shutdown], 2027).rows
    assert [(r.date, r.sequence_index) for r in rows] == [(D(2027, 12, 27 + i), i + 1) for i in range(5)]
    assert {r.category for r in rows} == {"SHUTDOWN"}
    assert gen([shutdown], 2028).rows == ()


def test_gv_rec_17_two_day_block_on_weekend_not_moved() -> None:
    national = fixed(12, 2, "National Day", duration_days=2)
    rows = gen([national], 2028).rows
    assert [(r.date, r.sequence_index, r.observance) for r in rows] == [
        (D(2028, 12, 2), 1, "ORIGINAL"),
        (D(2028, 12, 3), 2, "ORIGINAL"),
    ]


def _coinciding(flag: bool) -> list[RuleSpec]:
    return [
        fixed(1, 26, "Republic Day", 1, holiday_type="PUBLIC"),
        rule("FIXED_DATE", {"month": 1, "day": 26, "observe_if_coincides": flag}, "Founders' Day", 2),
    ]


def test_gv_rec_18_observe_if_coincides() -> None:
    moved = gen(_coinciding(True), 2027, SUN).rows
    assert [(r.date, r.name, r.observance) for r in moved] == [
        (D(2027, 1, 26), "Republic Day", "ORIGINAL"),
        (D(2027, 1, 26), "Founders' Day", "ORIGINAL"),
        (D(2027, 1, 27), "Founders' Day", "OBSERVED"),
    ]
    kept = gen(_coinciding(False), 2027, SUN).rows
    assert [(r.date, r.name, r.holiday_type) for r in kept] == [
        (D(2027, 1, 26), "Republic Day", "PUBLIC"),
        (D(2027, 1, 26), "Founders' Day", "COMPANY"),
    ]


EID = rule("ANNUAL_CONFIRM", {"hint": "1 Shawwal"}, "Eid al-Fitr", 9, duration_days=3, duration_range=(3, 4))


def test_gv_rec_19_annual_confirm_expected_start() -> None:
    missing = gen([EID], 2027)
    assert missing.rows == ()
    assert [(n.code, n.severity) for n in missing.notes] == [("HOLIDAY_DATE_NEEDED", "warning")]
    confirmed = gen([EID], 2027, expected={(9, 2027): D(2027, 3, 9)})
    assert rows_of(confirmed) == [(D(2027, 3, 9 + i), "ORIGINAL", "PROVISIONAL") for i in range(3)]


SHUTDOWN_ROW = Occurrence(date=D(2027, 12, 27), name="Year-end shutdown")


def test_gv_rec_20_chain_respects_fixed_manual_row() -> None:
    observed = [(r.date, r.name) for r in gen(UK_XMAS, 2027, fixed_rows=[SHUTDOWN_ROW]).rows if r.observance == "OBSERVED"]
    assert observed == [(D(2027, 12, 28), "Christmas Day"), (D(2027, 12, 29), "Boxing Day")]


def test_gv_rec_21_generation_is_deterministic() -> None:
    """GV-REC-21 (pure part): the same inputs give the same rows, so reconciliation inserts/updates/deletes nothing."""
    rules = [*UK_XMAS, NEW_YEAR, EID]
    expected = {(9, 2027): D(2027, 3, 9)}
    first, second = gen(rules, 2027, expected=expected), gen(list(reversed(rules)), 2027, expected=expected)
    assert first == second
    assert len({r.occurrence_key for r in first.rows if r.rule_id == 1}) == 2


def test_gv_rec_23_cross_year_item_for_legacy_year() -> None:
    legacy_2027 = [Occurrence(date=D(2027, 1, 1), name="New Year's Day", rule_id=7, rule_year=2027, occurrence_key="2027:1:O")]
    result = gen([NEW_YEAR], 2028, fixed_rows=legacy_2027)
    assert [(r.date, r.observance) for r in result.rows] == [(D(2028, 1, 1), "ORIGINAL")]
    assert [(r.date, r.rule_year, r.occurrence_key) for r in result.cross_year_items] == [
        (D(2027, 12, 31), 2028, "2028:1:B")
    ]
    published_2027 = [*legacy_2027, Occurrence(
        date=D(2027, 12, 31), name="New Year's Day", observance="OBSERVED", observed_from_date=D(2028, 1, 1),
        rule_id=7, rule_year=2028, occurrence_key="2028:1:B",
    )]
    assert gen([NEW_YEAR], 2028, fixed_rows=published_2027).cross_year_items == ()


EASTER_TABLE = [
    (2000, D(2000, 4, 23), D(2000, 4, 30)),
    (2008, D(2008, 3, 23), D(2008, 4, 27)),
    (2011, D(2011, 4, 24), D(2011, 4, 24)),
    (2024, D(2024, 3, 31), D(2024, 5, 5)),
    (2025, D(2025, 4, 20), D(2025, 4, 20)),
    (2026, D(2026, 4, 5), D(2026, 4, 12)),
    (2027, D(2027, 3, 28), D(2027, 5, 2)),
    (2028, D(2028, 4, 16), D(2028, 4, 16)),
    (2029, D(2029, 4, 1), D(2029, 4, 8)),
    (2030, D(2030, 4, 21), D(2030, 4, 28)),
    (2038, D(2038, 4, 25), D(2038, 4, 25)),
]


@pytest.mark.parametrize(("year", "western", "orthodox"), EASTER_TABLE, ids=[f"gv_rec_24-{y}" for y, *_ in EASTER_TABLE])
def test_gv_rec_24_easter_table(year: int, western: dt.date, orthodox: dt.date) -> None:
    assert R._easter(year, "WESTERN") == western
    assert R._easter(year, "ORTHODOX") == orthodox


@pytest.mark.parametrize(
    ("offset", "dates"),
    [
        (-47, ("02-17", "02-09", "02-29", "02-13", "03-05")),
        (-3, ("04-02", "03-25", "04-13", "03-29", "04-18")),
        (-2, ("04-03", "03-26", "04-14", "03-30", "04-19")),
        (1, ("04-06", "03-29", "04-17", "04-02", "04-22")),
        (39, ("05-14", "05-06", "05-25", "05-10", "05-30")),
        (50, ("05-25", "05-17", "06-05", "05-21", "06-10")),
        (60, ("06-04", "05-27", "06-15", "05-31", "06-20")),
    ],
)
def test_easter_common_offsets_2026_2030(offset: int, dates: tuple[str, ...]) -> None:
    easter_rule = rule("EASTER_OFFSET", {"offset_days": offset, "tradition": "WESTERN"})
    got = [R.expand(easter_rule, y, SAT_SUN)[0].date for y in range(2026, 2031)]
    assert got == [D.fromisoformat(f"{y}-{md}") for y, md in zip(range(2026, 2031), dates)]


def test_gv_rec_25_rrule_first_monday_of_june() -> None:
    parts = R.rrule_parse("FREQ=YEARLY;BYMONTH=6;BYDAY=1MO")
    native = R.rrule_to_native(parts, D(2026, 6, 1))
    assert (native.kind, dict(native.params)) == ("NTH_WEEKDAY", {"month": 6, "weekday": MON, "nth": 1})
    assert R.rrule_expand(parts, D(2026, 6, 1), 2027) == [D(2027, 6, 7)]
    imported = rule("RRULE", {"rrule": "FREQ=YEARLY;BYMONTH=6;BYDAY=1MO", "dtstart": "2026-06-01"}, valid_from_year=2026)
    assert errors(R.validate(imported, allow_rrule=True)) == []
    assert [r.date for r in gen([imported], 2027).rows] == [D(2027, 6, 7)]


def test_gv_rec_26_rrule_monthly_rejected() -> None:
    with pytest.raises(RecurrenceError) as exc:
        R.rrule_parse("FREQ=MONTHLY;BYDAY=2SA,4SA")
    assert exc.value.code == "RULE_INVALID"
    assert exc.value.issues[0].reason == "RRULE_UNSUPPORTED"
    assert "work-week" in exc.value.message
    bad = rule("RRULE", {"rrule": "FREQ=MONTHLY;BYDAY=2SA,4SA", "dtstart": "2027-01-01"})
    assert [(i.code, i.reason) for i in errors(R.validate(bad, allow_rrule=True))] == [("RULE_INVALID", "RRULE_UNSUPPORTED")]


def test_gv_rec_27_rrule_rscale_rejected() -> None:
    with pytest.raises(RecurrenceError) as exc:
        R.rrule_parse("RSCALE=HEBREW;FREQ=YEARLY;BYMONTH=7;BYMONTHDAY=1")
    assert exc.value.issues[0].reason == "RRULE_UNSUPPORTED"


@pytest.mark.parametrize(
    ("text", "kind", "params", "expansions"),
    [
        ("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=26", "FIXED_DATE", {"month": 1, "day": 26},
         ("2026-01-26", "2027-01-26", "2028-01-26")),
        ("FREQ=YEARLY;BYMONTH=11;BYDAY=4TH", "NTH_WEEKDAY", {"month": 11, "weekday": THU, "nth": 4},
         ("2026-11-26", "2027-11-25", "2028-11-23")),
        ("FREQ=YEARLY;BYMONTH=11;BYDAY=TH;BYSETPOS=4", "NTH_WEEKDAY", {"month": 11, "weekday": THU, "nth": 4},
         ("2026-11-26", "2027-11-25", "2028-11-23")),
        ("FREQ=YEARLY;BYMONTH=5;BYDAY=-1MO", "NTH_WEEKDAY", {"month": 5, "weekday": MON, "nth": -1},
         ("2026-05-25", "2027-05-31", "2028-05-29")),
        ("FREQ=YEARLY;BYMONTH=11;BYDAY=TU;BYMONTHDAY=2,3,4,5,6,7,8", "WEEKDAY_RELATIVE",
         {"month": 11, "day": 2, "weekday": TUE, "direction": "ON_OR_AFTER"}, ("2026-11-03", "2027-11-02", "2028-11-07")),
        ("FREQ=YEARLY;BYMONTH=5;BYDAY=MO;BYMONTHDAY=18,19,20,21,22,23,24", "WEEKDAY_RELATIVE",
         {"month": 5, "day": 18, "weekday": MON, "direction": "ON_OR_AFTER"}, ("2026-05-18", "2027-05-24", "2028-05-22")),
        ("FREQ=YEARLY;BYEASTER=-2", "EASTER_OFFSET", {"offset_days": -2, "tradition": "WESTERN"},
         ("2026-04-03", "2027-03-26", "2028-04-14")),
    ],
)
def test_rrule_native_mapping_table(text: str, kind: str, params: dict, expansions: tuple[str, ...]) -> None:
    """§7.2 mapping table, expansion 2026 -> 2028."""
    parts = R.rrule_parse(text)
    native = R.rrule_to_native(parts, D(2026, 1, 1))
    assert (native.kind, dict(native.params)) == (kind, params)
    assert [R.rrule_expand(parts, D(2026, 1, 1), y) for y in (2026, 2027, 2028)] == [[D.fromisoformat(e)] for e in expansions]


def test_rrule_leap_day_skips_and_count_until_bound_years() -> None:
    leap = R.rrule_parse("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29")
    assert [y for y in (2026, 2027, 2028) if R.rrule_expand(leap, D(2024, 2, 29), y)] == [2028]
    counted = R.rrule_to_native(R.rrule_parse("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;COUNT=2"), D(2024, 2, 29))
    assert (counted.valid_from_year, counted.valid_to_year) == (2024, 2028)
    until = R.rrule_parse("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=26;UNTIL=20280101")
    assert R.rrule_to_native(until, D(2026, 1, 26)).valid_to_year == 2027
    assert R.rrule_expand(until, D(2026, 1, 26), 2028) == []


@pytest.mark.parametrize(
    "text",
    [
        "FREQ=YEARLY;INTERVAL=2;BYMONTH=1;BYMONTHDAY=1",
        "FREQ=YEARLY;BYMONTH=1,2;BYMONTHDAY=1",
        "FREQ=YEARLY;BYMONTH=5L;BYMONTHDAY=1",
        "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=-1",
        "FREQ=YEARLY;BYMONTH=1;BYDAY=MO,TU",
        "FREQ=YEARLY;BYDAY=1MO",
        "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;COUNT=3;UNTIL=20300101",
        "FREQ=YEARLY;BYYEARDAY=100",
        "FREQ=YEARLY;BYMONTH=1;BYDAY=MO",
        "FREQ=YEARLY;BYEASTER=80",
    ],
)
def test_rrule_outside_subset_rejected(text: str) -> None:
    with pytest.raises(RecurrenceError):
        R.rrule_parse(text)


def test_rrule_kind_only_through_import() -> None:
    imported = rule("RRULE", {"rrule": "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=26", "dtstart": "2026-01-26"})
    assert [i.message for i in errors(R.validate(imported))] == ["Repeating rules in iCalendar format can only be imported."]


def test_us_company_2027_worked_example() -> None:
    """§4.8: 13 rows, 4 OBSERVED, 10 counting dates for a Mon-Fri employee."""
    nearest = {"observed_rule": "NEAREST_WORKDAY", "holiday_type": "PUBLIC"}
    rules = [
        fixed(1, 1, "New Year's Day", 1, **nearest),
        rule("NTH_WEEKDAY", {"month": 1, "weekday": MON, "nth": 3}, "MLK Day", 2, **nearest),
        rule("NTH_WEEKDAY", {"month": 5, "weekday": MON, "nth": -1}, "Memorial Day", 3, **nearest),
        fixed(6, 19, "Juneteenth", 4, **nearest),
        fixed(7, 4, "Independence Day", 5, **nearest),
        rule("NTH_WEEKDAY", {"month": 9, "weekday": MON, "nth": 1}, "Labor Day", 6, **nearest),
        rule("NTH_WEEKDAY", {"month": 11, "weekday": THU, "nth": 4}, "Thanksgiving", 7, **nearest),
        rule("NTH_WEEKDAY", {"month": 11, "weekday": THU, "nth": 4, "offset_days": 1}, "Day after", 8, **nearest),
        fixed(12, 25, "Christmas Day", 9, **nearest),
    ]
    result = gen(rules, 2027)
    assert len(result.rows) == 13
    assert sum(r.observance == "OBSERVED" for r in result.rows) == 4
    counted = sorted(
        {r.date for r in result.rows if r.observance == "OBSERVED"}
        | {r.date for r in result.rows if r.observance == "ORIGINAL" and r.date.weekday() < 5}
    )
    assert [d.strftime("%m-%d") for d in counted] == [
        "01-01", "01-18", "05-31", "06-18", "07-05", "09-06", "11-25", "11-26", "12-24", "12-31",
    ]
    assert result.cross_year_items == ()
    assert [n for n in result.notes if n.severity != "info"] == []


# --------------------------------------------------------------------------- edge cases LH-REC-*


def test_lh_rec_01_feb_29_fallbacks_and_invalid_dates() -> None:
    for missing, expected in (("SKIP", []), ("LAST_DAY_OF_MONTH", [D(2027, 2, 28)]), ("NEXT_DAY", [D(2027, 3, 1)])):
        leap = rule("FIXED_DATE", {"month": 2, "day": 29, "missing_date": missing})
        assert errors(R.validate(leap)) == []
        assert [r.date for r in gen([leap], 2027).rows] == expected
        assert [r.date for r in gen([leap], 2028).rows] == [D(2028, 2, 29)]
    for month, day in ((2, 30), (4, 31)):
        assert [(i.code, i.field) for i in errors(R.validate(fixed(month, day)))] == [("RULE_INVALID", "params.day")]
        with pytest.raises(RecurrenceError):
            R.expand(fixed(month, day), 2027, SAT_SUN)
    wrong_fallback = rule("FIXED_DATE", {"month": 3, "day": 1, "missing_date": "NEXT_DAY"})
    assert [i.field for i in errors(R.validate(wrong_fallback))] == ["params.missing_date"]


def test_lh_rec_02_fifth_weekday_missing_is_skipped() -> None:
    fifth = rule("NTH_WEEKDAY", {"month": 3, "weekday": MON, "nth": 5}, "Spring team day", valid_from_year=2027)
    info = [i for i in R.validate(fifth) if i.code == "RULE_NTH_5"]
    assert [i.message for i in info] == ["No 5th Monday in some years; use -1 for the last one"]
    assert errors(R.validate(fifth)) == []
    assert [r.date for r in R.preview(fifth, [], SAT_SUN, years=2)] == [D(2027, 3, 29)]
    assert [n.message for n in gen([fifth], 2028).notes if n.code == "DATE_SKIPPED"] == ["No 5th Monday in Mar 2028"]


def test_lh_rec_03_offset_into_previous_year() -> None:
    eve = rule("FIXED_DATE", {"month": 1, "day": 1, "offset_days": -1}, "New Year's Eve (company)", valid_from_year=2026)
    y2027 = gen([eve], 2027)
    assert [(r.date, r.rule_year) for r in y2027.rows] == [(D(2027, 12, 31), 2028)]
    y2026 = gen([eve], 2026)
    assert [(r.date, r.rule_year) for r in y2026.rows] == [(D(2026, 12, 31), 2027)]


def test_lh_rec_04_uk_substitute_chain_2026_and_2027() -> None:
    y2026 = [(r.date, r.name, r.observance) for r in gen(UK_XMAS, 2026).rows if r.date.month == 12]
    assert y2026 == [
        (D(2026, 12, 25), "Christmas Day", "ORIGINAL"),
        (D(2026, 12, 26), "Boxing Day", "ORIGINAL"),
        (D(2026, 12, 28), "Boxing Day", "OBSERVED"),
    ]
    y2027 = gen(UK_XMAS, 2027).rows
    holidays = {r.date for r in y2027 if r.observance == "OBSERVED" or r.date.weekday() < 5}
    leave_days = [D(2027, 12, 27) + dt.timedelta(days=i) for i in range(5)]
    assert sum(day not in holidays for day in leave_days) == 3


def test_lh_rec_04_apply_observance_adds_targets_to_taken() -> None:
    """G-01 through the public apply_observance: ascending order, every target joins the taken set."""
    base = [r for rs in UK_XMAS for r in R.expand(rs, 2027, SAT_SUN)]
    taken = {D(2027, 12, 25), D(2027, 12, 26)}
    out = R.apply_observance(list(reversed(base)), SAT_SUN, taken)
    assert [(r.date, r.observance) for r in out] == [
        (D(2027, 12, 25), "ORIGINAL"), (D(2027, 12, 27), "OBSERVED"),
        (D(2027, 12, 26), "ORIGINAL"), (D(2027, 12, 28), "OBSERVED"),
    ]
    assert taken == {D(2027, 12, 25), D(2027, 12, 26)}
    blocked = R.apply_observance(base, SAT_SUN, {D(2027, 12, 27)})
    assert [r.date for r in blocked if r.observance == "OBSERVED"] == [D(2027, 12, 28), D(2027, 12, 29)]


def test_lh_rec_05_chain_runs_into_manual_shutdown_row() -> None:
    result = gen(UK_XMAS, 2027, fixed_rows=[SHUTDOWN_ROW])
    assert [r.date for r in result.rows if r.observance == "OBSERVED"] == [D(2027, 12, 28), D(2027, 12, 29)]
    assert all(r.rule_id is not None for r in result.rows)
    cancelled = dataclasses.replace(SHUTDOWN_ROW, status="CANCELLED")
    assert [r.date for r in gen(UK_XMAS, 2027, fixed_rows=[cancelled]).rows if r.observance == "OBSERVED"] == [
        D(2027, 12, 27), D(2027, 12, 28),
    ]


def test_lh_rec_06_sunday_weekend_nearest_moves_forward_and_saturday_is_workday() -> None:
    rows = gen([fixed(8, 15, "Independence Day", 1, observed_rule="NEAREST_WORKDAY")], 2027, SUN).rows
    assert [(r.date, r.observance) for r in rows] == [(D(2027, 8, 15), "ORIGINAL"), (D(2027, 8, 16), "OBSERVED")]
    saturday = gen([fixed(8, 14, "Saturday holiday", 2, observed_rule="NEAREST_WORKDAY")], 2027, SUN).rows
    assert [(r.date, r.observance) for r in saturday] == [(D(2027, 8, 14), "ORIGINAL")]


def test_lh_rec_07_three_day_weekend() -> None:
    nearest = {"observed_rule": "NEAREST_WORKDAY"}
    rules = [fixed(6, 19, "Juneteenth", 1, **nearest), fixed(7, 4, "Independence Day", 2, **nearest),
             fixed(9, 24, "Company day", 3, **nearest)]
    observed = {r.observed_from_date: r.date for r in gen(rules, 2027, FRI_SUN).rows if r.observance == "OBSERVED"}
    assert observed == {D(2027, 6, 19): D(2027, 6, 21), D(2027, 7, 4): D(2027, 7, 5), D(2027, 9, 24): D(2027, 9, 23)}
    same_monday = [fixed(6, 19, "A", 1, **nearest), fixed(6, 20, "B", 2, **nearest)]
    observed = {r.observed_from_date: r.date for r in gen(same_monday, 2027, FRI_SUN).rows if r.observance == "OBSERVED"}
    assert observed == {D(2027, 6, 19): D(2027, 6, 21), D(2027, 6, 20): D(2027, 6, 22)}


def test_lh_rec_08_friday_saturday_weekend_sunday_is_workday() -> None:
    rules = [fixed(9, 24, "A", 1, observed_rule="NEAREST_WORKDAY"), fixed(9, 25, "B", 2, observed_rule="NEAREST_WORKDAY")]
    observed = {r.observed_from_date: r.date for r in gen(rules, 2027, FRI_SAT).rows if r.observance == "OBSERVED"}
    assert observed == {D(2027, 9, 24): D(2027, 9, 23), D(2027, 9, 25): D(2027, 9, 26)}


def test_lh_rec_09_coinciding_holidays_with_and_without_flag() -> None:
    variant_a = gen(_coinciding(True), 2027, SUN).rows
    assert [(r.date, r.name) for r in variant_a if r.observance == "OBSERVED"] == [(D(2027, 1, 27), "Founders' Day")]
    assert [r.name for r in variant_a if r.date == D(2027, 1, 26)] == ["Republic Day", "Founders' Day"]
    variant_b = gen(_coinciding(False), 2027, SUN).rows
    assert [r.observance for r in variant_b] == ["ORIGINAL", "ORIGINAL"]
    assert {r.date for r in variant_b} == {D(2027, 1, 26)}
    higher_rank_flagged = [
        rule("FIXED_DATE", {"month": 1, "day": 26, "observe_if_coincides": True}, "Republic Day", 1, holiday_type="PUBLIC"),
        fixed(1, 26, "Founders' Day", 2),
    ]
    assert [r.observance for r in gen(higher_rank_flagged, 2027, SUN).rows] == ["ORIGINAL", "ORIGINAL"]


def test_lh_rec_10_no_workday_within_horizon() -> None:
    shutdown = rule(
        "ONE_OFF", {"date": "2027-12-20"}, "Shutdown", 1, duration_days=12, valid_from_year=2027, valid_to_year=2027,
    )
    new_year = fixed(1, 1, "New Year's Day", 2, observed_rule="NEAREST_WORKDAY")
    result = gen([shutdown, new_year], 2027)
    assert [r for r in result.rows if r.observance == "OBSERVED"] == []
    assert len(result.rows) == 12 + 1
    misses = [n for n in result.notes if n.code == "NO_OBSERVED_WORKDAY"]
    assert [(n.message, n.rule_id, n.year) for n in misses] == [
        ("No working day found for the observed date of New Year's Day", 2, 2028)
    ]


def test_lh_rec_16_easter_range_limits() -> None:
    assert R._easter(2100, "WESTERN") == D(2100, 3, 28)
    assert R._easter(2100, "ORTHODOX") == D(2100, 5, 2)
    for bad_year in (1999, 2101):
        with pytest.raises(ValueError):
            R._easter(bad_year, "WESTERN")
    with pytest.raises(ValueError):
        R._easter(2027, "JULIAN")  # type: ignore[arg-type]
    good_friday = rule("EASTER_OFFSET", {"offset_days": -2, "tradition": "WESTERN"}, "Good Friday")
    assert R.expand(good_friday, 2027, SAT_SUN)[0].date == D(2027, 3, 26)
    beyond = rule("EASTER_OFFSET", {"offset_days": -2, "tradition": "WESTERN"}, valid_from_year=2101)
    assert [(i.code, i.field) for i in errors(R.validate(beyond))] == [("RULE_INVALID", "valid_from_year")]
    with pytest.raises(RecurrenceError):
        R.generate_year([good_friday], 2101, SAT_SUN)
    assert R.generate_year([good_friday], 2100, SAT_SUN).rows[0].date == D(2100, 3, 26)
    for year in range(2000, 2101):
        for tradition in ("WESTERN", "ORTHODOX"):
            easter = R._easter(year, tradition)
            assert easter.weekday() == 6
            assert (easter - dt.timedelta(days=70)).year == year == (easter + dt.timedelta(days=70)).year
        assert D(year, 3, 22) <= R._easter(year, "WESTERN") <= D(year, 4, 25)
    too_far = rule("EASTER_OFFSET", {"offset_days": 71, "tradition": "WESTERN"})
    assert [i.field for i in errors(R.validate(too_far))] == ["params.offset_days"]


def test_lh_rec_19_tenant_expected_date_beats_pack_estimate() -> None:
    diwali = rule("ANNUAL_CONFIRM", {"expected_month": 10}, "Diwali", 11)
    source, start = R.select_expected_start({"PACK": D(2027, 10, 30), "TENANT": D(2027, 10, 29)})
    assert (source, start) == ("TENANT", D(2027, 10, 29))
    assert R.select_expected_start({"PACK": D(2027, 10, 30), "IMPORT": D(2027, 10, 31)}) == ("IMPORT", D(2027, 10, 31))
    assert R.select_expected_start({}) is None
    rows = gen([diwali], 2027, SUN, expected={(11, 2027): start}).rows
    assert [(r.date, r.status) for r in rows] == [(D(2027, 10, 29), "PROVISIONAL")]


def test_lh_rec_20_one_off_in_the_past_or_wrong_year() -> None:
    flood = rule("ONE_OFF", {"date": "2027-07-14"}, "Flood closure", valid_from_year=2027, valid_to_year=2027)
    assert [i.code for i in errors(R.validate(flood, today=D(2027, 7, 15)))] == ["HOLIDAY_IN_PAST"]
    assert [i.code for i in errors(R.validate(flood, today=D(2027, 7, 14)))] == ["HOLIDAY_IN_PAST"]
    assert errors(R.validate(flood, today=D(2027, 7, 13))) == []
    wrong_year = rule("ONE_OFF", {"date": "2027-12-27"}, "Shutdown", valid_from_year=2026)
    assert [(i.code, i.field) for i in errors(R.validate(wrong_year))] == [("RULE_INVALID", "valid_from_year")]


def test_lh_rec_24_fourth_versus_last_weekday_first_difference() -> None:
    """Also GV-REC-33: both alternatives explain the samples; first differing year 2027 / 2028."""
    for month, weekday, samples, first_diff in (
        (5, MON, (D(2025, 5, 26), D(2026, 5, 25)), (D(2027, 5, 24), D(2027, 5, 31))),
        (11, THU, (D(2025, 11, 27), D(2026, 11, 26)), (D(2028, 11, 23), D(2028, 11, 30))),
    ):
        fourth = rule("NTH_WEEKDAY", {"month": month, "weekday": weekday, "nth": 4}, valid_from_year=2025)
        last = rule("NTH_WEEKDAY", {"month": month, "weekday": weekday, "nth": -1}, valid_from_year=2025)
        for sample in samples:
            assert R.expand(fourth, sample.year, SAT_SUN)[0].date == sample == R.expand(last, sample.year, SAT_SUN)[0].date
        differing = next(
            (a.date, b.date)
            for y in range(2027, 2037)
            for a, b in [(R.expand(fourth, y, SAT_SUN)[0], R.expand(last, y, SAT_SUN)[0])]
            if a.date != b.date
        )
        assert differing == first_diff


def test_lh_rec_28_multi_day_block_across_weekend() -> None:
    national = fixed(12, 2, "National Day", 1, duration_days=2)
    assert [(r.date, r.sequence_index) for r in gen([national], 2027).rows] == [(D(2027, 12, 2), 1), (D(2027, 12, 3), 2)]
    assert [r.observance for r in gen([national], 2028).rows] == ["ORIGINAL", "ORIGINAL"]
    moving = fixed(12, 2, "National Day", 1, duration_days=2, observed_rule="NEXT_WORKDAY")
    rows = gen([moving], 2028).rows
    assert [(r.date, r.observance, r.sequence_index, r.occurrence_key) for r in rows] == [
        (D(2028, 12, 2), "ORIGINAL", 1, "2028:1:O"),
        (D(2028, 12, 3), "ORIGINAL", 2, "2028:2:O"),
        (D(2028, 12, 4), "OBSERVED", 1, "2028:1:B"),
        (D(2028, 12, 5), "OBSERVED", 2, "2028:2:B"),
    ]
    assert {r.status for r in rows} == {"CONFIRMED"}


# --------------------------------------------------------------------------- edge cases LH-HOL-* (generation side)


def test_lh_hol_06_diamond_include_row_counts_once_as_taken() -> None:
    """Generation side of LH-HOL-06: the included Republic Day row reaching the generator through two include
    paths is one taken date, is never re-emitted, and moves a coinciding rule exactly once (flatten owns the dedupe)."""
    republic = Occurrence(date=D(2027, 1, 26), name="Republic Day", holiday_type="PUBLIC")
    founders = rule("FIXED_DATE", {"month": 1, "day": 26, "observe_if_coincides": True}, "Founders' Day", 5)
    result = gen([founders], 2027, SUN, fixed_rows=[republic, republic])
    assert [(r.date, r.name, r.observance) for r in result.rows] == [
        (D(2027, 1, 26), "Founders' Day", "ORIGINAL"),
        (D(2027, 1, 27), "Founders' Day", "OBSERVED"),
    ]
    duplicate_rule = fixed(1, 26, "Republic Day", 6, holiday_type="PUBLIC")
    notes = gen([duplicate_rule], 2027, SUN, fixed_rows=[republic, republic]).notes
    assert codes(notes) == ["DUPLICATE_HOLIDAY"]


def test_lh_hol_13_same_date_equal_rank_rows_are_all_generated() -> None:
    """Generation side of LH-HOL-13: both COMPANY rows on Mon 2027-12-27 are generated with their own portions and
    no extra day; the tie-break (rank -> portion -> id) is the calendar resolver's (resolve_same_date, V3-063)."""
    half = fixed(12, 27, "Foundation Day (half)", 400, portion="SECOND_HALF")
    shutdown = fixed(12, 27, "Year-end shutdown day 1", 900)
    assert [i.code for i in errors(R.validate(half))] == ["INVALID_PORTION"]
    assert errors(R.validate(half, half_days_enabled=True)) == []
    rows = gen([shutdown, half], 2027).rows
    assert [(r.date, r.rule_id, r.portion, r.observance) for r in rows] == [
        (D(2027, 12, 27), 400, "SECOND_HALF", "ORIGINAL"),
        (D(2027, 12, 27), 900, "FULL", "ORIGINAL"),
    ]


def test_lh_hol_14_half_day_rows_from_two_calendars_stay_separate() -> None:
    """Generation side of LH-HOL-14: the rule's SECOND_HALF row is generated beside the included calendar's
    FIRST_HALF row (not re-emitted, not merged); the portion union is the resolver's. Optional holidays stay FULL."""
    eve = fixed(12, 24, "Christmas Eve (early close)", 1, portion="SECOND_HALF")
    party = Occurrence(date=D(2026, 12, 24), name="Holiday party morning", portion="FIRST_HALF")
    rows = gen([eve], 2026, fixed_rows=[party]).rows
    assert [(r.date, r.name, r.portion, r.observance) for r in rows] == [
        (D(2026, 12, 24), "Christmas Eve (early close)", "SECOND_HALF", "ORIGINAL"),
    ]
    optional_half = fixed(12, 24, "Optional eve", 2, portion="FIRST_HALF", holiday_type="OPTIONAL")
    assert [i.code for i in errors(R.validate(optional_half, half_days_enabled=True))] == ["INVALID_PORTION"]


def test_lh_hol_38_observed_pair_identity() -> None:
    """Generation side of LH-HOL-38: the ORIGINAL/OBSERVED pair shares rule_id, rule_year and sequence_index, which
    day facts use to suppress the ORIGINAL weekend row (V3-063)."""
    rows = gen([fixed(7, 4, "Independence Day", 5, observed_rule="NEAREST_WORKDAY")], 2027).rows
    original, observed = rows
    assert (original.date, original.observance, original.occurrence_key) == (D(2027, 7, 4), "ORIGINAL", "2027:1:O")
    assert (observed.date, observed.observance, observed.occurrence_key) == (D(2027, 7, 5), "OBSERVED", "2027:1:B")
    assert observed.observed_from_date == original.date
    pair = ("rule_id", "rule_year", "sequence_index", "name", "holiday_type", "category", "portion", "status")
    assert all(getattr(original, f) == getattr(observed, f) for f in pair)


def test_lh_hol_39_weekend_holidays_not_moved_under_none() -> None:
    """Generation side of LH-HOL-39: IN rules with observed NONE give no OBSERVED row; the rollover note marks both
    dates 'Weekend' when a work week of the calendar's employees is off on Saturday."""
    rules = [fixed(8, 15, "Independence Day", 1, holiday_type="PUBLIC"), fixed(10, 2, "Gandhi Jayanti", 2, holiday_type="PUBLIC")]
    calendar_only = gen(rules, 2027, SUN)
    assert [(r.date, r.observance) for r in calendar_only.rows] == [(D(2027, 8, 15), "ORIGINAL"), (D(2027, 10, 2), "ORIGINAL")]
    assert [n.message for n in calendar_only.notes] == ["Independence Day falls on Sun 2027-08-15; not moved"]
    with_mon_fri = gen(rules, 2027, SUN, weekly_off_days={5})
    assert [(n.code, n.severity) for n in with_mon_fri.notes] == [("HOLIDAY_ON_WEEKEND", "info")] * 2
    assert [n.message for n in with_mon_fri.notes][1] == "Gandhi Jayanti falls on Sat 2027-10-02; not moved"


# --------------------------------------------------------------------------- validation and contract


def test_validate_accepts_typical_rules() -> None:
    for good in (
        fixed(1, 26, "Republic Day", holiday_type="PUBLIC", category="NATIONAL"),
        rule("NTH_WEEKDAY", {"month": 5, "weekday": MON, "nth": -1}, "Late May bank holiday"),
        rule("WEEKDAY_RELATIVE", {"month": 5, "day": 25, "weekday": MON, "direction": "BEFORE"}, "Victoria Day"),
        rule("EASTER_OFFSET", {"offset_days": 1, "tradition": "WESTERN"}, "Easter Monday"),
        EID,
        fixed(7, 4, observed_rule="NEAREST_WORKDAY", applies_on={5, 6}),
    ):
        assert errors(R.validate(good, weekend_days=SAT_SUN)) == []


@pytest.mark.parametrize(
    ("bad", "field"),
    [
        (rule("FIXED_DATE", {"month": 13, "day": 1}), "params.month"),
        (rule("FIXED_DATE", {"month": 1}), "params.day"),
        (rule("FIXED_DATE", {"month": 1, "day": 1, "offset_days": 32}), "params.offset_days"),
        (rule("FIXED_DATE", {"month": 1, "day": 1, "weekday": 0}), "params.weekday"),
        (rule("FIXED_DATE", {"month": True, "day": 1}), "params.month"),
        (rule("NTH_WEEKDAY", {"month": 1, "weekday": 0, "nth": 6}), "params.nth"),
        (rule("NTH_WEEKDAY", {"month": 1, "weekday": 7, "nth": 1}), "params.weekday"),
        (rule("WEEKDAY_RELATIVE", {"month": 1, "day": 1, "weekday": 0, "direction": "NEAR"}), "params.direction"),
        (rule("EASTER_OFFSET", {"offset_days": -2}), "params.tradition"),
        (rule("ANNUAL_CONFIRM", {"hint": "x" * 201}), "params.hint"),
        (rule("ONE_OFF", {"date": "27-12-2027"}), "params.date"),
        (rule("BIWEEKLY", {}), "kind"),
        (fixed(1, 1, duration_days=0), "duration_days"),
        (fixed(1, 1, duration_days=32), "duration_days"),
        (fixed(1, 1, duration_range=(1, 2)), "duration_range"),
        (rule("ANNUAL_CONFIRM", {}, duration_days=3, duration_range=(4, 3)), "duration_range"),
        (rule("ANNUAL_CONFIRM", {}, duration_days=4, duration_range=(3, 4)), "duration_days"),
        (fixed(1, 1, applies_on={6}), "applies_on"),
        (fixed(1, 1, valid_from_year=1999), "valid_from_year"),
        (fixed(1, 1, valid_from_year=2030, valid_to_year=2029), "valid_to_year"),
        (fixed(1, 1, name="  "), "name"),
        (fixed(1, 1, holiday_type="BANK"), "holiday_type"),
        (fixed(1, 1, category="RELIGIOUS"), "category"),
        (fixed(1, 1, observed_rule="SUBSTITUTE"), "observed_rule"),
    ],
)
def test_validate_rejects(bad: RuleSpec, field: str) -> None:
    assert field in [i.field for i in errors(R.validate(bad))]


def test_validate_trigger_days_must_be_weekend() -> None:
    friday_trigger = fixed(1, 1, observed_rule="NEXT_WORKDAY", applies_on={4})
    assert errors(R.validate(friday_trigger)) == []
    assert [i.field for i in errors(R.validate(friday_trigger, weekend_days=SAT_SUN))] == ["applies_on"]
    assert errors(R.validate(friday_trigger, weekend_days=FRI_SAT)) == []


def test_validate_calendar_level_name_duplicates_and_limit() -> None:
    xmas = fixed(12, 25, "Christmas Day", 1)
    same = fixed(12, 25, "christmas  day", None)
    issues = errors(R.validate(same, other_rules=[xmas]))
    assert [(i.field, i.reason) for i in issues] == [("name", "DUPLICATE")]
    assert issues[0].message == "Christmas Day already gives this holiday on Fri 2020-12-25"
    assert errors(R.validate(xmas, other_rules=[xmas])) == []
    assert errors(R.validate(fixed(12, 25, "Christmas Day", 1, status="INACTIVE"), other_rules=[fixed(12, 25, "Christmas Day", 2)])) == []
    many = [fixed(1, 1, f"Rule {i}", i) for i in range(1, 201)]
    assert [i.field for i in errors(R.validate(fixed(1, 2, "One more", None), other_rules=many))] == ["status"]


def test_expand_contract() -> None:
    with pytest.raises(RecurrenceError):
        R.expand(fixed(1, 1), 2027, SAT_SUN, expected_start=D(2027, 1, 1))
    with pytest.raises(RecurrenceError):
        R.expand(fixed(1, 1), 2027, {7})
    assert R.expand(EID, 2027, SAT_SUN) == []
    assert R.expand(fixed(1, 1, valid_from_year=2028), 2027, SAT_SUN) == []
    assert R.expand(fixed(1, 1, valid_to_year=2026), 2027, SAT_SUN) == []
    row = R.expand(fixed(1, 26, "Republic Day", 3, version=4, name_i18n={"hi": "गणतंत्र दिवस"}), 2027, SAT_SUN)[0]
    assert (row.rule_id, row.rule_year, row.rule_version, row.occurrence_key, row.name_i18n["hi"]) == (
        3, 2027, 4, "2027:1:O", "गणतंत्र दिवस",
    )


def test_inactive_rules_generate_nothing() -> None:
    assert gen([fixed(1, 26, status="INACTIVE")], 2027).rows == ()


def test_provisional_original_gives_provisional_observed() -> None:
    weekend_eid = rule("ANNUAL_CONFIRM", {}, "Eid", 9, observed_rule="NEXT_WORKDAY")
    rows = gen([weekend_eid], 2027, expected={(9, 2027): D(2027, 3, 13)}).rows
    assert [(r.date, r.observance, r.status) for r in rows] == [
        (D(2027, 3, 13), "ORIGINAL", "PROVISIONAL"), (D(2027, 3, 15), "OBSERVED", "PROVISIONAL"),
    ]


def test_preview_years_and_other_rules() -> None:
    boxing = UK_XMAS[1]
    rows = R.preview(boxing, [UK_XMAS[0]], SAT_SUN, years=2, from_year=2026)
    assert [(r.date, r.observance) for r in rows] == [
        (D(2026, 12, 26), "ORIGINAL"), (D(2026, 12, 28), "OBSERVED"),
        (D(2027, 12, 26), "ORIGINAL"), (D(2027, 12, 28), "OBSERVED"),
    ]
    unsaved = fixed(12, 26, "Boxing Day", None, observed_rule="NEXT_WORKDAY", status="INACTIVE")
    assert [r.date for r in R.preview(unsaved, [UK_XMAS[0]], SAT_SUN, years=1, from_year=2027)] == [
        D(2027, 12, 26), D(2027, 12, 28),
    ]
    for bad_years in (0, 11):
        with pytest.raises(RecurrenceError):
            R.preview(boxing, [], SAT_SUN, years=bad_years)


def test_module_is_pure() -> None:
    """IG-01: standard library only, no clock."""
    with open(R.__file__, encoding="utf-8") as fh:
        text = fh.read()
    imports = [line.split()[1] for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert {name.split(".")[0] for name in imports} <= {"__future__", "dataclasses", "datetime", "re", "collections", "types", "typing"}
    assert ".today(" not in text and ".now(" not in text
