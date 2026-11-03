# Ella: status and TODO

A review of where Ella stands on 2026-11-02, what is missing, and ideas for
what to build next. It complements [README.md](README.md) (goals and stage
history), the open questions in [docs/grammar.md](docs/grammar.md#open-questions),
and [docs/memory-model.md](docs/memory-model.md) §11, §12 and §15.

## Where things stand

All seven planned stages are done, and work since then added methods,
protocols, user-defined constraints, editor support, and the quick wins
below (a small library, destructuring, exhaustiveness checking, deeper
calls, then math, library functions as values, `eprint`, exit statuses
and warnings, then a prelude written in Ella, more of the library, `mut`
in patterns and more warnings, then inferred lambda types, built-ins and
constructors as values, a larger prelude with a hash map, prelude error
locations, and warnings for unused parameters and functions, then modules
and imports, with the prelude as standard library modules, then loops that
carry values instead of piling them up, and benchmarks, then generators
and iterators, then direct calls, inlining and register allocation, then
stack traces, errors with causes and locations, and `main` giving a
`Result`). `make check` passes (1234 tests, ruff, strict mypy).

| Area | State |
| --- | --- |
| Front end | Tokenizer and Pratt parser for everything in grammar.md; lenient parsing for editors |
| Type checker | Inference by unification (lambdas other than top-level functions can leave out their types), generics with built-in and user-defined constraints (met structurally), methods, `mut` rules and exclusivity, `nocopy` rules, generators, stages, exhaustive `match`, warnings (cases that never match, unused variables, parameters and functions, `mut` that isn't needed, code that never runs) |
| Interpreters | AST interpreter (the reference) and IR interpreter (with arena and memory checks); both agree on every test program |
| IR and optimizer | Typed SSA CFG with a verifier, and generator functions that `yield`; mem2reg, direct calls, inlining, constant folding, CFG simplification, DCE, copy-to-move |
| Native code | x64 assembly plus a C runtime with real arenas, and SSA register allocation; byte-for-byte the same output and errors as the interpreters |
| Memory model | Value semantics without a GC: arenas per call and per loop iteration, compiler-chosen placement, loops that carry values in two arenas taking turns (no region bloat), pools with generation-checked handles, arenas freed as a unit, `nocopy` and finalizers, generators that keep their frame and levels in the `Gen` |
| Staging | `quote` and `$` at any depth, compile-time splices, types as values, reflection, computed types (`new_struct`) |
| User types | Structs, enums, methods, operators (`add`, `compare`, ...), protocols (`get`, `set`, `iter`, `next`, `contains`, `len`); generators (`yield`, `Gen[T]`, `next`) |
| Library | Conversions and parsing of numbers, math (`sqrt`, `floor`, `ceil`, `round`, `exp`, `ln`, `sin`, `cos`, `abs`, `min`, `max`), `ord`, `chr`, strings (`split`, `join`, `find`, `replace`, `trim`, `chars`, `upper`, `lower`, `starts_with`, `ends_with`, `pad_left`, `pad_right`, `format_f64`), `hash`, slices, `read_line`, `read_file`, `write_file`, `args`, `env`, `exit`; all but `exit` usable as values, as are `print`, `eprint`, `len` and constructors; `print` and `eprint` with any number of values; a standard library in Ella (modules `lists`, `maps`, `math`, whose names are the prelude) with `map`, `filter`, `sort`, `sort_by`, `reverse`, `sum`, `any`, `all`, `zip`, `enumerate`, `range` with a step, `pi`, and `Map[K, V]`; lazy versions on generators in `iters` (`import iters`: `each`, `count`, `map`, `filter`, `take`, `skip`, `take_while`, `chain`, `enumerate`, `zip`, `scan`, `collect`) |
| Modules | A file is a module; `import geo.poly`, `as`, imports of one item (an enum's variants come with it); private names (`_x`) for the module and those below it, fields and methods too; other modules' variables are read-only; whole-program compilation, leaving out unused functions of modules |
| Tooling | CLI with `--ir`, `--asm`, `--placement`, `--profile`, `--max-depth`, program arguments after `--`, exit status from `main` (or a `Result`), stack traces for run-time errors in every backend, warnings; language server (errors, warnings, hover, go to definition, semantic tokens, completion, all across imported modules, reading open documents); VS Code extension; Makefile; benchmarks with memory bounds (`make bench`) |

Rough speed, from `fib(35)` (about 30 million calls, test/benchmarks):
the native executable takes 0.09 s, about 2× slower than C at `gcc -O0`
(it was 0.2 s before direct calls and register allocation). A `fib(24)` run takes
1 s in the AST interpreter and 2 s in the IR interpreter; the IR one is
slower because it checks every memory access.

## Findings

Gaps found by writing small programs against the current build, plus
what the docs already list as open. ★ marks items that make ordinary
programs hard to write today.

### Standard library and I/O

The built-ins are `print`, `eprint`, `len`, `run`, the pool and arena functions,
reflection, and the library (docs/grammar.md §10): number conversions and
parsing, math, code points, string functions, `hash`, files, arguments,
the environment and `exit`. The standard library's modules (src/ella/lib/,
whose names are the prelude) add list functions (`map`, `filter`, `sort`,
`sort_by`, `sum`, `zip`, ...), `pi`, and `Map[K, V]`, written in Ella.

- No set type, and no map literal (`{` is an unexpected character). A
  `Set[T]` would be a small layer over `Map`.
- `Map` tells keys apart by their text forms, so each lookup builds the
  key's text: simple, and right for every `Eq` type, but slow for big
  keys, and `0.0` and `-0.0` are different keys. A `Hash` constraint (a
  `hash` method) would let a type do better. A `Map` also prints, and
  compares with `==`, by its inner fields.
- `sum` takes a start value (`sum(xs, 0)`): a generic function has no way
  to make the zero of a `Num` type.
- `upper` and `lower` only change ASCII letters; the rest of Unicode would
  need case tables in the runtime.
- Missing math: `tan`, `atan2`, `log10`, `f64` powers other than `**`,
  and constants besides `pi`.
- A run-time error always gives status 1; there's no way to choose
  another.

### Language

- Top-level functions and methods must give their whole type, even
  `let f: (i64) -> i64 = (x) => x + 1`, whose declared type could supply
  it. Lambdas with inferred types have one type, decided by their first
  use; only top-level generic functions can be used at several.
- `print` and `eprint` as values take one argument, not any number.
- Generic functions must be top-level; there are no generic lambdas.
- Only `i64`, `f64`, `bool` and `str`. Narrower integers, `f32` and SIMD
  vectors (a stated goal) aren't designed yet.
- No negative indexing (deliberate for now).
- The warning for cases that never match doesn't compare ranges with
  literals or each other (`case 1` after `case 0..5` isn't reported), and
  ranges never count as covering an `i64`. Unused top-level variables,
  methods and enum variants aren't reported, nor two unused functions
  that only call each other.
- Native code has room for about two scope levels per nested call; a deep
  recursion through loops can run out of scope arenas before the depth
  limit, which the interpreters don't.
- Imports name one module or item each: there's no way to import several
  names of a module at once (`import geo: area, Shape`), no re-exports,
  and no libraries other than the program's own files and the standard
  library. Imports can't form cycles.
- Errors name types with their module (`maps.Map[str, i64]`, the name in
  the linked program), while values print without it (`Map(...)`).
- A `match` outside a module on its enum with a private variant suggests
  adding a case for that variant, which can't be written there (`_` works).
- `comptime` is still reserved, though `quote`/`$` may make it unnecessary.

### Types, methods and protocols

- `==` and the text form (`print`) can't be customized; both are structural
  and done by the runtime, so they would need callbacks into Ella code.
- A generator takes its nocopy arguments (a `Gen` given to another
  generator, say) only as new values: `squares(count(4))` works, but
  `squares(g)` for a variable `g` doesn't. Moving a local variable at its
  last use, as `return` does, would allow it.
- A function type can't say that a function takes its arguments, so the
  rule goes by the result type: every function that gives a `Gen` takes
  its nocopy arguments, even one that only borrows them.
- Generators can't yield nocopy values, and an iterator over a list (a
  generator or a struct with `next`) holds a copy of it, or the list
  itself when nothing changes it; `Map`'s `iter` still builds a list of
  its keys.
- The AST interpreter runs each generator on a thread of its own, so
  recursion through thousands of generators is slow there.
- No functions in a type's namespace that don't take a value
  (`Stack.new()`), and no methods on built-in types (`xs.len()`).
- Constraints can't require other constraints or operators
  (`constraint Ring: Add`), and `Num` is only met by `i64` and `f64`.
- Protocol method names (`get`, `set`, `iter`, `add`, ...) are reserved for
  their meaning, so they can't be ordinary methods.
- No generic computed types (`new_struct` makes non-generic structs only),
  and types can't be spliced into struct definitions.

### Memory model

From [docs/memory-model.md](docs/memory-model.md) §11, §12 and §15:

- Arenas hold at most 16 MiB and about a million values each (a slot
  arena's size), and a ref's generation is 24 bits, so a ref kept through
  16 million frees of its slot arena could be taken for a live one.
- Owning (non-copyable) handles, and handles that know their pool; a handle
  used with the wrong pool of the same type isn't caught.
- Region bloat remains where loops don't carry values (memory-model.md
  §12, §16): a variable a loop both replaces and changes in place, a global
  replaced in a loop that calls Ella functions, loops left by a labeled
  `break` or `continue` of an outer loop, and values rebuilt by recursion
  rather than a loop.
- A loop that carries values keeps one iteration's temporaries until the
  end of the next, so its peak can be up to twice a plain loop's.
- Moving at the last use (`x = y`) only sees `y`'s own block, not
  liveness in general.
- Copies are deep, even where a shallow copy would be safe.
- Exclusivity and in-place analysis go by name, so shadowing makes them
  more conservative than needed.
- No warnings or reports about costly copies or placement, beyond
  `--placement` and `--profile`.

### Staging

- Native code can't run code built at run time (`run` needs an interpreter).
- A quote inside a function defined inside a splice can't refer to the
  variables of the quote the splice is in. The checker now rejects this
  (the IR can't reach them); supporting it means passing the outer
  quote's fresh names into the function.

### Back end and performance

- Register allocation is simple: a value that finds no free register
  spills to a stack slot for its whole life (no splitting of live ranges,
  no choice of which value to spill), values live across a call can only
  use the five callee-saved registers, `f64` values use general registers
  (moved to xmm to compute), generator functions get none, and arguments
  are still pushed on the stack.
- Inlining goes by size alone (INLINE_LIMIT instructions), never inlines a
  recursive function, and gives each inlined call's scope levels to the
  caller, which takes them on every call, so a recursive function that
  inlines calls with levels uses more native scope arenas per call. An
  inlined call still counts toward the depth limit (`enter`/`leave`).
- No loop optimizations, common subexpression elimination, strength
  reduction (`n % 2` divides; test/benchmarks/arith.ella is mostly
  `idiv`), or escape analysis.
- `iter` and copies allocate where a smarter lowering wouldn't.
- Only x64 Linux, through gcc.

### Tooling

- The language server re-analyzes every open document after each change
  (one may import another), and checks the whole standard library each
  time; checked modules aren't kept between analyses. A file's imports
  are found from the workspace folder, and whether a file without `main`
  is a program or a module is a guess (for unused-function warnings).
- The language server has no rename, find references, formatting, signature
  help or code actions.
- No formatter, REPL or debugger support (no DWARF line info in native code).
- The benchmarks (test/benchmarks) check output and memory, not speed;
  `make bench` shows the times, which are for watching by hand.

## Quick wins

### Done (2026-11-02)

1. Conversion built-ins: `to_f64`, `to_i64` (truncating, with a trap if it
   doesn't fit), `parse_i64` and `parse_f64` returning `Result`.
2. Destructuring `let` (`let (a, b) = pair`, `let Point(x, y) = p`), with
   the patterns `match` and `for` use, and tuple items (`pair.0`).
3. Input: `read_line() -> Optional[str]`, `read_file(path) ->
   Result[str, Error]` and `args() -> [str]` (`ella prog.ella -- a b`).
4. Strings: `split`, `join`, `find`, `replace`, `trim`, `chars`, and
   slicing of strings and lists (`s[1..3]`).
5. Exhaustiveness checking for `match`, over enums, structs, tuples and
   bools, with an example of a missed value; `let` and `for` patterns must
   match every value.
6. An assignment used as a body has the value `()`, as it does at the end
   of a block.
7. Calls can nest 10,000 deep by default in every backend (the
   interpreters run on a big stack, native code on its own), and
   `--max-depth` changes it.
8. The checker rejects the staging case the IR can't compile.

The library is one IR operation (`builtin`), a Python module shared by
the interpreters (src/ella/library.py), and matching C functions in the
runtime; new functions with fixed types are cheap to add that way.

### Done (2026-11-02, second round)

1. Math and code points: `sqrt`, `floor`, `ord`, `chr`, and `abs`, `min`
   and `max`, which are generic over `Num` (the runtime has one C
   function per number type).
2. Library functions are values: `apply(trim, s)`, `[min, max]`. The
   lowering makes a small IR function for each one used, like `trim(str)`.
3. `let mut (a, b) = ...` and `let (a, b): (i64, i64) = ...`.
4. `print(a, b, ...)` with any number of values, separated by spaces, and
   `eprint` for standard error. Test programs can check standard error
   (`NAME.stderr`).
5. `main` can be `() -> i64`, giving the exit status (0 to 255), in every
   backend and the CLI. Test programs can check it (`NAME.status`).
6. Warnings for `match` cases that can never match (exhaustive.py's
   `redundant`), printed by the CLI and shown by the language server.

### Done (2026-11-02, third round)

1. A prelude written in Ella (src/ella/prelude.py) with `reverse`,
   `sort` (a stable merge sort by `<`), `map`, `filter` and `pi`. A
   program gets the definitions it uses, unless it defines the names
   itself, so every backend runs the same code and programs that don't
   use it are unchanged.
2. Math: `ceil`, `round` (halves away from zero), `exp`, `ln`, `sin` and
   `cos`, matching C's results for infinities and NaN.
3. Strings: `upper` and `lower` (ASCII letters), `starts_with`,
   `ends_with`, `pad_left`, `pad_right` and `format_f64(x, digits)`.
4. `exit(status)` stops the program anywhere, without running
   finalizers (its result type is never, so it fits anywhere a value
   does); `env(name)` and `write_file(path, text)`.
5. `mut` on one name of a pattern: `let (mut a, b)`, `for (i, mut row)`,
   `case Some(mut items)`.
6. Warnings for unused local variables (`_name` says it's on purpose)
   and for code after a line that never finishes.

### Done (2026-11-02, fourth round)

1. Lambdas other than top-level functions and methods can leave out
   their parameter and result types: `map(xs, (n) => n * 2)`,
   `xs |> filter(n => n > 0)`, `let inc = (x) => x + 1`. The missing types
   are type variables, decided by the lambda's use or its body. Bodies
   are still checked after the line they are in, except when a type they
   may decide is needed sooner (`-ys[0]` right after
   `let ys = map(xs, (n) => n * 2)`).
2. More of the prelude: `sum(xs, start)`, `any`, `all`, `zip`,
   `enumerate`, `sort_by(xs, key)` and `range(start, stop, step)`.
3. `print`, `eprint`, `len` and constructors are values:
   `map(xs, Some)`, `let make = Point`. The checker makes a lambda that
   calls each (`(_1) => Some(_1)`), with inferred types, and the lowering
   makes it a function; to the interpreters they were values already.
4. Prelude code has its own file in spans, so an error in it reads
   `<prelude>:12:5` in every backend and the CLI. That also lets prelude
   code use built-ins and bind names in patterns: there, a built-in's
   name always means the built-in, and a bare name always binds, whatever
   the program defines.
5. `hash(str) -> i64` (FNV-1a over code points) and `Map[K, V]` in the
   prelude, made with `new_map()`: `m[k]`, `m[k] = v`, `k in m`,
   `len(m)`, `for k in m`, `lookup`, `remove`, `keys`, `values` and
   `items`, keeping keys in the order they were first set.
6. Warnings for unused parameters, for top-level functions not used
   outside their own bodies, and for `mut` variables that never change.

### Done (2026-11-02, fifth round): modules

1. A file is a module, `import geo.poly` binds its whole path, and `as`
   gives a module, or an imported item, another name. An enum's variants
   come with it (bare with `as`), and paths work in types, patterns and
   constraints. Imports come first in a file, are found from the entry
   file's directory and then the standard library, and can't form
   cycles.
2. Names starting with `_` are private to their module and the modules
   below it: top-level names, fields (a struct with one can only be made
   or taken apart there), methods and variants. Other modules' variables
   can be read but not changed.
3. Linking (src/ella/modules.py) makes one program of the modules, with
   module names in front of their top-level names, so the checker,
   interpreters and back end are mostly unchanged; quoted code keeps the
   meaning of its names, private ones too, wherever it is spliced or run.
   Unused functions of modules aren't compiled.
4. The prelude is the standard library's modules, `lists`, `maps` and
   `math` (src/ella/lib/), which can be imported too; `Map`'s fields and
   helper methods are private. Their code means what it says whatever
   the program defines.
5. Errors in a module give its file in every backend
   (`geo/poly.ella:3:5`, `<std>/lists.ella:117:9`); native code passes a
   file number with each line. Warnings for unused imports, for a module
   named like a library module, and for unused private functions
   (public ones may be used elsewhere, except in the entry file).
6. `ella --expanded` shows each module with splices; the language server
   links each file with its imports, reading open documents first, shows
   errors from imports at the import line, finds definitions in other
   files (the standard library's too), and completes a module's names
   after `geo.`. Test programs can be directories (`NAME/main.ella`).

### Next

Small steps that build on the above:

1. `Set[T]` in the prelude, on top of `Map`, and a `Hash` constraint so
   that a key type can hash itself without building its text.
2. Take a top-level function's missing types from its `let`'s declared
   type: `let inc: (i64) -> i64 = (x) => x + 1`.
3. A way for generic code to make the zero of a `Num` type, so that
   `sum(xs)` needs no start value.
4. Warnings for unused methods, enum variants and top-level variables.

### Done (2026-11-02, ninth round): errors and diagnostics

1. Run-time errors print a stack trace in every backend, byte for byte
   the same: each call in progress, innermost first, where it is and in
   which function (`called at 10:44 in pick`), with the middle of a long
   one left out. Every call records its site at its call depth (natively,
   in `ella_trace`, which costs a store per call); an IR call's `within`
   names its function even after inlining. Inside library code, the trace
   shows the program's line that called it.
2. `Error` has a `cause` and where it was made (`at`): `Error(message)`
   or `Error(message, cause)`, and library functions make theirs where
   they are called. `==` and the text form leave out `at`, and patterns
   may leave out the last fields.
3. `main` may give `Result[(), E]` or `Result[i64, E]`, so it can use
   `?`; an `Err` stops the program with its error, an `Error` reported at
   its `at` with a line for each cause.
4. Fixes found on the way: a library function used as a value reported
   its errors at its first use in the IR (now at its call, as the AST
   interpreter does), and the AST interpreter allowed one more nested call
   from the top-level code than the other backends.

### Done (2026-11-02, eighth round): a faster back end

1. Benchmarks for speed (test/benchmarks: `fib`, `arith`, `methods`,
   `map_ops`), shown by `make bench`.
2. Calls of known functions are direct (`call_direct`, ella.passes.direct),
   and a closure that captures nothing is static data, so calling a
   top-level function no longer makes a closure.
3. Inlining (ella.passes.inline): small, non-recursive functions are
   copied into their callers, bottom up. The copy's scope levels go above
   the caller's, a `reset` frees them where it returns, and `enter` and
   `leave` keep counting the call toward the depth limit, so errors stay
   exactly the same. Functions left unused are dropped.
4. Register allocation (ella.regalloc): liveness, then greedy coloring in
   dominator-tree order, which SSA makes safe; values live across calls
   get callee-saved registers. Jumps and runtime calls move values all at
   once. Test programs also run with one register of each kind, so that
   values spill.
5. `//` and `%` on i64s, and indexing a range, are inline instructions
   rather than runtime calls.
6. A bug fix: after a loop that carried values ran an odd number of
   iterations, its two arenas stayed swapped, so a later loop nested one
   level deeper shared its arena with its outer loop and could free
   values still in use (native code printed garbage). `reset` now puts
   them back in order.

Times went from 204 to 90 ms (`fib`), 189 to 110 ms (`methods`) and 190
to 120 ms (`map_ops`); `arith` stays at 250 ms, bound by `idiv`.

### Done (2026-11-02, seventh round): generators and iterators

1. A function with `yield` is a generator: calling it gives a `Gen[T]`,
   which runs its body a step at a time. `next(mut g)` gives the next
   item as an `Optional`, and `for x in g` and `for x in mut g` (which
   advances it in place) loop over one. A `Gen` can't be copied, so a
   function that gives one takes its nocopy arguments, which must be new
   values (docs/grammar.md §9).
2. Iterators: a struct or enum with `next = (mut Self) -> Optional[T]` can
   be looped over, and `iter` may give an iterator or a `Gen`.
3. The IR has generator functions, which `resume` runs until a `yield`
   terminator; their frames and scope levels live in the `Gen`, so no
   state machine is needed (docs/memory-model.md §17). Natively, they
   address their frame from `r12`. Values with finalizers live across a
   `yield`, and are finalized when the generator moves past them or dies.
   The AST interpreter runs each generator's body on a thread.
4. The standard library module `iters` (imported, not in the prelude) has
   lazy `map`, `filter`, `take`, `zip`, ... on generators, and a benchmark
   checks that a pipeline of 200,000 items runs in under 2 KB of arenas.

### Done (2026-11-02, sixth round): region bloat

1. A loop that gives variables declared outside it new values carries
   them in two arenas that take turns (IR `swap` and `carry`), so it
   keeps at most two versions instead of one per iteration, in every
   backend (docs/memory-model.md §16).
2. `x = y` moves y's value when nothing reads y afterwards in its block,
   so `sort` and `sort_by` no longer copy the list on every pass.
3. Numbers read from variables no longer shorten a value's lifetime, and
   the `moves` pass counts constants as new, removing more copies.
4. test/benchmarks: six programs with their output and a bound on their
   arena peak, checked by the tests; `make bench` times them. Peaks fell
   from 18 MB to 24 KB (a string built in a loop) and from 36 MB to
   9.5 MB (sorting 200,000 numbers).

## Roadmap ideas

Bigger directions, roughly in order of how much they would unlock. Each fits
Ella's goals: learning, simplicity first, no GC, x64.

### 1. A standard library written in Ella

The protocols, constraints and methods are strong enough to write
collections in Ella itself, and the standard library (src/ella/lib/) now
has list functions and `Map[K, V]`, with C runtime primitives underneath
(I/O, math, `hash`) added as library functions (src/ella/library.py and
the runtime). Growing it tests the language on real code, finds rough
edges in the type system, and makes example programs possible. Writing
`Map` showed some: `sum` can't make a zero, `==` and printing can't be
customized, there is no `Map.new()` (so `new_map()`), and keys are told
apart by their text forms for lack of a `Hash` constraint. Now that it is
made of modules, it can grow by topic (`strings`, `sets`, ...).

### 2. Packages

Modules are done (see above): whole-program compilation, with generics and
methods specialized across modules, and compile-time code calling imported
functions. What's left is code from outside the program: a library path
or packages, importing several names at once, re-exports, and perhaps
separate compilation of modules that don't change.

### 3. A real back end: beyond register allocation and inlining

Direct calls, inlining and SSA register allocation are done (above), with
speed benchmarks in test/benchmarks. Next would be passing arguments in
registers, spilling smarter (splitting live ranges, choosing what to
spill), strength reduction, and simple loop optimizations (hoisting
invariant code, keeping a range loop's counter without `index`).

### 4. Error handling and diagnostics

Done: stack traces, `Error` with a cause and location, and `main` giving a
`Result` (ninth round, above). What's left is for tools: source lines in
traces, and DWARF line info so that native programs can be debugged.

### 5. Views without copying

Partly done: slices, `trim` and `split` are views in compiled code
wherever the lowering's borrow analysis shows no one could tell
(memory-model §18). Still open: views that are kept (a struct, iterator
or generator holding a slice, which gets a copy; memory-model §4), the
copy of a list that an iterator over it holds, and a loop that carries a
slice of its own variable (`rest = rest[1..n]`), which copies each time.
Views made list and string headers bigger (memory-model §18, Results);
inlining `ella_list_index` in codegen would win back the extra load.

### 6. SIMD and narrower number types

A stated goal. Add `i8`–`i32` and `f32`, then fixed-size vector types
(`f32x4`, ...) with element-wise operators through the `Num`-style
constraints, lowered to SSE/AVX in codegen. This is a natural place to use
the protocol methods, and it exercises the back end.

### 7. Native staging

Running quoted code in native executables needs either a JIT (generate x64
into executable memory at run time) or embedding the IR interpreter in the
runtime. A JIT is the bigger learning project; a smaller version could run
the existing code generator ahead of time for every quote whose shape is
known while compiling.

### 8. Concurrency

memory-model §10 sketches threads. Arenas per thread, `nocopy` values moved
between threads, and pools as the only shared state would fit the existing
model without a GC. This is a long-term item that depends on ideas 3 and 5.

### 9. Developer experience

A REPL built on the AST interpreter, `ella fmt`, LSP rename, references and
signature help, and DWARF line info so gdb can step through native programs.
Each is independent and fairly small.

### 10. Self-hosting (stretch)

Once ideas 1 and 2 exist, rewriting the tokenizer or parser in Ella would be
the ultimate test of the language, and a fun milestone.
