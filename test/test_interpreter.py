import io
from pathlib import Path

import pytest

from ella.interpreter import run
from ella.lexer import EllaError, Span
from ella.values import I64_MAX, I64_MIN, VariantValue, equal, fits_i64, to_text

EXAMPLE = Path(__file__).parent.parent / "example_syntax.ella"


def output(source: str) -> list[str]:
    """The lines a program prints."""
    out = io.StringIO()
    run(source, out)
    return out.getvalue().splitlines()


def value(expr: str) -> str:
    """The printed form of one expression's value."""
    [line] = output(f"print({expr})")
    return line


def error(source: str) -> EllaError:
    with pytest.raises(EllaError) as info:
        run(source, io.StringIO())
    return info.value


def test_example_file_runs() -> None:
    """docs/grammar.md says the syntax tour must run without errors."""
    assert output(EXAMPLE.read_text())[-1] == "done"


# Values


def test_i64_range() -> None:
    assert fits_i64(I64_MAX) and fits_i64(I64_MIN)
    assert not fits_i64(I64_MAX + 1) and not fits_i64(I64_MIN - 1)


def test_equal_distinguishes_bool_from_int() -> None:
    assert not equal(1, True)
    assert not equal([1], [True])
    assert equal(VariantValue("Optional", "Some", [1]), VariantValue("Optional", "Some", [1]))


def test_to_text() -> None:
    assert to_text("a") == "a"
    assert to_text(["a", ("b",)]) == '["a", ("b",)]'
    assert to_text(range(3)) == "0..3"
    assert to_text(1.0) == "1.0"


def test_functions_print() -> None:
    assert value("print") == "<built-in function print>"
    assert value("Some") == "<constructor Some>"
    assert value("() -> () => ()") == "<function>"


# Operators


@pytest.mark.parametrize(
    ("expr", "result"),
    [
        ("1 + 2 * 3", "7"),
        ("7 // -2", "-4"),
        ("-9223372036854775807 - 1", "-9223372036854775808"),
        ("2 ** 62", "4611686018427387904"),
        ("(-2) ** 63", "-9223372036854775808"),
        ("(-1) ** 1000001", "-1"),
        ("1.5 * 2.0", "3.0"),
        ("(-8.0) ** 0.5", "nan"),
        ("0.0 ** -1.0", "inf"),
        ('"ab" + "cd"', "abcd"),
        ("[1] + [2, 3]", "[1, 2, 3]"),
        ("1 << 62", "4611686018427387904"),
        ("-1 << 63", "-9223372036854775808"),
        ("-3 << 2", "-12"),
        ("~0", "-1"),
        ("not true", "false"),
        ("1 == 1 == 1", "true"),
        ("2.5 >= 2.5", "true"),
        ('"" in "abc"', "true"),
        ("[1, 2] in [[1, 2]]", "true"),
        ("0..3", "0..3"),
        ("len(0..=9)", "10"),
        ('"héllo"[1]', "é"),
    ],
)
def test_operators(expr: str, result: str) -> None:
    assert value(expr) == result


def test_and_or_short_circuit() -> None:
    assert value("false and 1 / 0 == 1.0") == "false"
    assert value("true or nothing_here") == "true"


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("1 + 1.0", "'+' needs two values of the same type, not i64 and f64"),
        ("true + true", "'+' doesn't work on bool"),
        ("1.0 // 2.0", "'//' doesn't work on f64"),
        ("1 // 0", "integer division by zero"),
        ("9223372036854775807 + 1", "i64 overflow"),
        ("-9223372036854775807 - 2", "i64 overflow"),
        ("9223372036854775807 * 2", "i64 overflow"),
        ("-9223372036854775808 // -1", "i64 overflow"),
        ("-(-9223372036854775807 - 1)", "i64 overflow"),
        ("2 ** 63", "i64 overflow"),
        ("3 ** 100000000000", "i64 overflow"),
        ("1 % 0", "integer division by zero"),
        ("2 ** -1", "negative power"),
        ("1 << 64", "shift amount must be between 0 and 63"),
        ("1 << 63", "i64 overflow"),
        ("3 << 62", "i64 overflow"),
        ("-2 << 63", "i64 overflow"),
        ("-true", "'-' doesn't work on bool"),
        ("not 1", "'not' doesn't work on i64"),
        ("1 and true", "'and' needs a bool, not i64"),
        ("1 < true", "'<' needs two values of the same type"),
        ("[1] < [2]", "'<' doesn't work on list"),
        ("print == print", "functions can't be compared"),
        ("1 in 5", "'in' needs a list, str or range"),
        ('1 in "abc"', "'in' can't look for a i64 in a str"),
        ("1.0..2.0", "'..' doesn't work on f64"),
        ("[1][1]", "index 1 is out of range for a list of length 1"),
        ("[1][-1]", "index -1 is out of range"),
        ("[1][true]", "an index must be an i64, not bool"),
        ("5[0]", "can't index a i64"),
        ("(1, 2).x", "a tuple has no field 'x'"),
        ("len(5)", "len() needs a list, str, range or Pool"),
        ("len()", "len takes 1 argument but was given 0"),
        ("Some(1, 2)", "Some takes 1 argument but was given 2"),
        ("5(1)", "can't call a value of type i64"),
        ("5?", "'?' needs a Result or Optional, not i64"),
    ],
)
def test_runtime_errors(expr: str, message: str) -> None:
    assert message in error(f"print({expr})").message


def test_error_spans_point_at_the_operator() -> None:
    assert error("let x = 1\nlet y = (x\n  + true)").span == Span(3, 3)


# Variables


def test_undefined_and_immutable_names() -> None:
    assert error("print(nope)").message == "undefined name 'nope'"
    assert "isn't mutable" in error("let x = 1\nx = 2").message
    assert "isn't mutable" in error("let xs = [1]\nxs[0] = 2").message
    assert "undefined name" in error("y = 1").message


def test_top_level_value_used_before_its_let() -> None:
    err = error("print(later)\nlet later = 1")
    assert err.message == "'later' is used before its 'let' has run"
    # but functions can be called before their let, and see later values once they've run
    assert output("let f = () -> i64 => later\nlet later = 2\nprint(f())") == ["2"]


def test_shadowing() -> None:
    assert output("let x = 1\nlet x = x + 1\nprint(x)") == ["2"]
    assert output("let x = 1\nlet f = () -> i64 =>\n    let x = 10\n    x\nprint(f())\nprint(x)") == ["10", "1"]


def test_let_in_nested_expression_binds_in_block() -> None:
    assert output("print(let a = 3)\nprint(a)") == ["3", "3"]


def test_block_ending_in_assignment_is_unit() -> None:
    assert output("let mut x = 1\nlet f = () -> () =>\n    let mut y = 0\n    y = 5\nprint(f())") == ["()"]


def test_assign_to_temporary_is_an_error() -> None:
    assert "can only assign" in error("let f = () -> [i64] => [1]\nf()[0] = 1").message


def test_strings_are_immutable() -> None:
    assert "can't assign to an element of a str" in error('let mut s = "abc"\ns[0] = "x"').message


def test_captured_variables_are_immutable_copies() -> None:
    source = "let f = () -> () =>\n    let mut n = 0\n    let g = () -> () => n += 1\n    g()\nf()"
    assert "isn't mutable" in error(source).message


# Control flow


def test_if_without_else_is_unit() -> None:
    assert value("if true then 5") == "()"
    assert "'if' condition needs a bool" in error("if 1 then 2").message


def test_no_matching_case() -> None:
    assert error("match 3\n    case 1 => 1").message == "no case matches 3"
    assert "case guard needs a bool" in error("match 3\n    case n if n => 1").message


def test_variant_names_in_patterns() -> None:
    # `None` is the variant, so it doesn't match Some(1); `other` binds anything
    source = "match Some(1)\n    case None => print(0)\n    case other => print(other)"
    assert output(source) == ["Some(1)"]


def test_break_value_only_in_loop() -> None:
    assert "only 'loop' can break with a value" in error("for x in [1] break 5").message
    assert "only 'loop' can break with a value" in error("while true break 5").message
    assert value("loop break") == "()"


def test_break_outside_loop() -> None:
    assert error("break").message == "'break' outside of a loop"
    assert error("continue").message == "'continue' outside of a loop"
    assert error("loop\n    (() -> () => break)()").message == "'break' outside of a loop"
    assert error("return 1").message == "'return' outside of a function"


def test_for_over_bad_values() -> None:
    assert "can't loop over a i64" in error("for x in 5 print(x)").message
    assert "pattern doesn't match" in error("for (a, b) in [1] print(a)").message


def test_loop_with_shadowed_labels() -> None:
    source = "l: for i in 0..3\n    l: for j in 0..3\n        if j == 1 then continue l\n        print((i, j))"
    # the inner `l` hides the outer one, so `continue l` continues the inner loop
    assert output(source) == ["(0, 0)", "(0, 2)", "(1, 0)", "(1, 2)", "(2, 0)", "(2, 2)"]


# Functions


def test_arity() -> None:
    assert "function takes 1 argument but was given 2" in error("((x: i64) -> i64 => x)(1, 2)").message


def test_where_reports_the_condition() -> None:
    source = "let f = (n: i64) -> i64 =>\n    where\n        n > 0\n        return < n\n    n\nf(5)"
    err = error(source)
    assert err.message == "postcondition failed: return < n"
    assert err.span == Span(4, 9)
    assert error(source.replace("f(5)", "f(0)")).message == "precondition failed: n > 0"
    assert "precondition needs a bool" in error("let f = () -> () =>\n    where\n        1\n    ()\nf()").message


def test_assert() -> None:
    assert error("assert 1 + 1 == 3").message == "assertion failed: 1 + 1 == 3"
    assert "assertion needs a bool" in error("assert 1").message


def test_try_on_optional() -> None:
    source = "let f = (o: Optional[i64]) -> Optional[i64] => Some(o? + 1)\nprint(f(Some(1)))\nprint(f(None))"
    assert output(source) == ["Some(2)", "None"]


def test_untyped_lambdas_run() -> None:
    # The type checker will reject these later; the interpreter doesn't mind.
    assert value("(x => x * 2)(4)") == "8"


def test_stack_overflow() -> None:
    assert "stack overflow" in error("let f = (n: i64) -> i64 => f(n + 1)\nf(0)").message


def test_main_runs_after_top_level() -> None:
    assert output("let main = () -> () => print(2)\nprint(1)") == ["1", "2"]
