from abc import ABC
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, NoReturn

import ella.token as tok
from ella import op


class Expr(ABC): ...

class LiteralExpr(Expr, ABC):
    _instances: ClassVar[dict[type[tok.Literal], type[LiteralExpr]]] = {}

    def __init_subclass__(cls, tokentype: type[tok.Literal] | None = None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if tokentype is not None:
            LiteralExpr._instances[tokentype] = cls


@dataclass(frozen=True, slots=True)
class LiteralIntegerExpr(LiteralExpr, tokentype=tok.LiteralInteger):
    token: tok.LiteralInteger
    value: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", int(self.token.value))


@dataclass(frozen=True, slots=True)
class LiteralFloatExpr(LiteralExpr, tokentype=tok.LiteralFloat):
    token: tok.LiteralFloat
    value: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", float(self.token.value))


@dataclass(frozen=True, slots=True)
class LiteralStringExpr(LiteralExpr, tokentype=tok.LiteralString):
    token: tok.LiteralString
    value: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.token.value)


@dataclass(frozen=True, slots=True)
class NameExpr(Expr):
    token: tok.Identifier
    value: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.token.value)


@dataclass(frozen=True, slots=True)
class KeywordExpr(Expr):
    token: tok.Keyword
    value: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.token.value)


@dataclass(frozen=True, slots=True)
class UnaryExpr(Expr):
    operator: type[op.Unary]
    operand: Expr


@dataclass(frozen=True, slots=True)
class BinaryExpr(Expr):
    operator: type[op.Binary]
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class CallExpr(Expr):
    callee: Expr
    arguments: tuple[Expr, ...]


class Parser:
    def __init__(self, tokens: Iterable[tok.Token]) -> None:
        self.tokens = list(tokens)
        self.position = 0

    def parse(self) -> Expr:
        if not self.tokens:
            raise SyntaxError("expected an expression")
        expression = self._expression(0)
        if self.position < len(self.tokens):
            self._raise(f"unexpected token {self._current().value!r}")
        return expression

    def _expression(self, min_precedence: int) -> Expr:
        left = self._prefix()

        while self.position < len(self.tokens):
            token = self._current()

            postfix = op.Postfix._types.get(token.value)
            if postfix is not None:
                if postfix.precedence < min_precedence:
                    break
                if postfix is op.Call:
                    left = self._call(left)
                else:
                    self.position += 1
                    left = UnaryExpr(postfix, left)
                continue

            operator = op.Binary._types.get(token.value)
            if operator is None or operator.precedence < min_precedence:
                break

            self.position += 1
            right_min_precedence = operator.precedence + (1 if operator.associativity is op.Associativity.LEFT else 0)
            right = self._expression(right_min_precedence)
            left = BinaryExpr(operator, left, right)

        return left

    def _prefix(self) -> Expr:
        if self.position >= len(self.tokens):
            self._raise("expected an expression")

        token = self._current()
        self.position += 1

        if isinstance(token, tok.Literal):
            return LiteralExpr._instances[type(token)](token)  # type: ignore

        if isinstance(token, tok.Identifier):
            return NameExpr(token)

        if isinstance(token, tok.Keyword):
            return KeywordExpr(token)

        prefix = op.Prefix._types.get(token.value)
        if prefix is not None:
            return UnaryExpr(prefix, self._expression(prefix.precedence))

        if token.value == "(":
            expression = self._expression(0)
            self._expect(")")
            return expression

        self._raise(f"unexpected token {token.value!r}", token)

    def _call(self, callee: Expr) -> CallExpr:
        self._expect("(")
        arguments: list[Expr] = []
        if not self._matches(")"):
            while True:
                arguments.append(self._expression(0))
                if self._matches(","):
                    self.position += 1
                    if self._matches(")"):
                        break
                else:
                    break
        self._expect(")")
        return CallExpr(callee, tuple(arguments))

    def _matches(self, value: str) -> bool:
        return self.position < len(self.tokens) and self._current().value == value

    def _expect(self, value: str) -> None:
        if not self._matches(value):
            self._raise(f"expected {value!r}")
        self.position += 1

    def _current(self) -> tok.Token:
        return self.tokens[self.position]

    def _raise(self, message: str, token: tok.Token | None = None) -> NoReturn:
        if token is None and self.position < len(self.tokens):
            token = self._current()
        if token is not None:
            message += f" @ {token.line}:{token.column}"
        raise SyntaxError(message)


def parse(source: str) -> Expr:
    return Parser(tok.lex(source)).parse()
