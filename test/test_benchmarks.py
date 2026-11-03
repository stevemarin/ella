"""The benchmarks in test/benchmarks, as tests: each must print what it should, within its memory bound.

Their speed is for test/bench.py to show; here only the output and the
arena peak (the most memory the arenas held at once) are checked, so that
region bloat (docs/memory-model.md §6) can't come back unnoticed.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ella.native import compile_native

BENCHMARKS = Path(__file__).parent / "benchmarks"

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="native code needs gcc")


@pytest.mark.parametrize("path", sorted(BENCHMARKS.glob("*.ella")), ids=lambda path: path.stem)
def test_benchmark(path: Path, tmp_path: Path) -> None:
    executable = tmp_path / "program"
    compile_native(path.read_text(), executable, path=path)
    env = os.environ | {"ELLA_ARENA_STATS": "1", "ELLA_POISON": "1"}
    result = subprocess.run([str(executable)], capture_output=True, text=True, check=True, env=env)
    assert result.stdout == path.with_suffix(".out").read_text()
    match = re.fullmatch(r"arena peak: (\d+) bytes\n", result.stderr)
    assert match is not None, result.stderr
    bound = int(path.with_suffix(".peak").read_text())
    assert int(match[1]) <= bound, f"{path.stem} held {int(match[1]):,} bytes, more than its bound of {bound:,}"
