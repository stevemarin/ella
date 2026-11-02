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
