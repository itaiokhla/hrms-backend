"""Division of a deceased employee's settlement among nominees (3.5 §15; EC-BNK-15, EC-COR-07): shares must be
valid and add up to exactly 100, amounts are rounded down per currency, the largest share absorbs the rounding and
the amounts always add up to the net."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll.banking.nominee_shares import NomineeShare, NomineeShareError, split_net, validate_shares

D = Decimal


def _codes(shares) -> set[str]:
    return {i.code for i in validate_shares(shares)}


def test_ec_bnk_15_shares_must_add_up_to_exactly_one_hundred() -> None:
    assert validate_shares([(1, D("60")), (2, D("40"))]) == ()
    assert _codes([]) == {"NO_NOMINEES"}
    assert _codes([(1, D("60")), (2, D("39.999999"))]) == {"SHARES_INCOMPLETE"}
    assert _codes([(1, D("60")), (2, D("40.000001"))]) == {"SHARES_EXCEED"}
    assert _codes([(1, D("100"))]) == set()


def test_share_range_precision_and_duplicates_are_refused() -> None:
    assert _codes([(1, D("0")), (2, D("100"))]) == {"SHARE_RANGE"}
    assert _codes([(1, D("-5")), (2, D("105"))]) == {"SHARE_RANGE"}
    assert _codes([(1, D("33.3333333")), (2, D("66.6666667"))]) == {"SHARE_PRECISION"}
    assert _codes([(1, D("50")), (1, D("50"))]) == {"DUPLICATE_NOMINEE"}


def test_a_float_share_is_refused() -> None:
    with pytest.raises(NomineeShareError):
        validate_shares([(1, 33.3), (2, 66.7)])


def test_split_hand_computed_inr_with_the_largest_share_absorbing_the_rounding() -> None:
    # 100,000.01 INR: 33.333333 % and 33.333333 % round down, the 33.333334 % nominee gets the rest.
    lines = split_net(D("100000.01"), "INR", [(1, D("33.333333")), (2, D("33.333333")), (3, D("33.333334"))])
    assert [(l.nominee_id, str(l.amount), l.remainder) for l in lines] == [
        (1, "33333.33", False), (2, "33333.33", False), (3, "33333.35", True)]
    assert sum(l.amount for l in lines) == D("100000.01")


def test_split_equal_shares_ties_go_to_the_lowest_nominee_id() -> None:
    lines = split_net(D("100.01"), "INR", [(7, D("50")), (3, D("50"))])
    by_id = {l.nominee_id: l for l in lines}
    assert by_id[3].remainder and by_id[3].amount == D("50.01") and by_id[7].amount == D("50.00")


def test_split_respects_the_minor_unit_of_the_currency() -> None:
    jpy = split_net(D("1001"), "JPY", [(1, D("50")), (2, D("50"))])
    assert [str(l.amount) for l in jpy] == ["501", "500"]  # 500.5 rounds down; nominee 1 (tie) takes the remainder
    assert sum(l.amount for l in jpy) == D("1001")
    kwd = split_net(D("100.001"), "KWD", [(1, D("25")), (2, D("75"))])
    assert [str(l.amount) for l in kwd] == ["25.000", "75.001"]


def test_one_hundred_percent_nominee_receives_the_whole_net() -> None:
    (line,) = split_net(D("48123.45"), "INR", [(9, D("100"))])
    assert line.amount == D("48123.45") and line.remainder


def test_a_tiny_share_that_rounds_to_zero_gets_no_money_and_nothing_is_lost() -> None:
    lines = split_net(D("10.00"), "INR", [(1, D("99.999999")), (2, D("0.000001"))])
    assert {l.nominee_id: l.amount for l in lines} == {1: D("10.00"), 2: D("0.00")}


def test_split_refuses_incomplete_shares_and_unroundable_nets() -> None:
    with pytest.raises(NomineeShareError) as info:
        split_net(D("100.00"), "INR", [(1, D("60"))])
    assert info.value.code == "SHARES_INCOMPLETE"
    with pytest.raises(Exception):
        split_net(D("100.005"), "INR", [(1, D("50")), (2, D("50"))])  # more decimals than INR carries


@settings(max_examples=150, deadline=None)
@given(
    net=st.integers(min_value=1, max_value=10**11).map(lambda cents: D(cents) / 100),
    cuts=st.lists(st.integers(min_value=1, max_value=999_998), min_size=1, max_size=5, unique=True),
)
def test_property_amounts_always_add_up_to_the_net(net: Decimal, cuts: list[int]) -> None:
    points = [0, *sorted(cuts), 1_000_000]
    shares = [(i + 1, D(points[i + 1] - points[i]) / D(10_000)) for i in range(len(points) - 1)]
    assert validate_shares(shares) == ()
    lines = split_net(net, "INR", shares)
    assert sum(l.amount for l in lines) == net
    assert all(l.amount >= 0 and l.amount == l.amount.quantize(D("0.01")) for l in lines)
    rounded_down = {l.nominee_id: (net * l.share_percent / 100).quantize(D("0.01"), rounding="ROUND_DOWN")
                    for l in lines}
    for line in lines:
        if not line.remainder:
            assert line.amount == rounded_down[line.nominee_id]
        else:  # the remainder nominee never loses and gains less than one minor unit per other nominee
            assert D(0) <= line.amount - rounded_down[line.nominee_id] <= D("0.01") * (len(lines) - 1)
