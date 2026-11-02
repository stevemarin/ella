from pathlib import Path

import pytest

from ella.checker import (
    I64,
    STR,
    UNIT,
    EnumTy,
    FuncTy,
    ListTy,
    TupleTy,
    TyVar,
    check,
    finish,
    show_type,
)
from ella.lexer import EllaError, Span
from ella.parser import parse
from ella.syntax import Let

EXAMPLE = Path(__file__).parent.parent / "example_syntax.ella"


def type_of(source: str) -> str:
    """The type of the last top-level expression."""
    program = parse(source)
    checker = check(program)
    return show_type(checker.type_of(program.body[-1]))


def binding_type(source: str, name: str) -> str:
    """The type of a top-level `let` binding."""
    program = parse(source)
    checker = check(program)
    for expr in program.body:
        if isinstance(expr, Let):
            for binding in expr.bindings:
                if binding.name == name:
                    return show_type(checker.type_of(binding))
    raise KeyError(name)


def error(source: str) -> EllaError:
    with pytest.raises(EllaError) as info:
        check(parse(source))
    return info.value


def test_example_file_checks() -> None:
    check(parse(EXAMPLE.read_text()))


# Types


def test_show_type() -> None:
    assert show_type(FuncTy((I64, STR), ListTy(I64))) == "(i64, str) -> [i64]"
    assert show_type(TupleTy((I64,))) == "(i64,)"
    assert show_type(EnumTy("Result", (I64, STR))) == "Result[i64, str]"
    assert show_type(TyVar(3)) == "?3"


def test_finish_defaults_unknowns_to_unit() -> None:
    assert finish(ListTy(TyVar(1))) == ListTy(UNIT)
    decided = TyVar(2, ref=I64)
    assert finish(TupleTy((decided,))) == TupleTy((I64,))


# Inference


@pytest.mark.parametrize(
    ("source", "t"),
    [
        ("1 + 2", "i64"),
        ("7 / 2", "f64"),
        ("1.5 ** 2.0", "f64"),
        ('"a" + "b"', "str"),
        ("[1] + [2]", "[i64]"),
        ("0..10", "range"),
        ("1 < 2 <= 3", "bool"),
        ('(1, "a", true)', "(i64, str, bool)"),
        ("()", "()"),
        ("[[1], []]", "[[i64]]"),
        ("Some(1)", "Optional[i64]"),
        ('Error("x")', "Result[(), str]"),  # nothing decides the Ok type, so it defaults to ()
        ("if true then 1 else 2", "i64"),
        ("if true then 1", "()"),
        ("let f = () -> i64 => 1\nf()", "i64"),
        ("(x: i64) -> [i64] => [x]", "(i64) -> [i64]"),
        ('"abc"[0]', "str"),
        ("loop break 5", "i64"),
        ("loop break", "()"),
        ('match 1\n    case 0 => "zero"\n    case _ => "other"', "str"),
    ],
)
def test_expression_types(source: str, t: str) -> None:
    assert type_of(source) == t


def test_empty_list_is_inferred_from_later_use() -> None:
    assert binding_type("let mut seen = []\nseen = seen + [5]", "seen") == "[i64]"
    assert binding_type('let mut found = None\nfound = Some("x")', "found") == "Optional[str]"


def test_unconstrained_types_default_to_unit() -> None:
    assert binding_type("let xs = []", "xs") == "[()]"


def test_never_fits_anywhere() -> None:
    source = "let f = (n: i64) -> i64 =>\n    let x = if n > 0 then n else return 0\n    x\nf(1)"
    assert type_of(source) == "i64"


def test_annotations_are_checked() -> None:
    assert binding_type("let xs: [i64] = []", "xs") == "[i64]"
    assert error("let x: str = 1").message == "the value of 'x' should be str, not i64"
    assert error('let f = () -> i64 => "no"').message == "the function's result should be i64, not str"


def test_error_type_means_str_for_now() -> None:
    assert type_of('let f = () -> Result[i64, Error] => Error("bad")\nf()') == "Result[i64, str]"


# Type errors


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("1 + 1.0", "'+' needs two values of the same type, not i64 and f64"),
        ("true + true", "'+' doesn't work on bool"),
        ("1.0 // 2.0", "'//' doesn't work on f64"),
        ('-"a"', "'-' doesn't work on str"),
        ("not 1", "'not' needs a bool, not i64"),
        ("1 and true", "'and' needs a bool, not i64"),
        ("1 < true", "'<' needs two values of the same type, not i64 and bool"),
        ("[1] < [2]", "'<' doesn't work on [i64]"),
        ("print == print", "'print' can only be called, not used as a value"),
        ("let f = () -> () => ()\nf == f", "functions can't be compared"),
        ("1 in 5", "'in' needs a list, str or range on the right, not i64"),
        ('1 in "abc"', "the value looked for with 'in' should be str, not i64"),
        ("[1, true]", "list items must have the same type, not i64 and bool"),
        ("[1][true]", "an index must be an i64, not bool"),
        ("5[0]", "can't index a i64"),
        ("(1, 2).x", "member access ('.name') isn't supported yet"),
        ("len(5)", "len() needs a list, str or range, not i64"),
        ("len()", "len takes 1 argument but was given 0"),
        ("Some(1, 2)", "Some takes 1 argument but was given 2"),
        ("5(1)", "can't call a value of type i64"),
        ("((x: i64) -> i64 => x)(true)", "argument 1 should be i64, not bool"),
        ("((x: i64) -> i64 => x)(1, 2)", "function takes 1 argument but was given 2"),
        ("if 1 then 2", "an 'if' condition needs a bool, not i64"),
        ('if true then 1 else "a"', "both branches of 'if' must have the same type, not i64 and str"),
        ("while 1 print(1)", "a 'while' condition needs a bool, not i64"),
        ("for x in 5 print(x)", "can't loop over a i64"),
        ("assert 1", "an assertion needs a bool, not i64"),
        ("nope", "undefined name 'nope'"),
        ("x => x", "parameter 'x' needs a type, like 'x: i64'"),
        ("(x: i64) => x", "a lambda needs a return type, like '(x: i64) -> i64 => ...'"),
        ("let x: Foo = 1", "unknown type 'Foo'"),
        ("let x: Optional[i64, str] = None", "Optional takes 1 type arguments, not 2"),
        ("let xs = []\nlet y = -xs[0]", "can't tell the type of this value yet; add a type annotation"),
    ],
)
def test_type_errors(source: str, message: str) -> None:
    assert error(source).message == message


def test_type_error_span() -> None:
    assert error("let x = 1\nlet y = (x\n  + true)").span == Span(3, 3)


# Variables


def test_mutability() -> None:
    message = "can't assign to 'x' because it isn't mutable; declare it with 'let mut'"
    assert error("let x = 1\nx = 2").message == message
    assert error("let xs = [1]\nxs[0] = 2").message.startswith("can't assign to 'xs'")
    assert error('let mut x = 1\nx = "s"').message == "the assigned value should be i64, not str"
    assert error("let mut x = 1\nx /= 2").message == "the result of '/=' should be i64, not f64"
    assert "can't assign to an element of a str" in error('let mut s = "abc"\ns[0] = "x"').message
    assert "can only assign" in error("let f = () -> [i64] => [1]\nf()[0] = 1").message


def test_captured_variables_are_immutable() -> None:
    source = "let f = () -> i64 =>\n    let mut n = 0\n    let g = () -> i64 => n += 1\n    g()\nf()"
    assert "can't assign to 'n'" in error(source).message


def test_top_level_names() -> None:
    # functions can be used before their `let`, values can't
    assert type_of("let f = () -> i64 => g()\nlet g = () -> i64 => 1\nf()") == "i64"
    assert error("print(later)\nlet later = 1").message == "'later' is used before its 'let' has run"
    # but a function body may use a later global, since it runs later
    assert type_of("let f = () -> i64 => later\nlet later = 2\nf()") == "i64"
    # a top-level name keeps its type when bound again
    assert "can't be bound again as str" in error('let x = 1\nlet x = "s"').message


def test_shadowing_locals_can_change_type() -> None:
    source = 'let f = () -> str =>\n    let x = 1\n    let x = "s"\n    x\nf()'
    assert type_of(source) == "str"


def test_main_must_be_unit_to_unit() -> None:
    assert error("let main = () -> i64 => 1").message == "main must have type () -> (), not () -> i64"


# Functions and control flow


def test_return_and_break_placement() -> None:
    assert error("return 1").message == "'return' outside of a function"
    assert error("break").message == "'break' outside of a loop"
    assert error("continue").message == "'continue' outside of a loop"
    assert error("loop\n    (() -> () => break)()").message == "'break' outside of a loop"
    assert error("for x in [1] break 5").message == "only 'loop' can break with a value, not 'for'"
    assert error('let f = () -> i64 => return "x"').message == "the returned value should be i64, not str"
    assert "values of a loop's breaks" in error('loop\n    if true then break 1\n    break "a"').message


def test_where_conditions() -> None:
    source = "let f = (n: i64) -> i64 =>\n    where\n        n > 0\n        return < n\n    n"
    check(parse(source))
    assert error(source.replace("n > 0", "n")).message == "a precondition needs a bool, not i64"


def test_try() -> None:
    ok = "let f = (r: Result[i64, str]) -> Result[(), str] =>\n    let v = r?\n    Ok(())"
    check(parse(ok))
    assert "same error type" in error(ok.replace("Result[(), str] =>", "Result[(), i64] =>")).message
    assert "needs the function to return an Optional" in error("let f = (o: Optional[i64]) -> i64 => o?").message
    assert error("Some(1)?").message == "'?' can only be used inside a function"
    assert error("let f = () -> i64 => 5?").message == "'?' needs a Result or Optional, not i64"


# Patterns


def test_patterns() -> None:
    source = """\
enum Shape
    Circle(f64)
    Rect(f64, f64)
    Empty
let area = (s: Shape) -> f64 =>
    match s
        case Circle(r) => r
        case Rect(w, _) | Rect(_, w) => w
        case Empty => 0.0
"""
    check(parse(source))


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ('"x"', "this pattern should be i64, not str"),
        ("Some(x)", "the pattern 'Some(...)' should be i64, not Optional[?"),
        ("(a, b)", "this tuple pattern should be i64, not (?"),
        ("Nope(x)", "'Nope' isn't an enum variant"),
        ("0.0..1.0", "this range pattern should be i64, not f64"),
        ("1 | x", "each alternative of '|' must bind the same names"),
    ],
)
def test_pattern_errors(pattern: str, message: str) -> None:
    assert error(f"match 1\n    case {pattern} => 0").message.startswith(message)


def test_variant_field_count() -> None:
    assert error("match Some(1)\n    case Some(a, b) => 0").message == "Some has 1 fields, but the pattern has 2"


def test_name_patterns_can_be_variants() -> None:
    # `None` here is the variant, so it must match the subject's type
    assert "should be i64, not Optional" in error("match 1\n    case None => 0").message
    assert type_of("match Some(1)\n    case None => 0\n    case Some(n) => n") == "i64"


def test_generic_enum() -> None:
    source = 'enum Pair[A, B]\n    Pair(A, B)\nlet p = Pair(1, "x")\nmatch p\n    case Pair(n, s) => s'
    assert type_of(source) == "str"


# mut parameters

PUSH = "let push = (mut xs: [i64], x: i64) -> () =>\n    xs = xs + [x]\n"


def test_mut_parameter_types() -> None:
    assert type_of(PUSH + "push") == "(mut [i64], i64) -> ()"
    assert type_of(PUSH + "let mut v = [1]\npush(mut v, 2)") == "()"
    assert type_of(PUSH + "let mut g = [[1]]\npush(mut g[0], 2)") == "()"
    # Function types with and without mut parameters are different types.
    assert type_of(PUSH + "let f: (mut [i64], i64) -> () = push\nf") == "(mut [i64], i64) -> ()"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("let mut v = [1]\npush(v, 2)", "argument 1 is for a 'mut' parameter, so mark it: 'mut ...'"),
        (
            "let mut v = 1\nlet f = (x: i64) -> i64 => x\nf(mut v)",
            "argument 1 isn't for a 'mut' parameter, so it can't be 'mut'",
        ),
        ("let v = [1]\npush(mut v, 2)", "can't pass 'v' as 'mut' because it isn't mutable; declare it with 'let mut'"),
        ("push(mut [1], 2)", "can only pass a variable or an element of one"),
        ('let mut s = "ab"\nlet f = (mut c: str) -> () => ()\nf(mut s[0])', "can't pass an element of a str"),
        ("let mut v = [true]\npush(mut v, 2)", "argument 1 should be [i64], not [bool]"),
        ("let mut v = [1]\nprint(mut v)", "'mut' marks an argument for a 'mut' parameter, and only works in a call"),
        (
            "let mut v = [1]\nlet f: ([i64], i64) -> () = push",
            "the value of 'f' should be ([i64], i64) -> (), not (mut [i64], i64) -> ()",
        ),
        (
            "let f = (mut x: i64) -> () => ()\nlet x = 1\nf(mut x)",
            "can't pass 'x' as 'mut' because it isn't mutable; declare it with 'let mut'",
        ),
    ],
)
def test_mut_errors(source: str, message: str) -> None:
    assert error(PUSH + source).message == message


@pytest.mark.parametrize(
    ("call", "message"),
    [
        ("swap(mut a, mut a)", "'a' can't be passed as 'mut' twice in one call"),
        ("swap_rows(mut a[0], mut a[1])", "'a' can't be passed as 'mut' twice in one call"),
        ("both(mut a, a)", "'a' is passed as 'mut', so it can't also be used in another argument of the same call"),
        (
            "both(mut a, [a[0]])",
            "'a' is passed as 'mut', so it can't also be used in another argument of the same call",
        ),
    ],
)
def test_mut_arguments_must_not_overlap(call: str, message: str) -> None:
    source = """\
let swap = (mut x: [[i64]], mut y: [[i64]]) -> () => ()
let swap_rows = (mut x: [i64], mut y: [i64]) -> () => ()
let both = (mut x: [[i64]], y: [[i64]]) -> () => ()
let mut a = [[1]]
"""
    assert error(source + call).message == message


def test_mut_arguments_may_share_values_without_lists() -> None:
    # A length or a string can't change when the list does, so they don't overlap.
    check(parse(PUSH + "let mut v = [1]\npush(mut v, len(v))\npush(mut v, v[0])"))


def test_mut_parameters_can_be_assigned_but_others_cant() -> None:
    check(parse("let f = (mut x: i64) -> () =>\n    x += 1"))
    assert error("let f = (x: i64) -> () =>\n    x += 1").message == (
        "can't assign to 'x' because it isn't mutable; declare it with 'let mut'"
    )
