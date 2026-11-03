"""Run every program in test/programs and compare with its expected output (see test/programs/README.md).

Each program is run five ways: by the tree-walking interpreter, through the
IR, through the optimized IR, and as native code made from the plain and the
optimized IR. All must give the expected output and error. This is what
checks that lowering, optimization and code generation keep the program's
meaning.
"""

import io
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import pytest

from ella.interpreter import Interpreter
from ella.ir_interpreter import IRInterpreter
from ella.lexer import EllaError
from ella.library import Host
from ella.lower import compile_program
from ella.native import run_native
from ella.parser import parse
from ella.passes import optimize
from ella.staging import prepare

PROGRAMS = Path(__file__).parent / "programs"


def run_tree(source: str, out: TextIO, stdin: bytes, args: list[str]) -> None:
    """Type check, then run with the tree-walking interpreter."""
    program = parse(source)
    program, checker = prepare(program, out)
    Interpreter(out, checker, host=Host(io.BytesIO(stdin), args)).run(program)


def run_ir(source: str, out: TextIO, stdin: bytes, args: list[str]) -> None:
    """Compile to IR and run it."""
    IRInterpreter(compile_program(source, out), out, Host(io.BytesIO(stdin), args)).run()


def run_optimized(source: str, out: TextIO, stdin: bytes, args: list[str]) -> None:
    """Compile to IR, optimize it, and run it."""
    IRInterpreter(optimize(compile_program(source, out)), out, Host(io.BytesIO(stdin), args)).run()


def run_native_unoptimized(source: str, out: TextIO, stdin: bytes, args: list[str]) -> None:
    run_native(source, out, False, stdin, args)


def run_native_optimized(source: str, out: TextIO, stdin: bytes, args: list[str]) -> None:
    run_native(source, out, True, stdin, args)


RUNNERS: dict[str, Callable[[str, TextIO, bytes, list[str]], None]] = {
    "tree": run_tree,
    "ir": run_ir,
    "optimized": run_optimized,
    "native": run_native_unoptimized,
    "native-optimized": run_native_optimized,
}


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("path", sorted(PROGRAMS.glob("*.ella")), ids=lambda path: path.stem)
def test_program(path: Path, runner: str) -> None:
    if runner.startswith("native") and shutil.which("gcc") is None:
        pytest.skip("native code needs gcc")
    out = io.StringIO()
    stdin = path.with_suffix(".in").read_bytes() if path.with_suffix(".in").exists() else b""
    args = path.with_suffix(".args").read_text().splitlines() if path.with_suffix(".args").exists() else []
    expected_error = path.with_suffix(".err")
    if expected_error.exists():
        with pytest.raises(EllaError) as info:
            RUNNERS[runner](path.read_text(), out, stdin, args)
        assert str(info.value) == expected_error.read_text().strip()
    else:
        RUNNERS[runner](path.read_text(), out, stdin, args)
    assert out.getvalue() == path.with_suffix(".out").read_text()
