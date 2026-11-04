"""Run the benchmarks in test/benchmarks natively, and report their time and memory.

    uv run python test/bench.py [NAME ...] [--runs N]

Each benchmark is compiled once (optimized), then run N times (default 5).
The table shows the fastest run's time and the arena peak: the most memory
the program's arenas held at once (ELLA_ARENA_STATS). test/test_benchmarks.py
checks each benchmark's output and that its peak stays under its `.peak`
bound; this script is for watching the numbers.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ella.native import compile_native

BENCHMARKS = Path(__file__).parent / "benchmarks"


def measure(path: Path, runs: int) -> tuple[float, int, int]:
    """The fastest run's time, arena peak in bytes, and resident peak in KiB."""
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "program"
        compile_native(path.read_text(), executable, path=path)
        env = os.environ | {"ELLA_ARENA_STATS": "1"}
        best, peak, rss = float("inf"), 0, 0
        for _ in range(runs):
            start = time.perf_counter()
            result = subprocess.run(
                ["/usr/bin/time", "-f", "max rss: %M KB", str(executable)],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            best = min(best, time.perf_counter() - start)
            match = re.search(r"arena peak: (\d+) bytes", result.stderr)
            assert match is not None, result.stderr
            peak = int(match[1])
            rss_match = re.search(r"max rss: (\d+) KB", result.stderr)
            assert rss_match is not None, result.stderr
            rss = max(rss, int(rss_match[1]))
    return best, peak, rss


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Run Ella's benchmarks natively.")
    parser.add_argument("names", nargs="*", help="benchmarks to run (default: all)")
    parser.add_argument("--runs", type=int, default=5, help="runs of each, keeping the fastest")
    options = parser.parse_args(argv)
    paths = sorted(BENCHMARKS.glob("*.ella"))
    if options.names:
        paths = [path for path in paths if path.stem in options.names]
    print(f"{'benchmark':<16} {'time (ms)':>10} {'arena peak (bytes)':>20} {'max RSS (KiB)':>15}")
    for path in paths:
        seconds, peak, rss = measure(path, options.runs)
        print(f"{path.stem:<16} {seconds * 1000:>10.1f} {peak:>20,} {rss:>15,}")


if __name__ == "__main__":
    main(sys.argv[1:])
