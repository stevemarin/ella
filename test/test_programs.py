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
from ella.ir_interpreter import IRInterpreter, run_ir
from ella.lexer import EllaError
from ella.lower import compile_program
from ella.native import run_native
from ella.parser import parse
from ella.passes import optimize
from ella.staging import prepare

PROGRAMS = Path(__file__).parent / "programs"


def run_tree(source: str, out: TextIO) -> None:
    """Type check, then run with the tree-walking interpreter."""
    program = parse(source)
    program, checker = prepare(program, out)
    Interpreter(out, checker).run(program)


def run_optimized(source: str, out: TextIO) -> None:
    """Compile to IR, optimize it, and run it."""
    IRInterpreter(optimize(compile_program(source, out)), out).run()


def run_native_unoptimized(source: str, out: TextIO) -> None:
    run_native(source, out, optimized=False)


RUNNERS: dict[str, Callable[[str, TextIO], None]] = {
    "tree": run_tree,
    "ir": run_ir,
    "optimized": run_optimized,
    "native": run_native_unoptimized,
    "native-optimized": run_native,
}


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("path", sorted(PROGRAMS.glob("*.ella")), ids=lambda path: path.stem)
def test_program(path: Path, runner: str) -> None:
    if runner.startswith("native") and shutil.which("gcc") is None:
        pytest.skip("native code needs gcc")
    out = io.StringIO()
    expected_error = path.with_suffix(".err")
    if expected_error.exists():
        with pytest.raises(EllaError) as info:
            RUNNERS[runner](path.read_text(), out)
        assert str(info.value) == expected_error.read_text().strip()
    else:
        RUNNERS[runner](path.read_text(), out)
    assert out.getvalue() == path.with_suffix(".out").read_text()
