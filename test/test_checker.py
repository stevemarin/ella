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
from ella.staging import prepare
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
        ("(1, 2).x", "a (i64, i64) has no fields"),
        ("len(5)", "len() needs a list, str, range or Pool, not i64"),
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
            "only a nocopy struct can have a finalize function",
        ),
        ("nocopy struct E\n    finalize = (e: E) -> () => ()", "a nocopy struct needs at least one field"),
        ("let mut p: Pool[str] = pool_new()\nlet q = p", "a Pool[str] can't be copied, so it can't be used here"),
        (
            "let mut p: Pool[str] = pool_new()\nlet h = alloc(p, 'x')",
            "alloc() changes its pool, so mark it: 'alloc(mut pool, ...)'",
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
        (
            "let f = () -> () =>\n    let g[T] = (x: T) -> T => x\n    ()",
            "only a top-level function can have type parameters",
        ),
        ("let add[T] = (a: T, b: T) -> T => a + b", "'+' doesn't work on T"),
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
