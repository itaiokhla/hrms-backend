"""Snapshot schema: strict parsing, normalized round trip, canonical JSON and the content hash (3.5 §8)."""

from __future__ import annotations

import copy
import datetime as dt
import json
from decimal import Decimal

import pytest

from app.payroll.engine.context import build_context
from app.payroll.engine.snapshot import Snapshot, SnapshotError, canonical_json, content_hash
from tests.payroll_unit.test_engine_fixtures import days, snapshot


def parse(data) -> Snapshot:
    return Snapshot.from_dict(data)


def error_of(data) -> SnapshotError:
    with pytest.raises(SnapshotError) as caught:
        parse(data)
    return caught.value


def test_round_trip_is_identity_and_hash_stable():
    data = snapshot(
        overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 90,
                           "start": "2026-11-05T18:00:00+05:30", "end": "2026-11-05T19:30:00+05:30"}],
        shifts=[{"date": "2026-11-05", "start": "2026-11-05T22:00:00+05:30", "end": "2026-11-06T06:00:00+05:30"}],
        wage_inputs=[{"id": 1, "component": "BONUS", "amount": "10000.00"}],
        reimbursements=[{"id": 3, "claim_type": "TRAVEL", "amount": "12.50", "currency": "INR"}],
        previous={"net": "1.00", "history": {"BONUS": ["5.00"]}, "bank_account_ids": [2, 1]},
        ytd={"BASIC": "350000.00"},
        tasks={"records": [{"task_id": "T", "task_type": "X", "status": "APPROVED", "quantity": "2",
                            "assignees": [{"employee_id": "501"}]}]},
        ledger_overrides={"2026-11-03": {"action": "TREAT_AS_PRESENT", "source_id": 5}},
        switch_ranges={"payroll.overtime.enabled": [["2026-11-01", None]]},
        pack={"code": "ZZ", "version": "ZZ-2026.1"}, pack_state={"esi": {"covered": True}},
    )
    snap = parse(data)
    normalized = snap.to_dict()
    assert parse(normalized) == snap
    assert parse(normalized).to_dict() == normalized
    assert snap.content_hash == content_hash(data) == content_hash(normalized)
    assert len(snap.content_hash) == 64


def test_content_hash_changes_with_any_input():
    base = content_hash(snapshot())
    changed = snapshot()
    changed["days"][4]["manual_lop"] = [{"source_id": 1}]
    assert content_hash(changed) != base
    other = snapshot(settings={"payroll.lop.proration_basis": "FIXED_30"})
    assert content_hash(other) != base


def test_canonical_json_sorted_compact_decimals_as_strings():
    text = canonical_json({"b": Decimal("1.50"), "a": [dt.date(2026, 1, 2)], "c": {"z": 1, "y": None}})
    assert text == '{"a":["2026-01-02"],"b":"1.50","c":{"y":null,"z":1}}'
    with pytest.raises(SnapshotError):
        canonical_json({"x": 1.5})


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.update(extra=1), "UNKNOWN_KEY"),
    (lambda d: d["employee"].update(nickname="x"), "UNKNOWN_KEY"),
    (lambda d: d.pop("currency"), "MISSING_KEY"),
    (lambda d: d["segments"][0]["compensation"]["lines"][0].update(amount=1.5), "INVALID_NUMBER"),
    (lambda d: d.update(ytd={"BASIC": 10.0}), "INVALID_NUMBER"),
    (lambda d: d["settings"].update({"payroll.x": 0.1}), "INVALID_NUMBER"),
    (lambda d: d.update(overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 60,
                                           "start": "2026-11-05T18:00:00"}]), "INVALID_DATE"),
    (lambda d: d.update(schema_version=2), "UNSUPPORTED_SCHEMA"),
    (lambda d: d.update(segments=[]), "MISSING_KEY"),
    (lambda d: d.update(components=d["components"] + [d["components"][0]]), "DUPLICATE_COMPONENT"),
    (lambda d: d["period"].update(end="2026-10-01"), "INVALID_SNAPSHOT"),
    (lambda d: d["days"][0].update(attendance={"status": "MAYBE"}), "INVALID_CHOICE"),
])
def test_strict_parsing_refuses_malformed_snapshots(mutate, code):
    data = copy.deepcopy(snapshot())
    mutate(data)
    assert error_of(data).code == code


def test_fixed_line_needs_exactly_one_amount():
    data = snapshot()
    data["segments"][0]["compensation"]["lines"] = [{"component": "BASIC", "method": "FIXED"}]
    assert "exactly one" in error_of(data).message


def test_overlapping_segments_refused():
    data = snapshot(segments=[
        {"key": "A", "start": "2026-11-01", "end": "2026-11-20"},
        {"key": "B", "start": "2026-11-15", "end": "2026-11-30"},
    ])
    assert error_of(data).code == "OVERLAPPING_SEGMENTS"


def test_snapshot_is_valid_json_after_to_dict():
    snap = parse(snapshot(days=days()))
    assert json.loads(json.dumps(snap.to_dict())) == snap.to_dict()


def test_employee_get_exposes_pack_facts_including_protected_attributes():
    data = snapshot(employee={"employee_id": 1, "hire_date": "2020-01-01", "country": "IN", "subdivision": "IN-KA",
                              "protected": {"gender": "FEMALE", "date_of_birth": "1990-05-01"}})
    employee = parse(data).employee
    assert employee.get("branch_subdivision") == "IN-KA"
    assert employee.get("gender") == "FEMALE"
    assert employee.get("date_of_birth") == dt.date(1990, 5, 1)
    assert employee.get("date_of_joining") == dt.date(2020, 1, 1)
    assert employee.get("missing", "x") == "x"


def test_context_settings_fall_back_to_registry_defaults_never_invented():
    ctx = build_context(parse(snapshot(settings={})))
    assert ctx.setting("payroll.lop.proration_basis") == "CALENDAR_DAYS"
    assert ctx.setting("payroll.deductions.min_net_pay_percent") is None
    assert ctx.min_net_pay_percent is None
    assert ctx.switch_ranges("payroll.overtime.enabled") == ()
    assert ctx.switch_ranges("payroll.holiday_pay.enabled") == ()  # legacy alias of the non-salaried switch


def test_context_exposes_stage_contract():
    ctx = build_context(parse(snapshot(run={"environment": "test", "run_type": "FINAL_SETTLEMENT"},
                                       loans=[{"loan_id": 1}], attestation={"active": True})))
    assert (ctx.currency, ctx.minor_unit, ctx.quantum) == ("INR", 2, Decimal("0.01"))
    assert ctx.period_start == dt.date(2026, 11, 1) and ctx.period_end == dt.date(2026, 11, 30)
    assert ctx.tax_periods_remaining == 5 and ctx.final_settlement and not ctx.is_production
    assert ctx.loans[0]["loan_id"] == 1 and ctx.attestation["active"] is True
    assert ctx.country == "IN" and ctx.minor_units["INR"] == 2


def test_unusable_currency_refused():
    from app.payroll.engine.context import ContextError

    with pytest.raises(ContextError):
        build_context(parse(snapshot(currency="XAU")))
