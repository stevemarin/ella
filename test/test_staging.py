import io
import shutil
from pathlib import Path

import pytest

from ella.checker import check
from ella.interpreter import Interpreter
from ella.ir_interpreter import IRInterpreter
from ella.lexer import EllaError
from ella.lower import compile_program
from ella.native import run_native
from ella.parser import parse
from ella.passes import optimize
from ella.staging import Tables, materialize_hygiene, prepare, start_code
from ella.syntax import Block, EnumDef, Let, Name, NamePattern, Quote, parts, show


def test_quoted_binders_and_references_share_hygiene_identity() -> None:
    program = parse("quote\n    let x = 1\n    x")
    checker = check(program)
    quote = program.body[0]
    assert isinstance(quote, Quote)

    code = start_code(quote, Tables.sharing(checker)).build(lambda _: None, lambda name: name)

    assert isinstance(code, Block)
    binding = code.exprs[0]
    reference = code.exprs[1]
    assert isinstance(binding, Let) and isinstance(reference, Name)
    assert binding.bindings[0].name == reference.name == "x"
    assert binding.bindings[0].hygiene is not None
    assert binding.bindings[0].hygiene == reference.hygiene


def test_materialized_hygiene_avoids_existing_names() -> None:
    program = parse("quote\n    let x = 1\n    x")
    checker = check(program)
    quote = program.body[0]
    assert isinstance(quote, Quote)
    code = start_code(quote, Tables.sharing(checker)).build(lambda _: None, lambda name: name)
    assert isinstance(code, Block)
    binding = code.exprs[0]
    assert isinstance(binding, Let)
    identity = binding.bindings[0].hygiene
    assert identity is not None

    materialized = materialize_hygiene(code, {f"__ella_hygiene_{identity}"})

    assert isinstance(materialized, Block)
    binding = materialized.exprs[0]
    reference = materialized.exprs[1]
    assert isinstance(binding, Let) and isinstance(reference, Name)
    assert binding.bindings[0].name == reference.name == f"__ella_hygiene_{identity}_"
    assert binding.bindings[0].hygiene is None
    assert reference.hygiene is None


def test_or_pattern_alternatives_share_hygiene_identity() -> None:
    program = parse(
        "enum Value\n    Left(i64)\n    Right(i64)\nquote\n    match Left(1)\n        case Left(x) | Right(x) => x"
    )
    checker = check(program)
    quote = program.body[1]
    assert isinstance(quote, Quote)
    code = start_code(quote, Tables.sharing(checker)).build(lambda _: None, lambda name: name)
    nodes: list[object] = [code]
    patterns: list[NamePattern] = []
    while nodes:
        node = nodes.pop()
        if isinstance(node, NamePattern) and node.name == "x":
            patterns.append(node)
        nodes.extend(parts(node))

    assert len(patterns) == 2
    assert patterns[0].hygiene is not None
    assert patterns[0].hygiene == patterns[1].hygiene


def test_staged_json_validation_example() -> None:
    path = Path(__file__).parent.parent / "examples" / "example_staged_json_validation.ella"
    source = path.read_text()
    expected = 'Some(Person("Ada", 36))\nNone\nNone\n'

    program, checker = prepare(parse(source), path=path)
    tree_out = io.StringIO()
    Interpreter(tree_out, checker).run(program)
    assert tree_out.getvalue() == expected

    module = compile_program(source, path=path)
    ir_out = io.StringIO()
    IRInterpreter(module, ir_out).run()
    assert ir_out.getvalue() == expected

    optimized_out = io.StringIO()
    IRInterpreter(optimize(module), optimized_out).run()
    assert optimized_out.getvalue() == expected

    if shutil.which("gcc") is not None:
        native_out = io.StringIO()
        run_native(source, native_out, path=path)
        assert native_out.getvalue() == expected


CONSTANTS = """\
constraint Counted
    const COUNT: i64
    const NAMES: [str]
let loud = () -> i64 =>
    print("computing")
    let mut n = 0
    for i in 0..4
        n += i
    n
struct R
    n: i64
    implements Counted
        const COUNT = loud()
        const NAMES = ["a", "b"]
let f[T: Counted] = (_x: T) -> i64 => T.COUNT + T.COUNT + len(T.NAMES)
print("run", f(R(1)))
"""


def test_constants_are_computed_once_while_compiling() -> None:
    # What the computation prints comes out while compiling, once, before the program runs.
    out = io.StringIO()
    program, checker = prepare(parse(CONSTANTS), out)
    assert out.getvalue() == "computing\n"
    tree_out = io.StringIO()
    Interpreter(tree_out, checker).run(program)
    assert tree_out.getvalue() == "run 14\n"
    # The code holds the values: a number as a literal, a list in a global, made before any code runs,
    # which reading the constant borrows.
    [struct] = [expr for expr in program.body if isinstance(expr, EnumDef) and expr.name == "R"]
    assert [show(c.value) for c in struct.implementations[0].constants] == ["6", "R.__const_Counted_NAMES"]
    assert show(program.body[0]) == '(let [R.__const_Counted_NAMES: [str]] (list "a" "b"))'

    ir_out = io.StringIO()
    IRInterpreter(compile_program(CONSTANTS, ir_out), ir_out).run()
    assert ir_out.getvalue() == "computing\nrun 14\n"
    if shutil.which("gcc") is not None:
        native_out = io.StringIO()
        run_native(CONSTANTS, native_out)
        assert native_out.getvalue() == "computing\nrun 14\n"


def compile_error(source: str) -> EllaError:
    with pytest.raises(EllaError) as info:
        prepare(parse(source), io.StringIO())
    return info.value


def test_constants_that_cant_be_computed() -> None:
    monoid = "constraint M\n    combine: (Self, Self) -> Self\n    const E: Self\n"
    fold = "let fold[T: M] = (xs: [T]) -> T =>\n    let mut acc = T.E\n    for x in xs\n        acc = acc.combine(x)\n    acc\n"
    own = "struct S\n    n: i64\n    implements M\n        combine = (a: S, _b: S) -> S => a\n        const E = fold([S(1)])\n"
    assert compile_error(monoid + own + fold).message == "constant S.E needs its own value to be computed"
    divide = "constraint K\n    const Z: i64\nlet div = (a: i64, b: i64) -> i64 => a // b\n"
    error = compile_error(divide + "struct R\n    n: i64\n    implements K\n        const Z = div(1, 0)\n")
    assert (str(error.span), error.message) == ("3:40", "integer division by zero")
    stop = "constraint K\n    const Z: i64\nlet stop = () -> i64 =>\n    exit(3)\n    0\n"
    message = compile_error(stop + "struct R\n    n: i64\n    implements K\n        const Z = stop()\n").message
    assert message == "exit() can't stop the program while it is being compiled"


def test_constants_of_generic_types_stay_literals() -> None:
    # A global has one type, but a Bag[T]'s constant is a Bag of each T it is used with.
    source = """\
constraint Empty
    const NONE: Self
struct Bag[T]
    items: [T]
    implements Empty
        const NONE = Bag([])
let none[T: Empty] = (_x: T) -> T => T.NONE
print(none(Bag([1])), none(Bag(["a"])))
"""
    program, checker = prepare(parse(source), io.StringIO())
    [bag] = [expr for expr in program.body if isinstance(expr, EnumDef) and expr.name == "Bag"]
    assert show(bag.implementations[0].constants[0].value) == "(call Bag (list))"
    out = io.StringIO()
    Interpreter(out, checker).run(program)
    assert out.getvalue() == "Bag([]) Bag([])\n"


def test_local_types_move_to_the_top_level_but_not_out_of_quotes() -> None:
    # Linking names a function's own types after it and puts them in front of it; a type in a quote is
    # part of the code the quote makes, and stays in it.
    source = """\
let f = () -> i64 =>
    struct S
        n: i64
    S(1).n
let g = () -> Code[i64] =>
    quote
        struct S
            n: i64
        S(4).n + 1
print(f(), run(g()))
"""
    program, checker = prepare(parse(source), io.StringIO())
    assert [show(expr) for expr in program.body[:2]] == [
        "(struct f#S n: i64)",
        "(let [f] (lambda () -> i64 (block (. (call f#S 1) n))))",
    ]
    assert "(struct S n: i64)" in show(program.body[2])
    out = io.StringIO()
    Interpreter(out, checker).run(program)
    assert out.getvalue() == "1 5\n"
