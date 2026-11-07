from pathlib import Path

import pytest

from ella.checker import (
    I64,
    STR,
    UNIT,
    Checker,
    EnumTy,
    Env,
    FuncTy,
    ListTy,
    TupleTy,
    TyVar,
    check,
    finish,
    show_type,
)
from ella.lexer import EllaError, Span
from ella.modules import Linker, link
from ella.parser import parse
from ella.staging import prepare
from ella.syntax import Binary, Block, Lambda, Let

EXAMPLE = Path(__file__).parent.parent / "example_syntax.ella"


def type_of(source: str) -> str:
    """The type of the last top-level expression."""
    program = parse(source)
    checker = check(program)
    return show_type(checker.type_of(program.body[-1]))


def shape_of(source: str) -> str:
    """The type of the last top-level expression, with array shapes (which `type_of`, as lowering, leaves out)."""
    program = parse(source)
    checker = check(program)
    return show_type(checker.types[id(program.body[-1])])


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
    prepare(parse(EXAMPLE.read_text()))  # it computes a type, which needs its splices run while checking


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
        ('Err("x")', "Result[(), str]"),  # nothing decides the Ok type, so it defaults to ()
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


def test_error_type() -> None:
    assert type_of('let f = () -> Result[i64, Error] => Err(Error("bad"))\nf()') == "Result[i64, Error]"
    assert type_of('Error("bad").message') == "str"
    assert error('let f = () -> Result[i64, Error] => Err("bad")').message == (
        "the function's result should be Result[i64, Error], not Result[i64, str]"
    )
    assert error("Error(1)").message == "the argument of Error should be str, not i64"
    assert error('Error("bad").msg').message == "Error has no field 'msg'"


def test_errors_have_a_cause_and_where_they_were_made() -> None:
    assert type_of('Error("bad", Error("worse")).cause') == "Optional[Error]"
    assert type_of('Error("bad").at') == "str"
    assert type_of("let f = Error\nf") == "(str) -> Error"
    assert error('Error("bad", "worse")').message == "the argument of Error should be Error, not str"
    assert error("Error()").message == "Error takes 1 or 2 arguments but was given 0"
    assert error('Error("a", Error("b"), "1:1")').message == "Error takes 1 or 2 arguments but was given 3"
    # A pattern may leave out the cause and where it was made, but not the message.
    check(parse('match Error("a")\n    case Error(m) => m'))
    check(parse('match Error("a")\n    case Error(m, Some(Error(n))) => n\n    case Error(_, None, at) => at'))
    assert error('match Error("a")\n    case Error() => 0').message == "Error has 3 fields, but the pattern has 0"
    # Where it was made can't change, though the rest of it can.
    check(parse('let mut e = Error("a")\ne.message = "b"\ne.cause = Some(Error("c"))'))
    assert error('let mut e = Error("a")\ne.at = "1:1"').message == (
        "an Error's 'at' is where it was made, so you can't assign to it"
    )
    # A program's own Error is an ordinary struct.
    check(parse('struct Error\n    text: str\n    at: i64\nlet mut e = Error("a", 1)\ne.at = 2'))


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
        ("print == print", "functions can't be compared"),
        ("run == run", "'run' can only be called, not used as a value"),
        ("let f = () -> () => ()\nf == f", "functions can't be compared"),
        ("1 in 5", "'in' needs a list, str or range on the right, not i64"),
        ('1 in "abc"', "the value looked for with 'in' should be str, not i64"),
        ("[1, true]", "list items must have the same type, not i64 and bool"),
        ("[1][true]", "an index must be an i64, not bool"),
        ("5[0]", "can't index a i64"),
        ("(1, 2).x", "a (i64, i64) has no fields"),
        ("len(5)", "len() needs a list, str, range, Array, Pool or Arena, not i64"),
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
        (
            "let f = x => x",
            "parameter 'x' needs a type, like 'x: i64', as a top-level function's or a method's always do",
        ),
        (
            "let f = (x: i64) => x",
            "a top-level function or method needs a return type, like '(x: i64) -> i64 => ...'",
        ),
        (
            "let g = () -> () =>\n    let f = (x) => -x\n    ()",
            "can't tell the type of this value yet; add a type annotation",
        ),
        ("let x: Foo = 1", "unknown type 'Foo'"),
        ("let x: Optional[i64, str] = None", "Optional takes 1 type arguments, not 2"),
        ("let xs = []\nlet y = -xs[0]", "can't tell the type of this value yet; add a type annotation"),
    ],
)
def test_type_errors(source: str, message: str) -> None:
    assert error(source).message == message


def test_array_types() -> None:
    assert type_of("fill([2, 3], 0.0)") == "Array[f64]"
    assert type_of("array([1, 2])[0]") == "i64"
    assert type_of("fill([2, 2], 1)[1, 1]") == "i64"
    assert type_of("fill([2, 2], 1)[0..1]") == "Array[i64]"
    assert type_of("fill([2, 2], 1.5)[0, 0..2]") == "Array[f64]"
    assert type_of("to_f32(1) / to_f32(3)") == "f32"
    assert type_of("fill([2], to_f32(1.5)) / to_f32(2.0)") == "Array[f32]"
    assert type_of("total(fill([2], to_f32(1.5)))") == "f32"
    assert type_of("to_f64(to_f32(0.5)) + to_f64(2)") == "f64"
    assert type_of("to_i32(7) // to_i32(2)") == "i32"
    assert type_of("to_i32(7) / to_i32(2)") == "f64"
    assert type_of("total(fill([2], to_i32(3)) * to_i32(2))") == "i32"
    assert type_of("let t = (0, 1)\nfill([2, 2], 1.5)[t]") == "f64"
    assert type_of("fill([2], 1.0) * 2.0") == "Array[f64]"
    assert type_of("2 - fill([2], 1)") == "Array[i64]"
    assert type_of("-fill([2], 1)") == "Array[i64]"
    assert type_of("total(fill([2], 1))") == "i64"
    assert type_of("dot(fill([2], 1.0), fill([2], 2.0))") == "f64"
    assert type_of("shape(fill([2], 1))") == "[i64]"
    assert type_of("flat(fill([2], 1.0))") == "[f64]"
    assert type_of("let mut a = array([])\na = a * 2.5\na") == "Array[f64]"  # decided by its use
    assert type_of("for x in fill([2], 1.5) print(x)\nfill([1], 1)") == "Array[i64]"
    assert binding_type("let f = (a: Array[i64]) -> i64 => len(a) + a[0, 0]", "f") == "(Array[i64]) -> i64"
    assert binding_type("let f[T: Num] = (a: Array[T], k: T) -> Array[T] => a * k", "f") == "(Array[T], T) -> Array[T]"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("let a: Array[str] = fill([1], 1)", "an Array's items must be i64, i32, f64 or f32, not str"),
        ("let a: Array[[i64]] = fill([1], 1)", "an Array's items must be i64, i32, f64 or f32, not [i64]"),
        ("to_i32(1) + 1", "'+' needs two values of the same type, not i32 and i64"),
        ("to_i32(1) & to_i32(2)", "'&' doesn't work on i32"),
        ("fill([1], to_i32(1)) / to_i32(2)", "'/' works on arrays of f64 or f32 only, not Array[i32]"),
        ("to_f32(1.0) + 1.0", "'+' needs two values of the same type, not f32 and f64"),
        ("to_f32(1.0) // to_f32(2.0)", "'//' doesn't work on f32"),
        ('fill([1], "a")', "fill needs T to be Num, and str isn't"),
        ("fill([1], 1) / 2", "'/' works on arrays of f64 or f32 only, not Array[i64]"),
        ("fill([1], 1) // 2", "'//' doesn't work on Array[i64]"),
        (
            "fill([1], 1) + 2.0",
            "'+' on an array needs another array or a number of its items' type, not Array[i64] and f64",
        ),
        ("fill([1], 1) + fill([1], 1.0)", "'+' needs two arrays of the same type, not Array[i64] and Array[f64]"),
        ("fill([1], 1) < fill([1], 1)", "'<' doesn't work on Array[i64]"),
        ("fill([1], 1)[1.0]", "an array's index must be an i64 or a range, or a tuple of them, not f64"),
        ("fill([1], 1)[0, true]", "an array's index must be an i64 or a range, or a tuple of them, not (i64, bool)"),
        ("let mut a = fill([2], 1)\na[0..1] = a", "can't assign to a slice; change the items one by one"),
        ("1 in fill([1], 1)", "'in' needs a list, str or range on the right, not Array[i64]"),
        (
            "let f[T] = (a: Array[T]) -> Array[T] => a + a",
            "'+' on T needs the constraint Num, like [T: Num]",
        ),
        (
            "fill([1], 1)[" + ", ".join(["0"] * 33) + "]",
            "an array has at most 32 dimensions, so it can't take 33 indexes",
        ),
        ("let a = fill([1], 1)\na[0] = 2", "can't assign to 'a' because it isn't mutable; declare it with 'let mut'"),
    ],
)
def test_array_type_errors(source: str, message: str) -> None:
    assert error(source).message == message


def test_a_failed_definition_hides_a_library_function_of_its_name() -> None:
    # Uses of `total` are of the program's, which failed, not the library's.
    assert all_errors("let total = 1 + true\nlet t = total + 1\n") == [
        "1:15: '+' needs two values of the same type, not i64 and bool"
    ]


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


def test_main_gives_nothing_or_an_exit_status() -> None:
    assert error('let main = () -> str => ""').message == (
        "main must have type () -> (), () -> i64, () -> Result[(), E] or () -> Result[i64, E], not () -> str"
    )
    assert error("let main = (n: i64) -> i64 => n").message.startswith("main must have type () -> (), () -> i64")
    assert error('let main = () -> Result[str, Error] => Ok("")').message.endswith("not () -> Result[str, Error]")
    check(parse("let main = () -> i64 => 3"))
    check(parse("let main = () -> Result[(), Error] => Ok(())"))
    check(parse('let main = () -> Result[i64, str] => Err("no")'))


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
        ("push(mut [1], 2)", "can only pass a variable or a part of one"),
        ('let mut s = "ab"\nlet f = (mut c: str) -> () => ()\nf(mut s[0])', "can't pass an element of a str"),
        ("let mut v = [true]\npush(mut v, 2)", "argument 1 should be [i64], not [bool]"),
        ("let mut v = [1]\nprint(mut v)", "print() has no 'mut' parameter"),
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


# Structs, generics, pools, nocopy values and stages

RES = """\
nocopy struct Res
    name: str
    finalize = (r: Res) -> () => print(r.name)
"""


def test_struct_and_generic_types() -> None:
    assert type_of("struct P\n    x: i64\nlet p = P(1)\np.x") == "i64"
    assert type_of("struct B[T]\n    item: T\nB([1]).item") == "[i64]"
    assert type_of("let first[T] = (xs: [T]) -> T => xs[0]\nfirst([true])") == "bool"
    assert type_of("let mut p: Pool[str] = pool_new()\nlet h = alloc(mut p, 'x')\nh") == "Handle[str]"
    assert type_of("let mut p: Pool[str] = pool_new()\nlet h = alloc(mut p, 'x')\np[h]") == "str"
    assert type_of("arena_new()") == "Arena"
    assert type_of("let mut a = arena_new()\nlet r = alloc(mut a, 'x')\nr") == "Ref[str]"
    assert type_of("let mut a = arena_new()\nlet r = alloc(mut a, [1])\na[r]") == "[i64]"
    assert type_of("let mut a = arena_new()\nfree(mut a)") == "()"
    assert type_of("let mut a = arena_new()\nlen(a)") == "i64"
    assert type_of("quote 1 + 2") == "Code[i64]"
    assert type_of("run(quote [1])") == "[i64]"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (RES + "let a = Res('a')\nlet b = a", "a Res can't be copied, so it can't be used here"),
        (RES + "let xs = [Res('a')]", "a list can't hold values that can't be copied; use a Pool"),
        (
            RES + "let mut a = Res('a')\na = Res('b')",
            "a Res can't be copied, so it can't be assigned; change its fields instead",
        ),
        (
            RES + "let f = () -> () =>\n    let a = Res('a')\n    let g = () -> str => a.name\n    ()",
            "a Res can't be copied, so it can't be captured by a closure",
        ),
        (
            RES + "let f = (r: Res) -> Res => r",
            "a Res can't be copied, so it can't be returned unless it is a local variable",
        ),
        (
            RES + "let id[T] = (x: T) -> T => x\nlet a = id(Res('a'))",
            "id can't be used with Res, which can't be copied",
        ),
        (RES + "struct Holder\n    r: Res", "field 'r' can't be copied, so Holder must be a nocopy struct too"),
        (
            "struct P\n    x: i64\n    finalize = (p: P) -> () => ()",
            "only a nocopy struct can have a finalize method",
        ),
        ("nocopy struct E\n    finalize = (e: E) -> () => ()", "a nocopy struct needs at least one field"),
        ("let mut p: Pool[str] = pool_new()\nlet q = p", "a Pool[str] can't be copied, so it can't be used here"),
        (
            "let mut p: Pool[str] = pool_new()\nlet h = alloc(p, 'x')",
            "alloc() changes its pool, so mark it: 'alloc(mut pool, ...)'",
        ),
        ("let mut a = arena_new()\nlet b = a", "an Arena can't be copied, so it can't be used here"),
        ("let mut a = arena_new()\nfree(a)", "free() changes its arena, so mark it: 'free(mut arena)'"),
        ("let mut a = arena_new()\nfree(mut a, 1)", "free takes 1 argument but was given 2"),
        ("let mut p: Pool[i64] = pool_new()\nfree(mut p)", "free takes 2 arguments but was given 1"),
        ("let mut a = arena_new()\na[0]", "an arena's index should be Ref[?1], not i64"),
        (
            "let mut a = arena_new()\nlet r = alloc(mut a, 1)\nlet s: str = a[r]",
            "the value of 's' should be str, not i64",
        ),
        ("let mut a = arena_new()\na == a", "Arena values can't be compared"),
        ("let x: Arena[i64] = arena_new()", "Arena takes 0 type arguments, not 1"),
        (
            "let mut a = arena_new()\nlet r = alloc(mut a, 'x')\nlet f = (mut b: Arena, s: str) -> () => ()\nf(mut a, a[r])",
            "'a' is passed as 'mut', so it can't also be used in another argument of the same call",
        ),
        (
            "let mut p: Pool[str] = pool_new()\nlet mut q: Pool[i64] = pool_new()\nlet h = alloc(mut p, 'x')\nq[h]",
            "a pool's index should be Handle[i64], not Handle[str]",
        ),
        (
            "let mut p: Pool[str] = pool_new()\nlet h = alloc(mut p, 'x')\nlet f = (mut q: Pool[str], s: str) -> () => ()\nf(mut p, p[h])",
            "'p' is passed as 'mut', so it can't also be used in another argument of the same call",
        ),
        ("struct P\n    x: i64\nP(1).y", "P has no field 'y'"),
        ("let add[T] = (a: T, b: T) -> T => a + b", "'+' on T needs the constraint Add, like [T: Add]"),
        (
            "let n = 3\nlet c = $(if n > 2 then quote 1 else quote 2)",
            "'n' belongs to a later stage, so it can't be used while compiling",
        ),
        (
            "let id = (c: Code[i64]) -> Code[i64] => c\nquote\n    let y = 1\n    $(id(y))",
            "'y' belongs to a later stage, so it can't be used outside the quote it is bound in",
        ),
        (
            "let f = () -> Code[i64] =>\n    let g = (x: i64) -> i64 => x\n    quote g(1)",
            "'g' is a (i64) -> i64, which can't be carried into a later stage (only data can)",
        ),
        ("quote $(1)", "the spliced value should be Code[?1], not i64"),
        ("let f = () -> Code[i64] => quote (return 1)", "'return' outside of a function"),
    ],
)
def test_new_errors(source: str, message: str) -> None:
    assert error(source).message == message


def test_types_as_values() -> None:
    assert type_of("type [i64]") == "Type[[i64]]"
    assert type_of("struct P\n    x: i64\nfields(type P)") == "[str]"
    assert type_of("struct P\n    x: i64\nfield_type(type P, 'x')") == "Type[?]"
    # A spliced type is known from the Type[T] it comes from.
    source = "let f[T] = (t: Type[T]) -> Code[(T) -> T] => quote (x: $t) -> $t => x\nf(type i64)"
    assert type_of(source) == "Code[(i64) -> i64]"
    # A spliced field's type is only known once the code is built.
    assert type_of("struct P\n    x: i64\nlet f = (v: Code[P], n: str) -> Code[bool] => quote $v.$n == 1\nf") == (
        "(Code[P], str) -> Code[bool]"
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "let t = type i64\nlet f = (x: $t) -> i64 => x",
            "'t' belongs to a later stage, so it can't be used while compiling",
        ),
        (
            "struct P\n    x: i64\nlet f = (p: P) -> i64 => p.$('x')",
            "a field name can only be spliced into quoted code",
        ),
        ("quote (x: $(1)) -> i64 => 1", "the spliced type should be Type[?1], not i64"),
        ("struct P\n    x: i64\nquote P(1).$(2)", "the spliced field name should be str, not i64"),
        ("fields(1)", "argument 1 of fields() should be Type[?1], not i64"),
        (
            "struct P\n    x: i64\nlet f = (v: Code[P]) -> () => print(run(quote $v.$('x')))",
            "this code's type is only known once it is built, so it can only run while compiling",
        ),
        (
            "let f = () -> Code[Type[i64]] =>\n    let t = field_type(type i64, 'x')\n    quote t",
            "'t' is a Type[?], which can't be carried into a later stage (only data can)",
        ),
    ],
)
def test_type_value_errors(source: str, message: str) -> None:
    assert error(source).message == message


STACK = """\
let Stack = (t: Type) -> Type => new_struct("Stack[{{type_name(t)}}]", ["items"], [type [$t]])
"""


def prepared_type_of(source: str) -> str:
    """The type of the last top-level expression, after expanding splices (which computed types need)."""
    program, checker = prepare(parse(source))
    return show_type(checker.type_of(program.body[-1]))


def prepared_error(source: str) -> EllaError:
    with pytest.raises(EllaError) as info:
        prepare(parse(source))
    return info.value


def test_computed_types() -> None:
    # A spliced type is computed while checking, and the same each time.
    assert prepared_type_of(STACK + "let s: $(Stack(type i64)) = $(Stack(type i64))([1])\ns.items") == "[i64]"
    assert prepared_type_of(STACK + "let f = (s: $(Stack(type str))) -> [str] => s.items\nf") == (
        "(Stack[str]) -> [str]"
    )
    assert type_of("type [str]") == "Type[[str]]"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            STACK + "let s: $(Stack(type i64)) = $(Stack(type str))(['a'])",
            "the value of 's' should be Stack[i64], not Stack[str]",
        ),
        (STACK + "let s = $(Stack(type i64))(1, 2)", "Stack[i64] takes 1 argument but was given 2"),
        ("let s = $(type i64)(1)", "only a struct type that isn't generic can be called, not i64"),
        (
            "let f = () -> Type => new_struct('Point', ['x'], [type i64])\nstruct Point\n    y: str\nlet p: $(f()) = 1",
            "there is already a different type named Point",
        ),
        ("let f = (n: i64) -> Type => type i64\nlet x: $(f(1 // 0)) = 1", "integer division by zero"),
        (
            STACK + "struct S\n    s: $(Stack(type i64))",
            "a type can't be spliced into a definition yet; use new_struct()",
        ),
    ],
)
def test_computed_type_errors(source: str, message: str) -> None:
    assert prepared_error(source).message == message


def test_constrained_generics() -> None:
    assert type_of("let sum[T: Add] = (a: T, b: T) -> T => a + b\nsum(1, 2)") == "i64"
    assert type_of("let half[T: Num] = (a: T) -> f64 => a / a\nhalf(1.5)") == "f64"
    assert type_of("let max[T: Ord] = (a: T, b: T) -> T => if a < b then b else a\nmax('a', 'b')") == "str"
    # A constrained type parameter meets the constraints it implies, in a nested use.
    source = "let sum[T: Add] = (a: T, b: T) -> T => a + b\nlet twice[T: Num] = (a: T) -> T => sum(a, a)\ntwice(2)"
    assert type_of(source) == "i64"


def test_local_generic_functions() -> None:
    source = "let apply = (x: i64) -> i64 =>\n    let id[T] = (y: T) -> T => y\n    id(x)\napply(3)"
    assert type_of(source) == "i64"


def test_type_namespace_functions() -> None:
    source = (
        "struct Stack[T]\n"
        "    items: [T]\n"
        "    new = () -> Stack[T] => Stack([])\n"
        "let stack: Stack[i64] = Stack.new()\n"
        "stack.items"
    )
    assert type_of(source) == "[i64]"
    assert type_of("enum Token\n    End\n    create = () -> Token => End\nToken.create()") == "Token"
    assert type_of("struct P\n    f = (n: i64) -> i64 => n\nP.f(3)") == "i64"


def test_builtin_receiver_methods() -> None:
    assert prepared_type_of("[1, 2].map((x: i64) -> i64 => x * 2)") == "[i64]"
    assert prepared_type_of('" a ".trim()') == "str"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("let sum[T] = (a: T, b: T) -> T => a + b", "'+' on T needs the constraint Add, like [T: Add]"),
        ("let same[T] = (a: T, b: T) -> bool => a == b", "'==' on T needs the constraint Eq, like [T: Eq]"),
        ("let less[T: Eq] = (a: T, b: T) -> bool => a < b", "'<' on T needs the constraint Ord, like [T: Ord]"),
        ("let neg[T: Add] = (a: T) -> T => -a", "'-' on T needs the constraint Num, like [T: Num]"),
        ("let f[T: Num] = (a: T) -> T => a // a", "'//' doesn't work on T"),
        ("let has[T] = (xs: [T], x: T) -> bool => x in xs", "'in' on T needs the constraint Eq, like [T: Eq]"),
        ("let f[T: Big] = (a: T) -> T => a", "unknown constraint 'Big'; the constraints are Eq, Ord, Add, Num"),
        ("let sum[T: Add] = (a: T, b: T) -> T => a + b\nsum(true, false)", "sum needs T to be Add, and bool isn't"),
        ("let max[T: Ord] = (a: T, b: T) -> T => a\nmax([1], [2])", "max needs T to be Ord, and [i64] isn't"),
        (
            "let same[T: Eq] = (a: T, b: T) -> bool => a == b\nlet f = () -> () => ()\nsame(f, f)",
            "same needs T to be Eq, and () -> () isn't",
        ),
        (
            "let sum[T: Add] = (a: T, b: T) -> T => a + b\nlet twice[T] = (a: T) -> T => sum(a, a)",
            "sum needs T to be Add, and T isn't",
        ),
    ],
)
def test_constraint_errors(source: str, message: str) -> None:
    assert error(source).message == message


SET = "struct Set[T: Eq]\n    items: [T]\n    has = (s: Set[T], x: T) -> bool => x in s.items\n"

SHAPES = """\
constraint Shape
    area: (Self) -> f64
struct Square
    side: f64
    implements Shape
        area = (s: Square) -> f64 => s.side * s.side
    compare = (a: Square, b: Square) -> i64 => 0
    add = (a: Square, b: Square) -> Square => a
    mul = (a: Square, k: f64) -> Square => a
    neg = (a: Square) -> str => "negative"
    len = (s: Square) -> i64 => 4
    grow = (mut s: Square, by: f64) -> () => ()
let total[T: Shape] = (xs: [T]) -> f64 => xs[0].area()
let sq = Square(2.0)
"""


def test_methods() -> None:
    assert type_of(SHAPES + "sq.area()") == "f64"
    assert type_of(SHAPES + "let mut s = sq\nmut s.grow(1.0)") == "()"
    assert type_of(SHAPES + "total([sq])") == "f64"
    # Operators and len() come from methods with those names.
    assert type_of(SHAPES + "sq + sq") == "Square"
    assert type_of(SHAPES + "sq * 2.0") == "Square"
    assert type_of(SHAPES + "-sq") == "str"
    assert type_of(SHAPES + "sq < sq") == "bool"
    assert type_of(SHAPES + "len(sq)") == "i64"
    # So Square is Ord and Add.
    assert type_of(SHAPES + "let max[T: Ord] = (a: T, b: T) -> T => a\nmax(sq, sq)") == "Square"
    assert type_of(SHAPES + "let sum[T: Add] = (a: T, b: T) -> T => a + b\nsum(sq, sq)") == "Square"
    # Methods of a generic type are generic in its parameters.
    source = "struct Box[T]\n    v: T\n    unwrap = (b: Box[T]) -> T => b.v\nBox('x').unwrap()"
    assert type_of(source) == "str"
    # A field holding a function is called like a method.
    assert type_of("struct H\n    f: (i64) -> str\nH((n: i64) -> str => '').f(1)") == "str"


def test_explicit_constraint_implementations() -> None:
    source = """\
constraint Lookup
    get: (Self, str) -> i64
struct MyType
    value: i64
    implements Lookup
        get = (m: MyType, s: str) -> i64 => if s == "four" then 4 else m.value
let twice[T: Lookup] = (t: T) -> i64 => t.get("four") * 2
twice(MyType(5))
"""
    assert type_of(source) == "i64"

    bad_call = (
        source
        + """\
struct Other
    value: i64
twice(Other(5))
"""
    )
    assert error(bad_call).message == (
        "twice needs T to be Lookup, and Other isn't: it doesn't explicitly implement Lookup"
    )

    missing = """\
constraint Lookup
    get: (Self, str) -> i64
struct MyType
    value: i64
    implements Lookup
        find = (m: MyType, s: str) -> i64 => m.value
"""
    assert error(missing).message == "MyType doesn't implement Lookup: it has no method 'get'"

    partial = """\
constraint Lookup
    get: (Self, str) -> i64
    contains: (Self, str) -> bool
struct MyType
    value: i64
    implements Lookup
        get = (m: MyType, s: str) -> i64 => m.value
"""
    assert error(partial).message == "MyType doesn't implement Lookup: it has no method 'contains'"

    outside_block = """\
constraint Lookup
    get: (Self, str) -> i64
struct MyType
    value: i64
    get = (m: MyType, s: str) -> i64 => m.value
    implements Lookup
"""
    assert type_of(outside_block + "let f[T: Lookup] = (x: T) -> i64 => x.get('x')\nf(MyType(1))") == "i64"

    wrong_type = """\
constraint Lookup
    get: (Self, str) -> i64
struct MyType
    value: i64
    implements Lookup
        get = (m: MyType, s: str) -> str => "four"
"""
    assert error(wrong_type).message == (
        "MyType doesn't implement Lookup: its method 'get' is (MyType, str) -> str, not (MyType, str) -> i64"
    )

    structural_only = """\
constraint Lookup
    get: (Self, str) -> i64
struct MyType
    value: i64
    get = (m: MyType, s: str) -> i64 => 1
let twice[T: Lookup] = (t: T) -> i64 => t.get("four")
twice(MyType(1))
"""
    assert error(structural_only).message == (
        "twice needs T to be Lookup, and MyType isn't: it doesn't explicitly implement Lookup"
    )


def test_composed_constraints_and_constants() -> None:
    composed = """\
constraint HasValue
    value: (Self) -> i64
constraint Named: HasValue
    name: (Self) -> str
struct Tagged
    id: i64
    implements Named
        value = (x: Tagged) -> i64 => 3
        name = (x: Tagged) -> str => "tag"
let describe[T: Named] = (x: T) -> str => if x.value() > 0 then x.name() else ""
describe(Tagged(1))
"""
    assert type_of(composed) == "str"

    associated_constant = """\
constraint Additive
    add: (Self, Self) -> Self
    const ZERO: Self
struct Counter
    n: i64
    implements Additive
        add = (a: Counter, b: Counter) -> Counter => Counter(a.n + b.n)
        const ZERO = Counter(0)
let zero[T: Additive] = (x: T) -> T => T.ZERO
zero(Counter(4))
"""
    assert type_of(associated_constant) == "Counter"

    # A parameter's constraint gives it the constraints it requires: a Named T is HasValue.
    assert (
        type_of(
            composed
            + "let total[T: HasValue] = (x: T) -> i64 => x.value()\nlet f[T: Named] = (x: T) -> i64 => total(x)\nf(Tagged(1))"
        )
        == "i64"
    )
    # A constraint can require a built-in one, whose operators its parameters then have.
    ranked = """\
constraint Ranked: Ord
    const WORST: Self
struct Score
    n: i64
    compare = (a: Score, b: Score) -> i64 => a.n - b.n
    implements Ranked
        const WORST = Score(0)
let worse[T: Ranked] = (x: T) -> bool => x < T.WORST
worse(Score(1))
"""
    assert type_of(ranked) == "bool"
    # A constant's value may use top-level names defined later, as function bodies do.
    later = associated_constant.replace("Counter(0)", "Counter(start())") + "let start = () -> i64 => 0\n"
    assert type_of(later + "zero(Counter(4))") == "Counter"
    # A local variable named like a type parameter is that variable.
    assert (
        type_of(
            associated_constant
            + "let g[T: Additive] = (_x: T) -> i64 =>\n    let T = Counter(2)\n    T.n\ng(Counter(1))"
        )
        == "i64"
    )


MONOID = """\
constraint Monoid
    combine: (Self, Self) -> Self
    const EMPTY: Self
"""


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "constraint A: Nope\n    f: (Self) -> i64",
            "unknown parent constraint 'Nope'; the constraints are Eq, Ord, Add, Num, A",
        ),
        (
            "constraint A: B\n    f: (Self) -> i64\nconstraint B: A\n    g: (Self) -> i64",
            "constraint cycle: A -> B -> A",
        ),
        ("constraint A: A\n    f: (Self) -> i64", "constraint cycle: A -> A"),
        (
            "constraint A\n    f: (Self) -> i64\nconstraint B: A + A\n    g: (Self) -> i64",
            "B lists a parent constraint more than once",
        ),
        (
            "constraint A\n    f: (Self) -> i64\nconstraint B\n    f: (Self) -> str\nconstraint C: A + B\n    g: (Self) -> i64",
            "constraints A and B have different methods named 'f'",
        ),
        (
            "constraint A\n    const X: i64\nconstraint B\n    const X: str\nconstraint C: A + B\n    const Y: i64",
            "constraints A and B have different constants named 'X'",
        ),
        (
            "constraint A\n    f: (Self) -> i64\nconstraint B: A\n    const f: i64",
            "'f' is both a method and a constant of B",
        ),
        (
            MONOID + "struct S\n    n: i64\n    implements Monoid\n        combine = (a: S, b: S) -> S => a",
            "S doesn't implement Monoid: it has no constant 'EMPTY'",
        ),
        (
            MONOID + "struct S\n    n: i64\n    implements Monoid\n        combine = (a: S, b: S) -> S => a\n"
            "        const EMPTY = S(0)\n        const Z = 1",
            "Monoid has no constant named 'Z'",
        ),
        (
            MONOID + "struct S\n    n: i64\n    implements Monoid\n        combine = (a: S, b: S) -> S => a\n"
            "        const EMPTY = 0",
            "constant 'EMPTY' should be S, not i64",
        ),
        (
            (
                "constraint A\n    f: (Self) -> i64\nconstraint B: A\n    g: (Self) -> i64\n"
                "struct S\n    n: i64\n    implements B\n        g = (s: S) -> i64 => 1"
            ),
            "S doesn't implement B: it has no method 'f'",
        ),
        (
            "constraint R: Ord\n    f: (Self) -> i64\nstruct S\n    n: i64\n    implements R\n        f = (s: S) -> i64 => 1",
            "S doesn't meet the built-in constraint Ord required by R",
        ),
        (
            (
                "constraint A\n    f: (Self) -> i64\nconstraint B: A\n    g: (Self) -> i64\nstruct S\n    n: i64\n"
                "    implements B\n        f = (s: S) -> i64 => 1\n        g = (s: S) -> i64 => 1\n    implements A"
            ),
            "S already implements A (through B)",
        ),
        (
            (
                "constraint A\n    f: (Self) -> i64\nstruct S\n    n: i64\n    f = (s: S) -> i64 => 1\n"
                "    implements A\n    implements A"
            ),
            "S already implements A",
        ),
        (MONOID + "let f[T: Monoid] = (x: T) -> T => T.Q", "T has no constant named 'Q'; its constraints have EMPTY"),
        ("let f[T] = (x: T) -> T => T.Q", "T has no constant named 'Q'; its constraints have none"),
        (
            "constraint A\n    const X: i64\nconstraint B\n    const X: str\nlet f[T: A + B] = (x: T) -> i64 => T.X",
            "T's constraints A and B have different constants named 'X'",
        ),
        (
            MONOID + "let f[T: Monoid] = (x: T) -> T =>\n    T.EMPTY = x\n    x",
            "can't assign to T.EMPTY: a constant can't change",
        ),
        (
            (
                "constraint A\n    f: (Self) -> i64\nconstraint B: A\n    g: (Self) -> i64\nstruct S\n    n: i64\n"
                "    f = (s: S) -> i64 => 1\n    implements A\nlet h[T: B] = (x: T) -> i64 => x.g()\nh(S(1))"
            ),
            "h needs T to be B, and S isn't: it doesn't explicitly implement B",
        ),
        (
            (
                "constraint K\n    const X: i64\nlet main = () -> i64 =>\n    struct S\n        n: i64\n"
                "        implements K\n            const X = 1\n    0"
            ),
            "only a type defined at the top level can have constants, like 'X'",
        ),
        (
            "let g = 5\nconstraint K\n    const Z: i64\nstruct R\n    n: i64\n    implements K\n        const Z = g",
            "'g' belongs to a later stage, so it can't be used while compiling",
        ),
        (
            "constraint K\n    const F: (i64) -> i64",
            "constant 'F' can't hold functions: it is computed while compiling and written into the code, which can only hold numbers, bools, strings, tuples, lists, structs and enums",
        ),
        (
            "constraint K\n    const A: [Array[f64]]",
            "constant 'A' can't hold arrays: it is computed while compiling and written into the code, which can only hold numbers, bools, strings, tuples, lists, structs and enums",
        ),
        (
            "constraint K\n    const Z: Self\nnocopy struct R\n    n: i64\n    implements K\n        const Z = R(1)",
            "constant 'Z' can't hold nocopy values (R): it is computed while compiling and written into the code, which can only hold numbers, bools, strings, tuples, lists, structs and enums",
        ),
        (
            (
                "constraint K\n    const Z: Self\nstruct R\n    f: (i64) -> i64\n    implements K\n"
                "        const Z = R((x: i64) => x)"
            ),
            "constant 'Z' can't hold functions: it is computed while compiling and written into the code, which can only hold numbers, bools, strings, tuples, lists, structs and enums",
        ),
    ],
)
def test_constraint_composition_errors(source: str, message: str) -> None:
    assert error(source).message == message


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("struct P\n    x: i64\n    f = 3", "a method must be a function, like 'f = (x: P) -> ... => ...'"),
        ("struct P\n    x: i64\n    x = (p: P) -> i64 => 1", "'x' is already a field of P"),
        (
            "struct P\n    x: i64\n    compare = (p: P, q: P) -> bool => true",
            "the method 'compare' must have the type (P, P) -> i64",
        ),
        (
            "struct P\n    x: i64\n    add = (mut p: P, q: P) -> P => p",
            "the method 'add' must have the type (P, T) -> R, without 'mut'",
        ),
        ("struct P\n    x: i64\n    len = (p: P) -> str => ''", "the method 'len' must have the type (P) -> i64"),
        (
            "let f = () -> () =>\n    struct Q\n        x: i64\n        g = (q: Q) -> i64 => 1\n    ()",
            "only a top-level struct or enum can have methods",
        ),
        (SHAPES + "sq.perimeter()", "Square has no method 'perimeter'"),
        (SHAPES + "sq.area(1)", "area takes 0 arguments but was given 1"),
        (SHAPES + "sq.grow(1.0)", "'grow' changes the value it is called on, so mark it: 'mut x.grow(...)'"),
        (
            SHAPES + "let mut s = sq\nmut s.area()",
            "'area' doesn't change the value it is called on, so it can't be 'mut'",
        ),
        (SHAPES + "mut sq.grow(1.0)", "can't pass 'sq' as 'mut' because it isn't mutable; declare it with 'let mut'"),
        (SHAPES + "sq - sq", "'-' doesn't work on Square"),
        ("struct P\n    x: i64\nP(1) < P(2)", "'<' doesn't work on P"),
        (SHAPES + "total([1])", "total needs T to be Shape, and i64 isn't"),
        (
            SHAPES + "struct Circle\n    r: f64\ntotal([Circle(1.0)])",
            "total needs T to be Shape, and Circle isn't: it doesn't explicitly implement Shape",
        ),
        (
            SHAPES + "struct Circle\n    r: f64\n    area = (c: Circle) -> i64 => 1\ntotal([Circle(1.0)])",
            "total needs T to be Shape, and Circle isn't: it doesn't explicitly implement Shape",
        ),
        (
            SHAPES + "let f[T: Shape] = (x: T) -> f64 => x.perimeter()",
            "T has no method 'perimeter' (its constraints don't have one)",
        ),
        (SHAPES + "let f[T: Shape] = (x: T) -> T => x + x", "'+' on T needs the constraint Add, like [T: Add]"),
        (SHAPES + "constraint Shape\n    area: (Self) -> i64", "there is already a constraint named Shape"),
        (
            "constraint Bad\n    area: (i64) -> f64",
            "a constraint's method is a function taking Self first, like 'area: (Self) -> i64'",
        ),
        (
            "let f = () -> () =>\n    constraint Bad\n        area: (Self) -> f64\n    ()",
            "a constraint can only be defined at the top level",
        ),
        (
            "constraint A\n    f: (Self) -> i64\nconstraint B\n    f: (Self) -> str\nlet g[T: A + B] = (x: T) -> i64 => x.f()",
            "the constraints A and B have different methods named 'f'",
        ),
        (
            SET + "let f = (x: i64) -> i64 => x\nSet([f]).has(f)",
            "Set.has needs T to be Eq, and (i64) -> i64 isn't",
        ),
        (
            "struct Set[T]\n    items: [T]\n    has = (s: Set[T], x: T) -> bool => x in s.items",
            "'in' on T needs the constraint Eq, like [T: Eq]",
        ),
        (
            "nocopy struct R\n    n: i64\n    f = (r: R) -> R => r",
            "a R can't be copied, so it can't be returned unless it is a local variable",
        ),
        (
            "struct P[T]\n    x: T\n    finalize = (p: P[T]) -> () => ()",
            "only a nocopy struct can have a finalize method",
        ),
        (
            RES + "let f = (r: Res) -> () => r.finalize()",
            "a finalize method runs by itself when the value dies, so it can't be called",
        ),
    ],
)
def test_method_errors(source: str, message: str) -> None:
    assert error(source).message == message


COPIED_ITEM = (
    "an item of a {} is a copy, so it can't be changed in place; only a set method can store one, with 'v[k] = x'"
)

PS = """\
struct P
    x: i64
struct Ps
    items: [P]
    get = (s: Ps, i: i64) -> P => s.items[i]
    set = (mut s: Ps, i: i64, p: P) -> () => ()
"""

BAG = """\
constraint Lookup
    get: (Self, i64) -> str
struct Bag
    items: [str]
    get = (b: Bag, i: i64) -> str => b.items[i]
    set = (mut b: Bag, i: i64, s: str) -> () =>
        b.items[i] = s
    iter = (b: Bag) -> [str] => b.items
    contains = (b: Bag, s: str) -> bool => s in b.items
    implements Lookup
let mut bag = Bag(["a"])
"""


def test_protocols() -> None:
    assert type_of(BAG + "bag[0]") == "str"
    assert type_of(BAG + "bag[0] = 'b'") == "str"
    assert type_of(BAG + "bag[0] += 'b'") == "str"
    # The last step of a place can be an item stored with `set`.
    assert type_of(BAG + "struct Box\n    b: Bag\nlet mut box = Box(bag)\nbox.b[0] = 'x'") == "str"
    assert type_of(BAG + "'a' in bag") == "bool"
    assert type_of(BAG + "let mut n = ''\nfor s in bag\n    n += s\nn") == "str"
    source = "let first[T: Lookup] = (t: T) -> str => t[0]\n"
    assert type_of(BAG + source + "first(bag)") == "str"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (BAG + "bag['x']", "the index should be i64, not str"),
        (BAG + "bag[0] = 1", "the assigned value should be str, not i64"),
        (BAG + "1 in bag", "the value looked for with 'in' should be str, not i64"),
        (
            BAG + "let f = (mut s: str) -> () => ()\nf(mut bag[0])",
            COPIED_ITEM.format("Bag"),
        ),
        (
            "struct R\n    n: i64\n    get = (r: R, i: i64) -> i64 => i\nlet mut r = R(1)\nr[0] = 2",
            COPIED_ITEM.format("R"),
        ),
        (
            PS + "let mut ps = Ps([P(1)])\nps[0].x = 2",
            COPIED_ITEM.format("Ps"),
        ),
        (
            "struct R\n    n: i64\n    set = (mut r: R, i: i64, v: i64) -> () => ()\nlet mut r = R(1)\nr[0] += 2",
            "'+=' on an item needs a get method as well as set",
        ),
        (
            "struct R\n    n: i64\n    get = (r: R, i: i64) -> i64 => i\n    set = (mut r: R, i: str, v: i64) -> () => ()",
            "set must take the key and value types get uses: (i64, i64)",
        ),
        (
            "struct R\n    n: i64\n    set = (r: R, i: i64, v: i64) -> () => ()",
            "the method 'set' must have the type (mut R, K, V) -> ()",
        ),
        (
            "struct R\n    n: i64\n    update = (mut r: R, i: i64, f: (i64) -> ()) -> () => ()",
            "the method 'update' must have the type (mut R, K, (mut V) -> ()) -> ()",
        ),
        (
            "struct R\n    n: i64\n    update = (mut r: R, i: i64, f: (mut i64) -> ()) -> () => ()",
            "a type with an update method needs get and set methods too",
        ),
        (
            (
                "struct R\n    n: i64\n    get = (r: R, i: i64) -> i64 => i\n    set = (mut r: R, i: i64, v: i64) -> () => ()\n"
                "    update = (mut r: R, i: i64, f: (mut str) -> ()) -> () => ()"
            ),
            "update must take the key type get uses, and a (mut i64) -> ()",
        ),
        (
            "struct R\n    n: i64\n    eq = (a: R, b: R) -> i64 => 1\n    hash = (r: R) -> i64 => 1",
            "the method 'eq' must have the type (R, R) -> bool",
        ),
        (
            "struct R\n    n: i64\n    hash = (r: R) -> bool => true",
            "the method 'hash' must have the type (R) -> i64",
        ),
        (
            "struct R\n    n: i64\n    eq = (a: R, b: R) -> bool => true",
            "a type with an eq method needs a hash method too, so that equal values hash the same",
        ),
        (
            "struct R\n    n: i64\n    text = (r: R) -> i64 => 1",
            "the method 'text' must have the type (R) -> str",
        ),
        (
            "struct R\n    n: i64\n    iter = (r: R) -> i64 => 1",
            "the method 'iter' must have the type (R) -> a list, str, range, iterator or Gen",
        ),
        (
            "struct R\n    n: i64\n    contains = (r: R, x: i64) -> i64 => 1",
            "the method 'contains' must have the type (R, T) -> bool, without 'mut'",
        ),
        (
            "constraint It\n    iter: (Self) -> i64\nlet f[T: It] = (t: T) -> () =>\n    for x in t\n        ()",
            "the iter method of T must give a list, str, range, iterator or Gen, not i64",
        ),
    ],
)
def test_protocol_errors(source: str, message: str) -> None:
    assert error(source).message == message


UPDATED = BAG.replace(
    "    iter =",
    "    update = (mut b: Bag, i: i64, f: (mut str) -> ()) -> () =>\n        f(mut b.items[i])\n    iter =",
)

BOXES = """\
struct Box
    x: i64
struct Boxes
    items: [Box]
    get = (b: Boxes, i: i64) -> Box => b.items[i]
    set = (mut b: Boxes, i: i64, x: Box) -> () =>
        b.items[i] = x
    update = (mut b: Boxes, i: i64, f: (mut Box) -> ()) -> () =>
        f(mut b.items[i])
"""


def test_assignments_through_update() -> None:
    # A line of a block assigning through an item uses `update`, and has the value ().
    assert prepared_type_of(UPDATED + "let f = () -> () =>\n    bag[0] += 'b'\n    bag[0] += 'c'") == "() -> ()"
    # Elsewhere the assignment's value is used: it gets and sets the item.
    assert prepared_type_of(UPDATED + "let f = () -> str =>\n    let y = (bag[0] += 'b')\n    y\nf()") == "str"
    boxed = BOXES + "let mut bag = Boxes([Box(1)])\n"
    assert prepared_type_of(boxed + "let f = () -> () =>\n    bag[0].x += 1\n    bag[0].x = 2\nf()") == "()"
    # A part of an item can only change through update, as a line.
    assert error(PS + "let mut ps = Ps([])\nps[0].x = 1").message == COPIED_ITEM.format("Ps")
    assert error(boxed + "let f = () -> i64 =>\n    let y = (bag[0].x = 1)\n    y").message == COPIED_ITEM.format(
        "Boxes"
    )


def all_errors(source: str) -> list[str]:
    """Every error the checker finds, when it collects them as editors want."""
    checker = Checker()
    checker.errors = []
    checker.check_program(parse(source))
    return [str(error) for error in checker.errors]


def test_collecting_errors_goes_on_after_each_one() -> None:
    source = """let f = (n: i64) -> i64 =>
    let a = nope + 1
    print(n + "x")
    if true then 1 else "no"
let g = (s: str) -> i64 => s
let x: str = 1
"""
    assert all_errors(source) == [
        "6:14: the value of 'x' should be str, not i64",
        "2:13: undefined name 'nope'",
        "3:13: '+' needs two values of the same type, not i64 and str",
        "4:5: both branches of 'if' must have the same type, not i64 and str",
        "5:28: the function's result should be i64, not str",
    ]


def test_collecting_errors_skips_those_from_names_that_failed() -> None:
    # `total` failed, so the errors of lines that use it may only follow from that.
    source = "let total = 1 + true\nprint(total + 1)\nlet f = () -> i64 => total * 2\nprint(nope)\n"
    assert all_errors(source) == [
        "1:15: '+' needs two values of the same type, not i64 and bool",
        "4:7: undefined name 'nope'",
    ]


def test_checker_records_the_scope_of_each_name() -> None:
    program = parse("let f = (n: i64) -> i64 =>\n    let a = 1\n    n + a\n")
    checker = check(program)
    let = program.body[0]
    assert isinstance(let, Let) and isinstance(let.value, Lambda) and isinstance(let.value.body, Block)
    sum_ = let.value.body.exprs[-1]
    assert isinstance(sum_, Binary)
    env: Env | None = checker.scopes[id(sum_.left)]
    names = set()
    while env is not None:
        names |= set(env.vars)
        env = env.parent
    assert {"a", "n", "f", "print"} <= names


# The library (docs/grammar.md §10)


@pytest.mark.parametrize(
    ("source", "t"),
    [
        ("to_f64(1)", "f64"),
        ("to_i64(1.5)", "i64"),
        ('parse_i64("1")', "Result[i64, Error]"),
        ('parse_f64("1")', "Result[f64, Error]"),
        ("read_line()", "Optional[str]"),
        ('read_file("x")', "Result[str, Error]"),
        ("args()", "[str]"),
        ('split("a b", " ")', "[str]"),
        ('join(["a"], ",")', "str"),
        ('find("ab", "b")', "Optional[i64]"),
        ('replace("ab", "b", "c")', "str"),
        ('trim(" a ")', "str"),
        ('chars("ab")', "[str]"),
        ('ord("a")', "i64"),
        ("chr(97)", "str"),
        ("sqrt(2.0)", "f64"),
        ("floor(2.5)", "f64"),
        ("abs(-2)", "i64"),
        ("abs(-2.5)", "f64"),
        ("min(1, 2)", "i64"),
        ("max(1.5, 2.5)", "f64"),
        ("trim", "(str) -> str"),
        ("let f: (f64) -> f64 = abs\nf", "(f64) -> f64"),
        ('print("a", 1, true)', "()"),
        ("print()", "()"),
        ('eprint("a", 1)', "()"),
        ('"abc"[0..2]', "str"),
        ("[1, 2, 3][1..=2]", "[i64]"),
    ],
)
def test_library_types(source: str, t: str) -> None:
    assert type_of(source) == t


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("to_i64(true)", "to_i64 needs T to be Num, and bool isn't"),
        ('split("a")', "split takes 2 arguments but was given 1"),
        ('let mut s = "a"\ntrim(mut s)', "trim() has no 'mut' parameter"),
        (
            "enum Optional[T]\n    Some(T)\n    Nothing\nread_line()",
            "read_line() needs the built-in Optional, which this program replaces",
        ),
        (
            'struct Error\n    code: i64\nparse_i64("1")',
            "parse_i64() needs the built-in Error, which this program replaces",
        ),
        ("let mut xs = [1, 2]\nxs[0..1] = [3]", "can't assign to a slice; change the items one by one"),
        ("(1, 2)[0..1]", "an index must be an i64, not range"),
        ('abs("a")', "abs needs T to be Num, and str isn't"),
        ("min(1, 2.5)", "argument 2 of min() should be i64, not f64"),
        ("let f = abs\nf(true)", "abs needs T to be Num, and bool isn't"),
        ("hash([(x: i64) -> i64 => x])", "hash needs T to be Eq, and [(i64) -> i64] isn't"),
        ("hash(pool_new())", "hash can't be used with Pool[()], which can't be copied"),
        ("let f = pool_new", "'pool_new' can only be called, not used as a value"),
        ("let x = 1\nprint(mut x)", "print() has no 'mut' parameter"),
        (
            "enum Optional[T]\n    Some(T)\n    Nothing\nlet f = read_line",
            "read_line() needs the built-in Optional, which this program replaces",
        ),
    ],
)
def test_library_errors(source: str, message: str) -> None:
    assert error(source).message == message


def test_library_names_can_be_shadowed() -> None:
    assert type_of("let split = (n: i64) -> i64 => n\nsplit(1)") == "i64"
    assert type_of("let f = (args: [i64]) -> i64 => len(args)\nf([])") == "i64"


# Tuple items and destructuring `let` (docs/grammar.md §4, §9)


def test_tuple_items() -> None:
    assert type_of('(1, "a").1') == "str"
    assert type_of("let t = (1, (true, 2.5))\nt.1.0") == "bool"
    assert error("(1, 2).2").message == "a tuple of 2 items has no item 2"
    assert error("[1].0").message == "a [i64] has no fields"
    assert error("let mut t = (1, 2)\nt.0 = 3").message == (
        "a tuple can't be changed in place, so you can't assign to its items"
    )


def test_let_with_a_pattern() -> None:
    assert binding_type("let (a, b) = (1, 'x')\nlet c = b", "c") == "str"
    assert type_of("let f = () -> f64 =>\n    let (x, (y, _)) = (1.0, (2.0, true))\n    x + y\nf()") == "f64"
    assert type_of("struct P\n    x: i64\n    y: i64\nlet P(a, b) = P(1, 2)\na + b") == "i64"
    assert error("let (a, b) = 1").message == "this tuple pattern should be i64, not (?1, ?2)"
    assert error("let (a, b) = (1, 2)\na = 3").message.startswith("can't assign to 'a' because it isn't mutable")


def test_let_mut_with_a_pattern() -> None:
    assert type_of("let mut (a, b) = (1, 2)\na = 3\nb += a\nb") == "i64"
    assert type_of("let f = () -> [i64] =>\n    let mut (xs, _) = ([1], 2)\n    xs[0] = 5\n    xs\nf()") == "[i64]"


def test_let_pattern_with_a_type() -> None:
    assert binding_type("let (a, b): (f64, [str]) = (1.0, [])\nlet c = b", "c") == "[str]"
    assert error("let (a, b): (i64, i64) = (1, 'x')").message == (
        "the value of the 'let' should be (i64, i64), not (i64, str)"
    )


def test_let_patterns_must_match_every_value() -> None:
    assert error("let (a, 1) = (1, 2)").message == (
        "'let' needs a pattern that matches every value, and this one doesn't match (_, _)"
    )
    assert error("let Some(x) = Some(1)").message == (
        "'let' needs a pattern that matches every value, and this one doesn't match None"
    )
    assert error("for (a, true) in [(1, true)]\n    print(a)").message == (
        "'for' needs a pattern that matches every value, and this one doesn't match (_, false)"
    )


# Exhaustiveness (docs/grammar.md §6)


@pytest.mark.parametrize(
    ("cases", "subject", "missing"),
    [
        (["Some(_)"], "Some(1)", "None"),
        (["None", "Some(0)"], "Some(1)", "Some(_)"),
        (["Ok(Some(_))", "Err(_)"], "let r: Result[Optional[i64], str] = Ok(None)\nr", "Ok(None)"),
        (["true"], "true", "false"),
        (["(true, _)", "(_, true)"], "(true, false)", "(false, false)"),
        (["Circle(_)"], "Rect(1.0)", "Rect(_)"),
        (["Some(true) | None"], "Some(false)", "Some(false)"),
    ],
)
def test_matches_must_cover_every_value(cases: list[str], subject: str, missing: str) -> None:
    *setup, last = subject.split("\n")
    lines = ["enum Shape\n    Circle(f64)\n    Rect(f64)", *setup, f"match {last}"]
    lines += [f"    case {case} => 1" for case in cases]
    assert error("\n".join(lines)).message == f"'match' doesn't cover every value: add a case for {missing}"


@pytest.mark.parametrize(
    ("cases", "subject"),
    [
        (["Some(_)", "None"], "Some(1)"),
        (["Ok(Some(_))", "Ok(None)", "Err(_)"], "let r: Result[Optional[i64], str] = Ok(None)\nr"),
        (["(true, _)", "(false, true)", "(_, false)"], "(true, false)"),
        (["0", "1", "n"], "5"),
        (["Some(0 | 1)", "Some(_) | None"], "Some(1)"),
        (["P(_, _)"], "P(1, 2)"),
        (["_"], "()"),
    ],
)
def test_matches_that_cover_every_value(cases: list[str], subject: str) -> None:
    *setup, last = subject.split("\n")
    lines = ["struct P\n    x: i64\n    y: i64", *setup, f"match {last}"]
    lines += [f"    case {case} => 1" for case in cases]
    assert [w for w in warnings("\n".join(lines)) if NEVER_MATCHES in w] == []


def warnings(source: str) -> list[str]:
    return [f"{warning.span}: {warning.message}" for warning in check(parse(source)).warnings]


NEVER_MATCHES = "this case can never match: the cases before it match every value it does"


@pytest.mark.parametrize(
    ("cases", "subject", "redundant"),
    [
        (["Some(_)", "None", "_"], "Some(1)", [3]),
        (["_", "Some(_)"], "Some(1)", [2]),
        (["Some(x)", "Some(1)", "None"], "Some(1)", [2]),
        (["1", "2", "1", "_"], "5", [3]),
        (['"a"', '"a" | "b"', "_"], '"a"', []),  # "b" can still match
        (['"a" | "a"', "_"], '"a"', []),
        (["(true, _)", "(_, true)", "(true, true)", "_"], "(true, false)", [3]),
        (["true", "false", "_"], "true", [3]),
        (["n if n > 0", "n", "0"], "5", [3]),
        (["n if n > 0", "n if n > 0", "_"], "5", []),  # guards may be false
        (["0..5", "1", "_"], "5", []),  # ranges aren't compared
        (["P(1, _)", "P(_, 2)", "P(1, 2)", "_"], "P(1, 2)", [3]),
    ],
)
def test_cases_that_can_never_match(cases: list[str], subject: str, redundant: list[int]) -> None:
    lines = ["struct P\n    x: i64\n    y: i64", f"match {subject}"]
    lines += [f"    case {case} => 1" for case in cases]
    expected = [f"{4 + i}:5: {NEVER_MATCHES}" for i in redundant]  # case i is on line 4 + i
    assert [w for w in warnings("\n".join(lines)) if NEVER_MATCHES in w] == expected


def test_cases_in_functions_that_can_never_match() -> None:
    source = "let f = (b: bool) -> i64 =>\n    match b\n        case _ => 1\n        case true => 2\nf(true)"
    assert warnings(source) == [f"4:9: {NEVER_MATCHES}"]


def test_infinite_types_need_a_catch_all() -> None:
    assert error('match "a"\n    case "a" => 1').message == "'match' on str needs a 'case _' to cover every value"
    assert error("match 1\n    case 0..10 => 1").message == "'match' on i64 needs a 'case _' to cover every value"


def test_guarded_cases_dont_count() -> None:
    source = (
        "let f = (n: Optional[i64]) -> i64 =>\n    match n\n        case Some(x) if x > 0 => x\n        case None => 0"
    )
    assert error(source).message == "'match' doesn't cover every value: add a case for Some(_)"


def test_generic_values_need_a_catch_all() -> None:
    source = "let f[T] = (x: T) -> i64 =>\n    match x\n        case y => 1\nf(1)"
    assert type_of(source) == "i64"


# Staging limits


def test_quote_in_a_function_in_a_splice_cant_use_the_outer_quotes_variables() -> None:
    source = """\
let make = () -> Code[i64] =>
    quote
        let y = 5
        $
            let f = () -> Code[i64] => quote y + 1
            f()
"""
    assert error(source).message == (
        "'y' belongs to a quote outside this function, which a quote inside it can't use yet"
    )


# Warnings


def test_unused_variables() -> None:
    source = """\
let f = (n: i64, unused_param: i64) -> i64 =>
    let a = 1
    let _b = 2
    let (c, d) = (3, 4)
    for i in 0..n
        print(c)
    match Some(n)
        case Some(x) | Some(x) => 0
        case None => 0
f(1, 2)
let top = 5
"""
    assert warnings(source) == [
        "1:18: parameter 'unused_param' is never used; start its name with '_' if that is on purpose",
        "2:9: 'a' is never used; start its name with '_' if that is on purpose",
        "4:13: 'd' is never used; start its name with '_' if that is on purpose",
        "5:9: 'i' is never used; start its name with '_' if that is on purpose",
        "8:19: 'x' is never used; start its name with '_' if that is on purpose",
    ]


def test_variables_used_in_lambdas_assignments_and_text_are_used() -> None:
    source = """\
let f = () -> i64 =>
    let a = 1
    let mut b = 0
    b = 2
    let c = "x"
    let g = () -> i64 => a
    print("{{c}}")
    g()
f()
"""
    assert warnings(source) == []


def test_code_that_never_runs() -> None:
    source = """\
let f = (n: i64) -> i64 =>
    if n > 0 then return 1 else return 2
    print("never")
    print("not reported twice")
    3
let g = () -> () =>
    for i in 0..3
        continue
        print(i)
    loop
        print(1)
    print("after an endless loop")
f(1)
g()
"""
    assert warnings(source) == [
        "3:5: this code never runs: the line before it never finishes",
        "9:9: this code never runs: the line before it never finishes",
        "12:5: this code never runs: the line before it never finishes",
    ]


def test_unused_functions_and_parameters() -> None:
    source = """\
let used = (n: i64) -> i64 => n
let unused = (n: i64) -> i64 => used(n)
let _kept = () -> () => ()
let loops = (n: i64) -> i64 => if n > 0 then loops(n - 1) else 0
let apply = (g: (i64) -> i64) -> i64 => g(0)
let main = () -> () =>
    let f = (a: i64, _b: i64) -> i64 => 1
    print(apply((x) => 2), f(1, 2))
"""
    assert warnings(source) == [
        "2:5: function 'unused' is never used",
        "3:5: function '_kept' is never used",
        "4:5: function 'loops' is never used",
        "7:14: parameter 'a' is never used; start its name with '_' if that is on purpose",
        "8:18: parameter 'x' is never used; start its name with '_' if that is on purpose",
    ]


def test_mut_variables_that_never_change() -> None:
    source = """\
let f = (xs: [i64]) -> i64 =>
    let mut same = 1
    let mut added = []
    added += [1]
    let mut items = [1]
    items[0] = 2
    let mut (a, b) = (1, 2)
    a += 1
    let mut pool = pool_new()
    let h = alloc(mut pool, 1)
    match Some(xs)
        case Some(mut ys) => len(ys) + same + len(added) + items[0] + a + b + pool[h]
        case None => 0
f([])
"""
    assert warnings(source) == [
        "2:13: 'same' never changes, so it doesn't need 'mut'",
        "7:17: 'b' never changes, so it doesn't need 'mut'",
        "12:23: 'ys' never changes, so it doesn't need 'mut'",
    ]


def test_no_unused_warnings_after_an_error() -> None:
    checker = Checker()
    checker.errors = []
    checker.check_program(parse("let f = () -> i64 =>\n    let a = 1\n    a + true\nf()"))
    assert checker.errors and checker.warnings == []


# `mut` in patterns


def test_mut_names_in_patterns() -> None:
    assert type_of("let (mut a, b) = (1, true)\na += 1\na") == "i64"
    assert error("let (a, b) = (1, true)\na += 1").message == (
        "can't assign to 'a' because it isn't mutable; declare it with 'let mut'"
    )
    assert error("match None\n    case mut None => 1").message == "'mut' marks a variable, and 'None' is a variant"
    assert error("match (1, 2)\n    case (mut x, 1) | (x, _) => x\n    case _ => 0").message == (
        "'x' must be 'mut' in every alternative of '|', or in none"
    )


# The library and the prelude


def test_exit_can_only_be_called() -> None:
    assert type_of("let f = (n: i64) -> i64 => if n > 0 then exit(1) else n\nf(1)") == "i64"
    assert error("let f = exit").message == "'exit' can only be called, not used as a value"


def test_prelude_functions() -> None:
    assert prepared_type_of("map([1], (n: i64) -> str => 'x')") == "[str]"
    assert prepared_type_of("sort(['b', 'a'])") == "[str]"
    assert prepared_type_of("pi") == "f64"
    assert prepared_type_of("let map = (n: i64) -> i64 => n\nmap(1)") == "i64"  # a program's own definition wins
    with pytest.raises(EllaError, match="sort needs T to be Ord, and bool isn't"):
        prepare(parse("sort([true])"))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("map([1], (n) => n > 0)", "[bool]"),
        ("[1] |> map(n => [n])", "[[i64]]"),
        ("let apply = (f: (i64) -> str, x: i64) -> str => f(x)\napply((n) => 'x', 1)", "str"),
        ("(x) => x + 1.0", "(f64) -> f64"),
        ("(x: str, y) => x + y", "(str, str) -> str"),
        ("let f = () -> (i64) -> bool =>\n    let g: (i64) -> bool = (n) => n > 0\n    g\nf()", "(i64) -> bool"),
        ("let ys = map([1], (n) => n * 2)\n-ys[0]", "i64"),  # the lambda's body is checked when ys[0] needs its type
        ("map(['a'], len)", "[i64]"),
        ("map([1], Some)", "[Optional[i64]]"),
        ("map([1], print)", "[()]"),
        ("let p = Error\np", "(str) -> Error"),
        ("enum E\n    Two(i64, str)\nTwo", "(i64, str) -> E"),
    ],
)
def test_lambda_types_are_inferred(source: str, expected: str) -> None:
    assert prepared_type_of(source) == expected


def test_inferred_lambda_errors() -> None:
    with pytest.raises(EllaError, match="can't tell the type of this value yet"):
        prepare(parse("let f = () -> () =>\n    let add = (x, y) => x + y\n    ()"))
    with pytest.raises(EllaError, match="'..' doesn't work on str"):
        prepare(parse("map(['a'], (s) => s..s)"))
    with pytest.raises(EllaError, match="argument 1 should be i64, not str"):
        prepare(parse("let f = () -> () =>\n    let g = (x) => x\n    g(1)\n    g('a')\n    ()"))
    with pytest.raises(EllaError, match="len[(][)] needs a list, str, range, Array, Pool or Arena, not i64"):
        prepare(parse("map([1], len)"))


def test_prelude_code_keeps_its_own_meaning() -> None:
    # The program's own len, hash and variants named like the prelude's variables don't change it.
    source = "let len = 5\nlet hash = 'h'\nenum E\n    x\n    i\nlet mut m = new_map()\nm[1] = 2\n(len, m[1], reverse([x, i]))"
    assert prepared_type_of(source) == "(i64, i64, [E])"


def test_prelude_modules_are_linked_when_used() -> None:
    assert link(parse("let x = 1")).modules == (("", "main"),)
    used = link(parse("sort([1])"))
    assert used.modules == (("<std>/lists.ella", "lists"), ("", "main"))
    assert link(used) is used
    # A type in an annotation counts.
    assert link(parse("let f = (m: Map[str, i64]) -> i64 => len(m)")).modules[0] == ("<std>/maps.ella", "maps")


def test_prelude_gives_no_warnings() -> None:
    _, checker = prepare(Linker(None, every=True).link(parse("let x = 1")))
    assert checker.warnings == []


GEN = "let count = (n: i64) -> Gen[i64] =>\n    yield n\n"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("yield 1", "'yield' can only be used in a function, which it makes a generator"),
        ("let f = () -> i64 =>\n    yield 1", "a generator (a function with 'yield') gives a Gen[T], not i64"),
        ("let f = () -> Gen[str] =>\n    yield 1", "the value given to 'yield' should be str, not i64"),
        (
            "let f = () -> Gen[i64] =>\n    yield 1\n    return 1",
            "a generator's 'return' can't have a value: it just ends the generator",
        ),
        (
            "let f = (mut n: i64) -> Gen[i64] =>\n    yield n",
            "a generator can't have 'mut' parameters; its parameters are its own variables, which it can change",
        ),
        (
            "let f = (n: i64) -> Gen[i64] =>\n    where\n        return == return\n    yield n",
            "a generator can't have postconditions, since it returns nothing",
        ),
        ("let f = () -> Gen[Code[i64]] =>\n    yield quote\n        yield 1", "'yield' can't be used in a quote"),
        (
            "nocopy struct R\n    n: i64\nlet f = () -> Gen[R] =>\n    yield R(1)",
            "a generator can't yield R values, which can't be copied",
        ),
        (GEN + "let g = count(1)\nnext(g)", "next() changes its generator or iterator, so mark it: 'next(mut g)'"),
        (
            "let mut xs = [1]\nnext(mut xs)",
            "next() needs a generator or an iterator (a type with a next method), not [i64]",
        ),
        (
            "let mut xs = [1]\nfor x in mut xs\n    ()",
            "only an iterator or a generator can be looped over with 'mut', not a [i64]",
        ),
        (GEN + "let g = count(1)\ng == g", "Gen[i64] values can't be compared"),
        (
            "struct R\n    n: i64\n    next = (r: R) -> Optional[i64] => None",
            "the method 'next' must have the type (mut R) -> Optional[T]",
        ),
    ],
)
def test_generator_errors(source: str, message: str) -> None:
    assert error(source).message == message


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            GEN + "let g = count(1)\nfor x in g\n    ()",
            "a Gen[i64] can't be copied, so a loop over it must advance it in place: 'for ... in mut ...'",
        ),
        (
            GEN
            + "let twice = (g: Gen[i64]) -> Gen[i64] =>\n    for x in mut g\n        yield x\nlet g = count(1)\ntwice(g)",
            "a Gen[i64] can't be copied, and a function that gives a generator takes its arguments, so this must be a new value",
        ),
        (GEN + "let g = count(1)\nlet h = g", "a Gen[i64] can't be copied, so it can't be used here"),
        (
            GEN + "let main = () -> () =>\n    let g = count(1)\n    let f = () => print(g)\n    ()",
            "a Gen[i64] can't be copied, so it can't be captured by a closure",
        ),
    ],
)
def test_generators_cant_be_copied(source: str, message: str) -> None:
    with pytest.raises(EllaError) as info:
        prepare(parse(source))
    assert info.value.message == message


def test_generator_types() -> None:
    # A lambda with yield gives a Gen of what it yields; next gives an Optional of it.
    assert (
        prepared_type_of("let f = () -> () =>\n    let g = (x) =>\n        yield [x]\n    ()\n(1, 2)") == "(i64, i64)"
    )
    assert prepared_type_of(GEN + "let mut g = count(1)\nnext(mut g)") == "Optional[i64]"
    struct = "struct C\n    n: i64\n    next = (mut c: C) -> Optional[str] => None\n"
    assert prepared_type_of(struct + "let mut c = C(1)\nnext(mut c)") == "Optional[str]"


def test_array_shapes() -> None:
    # TODO-Claude.md, "arrays typed by shape": dimensions are sizes, size names and `_`; checked statically.
    ok = "let a: Array[f64; N, M] = transpose(reshape(array([1.0, 2.0]), [1, 2]))\n"
    assert shape_of(ok + "let b: Array[f64; M, N] = transpose(a)\nb") == "Array[f64; M, N]"
    assert shape_of(ok + "a[0, 1]") == "f64"
    assert shape_of(ok + "a[0..1]") == "Array[f64; _, M]"
    assert shape_of(ok + "a[0, 0..1]") == "Array[f64; _]"
    assert shape_of(ok + "a * 2.0 + a") == "Array[f64; N, M]"
    assert shape_of("let v = array([1.0])\nv") == "Array[f64; _]"
    assert shape_of("let a: Array[f64] = array([1.0])\na") == "Array[f64]"
    assert error(ok + "let b: Array[f64; N, M] = transpose(a)").message == (
        "the value of 'b' should be Array[f64; N, M], not Array[f64; M, N]"
    )
    assert error(ok + "a[0]").message == "Array[f64; N, M] takes 2 indexes, not 1"
    assert error(ok + "a[0, 0, 0]").message == "Array[f64; N, M] takes 2 indexes, not 3"
    assert error(ok + "a + transpose(a)").message == (
        "'+' needs arrays whose shapes broadcast, not Array[f64; N, M] and Array[f64; M, N]"
    )
    assert error(ok + "dot(a, transpose(a))").message == (
        "dot() needs two arrays of the same shape, not Array[f64; N, M] and Array[f64; M, N]"
    )
    assert error(ok + "matmul(a, a)").message.startswith("argument 2 of matmul() should be")
    assert error("let a: Array[f64; 2] = fill([3], 1.0)\nlet b: Array[f64; 3] = a").message == (
        "the value of 'b' should be Array[f64; 3], not Array[f64; 2]"
    )
    assert error("let a: [i64; 2] = [1]").message.startswith("expected")
    assert error("let a: Optional[i64; 2] = None").message == "only an Array has dimensions, not Optional"
    assert error("let f[T] = (a: Array[f64; T]) -> f64 => 1.0").message == "'T' is a type, not a size"
    assert error("let f[; N] = (a: N) -> f64 => 1.0").message == "'N' is a size, not a type"
    assert error("let f = (a: Array[f64; N]) -> f64 => 1.0").message.startswith("unknown size 'N'")


def test_broadcasting_shapes() -> None:
    ok = "let a: Array[i64; N, 3] = fill([2, 3], 1)\nlet r: Array[i64; 1, 3] = fill([1, 3], 1)\n"
    assert shape_of(ok + "a + array([1, 2, 3])") == "Array[i64; N, _]"
    assert shape_of(ok + "a + r") == "Array[i64; N, 3]"
    assert shape_of(ok + "a + fill([3], 1)") == "Array[i64]"
    assert error(ok + "let c: Array[i64; 4] = fill([4], 1)\na + c").message.startswith("'+' needs arrays whose")


def test_size_parameters() -> None:
    mm = "let mm[T: Num; N, K, M] = (a: Array[T; N, K], b: Array[T; K, M]) -> Array[T; N, M] => matmul(a, b)\n"
    ok = mm + "let a: Array[f64; 2, 3] = fill([2, 3], 1.0)\nlet b: Array[f64; 3, 4] = fill([3, 4], 1.0)\n"
    assert shape_of(ok + "mm(a, b)") == "Array[f64; 2, 4]"
    assert error(ok + "mm(a, a)").message.startswith("argument 2 should be Array[f64; 3, ")
    assert error("let f[; N] = (a: Array[f64; N]) -> Array[f64; 3] => a").message == (
        "the function's result should be Array[f64; 3], not Array[f64; N]"
    )
    # Each use decides the sizes anew; a function with only size parameters can call itself with others.
    rec = "let f[; N] = (a: Array[f64; N], n: i64) -> f64 => if n == 0 then 1.0 else f(a[0..1], n - 1)\n"
    assert shape_of(rec + "f(array([1.0]), 1)") == "f64"
    assert error("let f[; N, N] = (a: Array[f64; N]) -> f64 => 1.0").message == "'N' is already a parameter"
    assert error("let g = () -> i64 =>\n    let f[; N] = 1\n    f\nprint(g())").message == (
        "only a top-level function can have size parameters"
    )


def shape_checks(source: str) -> list[tuple[str, tuple[int | str, ...], tuple[tuple[str, int], ...], str]]:
    """The run-time shape checks of a program's functions and `let`s, in the order they were made."""
    checker = check(parse(source))
    found = [*checker.shape_checks.values(), *((check,) for check in checker.result_shapes.values())]
    return [(c.name, c.expected, c.binds, c.what) for checks in found for c in checks]


def test_shape_checks() -> None:
    # A parameter is checked on every call, defining its size names; a repeated name must have one size.
    assert shape_checks("let f[; N] = (xs: Array[f64; N]) -> i64 => N") == [
        ("xs", (-1,), (("N", 0),), "argument 'xs'")
    ]
    assert shape_checks("let f[; N] = (m: Array[f64; N, N, 3, _]) -> i64 => N") == [
        ("m", (-1, -2, 3, -1), (("N", 0),), "argument 'm'")
    ]
    two = "let f[; N] = (a: Array[f64; N], b: Array[f64; 2, N]) -> i64 => N"
    assert shape_checks(two) == [("a", (-1,), (("N", 0),), "argument 'a'"), ("b", (2, "N"), (), "argument 'b'")]
    # A `let` is checked unless its value is sure to have the shape, or it defines size names.
    assert shape_checks("let a: Array[f64; 2] = fill([2], 1.0)") == [("a", (2,), (), "the value of 'a'")]
    assert shape_checks("let a: Array[f64; 2] = fill([2], 1.0)\nlet b: Array[f64; 2] = a")[1:] == []
    assert shape_checks("let a: Array[f64; N] = array([1.0])\nlet b: Array[f64; N] = a * 2.0")[1:] == []
    assert shape_checks("let a: Array[f64; N] = array([1.0])\nlet b: Array[f64; N] = array([2.0])")[1:] == [
        ("b", ("N",), (), "the value of 'b'")
    ]
    assert shape_checks("let a = array([1.0])\nlet b: Array[f64] = a") == []
    # A result is checked unless every value the function gives is sure to have its shape.
    sure = "let f[; N] = (a: Array[f64; N]) -> Array[f64; N] => a * 2.0"
    assert [c[0] for c in shape_checks(sure)] == ["a"]
    unsure = "let f[; N] = (a: Array[f64; N]) -> Array[f64; N] => fill([3], 1.0)"
    assert shape_checks(unsure)[1:] == [("", ("N",), (), "the function's result")]
    returned = "let f[; N] = (a: Array[f64; N]) -> Array[f64; N] =>\n    if N > 1 then return array([1.0])\n    a\n"
    assert shape_checks(returned)[1:] == [("", ("N",), (), "the function's result")]
    # A size known only while checking isn't compared with at run time.
    nested = "let f[; N] = (xs: [Array[f64; N]], a: Array[f64; N]) -> i64 => 1"
    assert shape_checks(nested) == [("a", (-1,), (("N", 0),), "argument 'a'")]


def test_sizes_as_numbers() -> None:
    assert type_of("let f[; N, M] = (a: Array[f64; N, M]) -> i64 => N * M\nf") == "(Array[f64]) -> i64"
    assert type_of("let a: Array[f64; N] = array([1.0])\nN") == "i64"
    assert type_of("let f = () -> i64 =>\n    let a: Array[f64; N] = array([1.0])\n    N + 1\nf()") == "i64"
    assert type_of("let f[; N] = (a: Array[f64; N]) -> i64 =>\n    let g = (x: i64) => x * N\n    g(2)\nf") == (
        "(Array[f64]) -> i64"
    )
    assert type_of("let f[; N] = (a: Array[f64; N]) -> i64 =>\n    where\n        N > 0\n    N\nf") == (
        "(Array[f64]) -> i64"
    )
    assert error("let f[; N] = (xs: [Array[f64; N]]) -> i64 => N").message.startswith(
        "the size 'N' isn't known when the program runs"
    )
    assert error("let f[; N] = (N: Array[f64; N]) -> i64 => 1").message == (
        "'N' is both a parameter and a size parameter"
    )
    assert error("let f[; N] = (a: Array[f64; N]) -> () =>\n    N = 2").message.startswith("can't assign")
    assert error('match_shape(array([1.0]), [1], "")').message == "undefined name 'match_shape'"
    # A parameter (or variable) that is only there for its sizes is used.
    assert warnings("let f[; N] = (a: Array[f64; N]) -> i64 => N\nprint(f(array([1.0])))") == []
    assert warnings("let f[; N] = (a: Array[f64; N], b: Array[f64; N]) -> f64 => total(b)\nprint(f)") == []
    assert warnings("let main = () -> () =>\n    let a: Array[f64; N] = array([1.0])\n    print(N)") == []


def test_sums_shapes() -> None:
    m = "let m: Array[f64; A, B] = fill([2, 3], 1.0)\n"
    assert shape_of(m + "sums(m, 0)") == "Array[f64; B]"
    assert shape_of(m + "sums(m, 1)") == "Array[f64; A]"
    assert shape_of(m + "let axis = 1\nsums(m, axis)") == "Array[f64]"
    assert shape_of("sums(fill([2, 3], 1), 0)") == "Array[i64]"
    assert error(m + "sums(m, 2)").message == "sums() can't add along axis 2 of Array[f64; A, B]"
    assert error("sums(array([1.0]), 0)").message == (
        "sums() needs an array of 2 or more dimensions, not Array[f64; _]; use total()"
    )
