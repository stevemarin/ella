"""Building and running native executables.

    source --compile_program--> IR --optimize--> IR --codegen--> assembly
    assembly + runtime.c --gcc--> executable

gcc does the assembling and linking. The runtime is compiled once and the
object file kept in the temporary directory, named by a hash of its source,
so that building many programs (as the tests do) stays quick.
"""

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TextIO

from ella.codegen import generate
from ella.ir import Module
from ella.lexer import EllaError, Span
from ella.lower import compile_program
from ella.passes import optimize

RUNTIME = Path(__file__).with_name("runtime.c")
CFLAGS = ["-std=gnu17", "-O2", "-Wall", "-Wextra", "-Wpedantic"]


def runtime_object() -> Path:
    """The compiled runtime, building it if this version hasn't been built yet."""
    source = RUNTIME.read_bytes()
    digest = hashlib.sha256(source + " ".join(CFLAGS).encode()).hexdigest()[:16]
    obj = Path(tempfile.gettempdir()) / f"ella-runtime-{digest}.o"
    if not obj.exists():
        partial = obj.with_suffix(f".{digest}.tmp")  # renamed into place, so a half-written file is never used
        subprocess.run(["gcc", *CFLAGS, "-c", str(RUNTIME), "-o", str(partial)], check=True)
        partial.replace(obj)
    return obj


def build(module: Module, executable: Path) -> None:
    """Generate assembly for a module and link it with the runtime into an executable."""
    with tempfile.TemporaryDirectory() as directory:
        assembly = Path(directory) / "program.s"
        assembly.write_text(generate(module))
        subprocess.run(["gcc", str(assembly), str(runtime_object()), "-lm", "-o", str(executable)], check=True)


def compile_native(source: str, executable: Path, optimized: bool = True) -> None:
    """Compile Ella source into a native executable. Raises EllaError for errors in the program."""
    module = compile_program(source)
    if optimized:
        optimize(module)
    build(module, executable)


def run_native(source: str, out: TextIO, optimized: bool = True) -> None:
    """Compile a program to a native executable and run it, like the interpreters' `run`.

    What the program prints goes to `out`. If it stops with a run-time
    error, that is raised as an EllaError, as an interpreter would. Freed
    arena memory is overwritten (ELLA_POISON), so that a program that uses
    an object after its arena is freed prints nonsense instead of passing
    by luck.
    """
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "program"
        compile_native(source, executable, optimized)
        env = os.environ | {"ELLA_POISON": "1"}
        result = subprocess.run([str(executable)], check=False, capture_output=True, text=True, env=env)
    out.write(result.stdout)
    if result.returncode != 0:
        raise parse_error(result.stderr)


def parse_error(stderr: str) -> EllaError:
    """Turn a native program's "line:column: message" error back into an EllaError."""
    match = re.fullmatch(r"(\d+):(\d+): (.*)", stderr.removesuffix("\n"), re.DOTALL)
    if match is None:
        return EllaError(f"the program failed: {stderr.strip()}", Span(0, 0))
    return EllaError(match[3], Span(int(match[1]), int(match[2])))
