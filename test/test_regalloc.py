"""Tests of register allocation (ella.regalloc) and the code that uses it.

The programs in test/programs check its results natively, with all
registers and with one of each kind; these tests look at its decisions.
"""

import io
import shutil

import pytest

from ella.codegen import generate
from ella.ir import Function, Module
from ella.lower import compile_program
from ella.native import run_native
from ella.passes import optimize
from ella.regalloc import CALLEE_SAVED, CALLER_SAVED, allocate, dominator_order, liveness

needs_gcc = pytest.mark.skipif(shutil.which("gcc") is None, reason="native code needs gcc")


def optimized(source: str) -> Module:
    return optimize(compile_program(source))


SUM = """\
let sum_squares = (n: i64) -> i64 =>
    let mut total = 0
    let mut i = 0
    while i < n
        i += 1
        total += i * i
    total
let main = () -> () =>
    print(sum_squares(10))
    print(sum_squares(20))
"""


def test_blocks_come_after_their_dominators() -> None:
    function = optimized(SUM).functions["sum_squares"]
    order = dominator_order(function)
    assert order[0] is function.blocks[0]
    assert set(order) == set(function.blocks)
    [header] = [b for b in function.blocks if b.name.startswith("while")]
    assert all(order.index(header) < order.index(b) for b in function.blocks if b.name.startswith(("body", "exit")))


def test_loop_values_are_live_around_the_loop() -> None:
    function = optimized(SUM).functions["sum_squares"]
    blocks = dominator_order(function)
    live_in, _ = liveness(function, blocks)
    [n] = function.params
    [body] = [b for b in function.blocks if b.name.startswith("body")]
    assert n.id in live_in[body]  # still needed by the next test of `i < n`


def test_values_without_calls_get_registers() -> None:
    function = optimized(SUM).functions["sum_squares"]
    allocation = allocate(function)
    values = [p.id for b in function.blocks for p in b.params] + [
        i.dest.id for b in function.blocks for i in b.instrs if i.dest is not None
    ]
    assert all(v in allocation.registers for v in values)
    assert set(allocation.registers.values()) <= set(CALLER_SAVED)  # nothing lives across a call
    assert allocation.saved == []


def registers_of(function: Function) -> dict[str, str]:
    """Each value's register, by the name of the parameter or value as written in the IR."""
    allocation = allocate(function)
    return {f"%{v}": r for v, r in allocation.registers.items()}


def test_values_live_across_calls_get_callee_saved_registers() -> None:
    source = """\
let f = (n: i64) -> i64 =>
    if n < 2 then n else f(n - 1) + f(n - 2)
print(f(10))
"""
    function = optimized(source).functions["f"]
    allocation = allocate(function)
    [n] = function.params
    calls = [i for b in function.blocks for i in b.instrs if i.op == "call_direct"]
    first = calls[0].dest
    assert first is not None
    assert allocation.registers[n.id] in CALLEE_SAVED  # needed after the first call
    assert allocation.registers[first.id] in CALLEE_SAVED  # needed after the second
    assert allocation.saved == sorted(set(allocation.registers.values()) & set(CALLEE_SAVED), key=CALLEE_SAVED.index)


def test_registers_can_be_limited() -> None:
    function = optimized(SUM).functions["sum_squares"]
    allocation = allocate(function, limit=1)
    assert set(allocation.registers.values()) <= {CALLEE_SAVED[0], CALLER_SAVED[0]}
    assert allocate(function, limit=0).registers == {}


def test_generator_functions_keep_values_in_their_frame() -> None:
    source = """\
let count = (n: i64) -> Gen[i64] =>
    let mut i = 0
    while i < n
        yield i
        i += 1
for x in count(3)
    print(x)
"""
    module = optimized(source)
    [body] = [f for f in module.functions.values() if f.generator]
    assert allocate(body).registers == {}


def test_assembly_saves_and_restores_callee_saved_registers() -> None:
    source = """\
let f = (n: i64) -> i64 =>
    if n < 2 then n else f(n - 1) + f(n - 2)
print(f(10))
"""
    assembly = generate(optimized(source))
    body = assembly.split("# function f\n")[1].split("# function")[0]
    assert "mov qword ptr [rbp - 8], rbx" in body and "mov rbx, qword ptr [rbp - 8]" in body


@needs_gcc
@pytest.mark.parametrize("registers", [None, 1, 0])
def test_values_swapped_in_a_loop(registers: int | None) -> None:
    # The loop's jump passes a and b to each other's parameters: a cycle of moves.
    source = """\
let main = () -> () =>
    let mut a = 1
    let mut b = 2
    let mut c = 3
    for _ in 0..7
        let t = a
        a = b
        b = c
        c = t
    print(a, b, c)
    let mut x = 1.5
    let mut y = -2.5
    for _ in 0..3
        let t = x
        x = y * 2.0
        y = t
    print(x, y)
"""
    out = io.StringIO()
    run_native(source, out, registers=registers)
    assert out.getvalue() == "2 3 1\n-10.0 3.0\n"


@needs_gcc
@pytest.mark.parametrize("registers", [None, 1])
def test_runtime_calls_get_arguments_held_in_argument_registers(registers: int | None) -> None:
    # The operands of `ella_overflow` may be in rdi and rsi the other way round.
    source = """\
let f = (a: i64, b: i64) -> i64 => b * a
print(f(3, 4))
print(f(9223372036854775807, 2))
"""
    out = io.StringIO()
    with pytest.raises(Exception, match=r"i64 overflow: 2 \* 9223372036854775807 doesn't fit in 64 bits"):
        run_native(source, out, registers=registers)
    assert out.getvalue() == "12\n"
