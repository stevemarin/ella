from abc import ABC
from collections.abc import Iterable
from typing import ClassVar, NoReturn

import ella.token as tok
from ella import op


class Expr(ABC): ...


class LiteralExpr(Expr, ABC):
    _instances: ClassVar[dict[type[tok.Literal], type[LiteralExpr]]] = {}

    def __init_subclass__(cls, tokentype: type[tok.Literal]) -> None:
        LiteralExpr._instances[tokentype] = cls


class LiteralIntegerExpr(LiteralExpr, tokentype=tok.LiteralInteger):
    def __init__(self, token: tok.LiteralInteger):
        self.token = token


class LiteralFloatExpr(LiteralExpr, tokentype=tok.LiteralFloat):
    def __init__(self, token: tok.LiteralFloat):
        self.token = token


class LiteralStringExpr(LiteralExpr, tokentype=tok.LiteralString):
    def __init__(self, token: tok.LiteralString):
        self.token = token


class NameExpr(Expr):
    def __init__(self, token: tok.Identifier) -> None:
        self.token = token


class UnaryExpr(Expr):
    def __init__(self, operator: type[op.Prefix], operand: Expr) -> None:
        self.operator = operator
        self.operand = operand


class BinaryExpr(Expr):
    def __init__(self, operator: type[op.Binary], left: Expr, right: Expr) -> None:
        self.operator = operator
        self.left = left
        self.right = right


class CallExpr(Expr):
    def __init__(self, callee: Expr, arguments: tuple[Expr, ...]) -> None:
        self.callee = callee
        self.arguments = arguments


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
            if token.value == "(":
                precedence = op.Binary._types["("].precedence
                if precedence < min_precedence:
                    break
                left = self._call(left)
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

        prefix = op.Prefix._types.get(token.value)
        if prefix is not None:
            return UnaryExpr(prefix, self._expression(prefix.precedence))

        if token.value == "(":
            expression = self._expression(0)
            self._expect(")")
            return expression

        self._raise(f"unexpected token {token.value!r}")

    def _call(self, callee: Expr) -> CallExpr:
        self._expect("(")
        arguments: list[Expr] = []
        if not self._matches(")"):
            while True:
                arguments.append(self._expression(0))
                if not self._matches(","):
                    break
                self.position += 1
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

    def _raise(self, message: str) -> NoReturn:
        if self.position < len(self.tokens):
            token = self._current()
            message += f" @ {token.line}:{token.column}"
        raise SyntaxError(message)


def parse(source: str) -> Expr:
    return Parser(tok.lex(source)).parse()
