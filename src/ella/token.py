from abc import ABC
from collections.abc import Iterator
from typing import ClassVar

from ella import op

DIGITS = frozenset("0123456789")
LOWER = frozenset("_abcdefghijklmnopqrstuvwxyz")
UPPER = frozenset("_ABCDEFGHIJKLMNOPQRSTUVWXYZ")
LETTERS = LOWER | UPPER
WHITESPACE = frozenset(" \t\r")
QUOTES = frozenset("\"'")


def _get_word_operators() -> set[str]:
    return {k for k in (set(op.Prefix._types) | set(op.Binary._types) | set(op.Postfix._types)) if k.isidentifier()}


def _get_operators() -> list[str]:
    return sorted(
        set(op.Prefix._types) | set(op.Binary._types) | set(op.Postfix._types) | frozenset(",)"),
        key=len,
        reverse=True,
    )


WORD_OPERATORS = _get_word_operators()
OPERATORS = _get_operators()


class Token(ABC):
    __slots__ = ("column", "line", "value")

    def __init__(self, value: str, line: int, column: int) -> None:
        self.value = value
        self.line = line
        self.column = column

    def __repr__(self) -> str:
        return f"{self.__class__}({self.value})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, type(self)):
            return self.value == other.value and self.line == other.line and self.column == other.column
        else:
            return NotImplemented


class Keyword(Token, ABC):
    _instances: ClassVar[dict[str, type[Keyword]]] = {}

    def __init_subclass__(cls, value: str) -> None:
        cls.value = value
        cls.__slots__ = ()
        Keyword._instances[value] = cls


# fmt: off
class Int(Keyword, value="int"): ...
class Float(Keyword, value="float"): ...
# fmt: on


def _lex_keyword_identifier(
    idx: int, column: int, line: int, src: str, word_operators: set[str] | None = None
) -> tuple[int, int, int, Keyword | OperatorToken | Identifier]:
    if word_operators is None:
        word_operators = _get_word_operators()
    start = idx
    idx += 1
    while idx < len(src) and src[idx] in LETTERS | DIGITS:
        idx += 1

    value = src[start:idx]
    token_type = Keyword._instances.get(value)

    if token_type is not None:
        token = token_type(value, line, column)
    elif value in word_operators:
        token = OperatorToken(value, line, column)
    else:
        token = Identifier(value, line, column)

    column += idx - start
    return idx, column, line, token


class Identifier(Token):
    __slots__ = ()


class OperatorToken(Token):
    __slots__ = ()


# fmt: off
class Literal(Token, ABC): ...
class LiteralInteger(Literal): ...
class LiteralFloat(Literal): ...
# fmt: on


def _lex_literal_integer_float(
    idx: int, column: int, line: int, src: str
) -> tuple[int, int, int, LiteralInteger | LiteralFloat]:
    start, integer = idx, True
    while idx < len(src) and src[idx] in DIGITS | {"_"}:
        idx += 1

    if idx < len(src) and src[idx] == ".":
        integer = False
        idx += 1

    while idx < len(src) and src[idx] in DIGITS | {"_"}:
        idx += 1

    if idx < len(src) and (src[idx] == "e" or src[idx] == "E"):
        integer = False
        idx += 1

    if idx < len(src) and (src[idx] == "-" or src[idx] == "+"):
        idx += 1

    while idx < len(src) and src[idx] in DIGITS | {"_"}:
        idx += 1

    value = src[start:idx]
    if integer:
        try:
            _ = int(value)
        except ValueError:
            raise SyntaxError(f"invalid literal for base 10: {value} @ {line}:{column}") from None

        return idx, column + (idx - start), line, LiteralInteger(value, line, column)
    else:
        try:
            _ = float(value)
        except ValueError:
            raise SyntaxError(f"invalid literal float: {value} @ {line}:{column}") from None

        return idx, column + (idx - start), line, LiteralFloat(value, line, column)


class LiteralString(Literal):
    __slots__ = ("quote",)

    def __init__(self, value: str, line: int, column: int, quote: str) -> None:
        super().__init__(value, line, column)
        self.quote = quote

    def __eq__(self, other: object) -> bool:
        if isinstance(other, type(self)):
            return (
                self.value == other.value
                and self.line == other.line
                and self.column == other.column
                and self.quote == other.quote
            )
        else:
            return NotImplemented


def _lex_literal_string(idx: int, column: int, line: int, src: str) -> tuple[int, int, int, LiteralString]:
    start, quote = idx, src[idx]
    idx += 1

    while idx < len(src) and (src[idx] != quote and src[idx] != "\n"):
        if src[idx] == "\\":
            idx += 1  # escape next item
        idx += 1

    value = src[start + 1 : idx]
    if idx >= len(src) or src[idx] != quote:
        raise SyntaxError(f"unterminated string: {value} @ {line}:{column}")

    return idx + 1, column + (idx - start) + 1, line, LiteralString(value, line, column, quote)


def _lex_operator(
    idx: int, column: int, line: int, src: str, operators: list[str] | None = None
) -> tuple[int, int, int, OperatorToken | None]:
    if operators is None:
        operators = _get_operators()
    operator = next((value for value in operators if src.startswith(value, idx)), None)
    if operator is None:
        return idx, column, line, None
    token = OperatorToken(operator, line, column)
    return idx + len(operator), column + len(operator), line, token


class EOF(Token):
    def __init__(self, line: int, column: int) -> None:
        self.value = None
        self.line = line
        self.column = column


def lex(src: str) -> Iterator[Token]:
    idx, column, line = 0, 1, 1
    operators = _get_operators()
    word_operators = _get_word_operators()

    while idx < len(src):
        token: Token
        match src[idx]:
            case ch if ch in WHITESPACE:
                idx, column = idx + 1, column + 1
            case "\n":
                idx, column, line = idx + 1, 1, line + 1
            case ch if ch in DIGITS:
                idx, column, line, token = _lex_literal_integer_float(idx, column, line, src)
                yield token
            case ch if ch in QUOTES:
                idx, column, line, token = _lex_literal_string(idx, column, line, src)
                yield token
            case ch if ch in LETTERS:
                idx, column, line, token = _lex_keyword_identifier(idx, column, line, src, word_operators)
                yield token
            case _:
                idx, column, line, maybe_token = _lex_operator(idx, column, line, src, operators)
                if maybe_token is None:
                    raise SyntaxError(f"unexpected character {src[idx]!r} @ {line}:{column}")
                yield maybe_token

    return EOF(line, column)
