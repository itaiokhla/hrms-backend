"""Tokenizer for the formula language (3.3 §4.1, §4.6).

The tokenizer is the first safety gate. It accepts only what the language uses:
ASCII letters and digits, the operators ``+ - * / = <> < <= > >=``, ``( ) , %``,
dotted names (``att.paid_days``), double-quoted strings and plain whitespace.
Everything else is a PARSE_ERROR, and so is any identifier containing ``__``,
so text such as ``__import__("os")`` never reaches the parser (EC-CMP-08).

Two rules protect HR users from silent mistakes:

* a number must not run into letters (``1e5``, ``2x``) and may carry at most
  28 digits, the precision every calculation uses;
* a number written with thousands separators (``2,500``, ``1,00,000``,
  ``25,000.50``) is rejected, because inside a function call it would silently
  split one number into several arguments. Arguments may still be separated by a
  bare comma when the text cannot be read as a grouped number (``DATE(2026,2,28)``,
  ``IF(c,1500,0)``, ``CLAMP(x,0,100)``); ``MIN(x,2,500)`` must be written
  ``MIN(x, 2, 500)`` or ``MIN(x, 2500)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.payroll.formula.errors import ErrorCode, FormulaError

MAX_LENGTH = 2000
MAX_LITERAL_DIGITS = 28
KEYWORDS = frozenset({"TRUE", "FALSE", "AND", "OR", "NOT", "NULL"})

_DIGITS = frozenset("0123456789")
_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_WORD = _LETTERS | _DIGITS | {"_"}
_WHITESPACE = frozenset(" \t\r\n")
_GROUP_TAIL = re.compile(r"(?:[0-9]{2,3},)*[0-9]{3}(?![0-9])")
_HINTS = {
    "×": "use * to multiply",
    "÷": "use / to divide",
    "−": "use - for minus",
    "–": "use - for minus",
    "—": "use - for minus",
    "≤": "use <=",
    "≥": "use >=",
    "≠": "use <> for 'not equal'",
    "!": "use <> for 'not equal' and NOT for negation",
    "&": "use AND",
    "|": "use OR",
    "^": "there is no power operator; multiply instead",
    ";": "separate arguments with ','",
    "?": "use IF(condition, then, else)",
    ":": "use IF or SWITCH for conditions",
    "'": "strings use double quotes",
    "‘": "strings use straight double quotes",
    "’": "strings use straight double quotes",
    "“": "use straight double quotes",
    "”": "use straight double quotes",
    " ": "this is a non-breaking space; use a normal space",
}


class TokenKind(StrEnum):
    NUMBER = "NUMBER"
    STRING = "STRING"
    NAME = "NAME"
    KEYWORD = "KEYWORD"
    OPERATOR = "OPERATOR"
    PERCENT = "PERCENT"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COMMA = "COMMA"
    EOF = "EOF"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str  # as written; for STRING the content between the quotes, for KEYWORD upper case
    pos: int
    end: int

    def describe(self) -> str:
        if self.kind is TokenKind.EOF:
            return "the end of the formula"
        if self.kind is TokenKind.STRING:
            return f'the string "{self.text}"'
        return f"'{self.text}'"


def _error(message: str, position: int, end: int | None = None) -> FormulaError:
    return FormulaError(ErrorCode.PARSE_ERROR, message, position=position, end=end if end is not None else position + 1)


def _invalid_character(ch: str, position: int) -> FormulaError:
    shown = repr(ch) if ch.isprintable() and ch != " " else f"U+{ord(ch):04X}"
    hint = _HINTS.get(ch)
    message = f"character {shown} is not allowed in formulas" + (f" ({hint})" if hint else "")
    return _error(message, position)


def literal_digit_count(text: str) -> int:
    """Significant digits of a number literal: leading zeros (also after the point) do not count."""
    whole, _, fraction = text.partition(".")
    return len((whole + fraction).lstrip("0"))


def _thousands_separator(source: str, comma: int) -> bool:
    """True when the comma sits inside text readable as a grouped number (1,500 or 1,00,000)."""
    start = comma
    while start > 0 and source[start - 1] in _DIGITS:
        start -= 1
    head = source[start:comma]
    if not 1 <= len(head) <= 3 or head[0] == "0":
        return False
    if start > 0 and (source[start - 1] in _WORD or source[start - 1] == "."):
        return False
    return _GROUP_TAIL.match(source, comma + 1) is not None


def tokenize(source: str) -> list[Token]:
    """Split a formula into tokens, ending with an EOF token.

    Raises FormulaError: LIMIT_EXCEEDED for sources over MAX_LENGTH characters or
    numbers with more than 28 digits, PARSE_ERROR for anything outside the language.
    """
    if not isinstance(source, str):
        raise TypeError("formula source must be a str")
    if len(source) > MAX_LENGTH:
        raise FormulaError(
            ErrorCode.LIMIT_EXCEEDED,
            f"formulas are limited to {MAX_LENGTH} characters (this one has {len(source)})",
            position=MAX_LENGTH,
            end=len(source),
        )
    tokens: list[Token] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch in _WHITESPACE:
            i += 1
        elif ch in _DIGITS:
            i = _number(source, i, tokens)
        elif ch in _LETTERS:
            i = _name(source, i, tokens)
        elif ch == "_":
            j = i
            while j < n and source[j] in _WORD:
                j += 1
            if "__" in source[i:j]:
                raise _error("identifiers cannot contain '__'", i, j)
            raise _error("names must start with a letter", i, j)
        elif ch == '"':
            i = _string(source, i, tokens)
        elif ch in "+-*/":
            tokens.append(Token(TokenKind.OPERATOR, ch, i, i + 1))
            i += 1
        elif ch == "<":
            op = source[i : i + 2] if source[i + 1 : i + 2] in ("=", ">") else "<"
            tokens.append(Token(TokenKind.OPERATOR, op, i, i + len(op)))
            i += len(op)
        elif ch == ">":
            nxt = source[i + 1 : i + 2]
            if nxt == "<":
                raise _error("'><' is not an operator; use <> for 'not equal'", i, i + 2)
            op = ">=" if nxt == "=" else ">"
            tokens.append(Token(TokenKind.OPERATOR, op, i, i + len(op)))
            i += len(op)
        elif ch == "=":
            nxt = source[i + 1 : i + 2]
            if nxt == "=":
                raise _error("use a single = to compare", i, i + 2)
            if nxt in ("<", ">"):
                raise _error(f"'={nxt}' is not an operator; use {nxt}=", i, i + 2)
            tokens.append(Token(TokenKind.OPERATOR, "=", i, i + 1))
            i += 1
        elif ch == "(":
            tokens.append(Token(TokenKind.LPAREN, ch, i, i + 1))
            i += 1
        elif ch == ")":
            tokens.append(Token(TokenKind.RPAREN, ch, i, i + 1))
            i += 1
        elif ch == ",":
            if _thousands_separator(source, i):
                raise _error(
                    "write numbers without thousands separators (2500, not 2,500) and put a space "
                    "after the commas between arguments",
                    i,
                )
            tokens.append(Token(TokenKind.COMMA, ch, i, i + 1))
            i += 1
        elif ch == "%":
            tokens.append(Token(TokenKind.PERCENT, ch, i, i + 1))
            i += 1
        elif ch == ".":
            raise _error("a '.' must join a namespace and a name (att.paid_days) or sit inside a number", i)
        else:
            raise _invalid_character(ch, i)
    tokens.append(Token(TokenKind.EOF, "", n, n))
    return tokens


def _number(source: str, start: int, tokens: list[Token]) -> int:
    n = len(source)
    i = start
    while i < n and source[i] in _DIGITS:
        i += 1
    if i < n and source[i] == ".":
        if i + 1 < n and source[i + 1] in _DIGITS:
            i += 1
            while i < n and source[i] in _DIGITS:
                i += 1
        else:
            raise _error("a number needs digits after its decimal point", start, i + 1)
    if i < n and (source[i] in _WORD or source[i] == "."):
        j = i
        while j < n and (source[j] in _WORD or source[j] == "."):
            j += 1
        raise _error(f"malformed number '{source[start:j]}'", start, j)
    text = source[start:i]
    if literal_digit_count(text) > MAX_LITERAL_DIGITS:
        raise FormulaError(
            ErrorCode.LIMIT_EXCEEDED,
            f"numbers may have at most {MAX_LITERAL_DIGITS} digits",
            position=start,
            end=i,
        )
    tokens.append(Token(TokenKind.NUMBER, text, start, i))
    return i


def _name(source: str, start: int, tokens: list[Token]) -> int:
    n = len(source)
    i = start
    while True:
        while i < n and source[i] in _WORD:
            i += 1
        if i < n and source[i] == ".":
            if i + 1 < n and source[i + 1] in _LETTERS:
                i += 1
                continue
            raise _error("expected a name after '.'", i, i + 1)
        break
    text = source[start:i]
    double = text.find("__")
    if double >= 0:
        raise _error("identifiers cannot contain '__'", start + double, start + double + 2)
    if "." not in text and text.upper() in KEYWORDS:
        tokens.append(Token(TokenKind.KEYWORD, text.upper(), start, i))
    else:
        tokens.append(Token(TokenKind.NAME, text, start, i))
    return i


def _string(source: str, start: int, tokens: list[Token]) -> int:
    n = len(source)
    i = start + 1
    while i < n and source[i] != '"':
        ch = source[i]
        if ch in "\r\n":
            break
        if ch == "\\":
            raise _error("backslashes are not allowed in strings", i)
        if ch != " " and not ch.isprintable():
            raise _invalid_character(ch, i)
        i += 1
    if i >= n or source[i] != '"':
        raise _error("this string has no closing double quote", start, i)
    tokens.append(Token(TokenKind.STRING, source[start + 1 : i], start, i + 1))
    return i + 1
