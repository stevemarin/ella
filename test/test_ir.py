import io
from collections.abc import Callable

import pytest

from ella.checker import BOOL, I64, check
from ella.interpreter import Interpreter
from ella.ir import (
    Block,
    Branch,
    Function,
    Instr,
    IRError,
    Jump,
    Module,
    Return,
    Terminator,
    Value,
    format_module,
    verify,
)
from ella.ir_interpreter import IRInterpreter, run_ir
from ella.lexer import EllaError, Span
from ella.lower import compile_program
from ella.parser import parse
from ella.passes import optimize

S = Span(1, 1)


def ir_output(source: str) -> list[str]:
    out = io.StringIO()
    run_ir(source, out)
    return out.getvalue().splitlines()


def optimized_output(source: str) -> list[str]:
    out = io.StringIO()
    IRInterpreter(optimize(compile_program(source)), out).run()
    return out.getvalue().splitlines()


def tree_output(source: str) -> list[str]:
    program = parse(source)
    check(program)
    out = io.StringIO()
    Interpreter(out).run(program)
    return out.getvalue().splitlines()


def outcome(run: Callable[[str], list[str]], source: str) -> tuple[list[str], str | None]:
    """What a program prints, and the error it stops with, if any."""
    try:
        return run(source), None
    except EllaError as error:
        return [], str(error)


# Both interpreters must agree on each of these (beyond test/programs).
PROGRAMS = {
    "closures capture by copy": """\
let make = () -> () -> i64 =>
    let mut n = 1
    let get = () -> i64 => n
    n = 2
    get
print(make()())
""",
    "nested closures": """\
let adder = (a: i64) -> (i64) -> (i64) -> i64 =>
    (b: i64) -> (i64) -> i64 => (c: i64) -> i64 => a + b + c
print(adder(1)(2)(3))
""",
    "local recursion inside closure": """\
let outer = (k: i64) -> i64 =>
    let sum_to = (n: i64) -> i64 => if n == 0 then 0 else n + k + sum_to(n - 1)
    sum_to(3)
print(outer(10))
""",
    "shadowed functions": """\
let f = () -> i64 => 1
print(f())
let f = () -> i64 => 2
print(f())
""",
    "mutable function": """\
let mut op = (a: i64) -> i64 => a + 1
op = (a: i64) -> i64 => a * 10
print(op(5))
""",
    "nested index assignment": """\
let mut grid = [[1, 2], [3, 4]]
grid[1][0] += 10
grid[0] = [0]
print(grid)
""",
    "global assignment from function": """\
let mut count = 0
let bump = () -> () =>
    count += 1
bump()
bump()
print(count)
""",
    "or patterns with bindings": """\
enum E
    A(i64)
    B(i64)
    C
for e in [A(1), B(2), C]
    match e
        case A(n) | B(n) => print(n)
        case C => print("c")
""",
    "guards fall through": """\
for n in 0..5
    match n
        case k if k % 2 == 0 => print("{{k}} even")
        case 3 => print("three")
        case _ => print("odd")
""",
    "labeled break and continue": """\
let mut out = []
outer: for i in 0..4
    inner: for j in 0..4
        if j > i then continue outer
        if i == 3 then break outer
        out = out + [(i, j)]
print(out)
""",
    "loop value": """\
let mut i = 0
let first_square_over = (limit: i64) -> i64 =>
    let mut k = 0
    loop
        k += 1
        if k * k > limit then break k * k
print(first_square_over(30))
""",
    "while with continue": """\
let mut i = 0
let mut odds = 0
while i < 10
    i += 1
    if i % 2 == 0 then continue
    odds += i
print(odds)
""",
    "try on optional": """\
let half = (n: i64) -> Optional[i64] => if n % 2 == 0 then Some(n // 2) else None
let quarter = (n: i64) -> Optional[i64] => half(half(n)?)
print(quarter(8))
print(quarter(6))
""",
    "try rewraps the error": """\
let parse = (s: str) -> Result[i64, str] => if s == "1" then Ok(1) else Error("bad: {{s}}")
let check = (s: str) -> Result[(), str] =>
    let n = parse(s)?
    print(n)
    Ok(())
print(check("1"))
print(check("x"))
""",
    "postcondition runs on early return": """\
let f = (n: i64) -> i64 =>
    where
        return > 0
    if n > 5 then return n
    -1
print(f(9))
print(f(1))
""",
    "string iteration and indexing": """\
let word = "héllo"
for c in word
    print(c)
print(word[1] + word[4])
print(len(word))
""",
    "chained comparison short circuits": """\
let loud = (n: i64) -> i64 =>
    print("eval {{n}}")
    n
print(loud(3) < loud(2) < loud(1))
print(1 < 2 < 3)
""",
    "and or short circuit": """\
let loud = (b: bool) -> bool =>
    print("eval {{b}}")
    b
print(loud(false) and loud(true))
print(loud(true) or loud(false))
""",
    "main runs last": """\
let main = () -> () => print("main")
print("top")
""",
    "value used before let in a function": """\
let f = () -> i64 => later
print(f())
let later = 1
""",
    "index out of range": "let xs = [1, 2]\nprint(xs[2])",
    "no case matches": "match 3\n    case 1 => print(1)",
    "for pattern doesn't match": "for (a, 1) in [(1, 1), (2, 2)]\n    print(a)",
    "stack overflow": "let f = (n: i64) -> i64 => f(n + 1)\nf(0)",
    "division by zero": "print(1 // 0)",
}


@pytest.mark.parametrize("source", PROGRAMS.values(), ids=PROGRAMS.keys())
def test_interpreters_agree(source: str) -> None:
    expected = outcome(tree_output, source)
    assert outcome(ir_output, source) == expected
    assert outcome(optimized_output, source) == expected


def test_some_programs_fail_the_same_way() -> None:
    assert outcome(ir_output, PROGRAMS["no case matches"])[1] == "1:1: no case matches 3"
    assert outcome(ir_output, PROGRAMS["value used before let in a function"])[1] == (
        "1:22: 'later' is used before its 'let' has run"
    )


# Lowering and printing


def test_ir_for_a_small_function() -> None:
    module = compile_program("let add = (a: i64, b: i64) -> i64 => a + b\nprint(add(1, 2))")
    assert (
        format_module(module).split("\n\n")[1]
        == """\
function add(%2: i64, %3: i64) -> i64
    local a: i64
    local b: i64
  entry1(%2: i64, %3: i64):
    store a, %2
    store b, %3
    %6: i64 = load a
    %7: i64 = load b
    %8: i64 = add %6, %7
    jump exit4(%8)
  exit4(%5: i64):
    return %5
"""
    )


def test_if_joins_with_a_block_parameter() -> None:
    text = format_module(compile_program("let f = (c: bool) -> i64 => if c then 1 else 2"))
    assert "branch %" in text
    assert "jump join" in text and "(%" in text.split("join")[-1]


def test_closures_capture_locals() -> None:
    text = format_module(compile_program("let f = (n: i64) -> () -> i64 => () -> i64 => n"))
    assert "closure lambda(" in text
    assert "function lambda(%" in text  # the captured n is the lambda's first parameter


def test_list_assignment_builds_a_new_list() -> None:
    text = format_module(compile_program("let mut xs = [1]\nxs[0] = 2"))
    assert "set_index" in text and "global_set xs" in text


def test_dead_code_after_return_is_removed() -> None:
    text = format_module(compile_program("let f = () -> i64 =>\n    return 1\n    2"))
    assert "const 2" not in text
    assert "dead" not in text


# The verifier


def function_with(*instrs: Instr, terminator: Terminator | None = None) -> Module:
    """A module with one function `f` returning i64, whose entry block holds `instrs`.

    Unless told otherwise, the block returns the last instruction's result.
    """
    block = Block("entry", [], list(instrs))
    last = instrs[-1].dest
    assert last is not None
    block.terminator = terminator if terminator is not None else Return(last)
    function = Function("f", I64, S, [block])
    return Module({"f": function})


def test_verifier_accepts_valid_ir() -> None:
    one = Value(1, I64)
    verify(function_with(Instr("const", one, [], S, const=1)))


@pytest.mark.parametrize(
    ("module", "message"),
    [
        (function_with(Instr("const", Value(1, BOOL), [], S, const=1)), "bad operand or result types"),
        (function_with(Instr("add", Value(2, I64), [Value(1, I64), Value(1, I64)], S)), "used but never defined"),
        (function_with(Instr("frobnicate", Value(1, I64), [], S)), "unknown operation"),
        (
            function_with(
                Instr("const", Value(1, I64), [], S, const=1), Instr("const", Value(1, I64), [], S, const=2)
            ),
            "defined twice",
        ),
        (function_with(Instr("print", Value(1, I64), [], S)), "print has the wrong kind of result"),
        (function_with(Instr("load", Value(1, I64), [], S, name="nowhere")), "bad operand or result types"),
        (function_with(Instr("const", Value(1, BOOL), [], S, const=True)), "returns a value of the wrong type"),
    ],
)
def test_verifier_rejects(module: Module, message: str) -> None:
    with pytest.raises(IRError, match=message):
        verify(module)


def test_verifier_checks_jumps_and_dominance() -> None:
    cond = Value(1, BOOL)
    entry = Block("entry", [], [Instr("const", cond, [], S, const=True)])
    left, right, join = Block("left"), Block("right"), Block("join", [Value(4, I64)])
    entry.terminator = Branch(cond, left, right)
    x = Value(2, I64)
    left.instrs.append(Instr("const", x, [], S, const=1))
    left.terminator = Jump(join, [x])
    right.terminator = Jump(join, [x])  # x is defined in `left`, which doesn't dominate `right`
    join.terminator = Return(join.params[0])
    module = Module({"f": Function("f", I64, S, [entry, left, right, join])})
    with pytest.raises(IRError, match="doesn't dominate"):
        verify(module)

    right.instrs.append(Instr("const", Value(3, I64), [], S, const=2))
    right.terminator = Jump(join, [])  # the wrong number of values for join's parameter
    with pytest.raises(IRError, match="wrong number or types"):
        verify(module)


def test_verifier_requires_terminators() -> None:
    function = Function("f", I64, S, [Block("entry")])
    with pytest.raises(IRError, match="has no terminator"):
        verify(Module({"f": function}))
