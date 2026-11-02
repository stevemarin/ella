"""Tests of native code: each program must behave exactly as in the IR interpreter.

test_programs.py and test_ir.py run their programs natively too; these
tests aim at the runtime's trickier corners. Building a program takes gcc a
moment, so related checks share one program where they can.
"""

import io
import math
import random
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
from ella.native import run_native

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


# Strings, lists and other values


def test_text_of_values() -> None:
    assert_same_as_interpreter(
        r"""
print(["plain", "quote\" back\\slash", "new\nline\ttab\r\x01\x7f", "é😀"])
print(("one",))
print((1, 2.5, true, ()))
print([Some([1]), None])
print([Ok(1), Error("bad")])
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
