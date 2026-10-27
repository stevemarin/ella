from abc import ABC
from collections.abc import Iterator
from typing import ClassVar

DIGITS = frozenset("0123456789")
LOWER = frozenset("_abcdefghijklmnopqrstuvwxyz")
UPPER = frozenset("_ABCDEFGHIJKLMNOPQRSTUVWXYZ")
LETTERS = LOWER | UPPER
WHITESPACE = frozenset(" \t\r")
QUOTES = frozenset("\"\'")

class Token(ABC):
    __slots__ = ("column", "line", "value")

    def __init__(self, value: str, line: int, column: int) -> None:
        self.value = value
        self.line = line
        self.column = column

    def __repr__(self) -> str:
        return f"{self.__class__}({self.value})"


class Keyword(Token, ABC):
    _instances: ClassVar[dict[str, type[Keyword]]] = {}

    def __init_subclass__(cls, value: str) -> None:
        cls.value = value
        Keyword._instances[value] = cls


# fmt: off
class Int(Keyword, value="int"): ...
class Float(Keyword, value="float"): ...
# fmt: on


def _lex_keyword(idx: int, column, line: int, src: str) -> tuple[int, int, int, Keyword | None]:
    # all keywords have no spaces and must be followed by whitespace
    start = idx
    while idx < len(src) and src[idx] not in WHITESPACE:
        idx += 1

    value = src[start:idx]
    try:
        print("KW", value, Keyword._instances)
        kw = Keyword._instances[value]
        return idx, column + (idx - start), line, kw(value, line, idx)
    except KeyError:
        return start, column, line, None


# fmt: off
class Literal(Token, ABC): ...
class LiteralInteger(Literal): ...
class LiteralFloat(Literal): ...
# fmt: off

def _lex_literal_integer_float(idx: int, column: int, line: int, src: str) -> tuple[int, int, int, LiteralInteger | LiteralFloat]:
    start, integer = idx, True
    while idx < len(src) and src[idx] in DIGITS | {"_"}:
        idx += 1

    if idx < len(src) and src[idx] == ".":
        integer = False
        idx += 1

    while idx < len(src) and src[idx] in DIGITS:
        idx += 1

    if idx < len(src) and (src[idx] == "e" or src[idx] == "E"):
        integer = False
        idx += 1

    if idx < len(src) and (src[idx] == "-" or src[idx] == "+"):
        idx += 1

    while idx < len(src) and src[idx] in DIGITS:
        idx += 1

    value = src[start:idx]
    if integer:
        try:
            _ = int(value)
        except ValueError:
            raise ValueError(f"invalid literal for base 10: {value} @ {line}:{start}") from None

        return idx, column + (idx - start), line, LiteralInteger(value, line, start)
    else:
        try:
            _ = float(value)
        except ValueError:
            raise ValueError(f"invalid literal float: {value} @ {line}:{start}") from None

        return idx, column + (idx - start), line, LiteralFloat(value, line, start)


class LiteralString(Literal):
    __slots__ = ("quote",)

    def __init__(self, value: str, line: int, column: int, quote: str) -> None:
        super().__init__(value, line, column)
        self.quote = quote


def _lex_literal_string(idx: int, column: int, line: int, src: str) -> tuple[int, int, int, LiteralString]:
    start, quote = idx, src[idx]
    idx += 1

    while idx < len(src) and (src[idx] != quote and src[idx] != "\n"):
        idx += 1

    value = src[start + 1 : idx]
    if idx >= len(src) or src[idx] != quote:
        raise ValueError(f"unterminated string: {value} @ {line}:{column}")

    return idx, column + (idx - start), line, LiteralString(value, column, line, quote)

def lex(src: str) -> Iterator[Token]:
    idx, column, line = 0, 1, 1

    while idx < len(src):
        print("ooping")
        match src[idx]:
            case ch if ch in WHITESPACE:
                column += 1
                idx += 1
            case "\n":
                line += 1
                column = 1
            case ch if ch in DIGITS:
                idx, column, line, token = _lex_literal_integer_float(idx, column, line, src)
                yield token
            case ch if ch in QUOTES:
                idx, column, line, token = _lex_literal_string(idx, column, line, src)
                yield token
            case ch if ch in LETTERS:
                idx, column, line, token = _lex_keyword(idx, column, line, src)
                if token is None:
                    # will add identifiers eventually
                    raise NameError(f"cannot find name @ {line}:{column}")
                yield token

    return