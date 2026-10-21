"""Recursive-descent parser for the grammar of 3.3 §4.1.

    expression  := logical_or
    logical_or  := logical_and ( "OR" logical_and )*
    logical_and := comparison ( "AND" comparison )*
    comparison  := additive ( ("=" | "<>" | "<" | "<=" | ">" | ">=") additive )?
    additive    := term ( ("+" | "-") term )*
    term        := unary ( ("*" | "/") unary )*
    unary       := ("-" | "NOT")? primary
    primary     := number | number "%" | string | TRUE | FALSE | DATE(y, m, d)
                 | COMPONENT_CODE | namespace "." name | FUNCTION(args) | "(" expression ")"

Keywords and function names are case-insensitive; component codes are UPPER_SNAKE
and namespaced variables lower case (``att.paid_days``, ``emp.attr.region``).
Comparisons do not chain (write ``a < b AND b < c``). ``NOT`` applies to the next
primary, so ``NOT (a >= b)`` needs the parentheses.

The parser guarantees a well-formed tree: known functions with valid arity,
quoted codes where a function needs them (``SUM_TAG("OT_BASE")``), and limits of
MAX_LENGTH characters, MAX_NODES nodes and MAX_DEPTH levels (parentheses, call
arguments and operands; a chain such as ``a + b - c`` is one level, ``ast.depth``),
all LIMIT_EXCEEDED (EC-CMP-09). Reference resolution,
types and currencies are checked by ``analysis``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from app.payroll.formula.ast import (
    NODE_TYPES,
    Binary,
    Boolean,
    Call,
    Compare,
    ComponentRef,
    DateLiteral,
    Expr,
    Logical,
    Number,
    String,
    Unary,
    VariableRef,
    children,
    level_step,
)
from app.payroll.formula.errors import ErrorCode, FormulaError
from app.payroll.formula.functions import CODE, FUNCTIONS, ROUNDING_MODES, Quoted
from app.payroll.formula.lexer import KEYWORDS, Token, TokenKind, tokenize
from app.payroll.formula.types import new_context

MAX_NODES = 500
MAX_DEPTH = 50
_COMPARISONS = frozenset({"=", "<>", "<", "<=", ">", ">="})
_LOWER_NAME = re.compile(r"[a-z][a-z0-9_]*")
RESERVED_WORDS = frozenset(KEYWORDS | set(FUNCTIONS))


def is_valid_component_code(code: str) -> bool:
    """UPPER_SNAKE, no "__", and not a keyword or function name."""
    return (
        isinstance(code, str)
        and CODE.fullmatch(code) is not None
        and "__" not in code
        and code not in RESERVED_WORDS
    )


def parse(source: str) -> Expr:
    """Parse a formula into an immutable syntax tree.

    Raises FormulaError: PARSE_ERROR (syntax), UNKNOWN_REFERENCE (unknown function)
    or LIMIT_EXCEEDED (length, nodes, depth). Nothing in the formula is executed.
    """
    return _Parser(source).parse()


def enforce_limits(expr: Expr) -> None:
    """Apply the node and depth limits to a tree that was not produced by ``parse``.

    Raises TypeError for a malformed tree (a non-node, or a literal holding a float,
    a non-finite number or a value of the wrong type), so no float can enter evaluation.
    """
    count = 0
    stack: list[tuple[Expr, int]] = [(expr, 1)]
    while stack:
        node, level = stack.pop()
        _check_node(node)
        count += 1
        if count > MAX_NODES or level > MAX_DEPTH:
            limit = f"{MAX_NODES} elements" if count > MAX_NODES else f"{MAX_DEPTH} levels of nesting"
            raise FormulaError(ErrorCode.LIMIT_EXCEEDED, f"formulas are limited to {limit}",
                               position=node.pos, end=node.end)
        stack.extend((child, level + level_step(node, child)) for child in children(node))


def _check_node(node: object) -> None:
    if not isinstance(node, NODE_TYPES):
        raise TypeError(f"not a formula node: {type(node).__name__}")
    if isinstance(node, Number):
        valid = isinstance(node.value, Decimal) and node.value.is_finite() and isinstance(node.text, str)
    elif isinstance(node, Boolean):
        valid = isinstance(node.value, bool)
    elif isinstance(node, DateLiteral):
        valid = isinstance(node.value, date) and not isinstance(node.value, datetime)
    elif isinstance(node, String):
        valid = isinstance(node.value, str)
    elif isinstance(node, ComponentRef):
        valid = isinstance(node.code, str)
    elif isinstance(node, VariableRef):
        valid = isinstance(node.name, str)
    elif isinstance(node, Call):
        valid = isinstance(node.name, str)
    else:
        valid = isinstance(node.op, str)
    if not valid:
        raise TypeError(f"malformed {type(node).__name__} node: literals hold Decimal, str, bool or date values")


class _Parser:
    def __init__(self, source: str) -> None:
        self.tokens = tokenize(source)
        self.index = 0
        self.count = 0
        self.nesting = 0
        self.heights: dict[int, int] = {}

    # -- token helpers ---------------------------------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind is not TokenKind.EOF:
            self.index += 1
        return token

    def at_operator(self, *ops: str) -> bool:
        return self.current.kind is TokenKind.OPERATOR and self.current.text in ops

    def at_keyword(self, word: str) -> bool:
        return self.current.kind is TokenKind.KEYWORD and self.current.text == word

    def error(self, message: str, token: Token | None = None, code: ErrorCode = ErrorCode.PARSE_ERROR) -> FormulaError:
        token = token or self.current
        return FormulaError(code, message, position=token.pos, end=max(token.end, token.pos + 1))

    def unexpected(self, token: Token | None = None) -> FormulaError:
        token = token or self.current
        return self.error(f"unexpected {token.describe()}", token)

    # -- node construction and limits ------------------------------------------------------------

    def make(self, node: Expr) -> Expr:
        self.count += 1
        if self.count > MAX_NODES:
            raise FormulaError(
                ErrorCode.LIMIT_EXCEEDED,
                f"formulas are limited to {MAX_NODES} elements; split the calculation into components",
                position=node.pos,
                end=node.end,
            )
        height = max((self.heights[id(child)] + level_step(node, child) for child in children(node)), default=1)
        if height > MAX_DEPTH:
            raise FormulaError(
                ErrorCode.LIMIT_EXCEEDED,
                f"formulas are limited to {MAX_DEPTH} levels of nesting",
                position=node.pos,
                end=node.end,
            )
        self.heights[id(node)] = height
        return node

    def enter(self, token: Token) -> None:
        self.nesting += 1
        if self.nesting > MAX_DEPTH:
            raise FormulaError(
                ErrorCode.LIMIT_EXCEEDED,
                f"formulas are limited to {MAX_DEPTH} levels of nesting",
                position=token.pos,
                end=token.end,
            )

    # -- grammar ---------------------------------------------------------------------------------

    def parse(self) -> Expr:
        if self.current.kind is TokenKind.EOF:
            raise self.error("the formula is empty")
        node = self.expression()
        if self.current.kind is not TokenKind.EOF:
            if self.current.kind is TokenKind.OPERATOR and self.current.text in _COMPARISONS:
                raise self.error("comparisons cannot be chained; combine them with AND")
            raise self.unexpected()
        return node

    def expression(self) -> Expr:
        return self.logical("OR")

    def logical(self, op: str) -> Expr:
        operand = self.logical("AND") if op == "OR" else self.comparison()
        operands = [operand]
        while self.at_keyword(op):
            self.advance()
            operands.append(self.logical("AND") if op == "OR" else self.comparison())
        if len(operands) == 1:
            return operand
        return self.make(Logical(op, tuple(operands), operands[0].pos, operands[-1].end))

    def comparison(self) -> Expr:
        left = self.additive()
        if self.current.kind is TokenKind.OPERATOR and self.current.text in _COMPARISONS:
            op = self.advance().text
            right = self.additive()
            if self.current.kind is TokenKind.OPERATOR and self.current.text in _COMPARISONS:
                raise self.error("comparisons cannot be chained; combine them with AND")
            return self.make(Compare(op, left, right, left.pos, right.end))
        return left

    def additive(self) -> Expr:
        left = self.term()
        while self.at_operator("+", "-"):
            op = self.advance().text
            right = self.term()
            left = self.make(Binary(op, left, right, left.pos, right.end))
        return left

    def term(self) -> Expr:
        left = self.unary()
        while self.at_operator("*", "/"):
            op = self.advance().text
            right = self.unary()
            left = self.make(Binary(op, left, right, left.pos, right.end))
        return left

    def unary(self) -> Expr:
        if self.at_operator("-"):
            minus = self.advance()
            operand = self.primary()
            if isinstance(operand, Number):
                value = operand.value if operand.value.is_zero() else operand.value.copy_negate()
                text = operand.text[1:] if operand.text.startswith("-") else "-" + operand.text
                folded = Number(value, text, operand.percent, minus.pos, operand.end)
                self.heights[id(folded)] = 1
                return folded
            return self.make(Unary("-", operand, minus.pos, operand.end))
        if self.at_keyword("NOT"):
            word = self.advance()
            operand = self.primary()
            return self.make(Unary("NOT", operand, word.pos, operand.end))
        return self.primary()

    def primary(self) -> Expr:
        token = self.current
        if token.kind is TokenKind.NUMBER:
            return self.number()
        if token.kind is TokenKind.STRING:
            self.advance()
            return self.make(String(token.text, token.pos, token.end))
        if token.kind is TokenKind.KEYWORD:
            return self.keyword(token)
        if token.kind is TokenKind.NAME:
            if self.peek().kind is TokenKind.LPAREN:
                return self.call()
            return self.reference()
        if token.kind is TokenKind.LPAREN:
            self.advance()
            self.enter(token)
            if self.current.kind is TokenKind.RPAREN:
                raise self.error("empty parentheses")
            node = self.expression()
            self.expect_close(token)
            self.nesting -= 1
            return node
        if token.kind is TokenKind.PERCENT:
            raise self.error("% must follow a number, as in 40%")
        if token.kind is TokenKind.OPERATOR and token.text in "+-":
            raise self.error(f"'{token.text}' cannot follow another operator here; use parentheses")
        if token.kind is TokenKind.EOF:
            raise self.error("the formula ends too early")
        raise self.unexpected()

    def expect_close(self, opening: Token) -> None:
        if self.current.kind is TokenKind.RPAREN:
            self.advance()
            return
        if self.current.kind is TokenKind.EOF:
            raise self.error("missing ')'", opening)
        raise self.error(f"expected ')' but found {self.current.describe()}")

    def number(self) -> Expr:
        token = self.advance()
        text = token.text
        value = Decimal(text)
        end = token.end
        percent = self.current.kind is TokenKind.PERCENT
        if percent:
            end = self.advance().end
            value = value.scaleb(-2, new_context())
            text += "%"
        return self.make(Number(value, text, percent, token.pos, end))

    def keyword(self, token: Token) -> Expr:
        word = token.text
        if word in ("TRUE", "FALSE"):
            self.advance()
            return self.make(Boolean(word == "TRUE", token.pos, token.end))
        if word in ("AND", "OR") and self.peek().kind is TokenKind.LPAREN:
            self.advance()
            args, close = self.arguments(word)
            if len(args) < 2:
                raise self.error(f"{word} needs at least two conditions", token)
            return self.make(Logical(word, tuple(args), token.pos, close.end))
        if word == "NULL":
            raise self.error("there is no NULL literal; use COALESCE(value, default) for missing values", token)
        if word == "NOT":
            raise self.error("NOT cannot follow another operator; use parentheses, as in NOT (NOT x)", token)
        raise self.error(f"{word} needs a condition before it", token)

    def reference(self) -> Expr:
        token = self.advance()
        name = token.text
        if "." in name:
            parts = name.split(".")
            if not all(_LOWER_NAME.fullmatch(part) for part in parts):
                raise self.error(f"variables are written in lower case, as in {name.lower()}", token)
            if len(parts) > 3:
                raise self.error(f"'{name}' has too many parts", token)
            if self.current.kind is TokenKind.LPAREN:
                raise self.error("only functions can be called", self.current)
            return self.make(VariableRef(name, token.pos, token.end))
        upper = name.upper()
        if upper in FUNCTIONS:
            raise self.error(f"{upper} is a function; write {upper}(...)", token)
        if name != upper or not CODE.fullmatch(name):
            raise self.error(f"component codes are written in upper case, as in {upper}", token)
        return self.make(ComponentRef(name, token.pos, token.end))

    def arguments(self, name: str) -> tuple[list[Expr], Token]:
        opening = self.advance()
        self.enter(opening)
        args: list[Expr] = []
        if self.current.kind is TokenKind.RPAREN:
            close = self.advance()
        else:
            while True:
                if self.current.kind in (TokenKind.COMMA, TokenKind.RPAREN):
                    raise self.error(f"missing argument in {name}(...)")
                args.append(self.expression())
                if self.current.kind is TokenKind.COMMA:
                    self.advance()
                    continue
                if self.current.kind is TokenKind.RPAREN:
                    close = self.advance()
                    break
                if self.current.kind is TokenKind.EOF:
                    raise self.error("missing ')'", opening)
                raise self.error(f"expected ',' or ')' but found {self.current.describe()}")
        self.nesting -= 1
        return args, close

    def call(self) -> Expr:
        token = self.current
        name = token.text
        if "." in name:
            raise self.error("only functions can be called", self.peek())
        upper = name.upper()
        spec = FUNCTIONS.get(upper)
        if spec is None or spec.keyword:
            suggestion = difflib.get_close_matches(upper, [f for f in FUNCTIONS if not FUNCTIONS[f].keyword], n=1)
            hint = f"; did you mean {suggestion[0]}?" if suggestion else ""
            raise self.error(f"unknown function {name}{hint}", token, ErrorCode.UNKNOWN_REFERENCE)
        self.advance()
        args, close = self.arguments(upper)
        count = len(args)
        if count < spec.min_args or (spec.max_args is not None and count > spec.max_args):
            raise self.error(f"{spec.signature} takes {_arity(spec.min_args, spec.max_args)}, got {count}", token)
        if upper == "SWITCH" and count % 2:
            raise self.error("SWITCH needs key/value pairs followed by a default value", token)
        for index, kind in spec.literals.items():
            if index < count:
                args[index] = self.quoted(upper, args[index], kind)
        if upper == "DATE":
            literal = self.date_literal(args, token, close)
            if literal is not None:
                return literal
        return self.make(Call(upper, tuple(args), token.pos, close.end))

    def quoted(self, function: str, arg: Expr, kind: Quoted) -> Expr:
        what = {Quoted.TAG: "a tag", Quoted.CODE: "a component code", Quoted.TABLE: "a table code",
                Quoted.MODE: "a rounding mode"}[kind]
        if not isinstance(arg, String):
            raise FormulaError(
                ErrorCode.PARSE_ERROR,
                f'{function} needs {what} in double quotes, as in {FUNCTIONS[function].signature}',
                position=arg.pos,
                end=arg.end,
            )
        if kind is Quoted.MODE:
            mode = arg.value.upper()
            if mode not in ROUNDING_MODES:
                raise FormulaError(
                    ErrorCode.PARSE_ERROR,
                    f"rounding mode must be one of {', '.join(ROUNDING_MODES)}",
                    position=arg.pos,
                    end=arg.end,
                )
            canonical = replace(arg, value=mode)
            self.heights[id(canonical)] = 1
            return canonical
        if not CODE.fullmatch(arg.value) or "__" in arg.value:
            raise FormulaError(
                ErrorCode.PARSE_ERROR,
                f'{what} is written in upper case letters, digits and "_", as in "{arg.value.upper() or "CODE"}"',
                position=arg.pos,
                end=arg.end,
            )
        return arg

    def date_literal(self, args: list[Expr], token: Token, close: Token) -> Expr | None:
        if not all(isinstance(a, Number) and a.integer and not a.value.is_signed() for a in args):
            return None
        year, month, day = (int(a.value) for a in args)  # type: ignore[union-attr]
        try:
            value = date(year, month, day)
        except (ValueError, OverflowError):
            raise FormulaError(
                ErrorCode.PARSE_ERROR,
                f"DATE({year}, {month}, {day}) is not a valid date",
                position=token.pos,
                end=close.end,
            ) from None
        self.count -= len(args)
        return self.make(DateLiteral(value, token.pos, close.end))


def _arity(minimum: int, maximum: int | None) -> str:
    if maximum is None:
        return f"at least {minimum} arguments"
    if minimum == maximum:
        return f"{minimum} argument{'s' if minimum != 1 else ''}"
    return f"{minimum} to {maximum} arguments"
