import io

import pytest

from ella.ir import Function, IRError, Module, format_function
from ella.ir_interpreter import IRInterpreter
from ella.lexer import EllaError
from ella.lower import compile_program
from ella.passes import PASSES, optimize, run_pass


def compiled(source: str, passes: list[str] | None = None) -> Module:
    """The IR for `source`, after the given passes (or the whole default pipeline)."""
    module = compile_program(source)
    return optimize(module, passes)


def ops(function: Function) -> list[str]:
    """The operations in a function, in order."""
    return [instr.op for block in function.blocks for instr in block.instrs]


def run(module: Module) -> list[str]:
    out = io.StringIO()
    IRInterpreter(module, out).run()
    return out.getvalue().splitlines()


LOOP = """\
let sum_to = (n: i64) -> i64 =>
    let mut total = 0
    let mut i = 0
    while i < n
        i += 1
        total += i
    total
print(sum_to(10))
"""


# mem2reg


def test_mem2reg_removes_all_slots() -> None:
    module = compiled(LOOP, ["mem2reg"])
    function = module.functions["sum_to"]
    assert function.locals == {}
    assert "load" not in ops(function) and "store" not in ops(function)
    assert run(module) == ["55"]


def test_mem2reg_adds_parameters_where_values_join() -> None:
    function = compiled(LOOP, ["mem2reg"]).functions["sum_to"]
    [header] = [block for block in function.blocks if block.name.startswith("while")]
    # i and total change in the loop; n doesn't, so its trivial parameter is removed
    assert len(header.params) == 2


def test_mem2reg_keeps_values_in_straight_line_code() -> None:
    function = compiled(
        "let f = (a: i64) -> i64 =>\n    let b = a + 1\n    let c = b * 2\n    c", ["mem2reg"]
    ).functions["f"]
    assert ops(function) == ["const", "add", "const", "mul"]


def test_mem2reg_handles_branches_and_matches() -> None:
    source = """\
let f = (n: i64) -> str =>
    let mut label = "none"
    match n
        case 0 => label = "zero"
        case k if k < 0 => label = "negative"
        case _ => label = "positive"
    label
print(f(0))
print(f(-3))
print(f(4))
"""
    module = compiled(source, ["mem2reg"])
    assert module.functions["f"].locals == {}
    assert run(module) == ["zero", "negative", "positive"]


# Constant folding


def test_fold_evaluates_constant_expressions() -> None:
    function = compiled("print(2 * 3 + 1)", ["fold"]).functions["top"]
    text = format_function(function)
    assert "const 7" in text
    assert "mul" not in text and "add" not in text


def test_fold_builds_constant_strings() -> None:
    text = format_function(compiled('print("answer: {{ 6 * 7 }}, ok: {{ 1 < 2 }}")', ["fold"]).functions["top"])
    assert 'const "answer: 42, ok: true"' in text


@pytest.mark.parametrize("expr", ["1 // 0", "9223372036854775807 + 1", "1 << 64", "2 ** -1"])
def test_fold_leaves_failures_for_run_time(expr: str) -> None:
    module = compiled(f'print("before")\nprint({expr})', ["fold", "dce"])
    out = io.StringIO()
    with pytest.raises(EllaError):
        IRInterpreter(module, out).run()
    assert out.getvalue() == "before\n"  # it still fails, and only when it gets there


def test_fold_propagates_through_block_parameters() -> None:
    # Both branches give 5, so the joined value is the constant 5 too.
    function = compiled("let f = (c: bool) -> i64 => (if c then 5 else 5) + 1", ["mem2reg", "fold"]).functions["f"]
    assert "const 6" in format_function(function)


def test_fold_keeps_bool_and_int_apart() -> None:
    # true and 1 are different constants, so the join isn't folded to either.
    source = 'let f = (c: bool) -> str => if c then "{{ 1 }}" else "{{ true }}"\nprint(f(true))\nprint(f(false))'
    assert run(compiled(source)) == ["1", "true"]


# Simplify


def test_simplify_folds_constant_branches() -> None:
    module = compiled('if 1 < 2 then print("yes") else print("no")', ["fold", "simplify"])
    function = module.functions["top"]
    assert len(function.blocks) == 1
    assert '"no"' not in format_function(function)
    assert run(module) == ["yes"]


def test_simplify_merges_straight_line_blocks() -> None:
    function = compiled("let f = (a: i64) -> i64 =>\n    a + 1", ["simplify"]).functions["f"]
    assert len(function.blocks) == 1


# Dead-code elimination


def test_dce_removes_unused_pure_values() -> None:
    function = compiled(
        "let f = () -> i64 =>\n    let unused = (1, [2.5 * 2.0])\n    3", ["mem2reg", "dce"]
    ).functions["f"]
    assert ops(function) == ["const"]


def test_dce_keeps_instructions_that_can_fail() -> None:
    source = "let f = (xs: [i64], n: i64) -> () =>\n    let a = xs[5]\n    let b = n + 1\n    ()"
    function = compiled(source, ["mem2reg", "dce"]).functions["f"]
    assert "index" in ops(function)  # might be out of range
    assert "add" in ops(function)  # might overflow


def test_dce_keeps_arithmetic_on_arrays() -> None:
    # Even f64 arrays: their shapes may differ, which is an error.
    source = (
        "let f = (a: Array[f64], b: Array[f64]) -> () =>\n    let c = a / b\n    let d = -a\n    let e = a[0]\n    ()"
    )
    function = compiled(source, ["mem2reg", "dce"]).functions["f"]
    assert {"div", "neg", "array_offset"} <= set(ops(function))


def fused_steps(source: str) -> list[str]:
    """The steps of each fused instruction of f, after the whole pipeline."""
    function = compiled(source).functions["f"]
    return [" ".join(map(str, i.steps)) for b in function.blocks for i in b.instrs if i.op == "fused"]


def test_fuse_makes_one_loop_of_arithmetic_on_arrays() -> None:
    source = "let f = (a: Array[f64], b: Array[f64]) -> Array[f64] => -(a * 2.0 + b) / a"
    assert fused_steps(source) == ["mul(0, 1) add(s0, 2) neg(s1) div(s2, 0)"]


def test_fuse_keeps_arrays_used_twice_and_i64_multiplication() -> None:
    twice = "let f = (a: Array[f64]) -> Array[f64] =>\n    let t = a + 1.0\n    t * t"
    assert fused_steps(twice) == []
    ints = "let f = (a: Array[i64]) -> Array[i64] => (a + 1) * 2 - a"
    assert fused_steps(ints) == []  # neither step next to the `*` can join it


def test_fuse_doesnt_move_operations_past_others() -> None:
    # The total between them reads b, which must happen after the first operation (its error comes first).
    source = (
        "let f = (a: Array[f64], b: Array[f64]) -> Array[f64] =>\n    let t = a + b\n    let n = total(b)\n    t * n"
    )
    assert fused_steps(source) == []


def test_fuse_keeps_to_the_registers_there_are() -> None:
    chain = " + ".join(["a"] * 9)  # 8 additions: 6 in one loop, then 2
    assert fused_steps(f"let f = (a: Array[f64]) -> Array[f64] => {chain}") == [
        "add(0, 0) add(s0, 0)",
        "add(0, 1) add(s0, 1) add(s1, 1) add(s2, 1) add(s3, 1) add(s4, 1)",
    ]


def test_dce_keeps_effects() -> None:
    function = compiled('print("hi")', ["dce"]).functions["top"]
    assert "print" in ops(function)


# The pipeline


def test_full_pipeline_on_a_loop() -> None:
    module = compiled(LOOP)
    function = module.functions["sum_to"]
    assert run(module) == ["55"]
    assert (
        format_function(function)
        == """\
function sum_to(%2: i64) -> i64
  entry1(%2: i64):
    %5: i64 = const 0
    %6: i64 = const 0
    jump while7(%6, %5)
  while7(%22: i64, %24: i64):
    %12: bool = lt %22, %2
    branch %12, body8, exit9
  body8:
    %13: i64 = const 1
    %15: i64 = add %22, %13
    %18: i64 = add %24, %15
    jump while7(%15, %18)
  exit9:
    return %24"""
    )


def test_every_pass_can_run_alone() -> None:
    for name in PASSES:
        assert run(compiled(LOOP, [name])) == ["55"]


def test_passes_report_whether_they_changed_anything() -> None:
    module = compiled(LOOP)
    assert not any(run_pass(module, name) for name in PASSES)  # already fully optimized


def test_broken_pass_is_caught_by_the_verifier() -> None:
    def break_everything(function: Function, module: Module) -> bool:
        function.blocks[0].terminator = None
        return True

    PASSES["broken"] = break_everything
    try:
        with pytest.raises(IRError, match="after the broken pass"):
            compiled("print(1)", ["broken"])
    finally:
        del PASSES["broken"]


# Moves


def test_moves_turns_a_copy_into_a_move() -> None:
    source = """\
let f = () -> [i64] =>
    let a = [1, 2]
    let mut b = a
    b[0] = 5
    b
print(f())
"""
    before = compiled(source, ["mem2reg"]).functions["f"]
    assert "copy" in ops(before)
    module = compiled(source, ["mem2reg", "moves"])
    function = module.functions["f"]
    assert "copy" not in ops(function)
    # The list is now made where the copy was going: the caller's arena.
    assert "= list %4, %5 @result" in format_function(function)
    assert run(module) == ["[5, 2]"]


def test_moves_keeps_a_copy_made_in_a_loop() -> None:
    source = """\
let f = () -> () =>
    let base = [0, 0]
    for i in 0..3
        let mut row = base
        row[0] += i
        print(row)
f()
"""
    module = compiled(source)
    assert "copy" in ops(module.functions["f"])
    assert run(module) == ["[0, 0]", "[1, 0]", "[2, 0]"]


def test_moves_keeps_a_copy_of_shared_parts() -> None:
    # The list holds `row`, which is also a variable, so copying it copies `row` too.
    source = """\
let f = (row: [i64]) -> [[i64]] =>
    let rows = [row]
    let mut copied = rows
    copied[0][0] = 9
    copied
print(f([1]))
"""
    module = compiled(source)
    assert "copy" in ops(module.functions["f"])
    assert run(module) == ["[[9]]"]


def test_moves_counts_constants_as_new() -> None:
    # The struct's string is a constant, which is static, so the struct can
    # be made where its copy goes and keep sharing the constant.
    source = """\
struct P
    n: i64
    s: str
let f = () -> P =>
    let a = P(1, "x")
    let mut b = a
    b.n = 5
    b
print(f())
"""
    assert "copy" in ops(compiled(source, ["mem2reg"]).functions["f"])
    module = compiled(source, ["mem2reg", "moves"])
    assert "copy" not in ops(module.functions["f"])
    assert run(module) == ['P(5, "x")']


def test_dce_drops_levels_nothing_allocates_in() -> None:
    # The loop made a string until folding turned it into a constant.
    function = compiled('let f = () -> () =>\n    for i in 0..3\n        print("a" + "b")\nf()').functions["f"]
    assert function.levels == 1
    assert "reset" not in ops(function)


def test_dce_keeps_making_values_with_finalizers() -> None:
    source = """\
nocopy struct Res
    name: str
    finalize = (r: Res) -> () => print("free {{r.name}}")
let f = () -> () =>
    let unused = Res("unused")
    ()
f()
"""
    module = compiled(source)
    assert "variant" in ops(module.functions["f"])
    assert run(module) == ["free unused"]


# Direct calls

FIB = """\
let fib = (n: i64) -> i64 => if n < 2 then n else fib(n - 1) + fib(n - 2)
print(fib(10))
"""


def test_direct_calls_name_known_functions() -> None:
    module = compiled(FIB, ["mem2reg", "direct", "dce"])
    function = module.functions["fib"]
    assert ops(function).count("call_direct") == 2
    assert "call" not in ops(function) and "closure" not in ops(function)
    assert "call_direct fib(" in format_function(module.functions["top"])
    assert run(module) == ["55"]


def test_direct_calls_leave_globals_that_can_change() -> None:
    source = """\
let mut f = (n: i64) -> i64 => n + 1
let g = () -> i64 => f(1)
f = (n: i64) -> i64 => n + 2
print(g())
"""
    module = compiled(source, ["mem2reg", "direct"])
    assert "global_get" in ops(module.functions["g"]) and "call" in ops(module.functions["g"])
    assert run(module) == ["3"]


def test_closures_that_capture_nothing_are_static() -> None:
    module = compiled(FIB, [])
    [closure] = [i for i in module.functions["top"].blocks[0].instrs if i.op == "closure" and i.name == "fib"]
    assert closure.region == ""


# Inlining

VEC = """\
struct V
    x: i64
    y: i64
    add = (a: V, b: V) -> V => V(a.x + b.x, a.y + b.y)
let main = () -> () =>
    let mut p = V(0, 0)
    for i in 0..3
        p = p + V(i, 1)
    print(p)
"""


def test_inlining_puts_small_functions_where_they_are_called() -> None:
    module = compiled(VEC)
    function = module.functions["main"]
    assert "call_direct" not in ops(function)
    assert ops(function).count("enter") == ops(function).count("leave") == 1
    assert "V.add" not in module.functions  # unused now, so dropped
    assert run(module) == ["V(3, 3)"]


def test_inlining_leaves_recursive_functions() -> None:
    module = compiled(FIB)
    assert ops(module.functions["fib"]).count("call_direct") == 2
    assert run(module) == ["55"]


def test_inlining_passes_captured_values_and_mut_arguments() -> None:
    source = """\
let bump = (mut xs: [i64], k: i64) -> i64 =>
    xs += [k]
    len(xs)
let main = () -> () =>
    let base = 10
    let add = (n: i64) -> i64 => n + base
    let mut xs = [1]
    let n = bump(mut xs, add(5))
    print(xs, n)
"""
    module = compiled(source)
    assert not {"call", "call_direct"} & set(ops(module.functions["main"]))
    assert run(module) == ["[1, 15] 2"]


def test_inlined_levels_go_above_the_callers() -> None:
    # `words` makes a list in its own level 0, which becomes main's level 1:
    # freed where the inlined call returns, after the list's length is taken.
    source = """\
let words = (s: str) -> i64 => len(split(s, " "))
let main = () -> () =>
    print(words("a b c"))
"""
    module = compiled(source)
    function = module.functions["main"]
    assert function.levels == 2
    assert "@1" in format_function(function) and "reset 1" in format_function(function)
    assert run(module) == ["3"]


def test_inlined_calls_count_toward_the_depth_limit() -> None:
    source = """\
let less = (n: i64) -> i64 => n - 1
let down = (n: i64) -> i64 => if n == 0 then 0 else down(less(n)) + 1
print(down(100))
"""
    for module in (compile_program(source), compiled(source)):
        with pytest.raises(EllaError, match=r"^2:62: stack overflow: more than 50 nested calls"):
            IRInterpreter(module, io.StringIO(), max_depth=50).run()


def test_inlining_keeps_functions_code_built_at_run_time_may_call() -> None:
    source = """\
let twice = (n: i64) -> i64 => n * 2
let main = () -> () =>
    print(twice(2))
    print(run(quote twice(4)))
"""
    module = compiled(source)
    assert "twice" in module.functions
    assert run(module) == ["4", "8"]
