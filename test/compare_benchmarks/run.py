"""Compare Ella's interpreters and native code with Python and C."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ella.native import compile_native

BENCHMARKS = Path(__file__).parent
MODES = ("tree max", "IR max", "Ella native", "Python", "C")
INTERPRETER_TIMEOUT = 5.0


def run(command: list[str], timeout: float | None = None) -> tuple[float, str | None]:
    start = time.perf_counter()
    try:
        result = subprocess.run(
            command, cwd=BENCHMARKS, capture_output=True, text=True, check=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return time.perf_counter() - start, None
    return time.perf_counter() - start, result.stdout.strip()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="workloads to run (default: all)")
    parser.add_argument("--runs", type=int, default=3, help="timed runs per implementation")
    options = parser.parse_args(argv)
    if options.runs < 1:
        parser.error("--runs must be at least 1")
    names = options.names or ["fib", "arithmetic", "arrays"]
    unknown = [name for name in names if not (BENCHMARKS / f"{name}.ella").is_file()]
    if unknown:
        parser.error(f"unknown benchmark: {unknown[0]}")

    with tempfile.TemporaryDirectory() as temp:
        tempdir = Path(temp)
        print(f"{'benchmark':<14} " + " ".join(f"{mode:>13}" for mode in MODES))
        for name in names:
            ella = BENCHMARKS / f"{name}.ella"
            python = BENCHMARKS / f"{name}.py"
            c_source = BENCHMARKS / f"{name}.c"
            native_exe = tempdir / f"{name}-ella"
            c_exe = tempdir / f"{name}-c"
            compile_native(ella.read_text(), native_exe, optimized=True, path=ella)
            subprocess.run(["gcc", "-O2", "-o", str(c_exe), str(c_source)], check=True)
            commands = [
                [sys.executable, "-m", "ella", "--tree", str(ella)],
                [sys.executable, "-m", "ella", str(ella)],
                [str(native_exe)],
                [sys.executable, str(python)],
                [str(c_exe)],
            ]
            results: list[tuple[list[float], bool]] = []
            expected: str | None = None
            for index, command in enumerate(commands):
                timeout = INTERPRETER_TIMEOUT if index < 2 else None
                # One untimed pass verifies output and avoids measuring cold startup.
                _, output = run(command, timeout)
                if output is None:
                    results.append(([INTERPRETER_TIMEOUT], True))
                    continue
                if expected is None:
                    expected = output
                elif output != expected:
                    raise SystemExit(f"{name}: output mismatch\nexpected: {expected}\nactual:   {output}")
                times: list[float] = []
                timed_out = False
                for _ in range(options.runs):
                    elapsed, output = run(command, timeout)
                    times.append(elapsed)
                    if output is None:
                        timed_out = True
                        break
                    if output != expected:
                        raise SystemExit(f"{name}: output mismatch\nexpected: {expected}\nactual:   {output}")
                results.append((times, timed_out))
            cells_list = []
            for index, (times, timed_out) in enumerate(results):
                if timed_out:
                    cells_list.append(f"{'>5.00':>10} s")
                else:
                    ms = (max(times) if index < 2 else min(times)) * 1000
                    cells_list.append(f"{ms:>10.2f} ms")
            cells = " ".join(cells_list)
            print(f"{name:<14} {cells}")


if __name__ == "__main__":
    main(sys.argv[1:])
