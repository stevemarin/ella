"""Tests of native code: each program must behave exactly as in the IR interpreter.

test_programs.py and test_ir.py run their programs natively too; these
tests aim at the runtime's trickier corners. Building a program takes gcc a
moment, so related checks share one program where they can.
"""

import io
import math
import os
import random
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from ella import main
from ella.codegen import generate
from ella.ir_interpreter import run_ir
from ella.lexer import EllaError
from ella.lower import compile_program
from ella.native import compile_native, run_native

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="native code needs gcc")


def outcome(run: object, source: str) -> tuple[str, str | None]:
    assert callable(run)
    out = io.StringIO()
    try:
        run(source, out)
    except EllaError as error:
        return out.getvalue(), str(error)
    return out.getvalue(), None


def assert_same_as_interpreter(source: str) -> None:
    assert outcome(run_native, source) == outcome(run_ir, source)


FLOAT = re.compile(r"-?(?:\d+\.\d*(?:e[+-]?\d+)?|\d+e[+-]?\d+|inf|nan)")


def assert_close_to_interpreter(source: str, tolerance: float = 1e-9) -> None:
    """Native f64 results of arrays may differ in their last bits (fused multiply-adds, other orders of adding:
    docs/grammar.md §9). Everything else must be the same: the text around the floats, i64s, errors, and
    infinities and NaNs; finite floats must agree to `tolerance` of the largest in the output (f32s are only good to
    about 1e-7)."""
    (native, native_error), (expected, error) = outcome(run_native, source), outcome(run_ir, source)
    assert native_error == error
    assert FLOAT.split(native) == FLOAT.split(expected), native
    found, wanted = [float(x) for x in FLOAT.findall(native)], [float(x) for x in FLOAT.findall(expected)]
    scale = max((abs(x) for x in wanted if math.isfinite(x)), default=1.0)
    for x, y in zip(found, wanted, strict=True):
        if math.isfinite(y):
            assert math.isclose(x, y, rel_tol=tolerance, abs_tol=tolerance * scale), (x, y, native)
        else:
            assert repr(x) == repr(y), (x, y, native)


def native_lines(source: str) -> list[str]:
    out = io.StringIO()
    run_native(source, out)
    return out.getvalue().splitlines()


# Floats


def ella_float(x: float) -> str:
    """An Ella float literal for x (which needs digits on both sides of the point)."""
    text = f"{abs(x):.17e}"
    return f"-{text}" if x < 0 or str(x).startswith("-") else text


def test_floats_print_like_python() -> None:
    rng = random.Random(1234)
    values = [0.1, 0.5, 1.0, 100.0, 1e-5, 1e-4, 0.0001234, 1e15, 1e16, 1e22, 1e-7, 5e-324, 1.7976931348623157e308]
    values += [2.0**0.5, 1 / 3, 123456789012345680.0, 9007199254740993.0, 0.30000000000000004, -0.0, -2.5]
    while len(values) < 400:
        [x] = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))
        if math.isfinite(x):
            values.append(x)
    source = "\n".join(f"print({ella_float(x)})" for x in values)
    assert native_lines(source) == [repr(x) for x in values]


def test_float_arithmetic_and_specials() -> None:
    assert_same_as_interpreter(
        """\
print(0.1 + 0.2)
print(1.0 / 0.0)
print(-1.0 / 0.0)
print(0.0 / 0.0)
print(0.0 / 0.0 == 0.0 / 0.0)
print(0.0 / 0.0 != 0.0 / 0.0)
print(0.0 / 0.0 < 1.0)
print(-0.0 == 0.0)
print(-(0.0))
print(7 / 2)
print(1 / 0)
print(2.0 ** 0.5)
print((-8.0) ** 0.5)
print(0.0 ** -1.0)
print((-0.0) ** -1.0)
print(10.0 ** 400.0)
print((-10.0) ** 401.0)
print(1.5 < 2.5 <= 2.5 > 0.0 >= 0.0)
"""
    )


# Arrays: AVX2 code


def random_floats(rng: random.Random, n: int) -> list[str]:
    """Ella literals of n floats, some of them special: zeros of both signs, infinities and NaN."""
    specials = ["0.0", "(-0.0)", "(1.0 / 0.0)", "(-1.0 / 0.0)", "(0.0 / 0.0)", "1.0e-310"]
    return [rng.choice(specials) if rng.random() < 0.2 else ella_float(rng.uniform(-1e3, 1e3)) for _ in range(n)]


def test_array_arithmetic_on_floats_matches_the_interpreter() -> None:
    # Each length up to 13 puts items in both the vector loop and the rest; the sums of
    # total and dot must come out the same, bit for bit, as the interpreter's four sums.
    rng = random.Random(7)
    lines = []
    for n in range(14):
        a, b = ", ".join(random_floats(rng, n)), ", ".join(random_floats(rng, n))
        lines += [
            f"let a{n} = array([{a}])",
            f"let b{n} = array([{b}])",
            f"print(a{n} + b{n}, a{n} - b{n}, a{n} * b{n}, a{n} / b{n}, -a{n}, a{n} * 0.5, 3.0 - a{n}, 1.0 / b{n})",
            f"print(total(a{n}), dot(a{n}, b{n}), total(a{n} * 1.0e300), dot(a{n} * 1.0e-200, b{n}))",
        ]
    big = ", ".join(ella_float(rng.uniform(-1, 1)) for _ in range(5 * 7))
    lines.append(f"let m = reshape(array([{big}]), [5, 7])")
    lines.append("print(matmul(m, transpose(m)), matmul(transpose(m), m))")
    assert_close_to_interpreter("\n".join(lines))


def test_array_arithmetic_on_integers_matches_the_interpreter() -> None:
    rng = random.Random(8)
    edge = [2**62, -(2**62), 2**63 - 1, -(2**63) + 1, 3037000499, -3037000500]
    for n in (3, 4, 7, 9):
        for op in ("+", "-", "*"):
            items = [rng.choice(edge) if rng.random() < 0.3 else rng.randrange(-1000, 1000) for _ in range(n)]
            others = [rng.choice(edge) if rng.random() < 0.3 else rng.randrange(-1000, 1000) for _ in range(n)]
            for expr in (f"a {op} b", f"a {op} 7", f"-3 {op} a", "-a", "total(a)", "dot(a, b)"):
                source = f"let a = array({items})\nlet b = array({others})\nprint({expr})"
                assert_same_as_interpreter(source)


def random_chain(rng: random.Random, floats: bool, depth: int = 0) -> str:
    """An expression of arrays a, b, c and numbers, which the fuse pass makes one loop of."""
    leaves = ["a", "b", "c", ("2.5" if floats else "3"), ("(-0.5)" if floats else "(-7)")]
    if depth >= 3 or rng.random() < 0.25:
        return rng.choice(leaves[:3])
    ops = ["+", "-", "*", "/"] if floats else ["+", "-"]
    if rng.random() < 0.15:
        return f"-({random_chain(rng, floats, depth + 1)})"
    left, right = random_chain(rng, floats, depth + 1), random_chain(rng, floats, depth + 1)
    if rng.random() < 0.3:
        right = rng.choice(leaves[3:])
    return f"({left} {rng.choice(ops)} {right})"


def test_fused_array_arithmetic_matches_the_interpreter() -> None:
    # Native code is optimized, so fused (ella.passes.fuse); the IR interpreter's isn't. Some i64 chains
    # overflow, which must be reported as unfused code reports it.
    rng = random.Random(9)
    for floats in (True, False):
        for n in (1, 4, 6, 11):
            if floats:
                values = [", ".join(random_floats(rng, n)) for _ in range(3)]
            else:
                big = [2**62, -(2**62), 2**63 - 1, -(2**63)]
                values = [
                    ", ".join(str(rng.choice(big) if rng.random() < 0.1 else rng.randrange(-99, 99)) for _ in range(n))
                    for _ in range(3)
                ]
            for _ in range(4):
                expr = random_chain(rng, floats)
                source = (
                    f"let a = array([{values[0]}])\nlet b = array([{values[1]}])\nlet c = array([{values[2]}])\n"
                    f"print(a + 0{'.0' if floats else ''} - a, {expr}, total({expr}), dot({expr}, a))"
                )
                (assert_close_to_interpreter if floats else assert_same_as_interpreter)(source)


def test_f32_arrays_match_the_interpreter() -> None:
    # Eight f32s to an AVX2 vector: lengths around 8 and 16 put items in the loops and in the masked rest.
    rng = random.Random(10)
    for n in (1, 7, 8, 9, 23):
        values = [", ".join(f"to_f32({x})" for x in random_floats(rng, n)) for _ in range(3)]
        for _ in range(3):
            expr = random_chain(rng, floats=True).replace("2.5", "to_f32(2.5)").replace("(-0.5)", "to_f32(-0.5)")
            source = (
                f"let a = array([{values[0]}])\nlet b = array([{values[1]}])\nlet c = array([{values[2]}])\n"
                f"print({expr}, total({expr}), dot({expr}, a), total(a), dot(b, c), a / b, -c, matmul(reshape(a, [n_rows(a), 1]), reshape(b, [1, len(b)])))"
            ).replace("n_rows(a)", str(n))
            assert_close_to_interpreter(source, tolerance=1e-5)


def test_avx2_code_is_only_in_programs_with_arrays() -> None:
    plain = generate(compile_program("print([1.5] + [2.5])"))
    assert "ella_uses_avx2:\n    .quad 0" in plain and "ymm" not in plain
    text = generate(compile_program("let a = fill([8], 1.5)\nprint(total(a * a + a))"))
    assert "ella_uses_avx2:\n    .quad 1" in text
    assert "vmulpd ymm0, ymm0, ymmword ptr [rdi + 8 * r9]" in text  # four items at once
    assert "vaddpd ymm0, ymm0, ymm1" in text  # the four sums of total


# Integers


def test_integer_division_signs() -> None:
    lines = []
    for a in (7, -7, 0, 9223372036854775807, -9223372036854775807):
        for b in (2, -2, 1, -1, 3):
            lines.append(f"print(({a}) // ({b}))\nprint(({a}) % ({b}))")
    assert_same_as_interpreter("\n".join(lines))


def test_integer_operations() -> None:
    assert_same_as_interpreter(
        """\
print(-9223372036854775807 - 1)
print(9223372036854775807)
print(2 ** 62)
print((-2) ** 63)
print((-1) ** 1000001)
print(0 ** 0)
print(3 ** 39)
print(1 << 62)
print(-1 << 63)
print(-16 >> 2)
print(5 >> 63)
print(6 & 3)
print(6 | 3)
print(6 ^ 3)
print(~5)
print(-9223372036854775808 % -1)
"""
    )


@pytest.mark.parametrize(
    "expr",
    [
        "9223372036854775807 + 1",
        "-9223372036854775807 - 2",
        "4611686018427387904 * 2",
        "-(-9223372036854775807 - 1)",
        "(-9223372036854775807 - 1) // -1",
        "2 ** 63",
        "3 ** 100000000000",
        "2 ** -1",
        "1 << 63",
        "1 << 64",
        "1 >> -1",
        "1 // 0",
        "1 % 0",
    ],
)
def test_integer_errors(expr: str) -> None:
    assert_same_as_interpreter(f'print("before")\nlet n = {expr}\nprint(n)')


@pytest.mark.parametrize(
    "expr",
    ['ord("ab")', 'ord("")', 'ord("\\n\\"x")', "chr(-1)", "chr(1114112)", "chr(55296)", "chr(57343)", "chr(57344)"],
)
def test_code_point_errors(expr: str) -> None:
    assert_same_as_interpreter(f'print("before")\nlet n = {expr}\nprint([n])')


def test_cli_native_exit_status_and_standard_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "program.ella"
    path.write_text('eprint("to stderr", 1)\nprint("to stdout")\nlet main = () -> i64 => 7\n')
    assert main(["--native", str(path)]) == 7
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("to stdout\n", "to stderr 1\n")
    path.write_text('eprint("first")\nassert false\n')
    assert main(["--native", str(path)]) == 1
    assert capsys.readouterr().err == f"first\n{path}:2:8: assertion failed: false\n"


# Strings, lists and other values


def test_text_of_values() -> None:
    assert_same_as_interpreter(
        r"""
print(["plain", "quote\" back\\slash", "new\nline\ttab\r\x01\x7f", "é😀"])
print(("one",))
print((1, 2.5, true, ()))
print([Some([1]), None])
print([Ok(1), Err("bad")])
print(0..3)
print(0..=9223372036854775807)
print([[], [[1]]])
print("{{ 1 }} and {{ [1.5] }} and {{ "nested" }}")
print("héllo"[1] + "😀")
print(len("héllo😀"))
print("b" > "a" and "é" > "z" and "ab" < "abc")
print("ell" in "hello" and "" in "x" and not ("z" in "hello"))
print([1, 2] + [3] == [1, 2, 3])
print((0..0) == (5..5) and (1..3) == (1..3) and (1..3) != (1..4))
print(3 in 0..=3 and 4 not in 0..=3)
print(print)
"""
    )


def test_runtime_errors_with_values() -> None:
    assert_same_as_interpreter('match ("x", 1.5)\n    case ("y", _) => print(0)')
    assert_same_as_interpreter("for (a, 1) in [(1, 1), (2, 2)]\n    print(a)")
    assert_same_as_interpreter("let xs = [1, 2]\nprint(xs[-1])")
    assert_same_as_interpreter('print("abc"[3])')
    assert_same_as_interpreter("let mut xs = [1]\nxs[1] = 5")
    assert_same_as_interpreter("let f = () -> i64 => later\nprint(f())\nlet later = 1")
    assert_same_as_interpreter("assert 1 + 1 == 3")


def test_deep_recursion_and_stack_overflow() -> None:
    assert_same_as_interpreter(
        "let count = (n: i64) -> i64 => if n == 0 then 0 else 1 + count(n - 1)\nprint(count(998))"
    )
    assert_same_as_interpreter("let f = (n: i64) -> i64 => f(n + 1)\nf(0)")


def test_many_arguments_and_captures() -> None:
    assert_same_as_interpreter(
        """\
let f = (a: i64, b: f64, c: str, d: bool, e: i64, g: i64, h: i64, i: i64) -> str =>
    "{{a}} {{b}} {{c}} {{d}} {{e + g + h + i}}"
print(f(1, 2.5, "c", true, 5, 6, 7, 8))
let make = (x: f64, label: str) -> (i64) -> str =>
    (n: i64) -> str => "{{label}}: {{x * 2.0}} {{n}}"
print(make(1.25, "go")(3))
"""
    )


# The assembly and the command line


def test_assembly_is_readable() -> None:
    text = generate(compile_program("let add = (a: i64, b: i64) -> i64 => a + b\nprint(add(1, 2))"))
    assert ".intel_syntax noprefix" in text
    assert "# function add" in text
    assert "# %8: i64 = add %6, %7" in text  # each IR instruction appears as a comment
    assert "jno" in text  # the overflow check


def test_cli_native(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "program.ella"
    path.write_text('print("native {{ 6 * 7 }}")\n')
    assert main(["--native", str(path)]) == 0
    assert capsys.readouterr().out == "native 42\n"

    executable = tmp_path / "program"
    assert main(["-o", str(executable), str(path)]) == 0
    assert subprocess.run([str(executable)], capture_output=True, text=True, check=True).stdout == "native 42\n"

    assert main(["--asm", str(path)]) == 0
    assert "ella_main:" in capsys.readouterr().out


def test_cli_native_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "program.ella"
    path.write_text('print("before")\nassert false\n')
    assert main(["--native", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "before\n"
    assert captured.err == f"{path}:2:8: assertion failed: false\n"


# Arenas


def arena_peak(source: str, tmp_path: Path) -> tuple[str, int]:
    """Run a program natively, returning what it prints and the most arena memory it held at once."""
    executable = tmp_path / "program"
    compile_native(source, executable)
    env = os.environ | {"ELLA_ARENA_STATS": "1", "ELLA_POISON": "1"}
    result = subprocess.run([str(executable)], capture_output=True, text=True, check=True, env=env)
    match = re.fullmatch(r"arena peak: (\d+) bytes\n", result.stderr)
    assert match is not None, result.stderr
    return result.stdout, int(match[1])


def test_loops_free_their_temporaries(tmp_path: Path) -> None:
    # Each iteration makes a list and a string, about 300 bytes; 100,000 of
    # them would be 30 MB, but each iteration's are freed when it ends.
    source = """\
let work = (n: i64) -> i64 =>
    let mut total = 0
    for i in 0..n
        let row = [i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i]
        total += len("row {{row}}")
    total
print(work(100000))
"""
    out, peak = arena_peak(source, tmp_path)
    assert out == "11422240\n"
    assert peak < 10_000


def test_calls_free_their_arenas(tmp_path: Path) -> None:
    source = """\
let temp = (i: i64) -> i64 => len([i, i, i, i] + [i, i, i, i])
let mut total = 0
for i in 0..50000
    total += temp(i)
print(total)
"""
    out, peak = arena_peak(source, tmp_path)
    assert out == "400000\n"
    assert peak < 1_000


def test_appending_grows_lists_in_place(tmp_path: Path) -> None:
    # A list that is the last object in its arena grows where it is, so
    # appending 100,000 items needs about 800 KB, not the sum of every
    # intermediate list.
    source = """\
let mut xs = []
for i in 0..100000
    xs += [i]
print(len(xs))
print(xs[99999])
"""
    out, peak = arena_peak(source, tmp_path)
    assert out == "100000\n99999\n"
    assert peak < 1_100_000


def test_appending_moves_lists_that_cant_grow() -> None:
    # Two lists growing in turn in the same arena: each move makes room for
    # twice the items, so this still takes O(n) time.
    assert native_lines("""\
let mut a = []
let mut b = []
for i in 0..1000
    a += [i]
    b += [-i, i]
print(len(a))
print(len(b))
print(a[999] + b[1999])
""") == ["1000", "2000", "1998"]


def test_copies_are_deep() -> None:
    # Closures, variants, tuples and strings made inside a loop are copied out
    # to the variable outside it, with everything they hold.
    assert_same_as_interpreter("""\
let f = () -> () -> str =>
    let mut keep = () -> str => ""
    let mut found = (Some(["none"]), 0..1)
    for i in 0..3
        let word = "w{{i}}"
        let words = [word, word + word]
        keep = () -> str => words[1]
        found = (Some(words), i..i + 1)
    print(found)
    keep
print(f()())
""")


def test_pools_free_their_values(tmp_path: Path) -> None:
    # Each value lives in an arena of its own, freed with it.
    source = """\
struct Item
    name: str
    tags: [str]
let mut pool: Pool[Item] = pool_new()
let mut total = 0
for i in 0..20000
    let h = alloc(mut pool, Item("item {{i}}", ["a", "b", "c"]))
    pool[h].tags += ["d"]
    total += len(pool[h].tags)
    free(mut pool, h)
print(total)
print(len(pool))
"""
    out, peak = arena_peak(source, tmp_path)
    assert out == "80000\n0\n"
    assert peak < 10_000


def test_arenas_free_their_values_at_once(tmp_path: Path) -> None:
    # An arena's values all live in its slot arena, freed by `free(mut arena)`.
    source = """\
struct Item
    name: str
    tags: [str]
let mut arena: Arena = arena_new()
let mut total = 0
for round in 0..200
    for i in 0..100
        let r = alloc(mut arena, Item("item {{i}}", ["a", "b", "c"]))
        arena[r].tags += ["d"]
        total += len(arena[r].tags)
    free(mut arena)
print(total)
print(len(arena))
"""
    out, peak = arena_peak(source, tmp_path)
    assert out == "80000\n0\n"
    assert peak < 100_000


def test_finalizers_run_natively(tmp_path: Path) -> None:
    source = """\
nocopy struct Res
    n: i64
    finalize = (r: Res) -> () => print("free {{r.n}}")
for i in 0..3
    let r = Res(i)
print("done")
"""
    out, _ = arena_peak(source, tmp_path)
    assert out == "free 0\nfree 1\nfree 2\ndone\n"
