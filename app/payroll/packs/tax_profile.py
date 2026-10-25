"""Employee tax-profile schema of a country pack (3.2 §6.1 "Employee tax profile schema", §7.2, §7.3).

A pack declares which declaration fields the jurisdiction needs (and only those) through
``BasePack.tax_profile_schema()``: a tuple of ``TaxProfileField``. This module is pure (no database, no clock,
no network, no import of the pack classes): it defines the field model, checks a schema for programming errors
and validates a submitted set of values against it. The framework invents nothing: field codes, labels, choices,
limits and the proof requirement come from the pack; a pack that declares no field offers no declaration.

Value types and what they store (one flat ``code -> value`` mapping per profile version):

    ENUM        one of the declared choices (stored as the choice's value string)
    MONEY       a decimal string in the pack's currency, never a JSON number, never negative, with at most the
                currency's minor-unit digits (stored through ``money.amount_str``)
    DECIMAL     a decimal string within the optional min/max
    INTEGER     a whole number within the optional min/max
    BOOL        true or false (a JSON boolean only)
    DATE        an ISO 8601 date
    TEXT        free text up to ``max_length`` characters
    IDENTIFIER  a tax or social identifier: kept encrypted and masked by the service, never part of the payroll
                snapshot; the pack may supply a ``pattern`` taken from a sourced rule, the framework adds none

A code may contain one dot to group fields (``previous_employment.income``); ``nest`` turns the flat values into
the nested mapping packs read from the snapshot. A code is never both a field and the group of another field.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.payroll.money import MoneyError, amount_str, to_decimal

FIELD_TYPES = ("ENUM", "MONEY", "DECIMAL", "INTEGER", "BOOL", "DATE", "TEXT", "IDENTIFIER")
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39})?$")
MAX_MONEY = Decimal("999999999999.99")
MAX_TEXT = 500
MAX_IDENTIFIER = 60
IDENTIFIER_CHARS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./_\-]*$")
MAX_FIELDS = 40
PLAIN_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?")


class TaxProfileSchemaError(ValueError):
    """A schema that a pack must not declare (a programming error, never a run-time data problem)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TaxProfileField:
    """One declaration field a pack needs from the employee."""

    code: str
    label: str
    type: str
    required: bool = False
    help: str = ""
    group: str = ""
    choices: Tuple[Tuple[str, str], ...] = ()            # (value, label) of an ENUM
    minimum: Optional[Decimal] = None                      # DECIMAL / INTEGER / MONEY lower bound
    maximum: Optional[Decimal] = None
    min_length: int = 0                                    # TEXT / IDENTIFIER
    max_length: Optional[int] = None
    pattern: Optional[str] = None                          # IDENTIFIER / TEXT, from a sourced rule only
    proof_required: bool = False                           # HR cannot approve a value without a proof (§7.3)
    note: str = ""                                         # shown next to the field, e.g. "FIXTURE — not law"

    def __post_init__(self) -> None:
        object.__setattr__(self, "choices", tuple((str(v), str(l)) for v, l in self.choices))
        if self.type not in FIELD_TYPES:
            raise TaxProfileSchemaError("INVALID_FIELD_TYPE", f"{self.code}: type must be one of {FIELD_TYPES}")
        if not CODE_PATTERN.match(self.code or ""):
            raise TaxProfileSchemaError("INVALID_FIELD_CODE", f"{self.code!r}: use lower-case letters, digits, "
                                        "underscores and at most one dot")
        if not (self.label or "").strip():
            raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: a label is required")
        if self.type == "ENUM":
            values = [v for v, _ in self.choices]
            if not values or len(values) != len(set(values)):
                raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: an ENUM needs distinct choices")
        elif self.choices:
            raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: only an ENUM has choices")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: minimum is above maximum")
        if self.type in ("TEXT", "IDENTIFIER"):
            cap = MAX_IDENTIFIER if self.type == "IDENTIFIER" else MAX_TEXT
            if self.max_length is not None and not (1 <= self.max_length <= cap):
                raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: max_length must be 1 to {cap}")
            if self.min_length < 0 or (self.max_length is not None and self.min_length > self.max_length):
                raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: min_length is out of range")
            if self.pattern is not None:
                try:
                    re.compile(self.pattern)
                except re.error as exc:
                    raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: pattern does not compile: {exc}") from None
        elif self.pattern is not None or self.min_length or self.max_length is not None:
            raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: length and pattern limits are for text fields")
        if self.proof_required and self.type == "IDENTIFIER":
            raise TaxProfileSchemaError("INVALID_FIELD", f"{self.code}: an identifier is not proven by a document")

    @property
    def parent(self) -> Optional[str]:
        return self.code.split(".", 1)[0] if "." in self.code else None

    def to_json(self) -> Dict[str, Any]:
        return {
            "code": self.code, "label": self.label, "type": self.type, "required": self.required,
            "help": self.help, "group": self.group, "note": self.note, "proof_required": self.proof_required,
            "choices": [{"value": v, "label": l} for v, l in self.choices],
            "minimum": None if self.minimum is None else str(self.minimum),
            "maximum": None if self.maximum is None else str(self.maximum),
            "min_length": self.min_length, "max_length": self.max_length,
            "sensitive": self.type == "IDENTIFIER",
        }


def validate_schema(schema: Sequence[TaxProfileField]) -> Tuple[TaxProfileField, ...]:
    """The schema as a tuple, or ``TaxProfileSchemaError`` (duplicate codes, a field that is also a group)."""
    fields = tuple(schema)
    if len(fields) > MAX_FIELDS:
        raise TaxProfileSchemaError("TOO_MANY_FIELDS", f"a tax profile has at most {MAX_FIELDS} fields")
    codes = [f.code for f in fields]
    if len(codes) != len(set(codes)):
        raise TaxProfileSchemaError("DUPLICATE_FIELD", "a field code is declared twice")
    groups = {f.parent for f in fields if f.parent}
    clash = sorted(groups & set(codes))
    if clash:
        raise TaxProfileSchemaError("FIELD_GROUP_CLASH", f"{clash[0]} is both a field and a group of other fields")
    return fields


def schema_json(schema: Iterable[TaxProfileField]) -> List[Dict[str, Any]]:
    return [f.to_json() for f in schema]


def schema_hash(schema: Iterable[TaxProfileField]) -> str:
    """SHA-256 of the canonical schema: a stored profile records the schema it was filled in against."""
    canonical = json.dumps(schema_json(schema), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- validation


def _issue(code: str, field_code: str, message: str) -> Dict[str, str]:
    return {"code": code, "field": field_code, "message": message}


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


is_empty = _empty


def _check_decimal(f: TaxProfileField, raw: Any) -> Tuple[Optional[Decimal], Optional[Dict[str, str]]]:
    if f.type in ("MONEY", "DECIMAL") and not isinstance(raw, str):
        return None, _issue("NUMBER_AS_TEXT", f.code, f"{f.label}: send the amount as text, for example \"1200.50\".")
    if isinstance(raw, str) and not PLAIN_NUMBER.fullmatch(raw.strip()):   # no exponent, no separators (EC-MON-15)
        return None, _issue("INVALID_NUMBER", f.code, f"{f.label}: enter a plain number such as 1200.50.")
    try:
        value = to_decimal(raw)
    except MoneyError:
        return None, _issue("INVALID_NUMBER", f.code, f"{f.label}: enter a number as text, for example \"1200.50\".")
    if f.type == "INTEGER" and value != value.to_integral_value():
        return None, _issue("INVALID_INTEGER", f.code, f"{f.label}: enter a whole number.")
    if f.minimum is not None and value < f.minimum:
        return None, _issue("BELOW_MINIMUM", f.code, f"{f.label}: at least {f.minimum}.")
    if f.maximum is not None and value > f.maximum:
        return None, _issue("ABOVE_MAXIMUM", f.code, f"{f.label}: at most {f.maximum}.")
    return value, None


def _clean_one(f: TaxProfileField, raw: Any, currency: str) -> Tuple[Any, Optional[Dict[str, str]]]:
    if f.type == "ENUM":
        if not isinstance(raw, str) or raw not in {v for v, _ in f.choices}:
            return None, _issue("INVALID_CHOICE", f.code, f"{f.label}: choose one of the listed options.")
        return raw, None
    if f.type == "BOOL":
        if not isinstance(raw, bool):
            return None, _issue("INVALID_BOOLEAN", f.code, f"{f.label}: answer yes or no.")
        return raw, None
    if f.type == "DATE":
        if not isinstance(raw, str):
            return None, _issue("INVALID_DATE", f.code, f"{f.label}: enter a date as YYYY-MM-DD.")
        try:
            return date.fromisoformat(raw.strip()).isoformat(), None
        except ValueError:
            return None, _issue("INVALID_DATE", f.code, f"{f.label}: enter a valid date as YYYY-MM-DD.")
    if f.type in ("TEXT", "IDENTIFIER"):
        if not isinstance(raw, str):
            return None, _issue("INVALID_TEXT", f.code, f"{f.label}: enter text.")
        text = raw.strip()
        cap = f.max_length or (MAX_IDENTIFIER if f.type == "IDENTIFIER" else MAX_TEXT)
        if len(text) > cap or len(text) < max(f.min_length, 1):
            return None, _issue("INVALID_LENGTH", f.code, f"{f.label}: {max(f.min_length, 1)} to {cap} characters.")
        if f.type == "IDENTIFIER" and not IDENTIFIER_CHARS.match(text):
            return None, _issue("INVALID_IDENTIFIER", f.code, f"{f.label}: letters, digits, spaces and . / _ - only.")
        if f.pattern is not None and not re.fullmatch(f.pattern, text):
            return None, _issue("INVALID_FORMAT", f.code, f"{f.label}: the format is not valid.")
        return text, None
    value, problem = _check_decimal(f, raw)
    if problem is not None:
        return None, problem
    assert value is not None
    if f.type == "INTEGER":
        return int(value), None
    if f.type == "MONEY":
        if value < 0:
            return None, _issue("NEGATIVE_AMOUNT", f.code, f"{f.label}: an amount cannot be negative.")
        if value > MAX_MONEY:
            return None, _issue("AMOUNT_TOO_LARGE", f.code, f"{f.label}: the amount is too large.")
        try:
            return amount_str(value, currency), None
        except MoneyError:
            return None, _issue("TOO_MANY_DECIMALS", f.code, f"{f.label}: at most the {currency} minor-unit digits.")
    return format(value.normalize(), "f"), None


def validate_values(schema: Sequence[TaxProfileField], values: Mapping[str, Any], currency: str, *,
                    carried: Iterable[str] = ()) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """``(clean flat values, issues)`` for a submission against the pack schema.

    Unknown codes are issues (never silently dropped), an empty optional field is left out, a required field
    must be present. ``carried`` names identifier fields whose earlier value the service keeps when the client
    sends none, so they count as present."""
    by_code = {f.code: f for f in schema}
    kept = set(carried)
    clean: Dict[str, Any] = {}
    issues: List[Dict[str, str]] = []
    for code in sorted(set(values) - set(by_code)):
        issues.append(_issue("UNKNOWN_FIELD", code, f"{code} is not a field of this country's tax profile."))
    for f in schema:
        raw = values.get(f.code)
        if _empty(raw):
            if f.required and f.code not in kept:
                issues.append(_issue("REQUIRED", f.code, f"{f.label} is required."))
            continue
        value, problem = _clean_one(f, raw, currency)
        if problem is not None:
            issues.append(problem)
        else:
            clean[f.code] = value
    return clean, issues


def nest(flat: Mapping[str, Any]) -> Dict[str, Any]:
    """``{"a": 1, "g.x": 2}`` -> ``{"a": 1, "g": {"x": 2}}``: the shape packs read from the snapshot."""
    result: Dict[str, Any] = {}
    for code in sorted(flat):
        head, _, tail = code.partition(".")
        if tail:
            result.setdefault(head, {})[tail] = flat[code]
        else:
            result[head] = flat[code]
    return result


def proof_required_codes(schema: Sequence[TaxProfileField], values: Mapping[str, Any]) -> List[str]:
    """Fields with a value that the pack wants proven (HR cannot approve them without a proof, §7.3)."""
    return [f.code for f in schema if f.proof_required and not _empty(values.get(f.code))]


__all__ = [
    "CODE_PATTERN", "FIELD_TYPES", "MAX_FIELDS", "TaxProfileField", "TaxProfileSchemaError", "is_empty", "nest",
    "proof_required_codes", "schema_hash", "schema_json", "validate_schema", "validate_values",
]
