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
* `generators`: a pipeline of generators (the `iters` module, and one of
  its own that rebuilds a string for each item) over 200,000 items.
* `fib`: one million steps of tail-recursive Fibonacci with a modulus, which
  checks that the native backend reuses its call frame.
* `arith`: integer arithmetic in nested loops, calling small helpers.
* `methods`: small methods and an operator on a struct of floats, in a loop.
* `map_ops`: inserting, updating and looking up the keys of a `Map`.
* `map_lists`: lists grown inside a `Map`, and fields of structs in one,
  which change in place through `Map`'s `update` (`m[k] += [x]`).
* `halves`: divide and conquer on list slices, which are views of the list
  (docs/memory-model.md §18); copying them would pass the `.peak` bound.
* `fields`: `split`, `trim` and string slices, all views of the text.
* `arrays`: arrays updated item by item in a loop that carries them (AVX2
  code), with `total`, `dot` and `matmul`.
