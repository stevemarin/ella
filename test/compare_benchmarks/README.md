# Cross-language benchmarks

These small workloads run the same algorithm and input in five modes: Ella's
tree interpreter, Ella's IR interpreter, optimized native Ella, Python, and
C compiled with `gcc -O2`. They are separate from `test/benchmarks`, whose
programs focus on Ella's native performance and arena use.

Run all workloads (or name a subset) from the repository root:

```sh
uv run python test/compare_benchmarks/run.py
uv run python test/compare_benchmarks/run.py fib arithmetic
uv run python test/compare_benchmarks/run.py --runs 5
```

The runner compiles each native program once, checks that all implementations
print identical results, then reports the maximum time for Ella's two
interpreters and the minimum time for native Ella, Python, and C. Timing
includes process startup and interpreter startup, but excludes native
compilation. Each Ella interpreter invocation has a five-second timeout; a
timed-out mode is reported as `>5.00 s`. This makes short workloads sensitive
to startup overhead; increase
the work factors in all three implementations together for longer measurements.
The numbers are for orientation, not a language ranking: Python integers are
arbitrary precision, and the array workload deliberately compares Ella's
multidimensional-array kernel with explicit Python and C loops.

Workloads:

* `fib`: recursive Fibonacci, exercising calls and branches.
* `arithmetic`: repeated GCDs plus a Collatz walk, exercising integer loops
  and helper calls.
* `arrays`: repeated spring-particle updates and checksums, exercising flat
  numeric data and loop kernels.
