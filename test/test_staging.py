import io
import shutil
from pathlib import Path

from ella.checker import check
from ella.interpreter import Interpreter
from ella.ir_interpreter import IRInterpreter
from ella.lower import compile_program
from ella.native import run_native
from ella.parser import parse
from ella.passes import optimize
from ella.staging import Tables, materialize_hygiene, prepare, start_code
from ella.syntax import Block, Let, Name, NamePattern, Quote, parts


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
