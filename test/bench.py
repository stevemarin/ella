"""Run the benchmarks in test/benchmarks natively, and report their time and memory.

    uv run python test/bench.py [NAME ...] [--runs N]

Each benchmark is compiled once (optimized), then run N times (default 5).
The table shows the fastest run's time and the high-water memory and deep-copy
volume. test/test_benchmarks.py checks each benchmark's output and arena peak
bound; this script is for watching the other costs too.
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


def measure(path: Path, runs: int) -> tuple[float, int, int, int, int]:
    """Return fastest wall time, arena peak, peak RSS, virtual peak, and deep-copy bytes."""
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "program"
        compile_native(path.read_text(), executable, path=path)
        env = os.environ | {"ELLA_RUNTIME_STATS": "1"}
        best, peak, rss, virtual, copied = float("inf"), 0, 0, 0, 0
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
            match = re.search(
                r"arena peak: (\d+) bytes\nvirtual peak: (\d+) bytes\nbytes copied: (\d+) bytes",
                result.stderr,
            )
            assert match is not None, result.stderr
            peak = max(peak, int(match[1]))
            virtual = max(virtual, int(match[2]))
            copied = max(copied, int(match[3]))
            rss_match = re.search(r"max rss: (\d+) KB", result.stderr)
            assert rss_match is not None, result.stderr
            rss = max(rss, int(rss_match[1]))
    return best, peak, rss, virtual, copied


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Run Ella's benchmarks natively.")
    parser.add_argument("names", nargs="*", help="benchmarks to run (default: all)")
    parser.add_argument("--runs", type=int, default=5, help="runs of each, keeping the fastest")
    options = parser.parse_args(argv)
    paths = sorted(BENCHMARKS.glob("*.ella"))
    if options.names:
        paths = [path for path in paths if path.stem in options.names]
    print(
        f"{'benchmark':<16} {'time (ms)':>10} {'arena (MiB)':>13} {'peak RSS (MiB)':>16} "
        f"{'virtual (GiB)':>15} {'deep-copy (MiB)':>17}"
    )
    for path in paths:
        seconds, peak, rss, virtual, copied = measure(path, options.runs)
        mib = 1024**2
        gib = 1024**3
        print(
            f"{path.stem:<16} {seconds * 1000:>10.1f} {peak / mib:>13,.1f} {rss / 1024:>16,.1f} "
            f"{virtual / gib:>15,.2f} {copied / mib:>17,.1f}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
