# Benchmarks

Programs that measure native runtime and memory costs. Each `NAME.ella` has
`NAME.out`, exactly what it prints, and `NAME.peak`, the maximum bytes its
arenas may hold at once; [test_benchmarks.py](../test_benchmarks.py) checks
the output and arena bound, so region bloat (docs/memory-model.md §6) fails
the tests. Timing and other resource metrics are reported, not checked,
because they depend on the machine:

    uv run python test/bench.py [NAME ...]

compiles each one (optimized), runs it a few times, and prints the fastest
wall time and maximum measured memory/copy volume:

* **Arena peak** is the high-water amount held by Ella's arenas.
* **Peak RSS** is the process's maximum resident set size, measured by
  `/usr/bin/time`; it includes the runtime and other resident process data.
* **Virtual peak** is the process's `VmPeak` from `/proc/self/status`, so it
  includes reserved arena and stack address space even when most pages were
  never resident.
* **Deep-copy bytes** is the total rounded arena footprint created by
  `ella_copy`, including recursively copied objects, headers and alignment.
  It does not count moves, list appends, or other ordinary memory writes.

Memory columns use MiB except virtual peak, which uses GiB. Memory and copy
columns show the maximum observed across runs; time shows the fastest run.
`make bench` runs all benchmarks.

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
