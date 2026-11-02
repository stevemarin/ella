"""Run every program in test/programs and compare with its expected output (see test/programs/README.md).

Each program is run three ways: by the tree-walking interpreter, through the
IR, and through the optimized IR. All must give the expected output and
error. This is what checks that lowering and optimization keep the program's
meaning.
"""

import io
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import pytest

from ella.checker import check
from ella.interpreter import Interpreter
from ella.ir_interpreter import IRInterpreter, run_ir
from ella.lexer import EllaError
from ella.lower import compile_program
from ella.parser import parse
from ella.passes import optimize

PROGRAMS = Path(__file__).parent / "programs"


def run_tree(source: str, out: TextIO) -> None:
    """Type check, then run with the tree-walking interpreter."""
    program = parse(source)
    check(program)
    Interpreter(out).run(program)


def run_optimized(source: str, out: TextIO) -> None:
    """Compile to IR, optimize it, and run it."""
    IRInterpreter(optimize(compile_program(source)), out).run()


RUNNERS: dict[str, Callable[[str, TextIO], None]] = {"tree": run_tree, "ir": run_ir, "optimized": run_optimized}


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("path", sorted(PROGRAMS.glob("*.ella")), ids=lambda path: path.stem)
def test_program(path: Path, runner: str) -> None:
    out = io.StringIO()
    expected_error = path.with_suffix(".err")
    if expected_error.exists():
        with pytest.raises(EllaError) as info:
            RUNNERS[runner](path.read_text(), out)
        assert str(info.value) == expected_error.read_text().strip()
    else:
        RUNNERS[runner](path.read_text(), out)
    assert out.getvalue() == path.with_suffix(".out").read_text()
