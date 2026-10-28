"""Pure rotation engine (WI-AS-5.2 / 5.6): GV-AS-ROT-01..08 (+ the pure part of -10) and the 22 templates.

Owner doc ``docs/attendance-shift/03-shifts/02-rosters-rotations-assignment.md`` (§2.1 model, §2.2 structural
validation, §2.3 catalogue, §2.6 anchors, §2.7 lints, §3.2 resolver, §14.1 Pune example, §15 vectors). No database.
"""

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import roster_rules as rr
from app.services import rotation_engine as rot
from app.services import shift_engine as se

D = dt.date
IST = "Asia/Kolkata"

# Pune shifts (owner doc §2.4 example): A 06:00-14:00, B 14:00-22:00, C 22:00-06:00 (+1), 480 net each.
SHIFT_A, SHIFT_B, SHIFT_C = 201, 202, 203
PUNE_SHIFTS = {
    SHIFT_A: rot.ShiftTimes("06:00", "14:00", 480),
    SHIFT_B: rot.ShiftTimes("14:00", "22:00", 480),
    SHIFT_C: rot.ShiftTimes("22:00", "06:00", 480),
}
PUNE = rot.TEMPLATES["SIX_ON_TWO_OFF_SLOW"].pattern({"M": SHIFT_A, "A": SHIFT_B, "N": SHIFT_C}, code="PUNE-6ON2OFF")
PUNE_ANCHOR = D(2026, 11, 2)  # Monday
CREW_OFFSETS = {"CREW-A": "A", "CREW-B": "B", "CREW-C": "C", "CREW-D": "D"}


def crew(name, **kw):
    return rot.assignment_for_crew(PUNE, CREW_OFFSETS[name], PUNE_ANCHOR, **kw)


def shift_on(name, day):
    a = crew(name)
    return rot.shift_for_token(PUNE, rot.token_on(PUNE, a.anchor_date, a.offset_days, day))


# --------------------------------------------------------------------------- catalogue (§2.3)

SPEC_KEYS = [
    "PANAMA_223_ROT", "PITMAN_FIXED", "DUPONT", "FOUR_ON_FOUR_OFF_ROT", "FOUR_ON_FOUR_OFF_FIXED", "DDNNOOOO_4C",
    "SEVEN_ON_SEVEN_OFF", "FOUR_THREE_ALT_12H", "DDNNOO_3C", "FIVE_CREW_12H", "CONTINENTAL_28",
    "CONTINENTAL_222_8D", "SIX_ON_TWO_OFF_SLOW", "SOUTHERN_SWING", "FIVE_CREW_8H", "ABC_WEEKLY_24X6",
    "ABC_WEEKLY_STAGGERED_OFF", "FOUR_ON_THREE_OFF_10H", "FOUR_ON_THREE_OFF_12H_IN", "GENERAL_MON_SAT",
    "FIVE_TWO_ROTATING_OFF", "KELLY_24_48",
]

# (cycle_days, number of crews) per the §2.3 table
SPEC_SHAPE = {
    "PANAMA_223_ROT": (28, 4), "PITMAN_FIXED": (14, 4), "DUPONT": (28, 4), "FOUR_ON_FOUR_OFF_ROT": (16, 4),
    "FOUR_ON_FOUR_OFF_FIXED": (8, 4), "DDNNOOOO_4C": (8, 4), "SEVEN_ON_SEVEN_OFF": (14, 4),
    "FOUR_THREE_ALT_12H": (14, 4), "DDNNOO_3C": (6, 3), "FIVE_CREW_12H": (10, 5), "CONTINENTAL_28": (28, 4),
    "CONTINENTAL_222_8D": (8, 4), "SIX_ON_TWO_OFF_SLOW": (24, 4), "SOUTHERN_SWING": (28, 4), "FIVE_CREW_8H": (10, 5),
    "ABC_WEEKLY_24X6": (21, 3), "ABC_WEEKLY_STAGGERED_OFF": (21, 21), "FOUR_ON_THREE_OFF_10H": (7, 2),
    "FOUR_ON_THREE_OFF_12H_IN": (7, 1), "GENERAL_MON_SAT": (7, 1), "FIVE_TWO_ROTATING_OFF": (7, 7),
    "KELLY_24_48": (3, 3),
}

L = rot
# Pattern-level lints with the research default clock times (§2.3), cycle index 0 on the template's
# week_anchor_weekday or Monday. Derived by hand from the §2.7 definitions.
EXPECTED_LINTS = {
    "PANAMA_223_ROT": {L.LINT_WEEKLY_ROTATION_SPEED},  # 7 D then 7 N (OFF skipped)
    "PITMAN_FIXED": {L.LINT_PERMANENT_NIGHTS},  # crews C, D work nights only
    "DUPONT": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT},  # NNNN, DDDD (12 h > 8 h)
    "FOUR_ON_FOUR_OFF_ROT": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT},
    "FOUR_ON_FOUR_OFF_FIXED": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_PERMANENT_NIGHTS},
    "DDNNOOOO_4C": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_DAY_NIGHT_SWITCH_REST},  # D -> N with 1 free night
    "SEVEN_ON_SEVEN_OFF": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_PERMANENT_NIGHTS},  # 7 days is not > 7
    "FOUR_THREE_ALT_12H": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_PERMANENT_NIGHTS, L.LINT_NO_FREE_WEEKEND},
    "DDNNOO_3C": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_DAY_NIGHT_SWITCH_REST},
    "FIVE_CREW_12H": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_DAY_NIGHT_SWITCH_REST},
    "CONTINENTAL_28": {L.LINT_DAY_NIGHT_SWITCH_REST, L.LINT_EARLY_START},  # forward, runs <= 3, M 06:00
    "CONTINENTAL_222_8D": {L.LINT_DAY_NIGHT_SWITCH_REST, L.LINT_EARLY_START},
    "SIX_ON_TWO_OFF_SLOW": {L.LINT_WEEKLY_ROTATION_SPEED, L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_EARLY_START},
    "SOUTHERN_SWING": {L.LINT_WEEKLY_ROTATION_SPEED, L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_EARLY_START},
    "FIVE_CREW_8H": {L.LINT_DAY_NIGHT_SWITCH_REST, L.LINT_EARLY_START},
    "ABC_WEEKLY_24X6": {L.LINT_WEEKLY_ROTATION_SPEED, L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_EARLY_START,
                        L.LINT_NO_FREE_WEEKEND},  # Sunday-only weekly off
    "ABC_WEEKLY_STAGGERED_OFF": {L.LINT_WEEKLY_ROTATION_SPEED, L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_EARLY_START,
                                 L.LINT_NO_FREE_WEEKEND, L.LINT_DAY_NIGHT_SWITCH_REST, L.LINT_REST_BELOW_10H},
    "FOUR_ON_THREE_OFF_10H": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_NO_FREE_WEEKEND},  # squad B works Thu-Sun
    "FOUR_ON_THREE_OFF_12H_IN": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT},
    "GENERAL_MON_SAT": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_NO_FREE_WEEKEND},  # 9 h gross > 8 h
    "FIVE_TWO_ROTATING_OFF": {L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_NO_FREE_WEEKEND},  # crews B..G
    "KELLY_24_48": {L.LINT_SHIFT_OVER_12H, L.LINT_CONSECUTIVE_LONG_OR_NIGHT, L.LINT_PERMANENT_NIGHTS},
}


def test_catalogue_has_the_22_spec_templates_in_order():
    assert list(rot.TEMPLATES) == SPEC_KEYS
    assert [t.number for t in rot.TEMPLATES.values()] == list(range(1, 23))
    assert rot.ROTATION_TEMPLATES is rot.TEMPLATES


@pytest.mark.parametrize("key", SPEC_KEYS)
def test_template_cycle_length_and_crews(key):
    template = rot.TEMPLATES[key]
    length, crews = SPEC_SHAPE[key]
    assert template.cycle_days == length
    assert len(template.crews) == crews
    rows = template.crew_rows.values() if template.crew_rows else [template.tokens]
    assert all(len(row) == length for row in rows)
    assert all(0 <= offset < length for offset in template.crew_offsets.values())
    assert (template.tokens is None) != (template.crew_rows is None)  # exactly one representation


@pytest.mark.parametrize("key", SPEC_KEYS)
def test_template_tokens_mapped_and_coverage_holds(key):
    template = rot.TEMPLATES[key]
    pattern = template.pattern()
    assert rot.validate_pattern(pattern, dict(template.default_shifts)) == []
    # every work token has default clock times, and copy-on-use starts with an empty token_map (§2.4)
    assert set(template.work_tokens) <= set(template.default_shifts)
    empty = template.pattern(token_map={})
    assert {i.rule for i in rot.validate_pattern(empty)} == {rot.RULE_TOKEN_UNMAPPED}
    for counts in rot.coverage_by_index(pattern):
        for token, needed in template.coverage.items():
            assert counts.get(token, 0) == needed


@pytest.mark.parametrize("key", SPEC_KEYS)
def test_template_figures_match_the_catalogue_table(key):
    template = rot.TEMPLATES[key]
    stats = rot.cycle_stats(template.pattern(), minutes_basis="gross")
    if template.expected_avg_week_hours is not None:
        assert (stats.avg_week_minutes / 60).quantize(Decimal("0.1")) == template.expected_avg_week_hours
    assert stats.max_run == template.expected_max_run
    assert stats.max_night_run == template.expected_max_night_run
    assert stats.worst_7d_days == template.expected_worst_7d_days
    assert Decimal(stats.worst_7d_minutes) / 60 == template.expected_worst_7d_hours


@pytest.mark.parametrize("key", SPEC_KEYS)
def test_template_lint_behaviour(key):
    results = rot.lint(rot.TEMPLATES[key].pattern())
    assert rot.lint_keys(results) == EXPECTED_LINTS[key]
    assert {r.severity for r in results} == {rot.WARN}  # lints are WARN only, never BLOCK
    assert all(r.employee_id is None and r.details.get("crew_code") is not None for r in results)


def test_general_shift_net_hours_and_staggered_rows():
    general = rot.cycle_stats(rot.TEMPLATES["GENERAL_MON_SAT"].pattern())
    assert general.avg_week_minutes == 48 * 60  # 54 gross / 48 net
    rows = rot.TEMPLATES["ABC_WEEKLY_STAGGERED_OFF"].crew_rows
    assert rows["A-SUN"] == "MMMMMMOAAAAAAONNNNNNO"  # slot SUN = the 24x6 row
    assert rows["A-MON"] == "OMMMMMMOAAAAAAONNNNNN"
    assert rows["B-SUN"][:7] == "NNNNNNO" and rows["C-SUN"][:7] == "AAAAAAO"  # week offsets A0 B2 C1


# --------------------------------------------------------------------------- structural validation (§2.2)


def test_structural_validation_rules():
    ok = rot.PatternVersionData(cycle_days=4, tokens="DDOO", crew_offsets={"A": 0, "B": 2},
                                token_map={"D": 1}, coverage={"D": 1})
    assert rot.validate_pattern(ok) == []

    def rules(**changes):
        values = dict(cycle_days=4, tokens="DDOO", crew_offsets={"A": 0, "B": 2}, token_map={"D": 1},
                      coverage={"D": 1})
        values.update(changes)
        return {(i.code, i.rule) for i in rot.validate_pattern(rot.PatternVersionData(**values))}

    invalid = rot.ROTATION_PATTERN_INVALID
    assert (invalid, rot.RULE_ROW_LENGTH) in rules(tokens="DDO")
    assert (invalid, rot.RULE_MODE) in rules(crew_rows={"A": "DDOO"})
    assert (invalid, rot.RULE_MODE) in rules(tokens=None)
    assert (invalid, rot.RULE_TOKEN_UNMAPPED) in rules(tokens="DNOO", coverage={})
    assert (invalid, rot.RULE_O_REMAPPED) in rules(token_map={"D": 1, "O": 2})
    assert (invalid, rot.RULE_OFFSET_RANGE) in rules(crew_offsets={"A": 0, "B": 4})
    assert (invalid, rot.RULE_COVERAGE_MISMATCH) in rules(crew_offsets={"A": 0, "B": 1})
    assert (rot.INVALID_FIELDS, None) in rules(week_anchor_weekday=7)
    assert (rot.INVALID_FIELDS, None) in rules(cycle_days=0)
    # a crew row without OFF is valid (it only raises LINT_CONSECUTIVE_DAYS)
    no_off = rot.PatternVersionData(cycle_days=8, tokens="DDDDDDDD", token_map={"D": 1})
    assert rot.validate_pattern(no_off) == []
    assert rot.LINT_CONSECUTIVE_DAYS in rot.lint_keys(rot.lint(no_off, {1: rot.ShiftTimes("09:00", "17:00")}))
    # "OFF" mapping (second OFF token) and the stored {"shift_id": n} form
    labelled = rot.PatternVersionData(cycle_days=3, tokens="DRO", token_map={"D": {"shift_id": 7}, "R": "OFF"})
    assert rot.validate_pattern(labelled) == []
    days = rot.expand(labelled, rot.AssignmentData(D(2026, 1, 1)), D(2026, 1, 1), D(2026, 1, 3))
    assert [(p.kind, p.shift) for p in days] == [("WORK", 7), ("OFF", None), ("OFF", None)]


def test_archived_shift_and_kelly_long_duty_mapping():
    pattern = rot.PatternVersionData(cycle_days=2, tokens="DO", token_map={"D": 5})
    issues = rot.validate_pattern(pattern, {5: rot.ShiftTimes("09:00", "17:00", archived=True)})
    assert [(i.code, i.rule) for i in issues] == [(rot.SHIFT_ARCHIVED, None)]
    kelly = rot.TEMPLATES["KELLY_24_48"]
    wrong = kelly.pattern({"K": 9})
    issues = rot.validate_pattern(wrong, {9: rot.ShiftTimes("08:00", "20:00")})  # not long_duty
    assert [(i.code, i.rule) for i in issues] == [(rot.ROTATION_PATTERN_INVALID, rot.RULE_TOKEN_UNMAPPED)]
    assert rot.validate_pattern(kelly.pattern({"K": 9}), {9: rot.ShiftTimes("08:00", "08:00", long_duty=True)}) == []


def test_lints_muted_except_kelly_by_design():
    muted = PUNE.__class__(**{**PUNE.__dict__, "lints_muted": frozenset({rot.LINT_EARLY_START})})
    assert rot.LINT_EARLY_START not in rot.lint_keys(rot.lint(muted, PUNE_SHIFTS))
    assert rot.LINT_EARLY_START in rot.lint_keys(rot.lint(muted, PUNE_SHIFTS, include_muted=True))
    kelly = rot.TEMPLATES["KELLY_24_48"].pattern(lints_muted=frozenset(rot.UNMUTABLE_FOR_LONG_DUTY))
    keys = rot.lint_keys(rot.lint(kelly))
    assert {rot.LINT_SHIFT_OVER_12H, rot.LINT_CONSECUTIVE_LONG_OR_NIGHT} <= keys  # shown, never muted


def test_content_hash_is_canonical():
    same = rot.TEMPLATES["SIX_ON_TWO_OFF_SLOW"].pattern({"N": SHIFT_C, "A": SHIFT_B, "M": SHIFT_A})
    assert rot.content_hash(same) == rot.content_hash(PUNE)
    other = rot.TEMPLATES["SIX_ON_TWO_OFF_SLOW"].pattern({"M": SHIFT_A, "A": SHIFT_B, "N": SHIFT_A})
    assert rot.content_hash(other) != rot.content_hash(PUNE)
    assert len(rot.content_hash(PUNE)) == 64


# --------------------------------------------------------------------------- golden vectors (§15)


def test_gv_as_rot_01_pune_crews_on_monday_2_november():
    day = D(2026, 11, 2)
    assert rot.cycle_index(PUNE, PUNE_ANCHOR, crew("CREW-A").offset_days, day) == 0
    assert shift_on("CREW-A", day) == SHIFT_A  # idx 0 M -> A
    assert rot.cycle_index(PUNE, PUNE_ANCHOR, crew("CREW-B").offset_days, day) == 6
    assert shift_on("CREW-B", day) is None  # idx 6 OFF
    assert rot.token_on(PUNE, PUNE_ANCHOR, crew("CREW-C").offset_days, day) == "A"
    assert shift_on("CREW-C", day) == SHIFT_B  # idx 12 A-token -> B
    assert shift_on("CREW-D", day) == SHIFT_C  # idx 18 N -> C
    week = rot.expand(PUNE, crew("CREW-A"), D(2026, 11, 2), D(2026, 11, 8))
    assert [p.shift for p in week] == [SHIFT_A] * 6 + [None]
    assert [p.kind for p in week] == ["WORK"] * 6 + ["OFF"]


def test_gv_as_rot_01_november_grid_and_coverage():
    """§14.1: one crew on each of A, B, C and one OFF every date; work days A 23, B 22, C 22, D 23."""
    days = {name: rot.expand(PUNE, crew(name), D(2026, 11, 1), D(2026, 11, 30)) for name in CREW_OFFSETS}
    for i in range(30):
        assert sorted(str(days[n][i].shift) for n in CREW_OFFSETS) == sorted(["201", "202", "203", "None"])
    assert {n: sum(p.kind == "WORK" for p in v) for n, v in days.items()} == \
        {"CREW-A": 23, "CREW-B": 22, "CREW-C": 22, "CREW-D": 23}
    assert days["CREW-A"][29].index == 4 and days["CREW-A"][29].shift == SHIFT_A  # Mon 30: idx 4 -> A


def test_gv_as_rot_02_panama_suggest_anchor():
    s = rot.suggest_anchor(rot.TEMPLATES["PANAMA_223_ROT"].pattern(), week_start=0)
    assert (s.k, s.anchor_weekday_code, s.worst_week_minutes) == (1, "SUN", 48 * 60)
    assert s.table[0] == s.table[4] == 60 * 60
    assert dict(s.table) == {0: 3600, 1: 2880, 2: 2880, 3: 2880, 4: 3600, 5: 2880, 6: 2880}
    assert rot.next_anchor_date(D(2026, 10, 28), s.anchor_weekday) == D(2026, 11, 1)  # §14.6 Sun 1 Nov


def test_gv_as_rot_03_dupont_and_continental_suggest_anchor():
    dupont = rot.suggest_anchor(rot.TEMPLATES["DUPONT"].pattern(), week_start=0)
    assert (dupont.k, dupont.anchor_weekday_code, dupont.worst_week_minutes, dupont.worst_week_count) == \
        (3, "FRI", 48 * 60, 8)
    assert [dupont.table[k] // 60 for k in range(7)] == [72, 60, 48, 48, 48, 48, 60]
    assert [dupont.counts[k] for k in range(7)] == [4, 4, 12, 8, 8, 12, 4]
    cont = rot.suggest_anchor(rot.TEMPLATES["CONTINENTAL_28"].pattern(), week_start=0)
    assert (cont.k, cont.anchor_weekday_code, cont.worst_week_minutes, cont.worst_week_count) == (5, "WED", 2880, 4)
    assert [cont.table[k] // 60 for k in range(7)] == [56, 48, 56, 48, 56, 48, 48]
    assert [cont.counts[k] for k in range(7)] == [4, 8, 4, 8, 4, 4, 4]
    swing = rot.suggest_anchor(rot.TEMPLATES["SOUTHERN_SWING"].pattern(), week_start=0)
    assert (swing.k, swing.anchor_weekday_code) == (5, "WED")
    abc = rot.suggest_anchor(rot.TEMPLATES["ABC_WEEKLY_24X6"].pattern(), week_start=0)
    assert (abc.k, abc.anchor_weekday_code, abc.worst_week_minutes) == (0, "MON", 2880)


def test_suggest_anchor_alignment_free_when_cycle_not_multiple_of_7():
    s = rot.suggest_anchor(PUNE, week_start=0, shifts=PUNE_SHIFTS)
    assert (s.k, s.anchor_weekday) == (None, None)
    assert (s.min_week_minutes, s.worst_week_minutes) == (40 * 60, 48 * 60)  # "between 40 and 48 h"


def test_gv_as_rot_10_week_start_change_moves_the_worst_week():
    """Pure part of EC AS-ROT-07: anchor Sun 2026-11-01; week start MON -> k 1, 48 h; SUN -> k 0, 60 h."""
    pattern = rot.TEMPLATES["PANAMA_223_ROT"].pattern()
    anchor_weekday = D(2026, 11, 1).weekday()
    for week_start, expected in ((0, 48), (6, 60)):
        k = rot.alignment_k(anchor_weekday, week_start)
        assert rot.suggest_anchor(pattern, week_start).table[k] == expected * 60
    assert rot.alignment_k(anchor_weekday, 6) == 0


def test_gv_as_rot_04_floor_modulo_before_the_anchor():
    day = D(2026, 11, 1)
    assert rot.cycle_index(PUNE, PUNE_ANCHOR, 0, day) == 23  # (-1 + 0) mod 24
    assert shift_on("CREW-A", day) is None
    assert rot.cycle_index(PUNE, PUNE_ANCHOR, 0, D(2025, 1, 1)) == (D(2025, 1, 1) - PUNE_ANCHOR).days % 24


def test_gv_as_rot_05_individual_assignment_offset():
    offset = rot.offset_for_index(PUNE, PUNE_ANCHOR, D(2026, 11, 16), desired_index=8)
    assert offset == 18 == PUNE.crew_offsets["D"]
    assert rot.token_on(PUNE, PUNE_ANCHOR, offset, D(2026, 11, 16)) == "A"
    assert rot.shift_for_token(PUNE, "A") == SHIFT_B
    imran = rot.AssignmentData(PUNE_ANCHOR, offset, None, D(2026, 11, 16))
    first = rot.expand(PUNE, imran, D(2026, 11, 10), D(2026, 11, 17))
    assert first[0].date == D(2026, 11, 16) and first[0].index == 8  # nothing before effective_from


def test_gv_as_rot_06_ravi_republic_day_holiday_modes():
    day = D(2027, 1, 26)
    ravi = crew("CREW-B")
    assert rot.cycle_index(PUNE, PUNE_ANCHOR, ravi.offset_days, day) == 19
    assert shift_on("CREW-B", day) == SHIFT_C
    kept = rot.expand(PUNE, ravi, day, day, holidays={day}, holiday_mode=rot.WORK_THROUGH)
    assert (kept[0].kind, kept[0].shift, kept[0].source) == ("WORK", SHIFT_C, rot.SOURCE_ROTATION)
    off = rot.expand(PUNE, ravi, day, day, holidays={day}, holiday_mode=rot.HOLIDAY_OFF)
    assert (off[0].kind, off[0].shift, off[0].source, off[0].planned_shift) == \
        ("OFF", None, rot.SOURCE_HOLIDAY_RULE, SHIFT_C)
    # §14.3: CREW-D is idx 7 (OFF) that day; a holiday on a rostered OFF stays a plain OFF
    d_off = rot.expand(PUNE, crew("CREW-D"), day, day, holidays={day})
    assert (d_off[0].kind, d_off[0].source, d_off[0].planned_shift) == ("OFF", rot.SOURCE_ROTATION, None)


def test_gv_as_rot_07_six_on_two_off_lints_with_the_pune_mapping():
    keys = rot.lint_keys(rot.lint(PUNE, PUNE_SHIFTS))
    assert keys == {rot.LINT_WEEKLY_ROTATION_SPEED, rot.LINT_CONSECUTIVE_LONG_OR_NIGHT, rot.LINT_EARLY_START}
    assert rot.LINT_BACKWARD_ROTATION not in keys and rot.LINT_DAY_NIGHT_SWITCH_REST not in keys
    long_or_night = [r for r in rot.lint(PUNE, PUNE_SHIFTS) if r.rule_key == rot.LINT_CONSECUTIVE_LONG_OR_NIGHT]
    assert {r.measured for r in long_or_night} == {6} and {r.limit for r in long_or_night} == {3}


def test_gv_as_rot_07_free_nights_and_backward_rotation_cases():
    # the B -> C switch of the Pune cycle leaves 3 free nights (>= 2): no LINT_DAY_NIGHT_SWITCH_REST
    b_end = dt.datetime(2026, 11, 13, 16, 30, tzinfo=dt.timezone.utc)  # 22:00 IST
    c_start = dt.datetime(2026, 11, 16, 16, 30, tzinfo=dt.timezone.utc)  # 22:00 IST three days later
    assert rot.full_windows_between(b_end, c_start, IST) == 3
    # a backward rotation N -> A (§14.2(b) seam: afternoon B followed by morning A)
    backward = rot.PatternVersionData(cycle_days=4, tokens="AMOO", token_map={"M": SHIFT_A, "A": SHIFT_B})
    with_c = rot.PatternVersionData(cycle_days=6, tokens="AMNOOO",
                                    token_map={"M": SHIFT_A, "A": SHIFT_B, "N": SHIFT_C})
    assert rot.LINT_BACKWARD_ROTATION not in rot.lint_keys(rot.lint(backward, PUNE_SHIFTS))  # two types never fire
    assert rot.LINT_BACKWARD_ROTATION in rot.lint_keys(rot.lint(with_c, PUNE_SHIFTS))


def _instances_from_days(days, employee_id):
    versions = {
        "M": se.ShiftVersionData(start_time="06:00", end_time="14:00", shift_id=1),
        "A": se.ShiftVersionData(start_time="14:00", end_time="22:00", shift_id=2),
        "N": se.ShiftVersionData(start_time="22:00", end_time="06:00", shift_id=3),
    }
    out = []
    for p in days:
        if p.kind == "WORK":
            out.append(rr.from_shift_instance(se.build_instance(versions[p.shift], p.date, IST), changed=True))
        else:
            out.append(rr.PlannedInstance(p.date, "OFF", changed=True))
    return rr.EmployeeSchedule(employee_id, IST, tuple(out))


def test_gv_as_rot_08_staggered_off_day_moving_mon_to_sun_gives_12_days():
    pattern = rot.TEMPLATES["ABC_WEEKLY_STAGGERED_OFF"].pattern()
    anchor = D(2026, 11, 2)  # Monday = index 0 (template anchor weekday MON)
    week1 = rot.expand(pattern, rot.AssignmentData(anchor, 0, "A-MON", effective_to=D(2026, 11, 8)),
                       D(2026, 11, 2), D(2026, 11, 15))
    week2 = rot.expand(pattern, rot.AssignmentData(anchor, 0, "A-SUN", effective_from=D(2026, 11, 9)),
                       D(2026, 11, 2), D(2026, 11, 15))
    days = week1 + week2
    assert [p.kind for p in days].count("WORK") == 12
    schedule = rr.ScheduleData((_instances_from_days(days, 77),))
    limits = rr.EmployeeLimits(max_consecutive_days=rr.Limit(10, rr.WARN, "LH weekly_rest IN", verified=False),
                               max_days_per_week=rr.Limit(6, rr.WARN, verified=False))
    results = [r for r in rr.validate(schedule, limits, rr.RuleContext()) if r.rule_key == rr.WEEKLY_REST_EXCEEDED]
    runs = [r for r in results if r.details["check"] == "CONSECUTIVE_DAYS"]
    assert len(runs) == 1
    assert (runs[0].severity, runs[0].measured, runs[0].limit) == (rr.WARN, 12, 10)
    assert runs[0].dates[0] == D(2026, 11, 3) and runs[0].dates[-1] == D(2026, 11, 14)
    assert runs[0].message_key.endswith(".not_verified")
    # the template rows themselves (off day fixed) never exceed 6 in a row
    assert rot.cycle_stats(pattern).max_run == 6


# --------------------------------------------------------------------------- purity


@pytest.mark.parametrize("module", [rot, rr])
def test_engines_are_pure(module):
    """IG-AS-02: standard library, pytz and the pure engines only (no DB, no app config)."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.update(f"{node.module}.{alias.name}" if node.module == "app.services" else node.module
                           for alias in node.names)
    allowed_app = {"app.services.shift_engine", "app.services.rotation_engine"}
    assert {m for m in modules if m.startswith("app")} <= allowed_app
    assert {m.split(".")[0] for m in modules if not m.startswith("app")} <= {
        "__future__", "datetime", "hashlib", "json", "math", "collections", "dataclasses", "decimal", "types",
        "typing", "pytz"}
