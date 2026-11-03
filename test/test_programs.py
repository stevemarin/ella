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


def run_tree(source: str, out: TextIO, err: TextIO, stdin: bytes, args: list[str]) -> int:
    """Type check, then run with the tree-walking interpreter."""
    program = parse(source)
    program, checker = prepare(program, out)
    return Interpreter(out, checker, host=Host(io.BytesIO(stdin), args, err)).run(program)


def run_ir(source: str, out: TextIO, err: TextIO, stdin: bytes, args: list[str]) -> int:
    """Compile to IR and run it."""
    return IRInterpreter(compile_program(source, out), out, Host(io.BytesIO(stdin), args, err)).run()


def run_optimized(source: str, out: TextIO, err: TextIO, stdin: bytes, args: list[str]) -> int:
    """Compile to IR, optimize it, and run it."""
    return IRInterpreter(optimize(compile_program(source, out)), out, Host(io.BytesIO(stdin), args, err)).run()


def run_native_unoptimized(source: str, out: TextIO, err: TextIO, stdin: bytes, args: list[str]) -> int:
    return run_native(source, out, False, stdin, args, err)


def run_native_optimized(source: str, out: TextIO, err: TextIO, stdin: bytes, args: list[str]) -> int:
    return run_native(source, out, True, stdin, args, err)


RUNNERS: dict[str, Callable[[str, TextIO, TextIO, bytes, list[str]], int]] = {
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
    out, err = io.StringIO(), io.StringIO()
    stdin = path.with_suffix(".in").read_bytes() if path.with_suffix(".in").exists() else b""
    args = path.with_suffix(".args").read_text().splitlines() if path.with_suffix(".args").exists() else []
    expected_error = path.with_suffix(".err")
    if expected_error.exists():
        with pytest.raises(EllaError) as info:
            RUNNERS[runner](path.read_text(), out, err, stdin, args)
        assert str(info.value) == expected_error.read_text().strip()
    else:
        status = RUNNERS[runner](path.read_text(), out, err, stdin, args)
        expected_status = path.with_suffix(".status")
        assert status == (int(expected_status.read_text()) if expected_status.exists() else 0)
    assert out.getvalue() == path.with_suffix(".out").read_text()
    expected_stderr = path.with_suffix(".stderr")
    assert err.getvalue() == (expected_stderr.read_text() if expected_stderr.exists() else "")
