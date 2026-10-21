"""Rate tables read by SLAB, BAND and LOOKUP (3.3 §4.2; 3.9 ``payroll_tables``).

A ``Table`` is the data effective for one period, supplied by a ``TableProvider``
(the run snapshot, or the tester's sample data). Its kind decides which function
may read it, so a progressive table can never be applied as a flat band by
mistake:

* ``SLAB`` (progressive): rows ``[lower, upper)`` with a marginal ``rate`` (a
  fraction: 0.05 is 5%). ``SLAB(t, x) = Σ rate × (min(x, upper) − lower)`` over
  the rows that ``x`` exceeds. Rows must be contiguous; the last may be open.
* ``BAND`` (flat band): rows with a flat ``value``. By default a row matches
  ``lower <= x < upper``; ``upper_inclusive=True`` switches to
  ``lower < x <= upper`` for laws written as "above X up to Y". An open end is
  ``None``. A value in no row is NO_BAND_MATCH; rows may leave gaps, never overlap.
* ``LOOKUP``: rows with a text ``key`` and a ``value``; a missing key returns the
  formula's default.

``key_currency`` is the currency of the bounds (``x`` must be money in it; ``None``
means plain numbers) and ``value_currency`` the currency of BAND/LOOKUP values.
Malformed data raises INVALID_TABLE when the ``Table`` is built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Protocol, Union, runtime_checkable

from app.payroll.formula.errors import ErrorCode, FormulaError
from app.payroll.formula.types import new_context, plain_decimal, validate_currency

TABLE_CODE = re.compile(r"[A-Z][A-Z0-9_]*")


class TableKind(StrEnum):
    SLAB = "SLAB"
    BAND = "BAND"
    LOOKUP = "LOOKUP"


def _invalid(code: str, message: str) -> FormulaError:
    return FormulaError(ErrorCode.INVALID_TABLE, f"table {code}: {message}")


def _number(raw: object, code: str, what: str) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (Decimal, int)):
        raise _invalid(code, f"{what} must be a Decimal, got {type(raw).__name__}")
    number = Decimal(raw)
    if not number.is_finite():
        raise _invalid(code, f"{what} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class TableRow:
    value: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    key: str | None = None
    rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TableSig:
    """What static analysis needs to know about a table."""

    kind: TableKind
    key_currency: str | None = None
    value_currency: str | None = None


@dataclass(frozen=True)
class Table:
    code: str
    kind: TableKind
    rows: tuple[TableRow, ...]
    key_currency: str | None = None
    value_currency: str | None = None
    upper_inclusive: bool = False
    _keys: dict[str, Decimal] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not TABLE_CODE.fullmatch(self.code) or "__" in self.code:
            raise FormulaError(ErrorCode.INVALID_TABLE, f"table code {self.code!r} must be UPPER_SNAKE")
        try:
            kind = TableKind(self.kind)
        except ValueError:
            raise _invalid(self.code, f"kind must be SLAB, BAND or LOOKUP, got {self.kind!r}") from None
        object.__setattr__(self, "kind", kind)
        for currency in (self.key_currency, self.value_currency):
            if currency is not None:
                try:
                    validate_currency(currency)
                except ValueError as exc:
                    raise _invalid(self.code, str(exc)) from None
        rows = tuple(self._normalise(row) for row in self.rows)
        if not rows:
            raise _invalid(self.code, "has no rows")
        if kind is TableKind.LOOKUP:
            if self.key_currency is not None:
                raise _invalid(self.code, "LOOKUP tables are keyed by text; key_currency must be empty")
            keys: dict[str, Decimal] = {}
            for row in rows:
                if row.key is None or row.value is None or row.lower is not None or row.upper is not None:
                    raise _invalid(self.code, "LOOKUP rows need a key and a value, and no bounds")
                if row.rate is not None:
                    raise _invalid(self.code, "LOOKUP rows have no rate")
                if row.key in keys:
                    raise _invalid(self.code, f'key "{row.key}" appears twice')
                keys[row.key] = row.value
            object.__setattr__(self, "_keys", keys)
        else:
            rows = self._ranges(kind, rows)
            object.__setattr__(self, "_keys", {})
        object.__setattr__(self, "rows", rows)

    def _normalise(self, row: TableRow) -> TableRow:
        if not isinstance(row, TableRow):
            raise _invalid(self.code, f"rows must be TableRow, got {type(row).__name__}")
        if row.key is not None and (not isinstance(row.key, str) or not row.key):
            raise _invalid(self.code, "keys must be non-empty text")
        return TableRow(
            value=_number(row.value, self.code, "value"),
            lower=_number(row.lower, self.code, "lower"),
            upper=_number(row.upper, self.code, "upper"),
            key=row.key,
            rate=_number(row.rate, self.code, "rate"),
        )

    def _ranges(self, kind: TableKind, rows: tuple[TableRow, ...]) -> tuple[TableRow, ...]:
        for row in rows:
            if row.key is not None:
                raise _invalid(self.code, f"{kind} rows have no key")
            if row.lower is not None and row.upper is not None and row.lower >= row.upper:
                raise _invalid(self.code, "every row needs lower < upper")
            if kind is TableKind.SLAB:
                if row.rate is None or row.lower is None:
                    raise _invalid(self.code, "SLAB rows need a lower bound and a rate")
                if row.rate < 0 or row.rate > 1:
                    raise _invalid(self.code, "SLAB rates are fractions between 0 and 1 (5% is 0.05)")
                if row.value is not None:
                    raise _invalid(self.code, "SLAB rows use rate only; leave value empty")
            elif row.value is None or row.rate is not None:
                raise _invalid(self.code, "BAND rows need a flat value and no rate")
        if self.value_currency is not None and kind is TableKind.SLAB:
            raise _invalid(self.code, "a SLAB result is in the key currency; value_currency must be empty")
        lowest = Decimal("-Infinity")
        ordered = sorted(rows, key=lambda r: lowest if r.lower is None else r.lower)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.upper is None or current.lower is None or current.lower < previous.upper:
                raise _invalid(self.code, "rows overlap")
            if kind is TableKind.SLAB and current.lower != previous.upper:
                raise _invalid(self.code, "SLAB rows must be contiguous (each lower equals the previous upper)")
        return tuple(ordered)

    @property
    def signature(self) -> TableSig:
        return TableSig(self.kind, self.key_currency, self.value_currency)

    def slab(self, x: Decimal) -> tuple[Decimal, list[str]]:
        """Progressive amount for ``x`` and the parts that make it up."""
        context = new_context()
        total = Decimal(0)
        parts: list[str] = []
        for row in self.rows:
            assert row.lower is not None and row.rate is not None
            if x <= row.lower:
                break
            top = x if row.upper is None or x < row.upper else row.upper
            portion = context.subtract(top, row.lower)
            total = context.add(total, context.multiply(portion, row.rate))
            parts.append(f"{plain_decimal(row.rate)} × {plain_decimal(portion)}")
        return total, parts

    def band(self, x: Decimal) -> TableRow | None:
        for row in self.rows:
            if self.upper_inclusive:
                inside = (row.lower is None or x > row.lower) and (row.upper is None or x <= row.upper)
            else:
                inside = (row.lower is None or x >= row.lower) and (row.upper is None or x < row.upper)
            if inside:
                return row
        return None

    def lookup(self, key: str) -> Decimal | None:
        return self._keys.get(key)


@runtime_checkable
class TableProvider(Protocol):
    def get_table(self, code: str) -> Table | None: ...


TableSource = Union[TableProvider, Mapping[str, Union[Table, TableSig]]]


def resolve_table(source: TableSource, code: str) -> Table | TableSig | None:
    """Look a table code up in a provider or a mapping of tables or signatures."""
    if isinstance(source, TableProvider):
        return source.get_table(code)
    return source.get(code)


def describe_band(row: TableRow) -> str:
    low = "…" if row.lower is None else plain_decimal(row.lower)
    high = "…" if row.upper is None else plain_decimal(row.upper)
    return f"{low}–{high}"
