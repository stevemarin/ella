"""Building and running native executables."""

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from ella.codegen import generate
from ella.ir import Module
from ella.lexer import SITE_LINES, CallSite, EllaError, Span

SITES = {line: span for span, line in SITE_LINES.items()}  # a stack trace's line -> its call's site
from ella.lower import compile_program
from ella.passes import optimize
from ella.values import MAX_CALL_DEPTH

RUNTIME = Path(__file__).with_name("runtime.c")
# No fused multiply-adds, which round differently from Python (the array code multiplies, then adds).
CFLAGS = ["-std=gnu17", "-O2", "-Wall", "-Wextra", "-ffp-contract=off"]


def runtime_object() -> Path:
    """The compiled runtime, building it if this version hasn't been built yet."""
    source = RUNTIME.read_bytes()
    # Include flags in the key: identical C source can compile to different objects.
    digest = hashlib.sha256(source + " ".join(CFLAGS).encode()).hexdigest()[:16]
    obj = Path(tempfile.gettempdir()) / f"ella-runtime-{digest}.o"
    if not obj.exists():
        partial = obj.with_suffix(f".{digest}.tmp")  # renamed into place, so a half-written file is never used
        subprocess.run(["gcc", *CFLAGS, "-c", str(RUNTIME), "-o", str(partial)], check=True)
        partial.replace(obj)
    return obj


def build(module: Module, executable: Path, max_depth: int = MAX_CALL_DEPTH, registers: int | None = None) -> None:
    """Generate assembly for a module and link it with the runtime into an executable.

    `registers` limits the registers of each kind values may have (see ella.codegen.generate), for tests.
    """
    with tempfile.TemporaryDirectory() as directory:
        assembly = Path(directory) / "program.s"
        assembly.write_text(generate(module, max_depth, registers))
        subprocess.run(["gcc", str(assembly), str(runtime_object()), "-lm", "-o", str(executable)], check=True)


def compile_native(
    source: str,
    executable: Path,
    optimized: bool = True,
    out: TextIO | None = None,
    path: Path | None = None,
    registers: int | None = None,
) -> None:
    """Compile Ella source into a native executable. Raises EllaError for errors in the program.

    What the program prints while compiling (in splices) goes to `out`. `path` is its file, which imports are found from.
    """
    module = compile_program(source, out, path)
    if optimized:
        optimize(module)
    build(module, executable, registers=registers)


def run_native(
    source: str,
    out: TextIO,
    optimized: bool = True,
    stdin: bytes = b"",
    args: Sequence[str] = (),
    err: TextIO | None = None,
    path: Path | None = None,
    registers: int | None = None,
) -> int:
    """Compile a program to a native executable and run it, like the interpreters' `run`. Returns its exit status.

    The program reads `stdin` and gets the arguments `args`. What it prints
    goes to `out`, and what it prints to standard error to `err` (if given).
    If it stops with a run-time error, that is raised as an EllaError, as an
    interpreter would. Freed arena memory is overwritten (ELLA_POISON), so
    that a program that uses an object after its arena is freed prints
    nonsense instead of passing by luck.
    """
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "program"
        compile_native(source, executable, optimized, out, path, registers)
        env = os.environ | {"ELLA_POISON": "1"}
        result = subprocess.run([str(executable), *args], check=False, capture_output=True, input=stdin, env=env)
    out.write(result.stdout.decode("utf-8"))
    return finish_run(result.returncode, result.stderr.decode("utf-8"), err)


def finish_run(status: int, stderr: str, err: TextIO | None) -> int:
    """After a native program ends: pass on what it printed to standard error, and raise its run-time error if any.

    A run-time error is the last lines of standard error (its message, then
    its stack trace), and makes the status 1, which `main` can give too.
    """
    found = parse_error(stderr) if status == 1 else None
    error = None
    if found is not None:
        error, start = found
        stderr = stderr[:start]
    if err is not None:
        err.write(stderr)
    if error is not None:
        raise error
    if status not in range(256):  # killed by a signal
        raise EllaError(f"the program failed: {stderr.strip()}", Span(0, 0))
    return status


PLACE = r"(?:([^\n:]+):)?(\d+):(\d+)"  # line:column, or file:line:column in a module other than the entry file
CALL = re.compile(rf"  called at {PLACE}(?: in (.*))?")
CAUSE = re.compile(rf"  caused by {PLACE}: (.*)")


def parse_error(stderr: str) -> tuple[EllaError, int] | None:
    """The run-time error at the end of a native program's standard error, if there is one, and where it starts.

    It is "line:column: message", then its causes and stack trace (EllaError.format).
    An error in a module other than the entry file starts with its file:
    `geo/poly.ella:3:5: ...`.
    """
    match = re.search(rf"(?:^|\n)({PLACE}: ([^\n]*)((?:\n  [^\n]*)*))\n?\Z", stderr)
    if match is None:
        return None
    error = EllaError(match[5], place(match[2], match[3], match[4]))
    error.trace = []
    for line in match[6].splitlines()[1:]:
        if line in SITES:
            error.trace.append(CallSite(SITES[line], ""))
        elif omitted := re.fullmatch(r"  \.\.\. (\d+) more calls", line):
            error.omitted = int(omitted[1])
        elif call := CALL.fullmatch(line):
            error.trace.append(CallSite(place(call[1], call[2], call[3]), call[4] or ""))
        elif cause := CAUSE.fullmatch(line):
            error.causes.append((place(cause[1], cause[2], cause[3]), cause[4]))
        else:
            return None
    return error, match.start(1)


def place(file: str | None, line: str, column: str) -> Span:
    return Span(int(line), int(column), file or "")
