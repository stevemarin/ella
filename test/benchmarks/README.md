# Benchmarks

Programs that measure how fast native code runs and how much memory its
arenas hold. Each `NAME.ella` has `NAME.out`, exactly what it prints, and
`NAME.peak`, the most bytes its arenas may hold at once
(`ELLA_ARENA_STATS`); [test_benchmarks.py](../test_benchmarks.py) checks
both, so a change that brings back region bloat (docs/memory-model.md §6)
fails the tests. Speed isn't checked, since it depends on the machine:

    uv run python test/bench.py [NAME ...]

compiles each one (optimized), runs it a few times, and prints the fastest
time and the arena peak. `make bench` does the same for all of them.

* `string_build`: a string rebuilt on every iteration of a loop in a function.
* `tuple_replace`: a global tuple replaced now and then in a top-level loop.
* `list_replace`: a global list replaced on every iteration.
* `sort`: the standard library's merge sort on 200,000 numbers.
* `running_max`: nested loops, the outer one carrying a list and a string,
  with an early `break`.
* `words`: splitting, sorting and joining text, with a `Map` of counts.
