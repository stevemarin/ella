import io
import shutil
from collections.abc import Callable

import pytest

from ella.checker import BOOL, I64, UNIT, FuncTy, ListTy
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
    format_placement,
    verify,
)
from ella.ir_interpreter import IRInterpreter, run_ir
from ella.lexer import EllaError, Span
from ella.lower import compile_program
from ella.native import run_native
from ella.parser import parse
from ella.passes import optimize
from ella.staging import prepare

S = Span(1, 1)


def ir_output(source: str) -> list[str]:
    out = io.StringIO()
    run_ir(source, out)
    return out.getvalue().splitlines()


def optimized_output(source: str) -> list[str]:
    out = io.StringIO()
    IRInterpreter(optimize(compile_program(source, out)), out).run()
    return out.getvalue().splitlines()


def native_output(source: str) -> list[str]:
    out = io.StringIO()
    run_native(source, out)
    return out.getvalue().splitlines()


def tree_output(source: str) -> list[str]:
    out = io.StringIO()
    program, checker = prepare(parse(source), out)
    Interpreter(out, checker).run(program)
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
let parse = (s: str) -> Result[i64, str] => if s == "1" then Ok(1) else Err("bad: {{s}}")
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
    "slice out of range": 'print("abc"[1..2])\nprint("abc"[2..1])',
    "to_i64 out of range": "print(to_i64(-1.5))\nprint(to_i64(1.0e19))",
    "split by nothing": 'print(split("a b", " "))\nprint(split("a", ""))',
    "stack overflow": "let f = (n: i64) -> i64 => f(n + 1)\nf(0)",
    "division by zero": "print(1 // 0)",
    # Memory: copies, changing lists in place, and arenas (docs/memory-model.md).
    "shared until changed in place": """\
let f = () -> () =>
    let mut m = [[1], [2]]
    let r = m[0]
    m[0][0] = 5
    print(r)
    print(m)
f()
""",
    "a global changed in a function": """\
let mut g = [[1]]
let h = g
let poke = () -> () =>
    g[0][0] = 7
poke()
print(h)
print(g)
""",
    "a read keeps its value": """\
let mut xs = [1]
let zs = [xs, (xs += [5])]
xs[0] = 9
print(zs)
print(xs)
""",
    "break out of nested loops with a value": """\
let f = () -> [str] =>
    let r = outer: loop
        let mut i = 0
        loop
            i += 1
            let s = "v{{i}}"
            if i == 3 then break outer [s, s + "!"]
    r
print(f())
""",
    "a loop condition makes objects": """\
let mut s = ""
while len(s + "ab") < 9
    s = s + "x"
print(s)
""",
    "a closure made in a loop": """\
let f = () -> i64 =>
    let mut g = () -> i64 => 0
    for i in 0..4
        let name = "n{{i}}"
        g = () -> i64 => len(name) + i
    g()
print(f())
""",
    "return from inside a loop": """\
let find = (xs: [str], prefix: str) -> Optional[[str]] =>
    for x in xs
        let both = prefix + x
        if len(both) > 3 then return Some([both, x])
    None
print(find(["a", "bb", "ccc"], "z"))
print(find(["a"], "z"))
""",
    "try inside a loop": """\
let half = (n: i64) -> Result[[i64], str] => if n % 2 == 0 then Ok([n // 2]) else Err("odd {{n}}")
let halves = (xs: [i64]) -> Result[[i64], str] =>
    let mut out = []
    for x in xs
        out += half(x)?
    Ok(out)
print(halves([2, 4, 6]))
print(halves([2, 3]))
""",
    "an if joins a borrowed value": """\
let f = (c: bool) -> [[i64]] =>
    let xs = [[1], [2]]
    let mut best = if c then xs[0] else [0]
    best[0] = 9
    [xs[0], best]
print(f(true))
print(f(false))
""",
    "a global set from a mut parameter": """\
let mut g = [[0]]
let keep = (mut xs: [[i64]]) -> () =>
    g = xs
    xs[0][0] = 5
let mut mine = [[1]]
keep(mut mine)
print(g)
print(mine)
""",
    "write back out of range": """\
let mut g = [[1], [2]]
let shrink = (mut row: [i64]) -> () =>
    g = []
    row += [3]
shrink(mut g[1])
""",
    "mut index out of range": """\
let f = (mut x: i64) -> () =>
    x += 1
let mut xs = [1]
f(mut xs[3])
""",
    "nested mut calls": """\
let inner = (mut xs: [[i64]], i: i64) -> () =>
    xs[i] += [i]
let outer = (mut xs: [[i64]]) -> () =>
    for i in 0..len(xs)
        inner(mut xs, i)
        inner(mut xs, i)
let mut grid = [[], [], []]
outer(mut grid)
print(grid)
""",
    "labeled continue frees the inner loop": """\
let f = () -> i64 =>
    let mut count = 0
    outer: for i in 0..4
        for j in 0..4
            let pair = [i, j]
            if j > i then continue outer
            count += len(pair)
    count
print(f())
""",
    "an enum holding lists": """\
enum Tree
    Leaf([i64])
    Node(Tree, Tree)
let sum = (t: Tree) -> i64 =>
    match t
        case Leaf(xs)
            let mut s = 0
            for x in xs
                s += x
            s
        case Node(a, b) => sum(a) + sum(b)
let grow = (t: Tree, n: i64) -> Tree => if n == 0 then t else grow(Node(t, Leaf([n, n])), n - 1)
print(sum(grow(Leaf([1]), 5)))
""",
    "a copy in a loop of a list made before it": """\
let f = () -> () =>
    let base = [0, 0]
    for i in 0..3
        let mut row = base
        row[0] += i
        print(row)
f()
""",
}


# Code built and run at run time, which only the interpreters can do.
RUN_TIME_STAGING = {
    "run at run time": """\
let power = (n: i64, x: Code[i64]) -> Code[i64] =>
    if n == 0 then quote 1 else quote $x * $(power(n - 1, x))
let code = power(3, quote 2)
print(code)
print(run(code))
let k = 10
let add_k = quote (x: i64) -> i64 => x + k
print(run(add_k)(5))
let meta = quote quote 1 + 2
print(run(run(meta)))
""",
    "quoting a quote's variable in its splice": """\
let id = (c: Code[i64]) -> Code[i64] => c
let c = quote
    let y = 1
    $(id(quote y)) + 1
print(c)
print(run(c))
let wrap = (body: Code[i64]) -> Code[i64] => quote
    let y = 10
    $body * y
let d = quote
    let y = 2
    $(wrap(quote y))
print(run(d))
""",
    "reflection at run time": """\
struct P
    x: i64
    y: i64
let t = type P
print(t)
print(fields(t))
print(field_type(t, "y"))
print(type_kind(t))
let sum_fields = (names: [str], v: Code[P]) -> Code[i64] =>
    let mut total = quote 0
    for n in names
        total = quote $total + $v.$n
    total
let code = quote (p: $t) -> i64 => $(sum_fields(fields(t), quote p))
print(code)
print(run(code)(P(3, 4)))
print(type P == type P)
print(field_type(t, "x") == type str)
""",
    "carrying data into code": """\
enum Shape
    Circle(f64)
    Square(f64)
struct P
    x: i64
    tags: [str]
let p = P(1, ["a"])
let shapes = [Circle(1.5), Square(2.0)]
let r = 1..4
let c = quote (p, shapes, r, "q\\"", -0.0, ())
print(c)
print(run(c))
""",
    # The code uses a method and a generic function the program never did,
    # so they are lowered for the first time when it runs.
    "methods and generics first used by code": """\
struct V
    x: i64
    add = (a: V, b: V) -> V => V(a.x + b.x)
    double = (v: V) -> V => v + v
let twice[T: Add] = (x: T) -> T => x + x
print(run(quote V(5).double()))
print(run(quote twice(V(1)) + V(2)))
print(run(quote twice(3)))
""",
}


@pytest.mark.parametrize("source", PROGRAMS.values(), ids=PROGRAMS.keys())
def test_interpreters_agree(source: str) -> None:
    expected = outcome(tree_output, source)
    assert outcome(ir_output, source) == expected
    assert outcome(optimized_output, source) == expected
    if shutil.which("gcc") is not None:
        assert outcome(native_output, source) == expected


@pytest.mark.parametrize("source", RUN_TIME_STAGING.values(), ids=RUN_TIME_STAGING.keys())
def test_run_time_staging(source: str) -> None:
    expected = outcome(tree_output, source)
    assert expected[1] is None
    assert outcome(ir_output, source) == expected
    assert outcome(optimized_output, source) == expected
    if shutil.which("gcc") is not None:
        # Native code can only build code while compiling, and stops if it gets to a quote.
        _, error = outcome(native_output, source)
        assert error is not None and "at run time needs an interpreter" in error


def test_some_programs_fail_the_same_way() -> None:
    assert (
        outcome(ir_output, PROGRAMS["slice out of range"])[1]
        == "2:12: slice 2..1 is out of range for a str of length 3"
    )
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


def test_list_assignment_changes_the_list_in_place() -> None:
    text = format_module(compile_program("let mut xs = [1]\nxs[0] = 2"))
    assert "set_index %" in text and "@global" in text
    assert text.count("global_set xs") == 1  # only the `let`: the list itself changes


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


# Memory: regions, copies and the interpreter's checks


def test_objects_are_made_where_they_end_up() -> None:
    source = """\
let squares = (n: i64) -> [i64] =>
    let mut out = []
    for i in 0..n
        out += [i * i]
    out
print(squares(4))
"""
    text = format_module(compile_program(source))
    assert "function squares(%2: i64) -> [i64]  # levels: 2" in text
    assert "local out: [i64] @result" in text  # it is returned, so it lives in the caller's arena
    assert "= list @result" in text
    assert "= list %" in text and "@1" in text  # [i * i] is a temporary of the loop's level
    assert "= append %" in text and "@result" in text  # grows in place
    assert "reset 1" in text


def test_copies_are_explicit() -> None:
    text = format_module(compile_program("let a = [1]\nlet mut b = a\nb[0] = 2"))
    assert "copy %" in text


def test_mut_arguments_come_back_as_results() -> None:
    text = format_module(compile_program("let f = (mut xs: [i64]) -> () =>\n    xs += [1]\nlet mut v = [0]\nf(mut v)"))
    assert "function f(mut %2: [i64]) -> ()" in text
    assert "local xs: [i64] @mut0" in text
    assert "append %" in text and "@mut0" in text
    assert "return %" in text and ", mut %" in text
    assert "(mut %" in text and "@global, " not in text  # the call: `%5: (), %6: [i64] = call %4(mut %3 @global)`


@pytest.mark.parametrize(
    ("instr", "message"),
    [
        (Instr("list", Value(1, ListTy(I64)), [], S), "list needs a valid region"),
        (Instr("list", Value(1, ListTy(I64)), [], S, region="3"), "list needs a valid region"),
        (Instr("list", Value(1, ListTy(I64)), [], S, region="mut0"), "list needs a valid region"),
        (Instr("const", Value(1, I64), [], S, const=1, region="0"), "const can't have a region"),
        (Instr("reset", None, [], S, index=1), "bad operand or result types for reset 1"),
    ],
)
def test_verifier_checks_regions(instr: Instr, message: str) -> None:
    one = Instr("const", Value(9, I64), [], S, const=1)
    with pytest.raises(IRError, match=message):
        verify(function_with(instr, one))


def run_module(module: Module) -> list[str]:
    out = io.StringIO()
    IRInterpreter(module, out).run()
    return out.getvalue().splitlines()


def test_using_an_object_after_its_arena_is_freed_is_caught() -> None:
    # The list is made in level 1 and printed after `reset 1` has freed it.
    xs = Value(1, ListTy(I64))
    block = Block("entry", [], [Instr("list", xs, [], S, region="1"), Instr("reset", None, [], S, index=1)])
    block.instrs.append(Instr("print", None, [xs], Span(3, 4)))
    block.instrs.append(Instr("const", unit := Value(2, UNIT), [], S, const=()))
    block.terminator = Return(unit)
    module = Module({"top": Function("top", UNIT, S, [block], levels=2)})
    verify(module)
    with pytest.raises(EllaError, match=r"3:4: memory check failed: %1 is used after its arena \(depth 2\) was freed"):
        run_module(module)


def test_returning_an_object_from_a_freed_arena_is_caught() -> None:
    # f makes its result in its own level 0, which is freed when it returns.
    xs = Value(1, ListTy(I64))
    f_block = Block("entry", [], [Instr("list", xs, [], S, region="0")], Return(xs))
    f = Function("f", ListTy(I64), S, [f_block])
    function_value, result = Value(1, FuncTy((), ListTy(I64))), Value(2, ListTy(I64))
    top_block = Block("entry", [], [Instr("closure", function_value, [], S, "f", region="global")])
    top_block.instrs.append(Instr("call", result, [function_value], S, region="0"))
    top_block.instrs.append(Instr("const", unit := Value(3, UNIT), [], S, const=()))
    top_block.terminator = Return(unit)
    module = Module({"top": Function("top", UNIT, S, [top_block]), "f": f})
    verify(module)
    with pytest.raises(EllaError, match=r"memory check failed: the result %1 would outlive its arena"):
        run_module(module)


# Loops that carry values (docs/memory-model.md §6)


def test_a_loop_carries_a_variable_it_rebuilds() -> None:
    source = """\
let f = (n: i64) -> str =>
    let mut s = ""
    for i in 0..n
        s = s + "x"
    s
print(f(3))
"""
    module = compile_program(source)
    text = format_module(module)
    assert "local s.1: str @carry1" in text
    assert "swap 1" in text and "reset 1" in text
    assert "= add %20, %21 @1" in text  # the new string goes in the iteration's arena
    assert "= carry %" in text  # kept at the end of each iteration
    assert "= copy %" in text and "@result" in text  # and copied out when the loop ends
    assert module.functions["f"].levels == 3  # level 1 and its spare
    assert "<- in a loop" not in format_placement(module)
    assert run_module(module) == ["xxx"]


@pytest.mark.parametrize(
    "body",
    [
        "xs += [i]",  # grows in place
        "xs = [i]\n        xs[0] = 1",  # changed in place too
        "let xs = [i]\n        print(xs)",  # another variable
        "xs = [i]\n        g()",  # a global, and the loop calls a function
        "xs = [i]\n        let h = () -> i64 => 1",  # a global, and a closure
        "for j in 0..1\n            xs = [j]\n            break outer",  # (the inner loop) leaves by a labeled break
    ],
)
def test_loops_that_dont_carry(body: str) -> None:
    source = f"""\
let g = () -> () => print("g")
let mut xs = [0]
outer: for i in 0..2
    for _ in 0..1
        {body}
print(xs)
"""
    module = compile_program(source)
    swaps = [i.index for b in module.functions["top"].blocks for i in b.instrs if i.op == "swap"]
    # The outer loop may carry xs, but the inner one never does.
    assert all(index == 1 for index in swaps)


def test_a_value_left_in_the_carried_arena_is_caught() -> None:
    # Without `carry`, a value from the iteration before stays in the arena
    # that `swap` frees.
    source = """\
let f = (n: i64) -> str =>
    let mut s = "a"
    for i in 0..n
        if i == 0 then
            s = s + "x"
    s
print(f(3))
"""
    module = compile_program(source)
    for block in module.functions["f"].blocks:
        replacements = {i.dest.id: i.args[0] for i in block.instrs if i.op == "carry" and i.dest is not None}
        block.instrs = [i for i in block.instrs if i.op != "carry"]
        for instr in block.instrs:
            instr.args = [replacements.get(arg.id, arg) for arg in instr.args]
    verify(module)
    with pytest.raises(EllaError, match="memory check failed: .* is used after its arena"):
        run_module(module)


def test_the_last_use_of_a_variable_moves_its_value() -> None:
    source = """\
let f = () -> () =>
    let mut items = [1]
    let mut other = [2, 3]
    other[0] = 4
    items = other
    items[1] = 5
    print(items)
f()
"""
    module = compile_program(source)
    assert "copy" not in [i.op for b in module.functions["f"].blocks for i in b.instrs]
    assert run_module(module) == ["[4, 5]"]
    # If it is read afterwards, it is copied, so that changing items doesn't change it.
    module = compile_program(source.replace("    print(items)\n", "    print(items)\n    print(other)\n"))
    assert "copy" in [i.op for b in module.functions["f"].blocks for i in b.instrs]
    assert run_module(module) == ["[4, 5]", "[4, 3]"]


def test_verifier_checks_swap_and_carry() -> None:
    text = Value(1, ListTy(I64))
    one = Instr("const", Value(3, I64), [], S, const=1)
    swap = Instr("swap", None, [], S, index=1)  # the function has no level 1, nor a spare level 2
    with pytest.raises(IRError, match="bad operand or result types for swap 1"):
        verify(function_with(swap, one))
    carry = Instr("carry", Value(2, ListTy(I64)), [text], S, index=1, region="0")
    with pytest.raises(IRError, match="bad operand or result types for"):
        verify(function_with(Instr("list", text, [], S, region="0"), carry))


def test_storing_an_object_where_it_would_outlive_its_arena_is_caught() -> None:
    xs = Value(1, ListTy(I64))
    block = Block("entry", [], [Instr("list", xs, [], S, region="0"), Instr("global_set", None, [xs], S, "g")])
    block.instrs.append(Instr("const", unit := Value(2, UNIT), [], S, const=()))
    block.terminator = Return(unit)
    module = Module({"top": Function("top", UNIT, S, [block])}, {"g": ListTy(I64)})
    verify(module)
    with pytest.raises(EllaError, match=r"memory check failed: the value of global g would outlive its arena"):
        run_module(module)


def test_profile_and_placement_report() -> None:
    # The list is changed in place as well as replaced, so the loop doesn't
    # carry it: each new list goes to the function's result.
    source = """\
let f = (n: i64) -> [i64] =>
    let mut xs = [0]
    for i in 0..n
        xs = [i, i]
        xs[0] = 1
    xs
print(f(3))
"""
    module = compile_program(source)
    placement = format_placement(module)
    assert "function f\n" in placement
    assert "4:14     list      @result  [i64]  <- in a loop (level 1), but outlives the iteration" in placement
    interpreter = IRInterpreter(module, io.StringIO())
    interpreter.run()
    profile = interpreter.format_profile()
    assert "High water by scope:" in profile and "f level 0" in profile
    assert "3x  4:14 list in f" in profile


@pytest.mark.parametrize(
    ("instr", "message"),
    [
        (Instr("pool_new", Value(1, ListTy(I64)), [], S, region="0"), "bad operand or result types for"),
        (
            Instr("set_field", None, [Value(9, I64), Value(9, I64)], S, "P", region="0"),
            "bad operand or result types for",
        ),
        (Instr("set_index", None, [], S, region="global"), "bad operand or result types for"),
        (Instr("list", Value(1, ListTy(I64)), [], S, region="slot"), "list needs a valid region, not @slot"),
    ],
)
def test_verifier_checks_new_operations(instr: Instr, message: str) -> None:
    one = Instr("const", Value(9, I64), [], S, const=1)
    with pytest.raises(IRError, match=message):
        verify(function_with(one, instr, Instr("const", Value(10, I64), [], S, const=1)))


def test_lowering_new_operations() -> None:
    source = """\
struct Node
    name: str
let mut p: Pool[Node] = pool_new()
let h = alloc(mut p, Node("a"))
p[h].name = p[h].name + "!"
free(mut p, h)
let first[T] = (xs: [T]) -> T => xs[0]
print(first([1]))
print(first(["a"]))
"""
    text = format_module(compile_program(source))
    assert "= pool_new @global" in text and "= alloc %" in text and "free %" in text
    assert "= pool_get %" in text and "set_field %" in text and "@slot" in text
    assert "function first[i64](%" in text and "function first[str](%" in text
