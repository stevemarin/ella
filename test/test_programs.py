"""Run every program in test/programs and compare with its expected output (see test/programs/README.md)."""

import io
from pathlib import Path

import pytest

from ella.interpreter import run
from ella.lexer import EllaError

PROGRAMS = Path(__file__).parent / "programs"


@pytest.mark.parametrize("path", sorted(PROGRAMS.glob("*.ella")), ids=lambda path: path.stem)
def test_program(path: Path) -> None:
    out = io.StringIO()
    expected_error = path.with_suffix(".err")
    if expected_error.exists():
        with pytest.raises(EllaError) as info:
            run(path.read_text(), out)
        assert str(info.value) == expected_error.read_text().strip()
    else:
        run(path.read_text(), out)
    assert out.getvalue() == path.with_suffix(".out").read_text()
