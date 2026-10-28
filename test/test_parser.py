import pytest

from ella import op
from ella.op import Add, Exponent, Multiply, Negative
from ella.parser import BinaryExpr, CallExpr, KeywordExpr, LiteralExpr, NameExpr, UnaryExpr, parse
from ella.token import Float, Identifier, Int, OperatorToken, lex


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
    expression = parse("f(1, g(2), h(1,))")

    assert isinstance(expression, CallExpr)
    assert isinstance(expression.callee, NameExpr)
    assert len(expression.arguments) == 3
    assert isinstance(expression.arguments[0], LiteralExpr)
    assert isinstance(expression.arguments[1], CallExpr)
    assert isinstance(expression.arguments[2], CallExpr)


@pytest.mark.parametrize("source", ["", "1 +", "(1 + 2", "1 2", "f(1 2)"])
def test_parse_rejects_malformed_expressions(source: str) -> None:
    with pytest.raises(SyntaxError):
        parse(source)


def test_parse_call_empty() -> None:
    expression = parse("f()")

    assert isinstance(expression, CallExpr)
    assert isinstance(expression.callee, NameExpr)
    assert expression.arguments == ()


def test_parse_chained_calls() -> None:
    expression = parse("f(1)(2)")

    assert isinstance(expression, CallExpr)
    assert isinstance(expression.callee, CallExpr)
    assert len(expression.arguments) == 1


def test_parse_call_precedence() -> None:
    # Postfix call binds tighter than binary +
    expression = parse("f(1) + 2")

    assert isinstance(expression, BinaryExpr)
    assert expression.operator is Add
    assert isinstance(expression.left, CallExpr)


def test_lex_tracks_newlines() -> None:
    first, second = list(lex("1\n2"))

    assert (first.line, first.column) == (1, 1)
    assert (second.line, second.column) == (2, 1)


def test_ast_structural_equality() -> None:
    assert parse("1 + 2") == parse("1 + 2")
    assert parse("1 + 2") != parse("1 + 3")
    assert parse("f(1)") != parse("g(1)")


def test_syntax_error_location_precision() -> None:
    with pytest.raises(SyntaxError, match=r"unexpected token '\*' @ 1:5"):
        parse("1 + * 2")

    with pytest.raises(SyntaxError, match=r"unexpected token '\*' @ 1:5"):
        parse("1 + *")


def test_keywords_in_expressions() -> None:
    expression = parse("int(1)")
    assert isinstance(expression, CallExpr)
    assert isinstance(expression.callee, KeywordExpr)
    assert isinstance(expression.callee.token, Int)

    float_call = parse("float('1.5')")
    assert isinstance(float_call, CallExpr)
    assert isinstance(float_call.callee, KeywordExpr)
    assert isinstance(float_call.callee.token, Float)

    binary_keyword = parse("int + 1")
    assert isinstance(binary_keyword, BinaryExpr)
    assert isinstance(binary_keyword.left, KeywordExpr)


def test_dynamic_operator_extension() -> None:
    class Increment(op.Postfix, string="++", precedence=op.Precedence.Postfix): ...

    expression = parse("x++")
    assert isinstance(expression, UnaryExpr)
    assert expression.operator is Increment
