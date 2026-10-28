import pytest

from ella.op import Add, Exponent, Multiply, Negative
from ella.parser import BinaryExpr, CallExpr, LiteralExpr, NameExpr, UnaryExpr, parse
from ella.token import Identifier, OperatorToken, lex


def test_lex_operators_and_identifiers() -> None:
    tokens = list(lex("alpha + 2 ** 3"))

    assert isinstance(tokens[0], Identifier)
    assert isinstance(tokens[1], OperatorToken)
    assert tokens[1].value == "+"
    assert tokens[3].value == "**"


def test_parse_precedence() -> None:
    expression = parse("1 + 2 * 3")

    assert isinstance(expression, BinaryExpr)
    assert expression.operator is Add
    assert isinstance(expression.right, BinaryExpr)
    assert expression.right.operator is Multiply


def test_parse_right_associative_exponentiation() -> None:
    expression = parse("2 ** 3 ** 4")

    assert isinstance(expression, BinaryExpr)
    assert expression.operator is Exponent
    assert isinstance(expression.right, BinaryExpr)
    assert expression.right.operator is Exponent


def test_parse_prefix_and_grouping() -> None:
    expression = parse("-(1 + 2)")

    assert isinstance(expression, UnaryExpr)
    assert expression.operator is Negative
    assert isinstance(expression.operand, BinaryExpr)


def test_parse_calls_and_arguments() -> None:
    expression = parse("f(1, g(2))")

    assert isinstance(expression, CallExpr)
    assert isinstance(expression.callee, NameExpr)
    assert len(expression.arguments) == 2
    assert isinstance(expression.arguments[0], LiteralExpr)
    assert isinstance(expression.arguments[1], CallExpr)


@pytest.mark.parametrize("source", ["", "1 +", "(1 + 2", "1 2", "f(1,)"])
def test_parse_rejects_malformed_expressions(source: str) -> None:
    with pytest.raises(SyntaxError):
        parse(source)


def test_lex_tracks_newlines() -> None:
    first, second = list(lex("1\n2"))

    assert (first.line, first.column) == (1, 1)
    assert (second.line, second.column) == (2, 1)
