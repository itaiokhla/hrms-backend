"""Employee tax-profile framework (pure): the field model, schema checks, value validation, nesting, and what the
country packs declare (EC-STA-21, 3.2 §6.1 "Employee tax profile schema", §7.2).

The framework invents nothing: IN (fixture) declares illustrative fields labelled "FIXTURE — not law", AE and US
(drafts) declare none, ZZ (test only) declares what its fictional withholding reads.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.payroll import packs
from app.payroll.packs import tax_profile as tp
from app.payroll.packs.base import FIXTURE_LABEL
from app.payroll.packs.tax_profile import TaxProfileField, TaxProfileSchemaError


def field(code="regime", type_="ENUM", **kw):
    if type_ == "ENUM" and "choices" not in kw:
        kw["choices"] = (("A", "Option A"), ("B", "Option B"))
    return TaxProfileField(code, kw.pop("label", code.title()), type_, **kw)


# --------------------------------------------------------------------------- schema checks
@pytest.mark.parametrize("kwargs, code", [
    (dict(code="Bad Code", type_="BOOL"), "INVALID_FIELD_CODE"),
    (dict(code="a.b.c", type_="BOOL"), "INVALID_FIELD_CODE"),
    (dict(code="x", type_="WHATEVER"), "INVALID_FIELD_TYPE"),
    (dict(code="x", type_="ENUM", choices=()), "INVALID_FIELD"),
    (dict(code="x", type_="ENUM", choices=(("A", "a"), ("A", "again"))), "INVALID_FIELD"),
    (dict(code="x", type_="BOOL", choices=(("A", "a"),)), "INVALID_FIELD"),
    (dict(code="x", type_="INTEGER", minimum=Decimal(5), maximum=Decimal(1)), "INVALID_FIELD"),
    (dict(code="x", type_="MONEY", max_length=5), "INVALID_FIELD"),
    (dict(code="x", type_="IDENTIFIER", pattern="("), "INVALID_FIELD"),
    (dict(code="x", type_="IDENTIFIER", proof_required=True), "INVALID_FIELD"),
    (dict(code="x", type_="TEXT", min_length=9, max_length=3), "INVALID_FIELD"),
])
def test_a_pack_cannot_declare_a_broken_field(kwargs, code):
    with pytest.raises(TaxProfileSchemaError) as info:
        field(**kwargs)
    assert info.value.code == code


def test_a_label_is_required_and_a_group_cannot_also_be_a_field():
    with pytest.raises(TaxProfileSchemaError):
        TaxProfileField("x", "  ", "BOOL")
    with pytest.raises(TaxProfileSchemaError) as info:
        tp.validate_schema([field("a", "BOOL"), field("a", "BOOL")])
    assert info.value.code == "DUPLICATE_FIELD"
    with pytest.raises(TaxProfileSchemaError) as info:
        tp.validate_schema([field("prev", "BOOL"), field("prev.income", "MONEY")])
    assert info.value.code == "FIELD_GROUP_CLASH"
    assert len(tp.validate_schema([field("prev.income", "MONEY"), field("prev.tax", "MONEY")])) == 2


def test_schema_hash_is_stable_and_changes_with_any_field_property():
    base = [field("regime"), field("prev.income", "MONEY", proof_required=True)]
    assert tp.schema_hash(base) == tp.schema_hash(list(base))
    changed = [field("regime"), field("prev.income", "MONEY", proof_required=False)]
    assert tp.schema_hash(base) != tp.schema_hash(changed)
    assert tp.schema_hash([]) != tp.schema_hash(base)


# --------------------------------------------------------------------------- value validation
SCHEMA = (
    field("regime", required=True),
    field("prev.income", "MONEY", minimum=Decimal(0), proof_required=True),
    field("resident", "BOOL"),
    field("dependants", "INTEGER", minimum=Decimal(0), maximum=Decimal(10)),
    field("since", "DATE"),
    field("note", "TEXT", max_length=10),
    field("tax_id", "IDENTIFIER", max_length=12),
)


def check(values, currency="INR", **kw):
    return tp.validate_values(SCHEMA, values, currency, **kw)


def codes(issues):
    return {(i["field"], i["code"]) for i in issues}


def test_a_valid_submission_is_cleaned_and_money_keeps_the_currency_digits():
    clean, issues = check({"regime": "A", "prev.income": "1200.5", "resident": True, "dependants": 2,
                           "since": "2026-04-01", "note": " hello ", "tax_id": " AB-123 "})
    assert issues == []
    assert clean == {"regime": "A", "prev.income": "1200.50", "resident": True, "dependants": 2,
                     "since": "2026-04-01", "note": "hello", "tax_id": "AB-123"}
    clean, _ = check({"regime": "A", "prev.income": "1200"}, currency="JPY")
    assert clean["prev.income"] == "1200"


def test_required_unknown_and_empty_optional_fields():
    clean, issues = check({"prev.income": "", "resident": None, "bogus": "x"})
    assert codes(issues) == {("regime", "REQUIRED"), ("bogus", "UNKNOWN_FIELD")}
    assert clean == {}


@pytest.mark.parametrize("code, value, problem", [
    ("regime", "C", "INVALID_CHOICE"),
    ("regime", 5, "INVALID_CHOICE"),
    ("prev.income", 1200, "NUMBER_AS_TEXT"),          # a JSON number in a money field is refused
    ("prev.income", 1200.5, "NUMBER_AS_TEXT"),
    ("prev.income", "12,00", "INVALID_NUMBER"),
    ("prev.income", "-1", "BELOW_MINIMUM"),
    ("prev.income", "1200.555", "TOO_MANY_DECIMALS"),
    ("prev.income", "NaN", "INVALID_NUMBER"),
    ("prev.income", "1e3", "INVALID_NUMBER"),           # no exponent notation (EC-MON-15)
    ("prev.income", "1_000", "INVALID_NUMBER"),
    ("resident", "true", "INVALID_BOOLEAN"),
    ("resident", 1, "INVALID_BOOLEAN"),
    ("dependants", "2.5", "INVALID_INTEGER"),
    ("dependants", 11, "ABOVE_MAXIMUM"),
    ("dependants", True, "INVALID_NUMBER"),
    ("since", "01/04/2026", "INVALID_DATE"),
    ("since", "2026-02-30", "INVALID_DATE"),
    ("note", "x" * 11, "INVALID_LENGTH"),
    ("tax_id", "ab#12", "INVALID_IDENTIFIER"),
    ("tax_id", "A" * 13, "INVALID_LENGTH"),
])
def test_invalid_values_are_reported_per_field(code, value, problem):
    values = {"regime": "A", code: value}
    _, issues = check(values)
    assert (code, problem) in codes(issues), issues


def test_money_above_the_sane_limit_and_amounts_without_a_whole_part():
    _, issues = check({"regime": "A", "prev.income": "1000000000000"})
    assert ("prev.income", "AMOUNT_TOO_LARGE") in codes(issues)
    clean, issues = check({"regime": "A", "prev.income": "0"})
    assert issues == [] and clean["prev.income"] == "0.00"


def test_an_identifier_pattern_comes_from_the_pack_only():
    schema = (field("id", "IDENTIFIER", pattern=r"[A-Z]{2}[0-9]{3}"),)
    assert tp.validate_values(schema, {"id": "AB123"}, "INR")[1] == []
    assert codes(tp.validate_values(schema, {"id": "AB12"}, "INR")[1]) == {("id", "INVALID_FORMAT")}


def test_a_carried_identifier_counts_as_present():
    schema = (field("id", "IDENTIFIER", required=True),)
    assert codes(tp.validate_values(schema, {}, "INR")[1]) == {("id", "REQUIRED")}
    assert tp.validate_values(schema, {}, "INR", carried=["id"]) == ({}, [])


def test_nest_and_proof_required_codes():
    flat = {"regime": "A", "prev.income": "5.00", "prev.tax": "1.00"}
    assert tp.nest(flat) == {"regime": "A", "prev": {"income": "5.00", "tax": "1.00"}}
    assert tp.proof_required_codes(SCHEMA, {"prev.income": "5.00"}) == ["prev.income"]
    assert tp.proof_required_codes(SCHEMA, {"prev.income": ""}) == []
    assert tp.proof_required_codes(SCHEMA, {}) == []


# --------------------------------------------------------------------------- what the packs declare
def test_in_fixture_declares_labelled_illustrative_fields_ae_and_us_declare_none_zz_declares_its_own():
    by_code = {p.code: p for p in packs.all_packs(environment="test")}
    declared = by_code["IN"].tax_profile_schema()
    assert [f.code for f in declared] == ["regime", "previous_employment.income", "previous_employment.tax", "tax_id"]
    assert all(FIXTURE_LABEL in f.note for f in declared)
    assert next(f for f in declared if f.code == "tax_id").type == "IDENTIFIER"
    assert by_code["AE"].tax_profile_schema() == () and by_code["US"].tax_profile_schema() == ()
    assert [f.code for f in by_code["ZZ"].tax_profile_schema()] == ["resident", "dependants"]
    for pack in by_code.values():  # every declared schema is itself valid
        tp.validate_schema(pack.tax_profile_schema())


def test_declared_values_have_the_shape_the_in_fixture_reads_from_the_snapshot():
    schema = packs.find_pack("IN", environment="test").tax_profile_schema()
    clean, issues = tp.validate_values(schema, {"regime": "NEW", "previous_employment.income": "1000000.00",
                                                 "previous_employment.tax": "40000.00"}, "INR")
    assert issues == []
    vector = next(v for v in packs.find_pack("IN", environment="test").test_vectors() if v.id == "IN-V12")
    assert tp.nest(clean) == dict(vector.tax_profile)


def test_the_declaration_pack_of_a_country_depends_on_use_in_this_environment():
    on = date(2026, 10, 15)
    assert packs.tax_profile_pack("IN", on, environment="test").code == "IN"
    assert packs.tax_profile_pack("IN", on, environment="development").code == "IN"
    # A fixture never runs in production or staging: no form is built on data the platform will not use.
    assert packs.tax_profile_pack("IN", on, environment="production") is None
    assert packs.tax_profile_pack("IN", on, environment="staging") is None
    assert packs.tax_profile_pack("AE", on, environment="test") is None
    assert packs.tax_profile_pack("US", on, environment="test") is None
    assert packs.tax_profile_pack("DE", on, environment="test") is None
    assert packs.tax_profile_pack("ZZ", on, environment="test").code == "ZZ"
    assert packs.tax_profile_pack("ZZ", on, environment="production") is None   # test-only pack


def test_a_pack_without_an_override_declares_nothing():
    from app.payroll.packs.base import BasePack

    assert BasePack().tax_profile_schema() == ()
