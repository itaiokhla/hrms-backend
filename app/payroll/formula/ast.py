"""Immutable syntax tree of the formula language, with source positions.

Every node is a frozen dataclass carrying ``pos``/``end``, the 0-based span of
source characters it was parsed from, so editors can underline errors. Parentheses
leave no node behind; a minus sign in front of a number literal is folded into the
literal (``-5`` is one ``Number``). ``AND``/``OR`` chains and their call forms
(``AND(a, b, c)``) are one n-ary ``Logical`` node.

``render`` prints the canonical form (upper-case keywords and functions, minimal
parentheses that preserve the tree), so ``parse(render(e))`` has the same
``shape`` as ``e``. With ``values=True`` percent literals print as the decimal
they stand for (``40%`` as ``0.40``), which is how the evaluation trace shows them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterator, Union


@dataclass(frozen=True, slots=True)
class Number:
    value: Decimal
    text: str  # as written, sign folded in: "40%", "1.5", "-2"
    percent: bool
    pos: int
    end: int

    @property
    def integer(self) -> bool:
        """A whole-number literal written without a decimal point or percent sign."""
        return not self.percent and "." not in self.text


@dataclass(frozen=True, slots=True)
class String:
    value: str
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class Boolean:
    value: bool
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class DateLiteral:
    value: date
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class ComponentRef:
    code: str
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class VariableRef:
    name: str  # full dotted name: "att.paid_days", "emp.attr.region"
    pos: int
    end: int

    @property
    def namespace(self) -> str:
        return self.name.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class Unary:
    op: str  # "-" or "NOT"
    operand: Expr
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class Binary:
    op: str  # "+", "-", "*", "/"
    left: Expr
    right: Expr
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class Compare:
    op: str  # "=", "<>", "<", "<=", ">", ">="
    left: Expr
    right: Expr
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class Logical:
    op: str  # "AND" or "OR"
    operands: tuple[Expr, ...]
    pos: int
    end: int


@dataclass(frozen=True, slots=True)
class Call:
    name: str  # canonical upper case
    args: tuple[Expr, ...]
    pos: int
    end: int


Expr = Union[Number, String, Boolean, DateLiteral, ComponentRef, VariableRef, Unary, Binary, Compare, Logical, Call]
NODE_TYPES: tuple[type, ...] = (
    Number, String, Boolean, DateLiteral, ComponentRef, VariableRef, Unary, Binary, Compare, Logical, Call,
)

_OR, _AND, _COMPARE, _ADD, _MUL, _UNARY, _ATOM = 1, 2, 3, 4, 5, 6, 7


def children(node: Expr) -> tuple[Expr, ...]:
    if isinstance(node, (Binary, Compare)):
        return (node.left, node.right)
    if isinstance(node, Unary):
        return (node.operand,)
    if isinstance(node, Logical):
        return node.operands
    if isinstance(node, Call):
        return node.args
    return ()


def walk(node: Expr) -> Iterator[Expr]:
    """Every node in source order (pre-order)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(children(current)))


def node_count(node: Expr) -> int:
    return sum(1 for _ in walk(node))


def level_step(parent: Expr, child: Expr) -> int:
    """Levels a child adds below its parent: 1, or 0 for the left operand of a same-precedence chain.

    ``a + b - c`` and ``a * b / c`` are one level, like ``a AND b AND c``; only real
    nesting (parentheses, function arguments, operands of other operators) adds depth.
    """
    if (
        isinstance(parent, Binary)
        and isinstance(child, Binary)
        and child is parent.left
        and _precedence(child) == _precedence(parent)
    ):
        return 0
    return 1


def depth(node: Expr) -> int:
    """Nesting depth: 1 for a single literal or reference (see ``level_step``)."""
    best = 0
    stack = [(node, 1)]
    while stack:
        current, level = stack.pop()
        best = max(best, level)
        stack.extend((child, level + level_step(current, child)) for child in children(current))
    return best


def shape(node: Expr) -> tuple:
    """Position-free structural form, for comparing trees parsed from different text."""
    if isinstance(node, Number):
        return ("Number", node.value, node.percent)
    if isinstance(node, (String, Boolean, DateLiteral)):
        return (type(node).__name__, node.value)
    if isinstance(node, ComponentRef):
        return ("ComponentRef", node.code)
    if isinstance(node, VariableRef):
        return ("VariableRef", node.name)
    if isinstance(node, Unary):
        return ("Unary", node.op, shape(node.operand))
    if isinstance(node, (Binary, Compare)):
        return (type(node).__name__, node.op, shape(node.left), shape(node.right))
    if isinstance(node, Logical):
        return ("Logical", node.op, tuple(shape(o) for o in node.operands))
    if isinstance(node, Call):
        return ("Call", node.name, tuple(shape(a) for a in node.args))
    raise TypeError(f"not a formula node: {type(node).__name__}")


def _precedence(node: Expr) -> int:
    if isinstance(node, Logical):
        return _OR if node.op == "OR" else _AND
    if isinstance(node, Compare):
        return _COMPARE
    if isinstance(node, Binary):
        return _ADD if node.op in ("+", "-") else _MUL
    if isinstance(node, Unary):
        return _UNARY
    if isinstance(node, Number) and node.value.is_signed():
        return _UNARY
    return _ATOM


def render(node: Expr, *, values: bool = False) -> str:
    """Canonical source text of a node (see the module docstring)."""
    if isinstance(node, Number):
        if values and node.percent:
            return format(node.value, "f")
        return node.text
    if isinstance(node, String):
        return f'"{node.value}"'
    if isinstance(node, Boolean):
        return "TRUE" if node.value else "FALSE"
    if isinstance(node, DateLiteral):
        return f"DATE({node.value.year}, {node.value.month}, {node.value.day})"
    if isinstance(node, ComponentRef):
        return node.code
    if isinstance(node, VariableRef):
        return node.name
    if isinstance(node, Unary):
        inner = render(node.operand, values=values)
        if _precedence(node.operand) < _ATOM:
            inner = f"({inner})"
        return f"-{inner}" if node.op == "-" else f"NOT {inner}"
    if isinstance(node, (Binary, Compare)):
        own = _precedence(node)
        left = render(node.left, values=values)
        right = render(node.right, values=values)
        if _precedence(node.left) < own or (isinstance(node, Compare) and _precedence(node.left) == own):
            left = f"({left})"
        if _precedence(node.right) <= own:
            right = f"({right})"
        return f"{left} {node.op} {right}"
    if isinstance(node, Logical):
        own = _precedence(node)
        parts = []
        for operand in node.operands:
            text = render(operand, values=values)
            parts.append(f"({text})" if _precedence(operand) <= own else text)
        return f" {node.op} ".join(parts)
    if isinstance(node, Call):
        return f"{node.name}({', '.join(render(a, values=values) for a in node.args)})"
    raise TypeError(f"not a formula node: {type(node).__name__}")
