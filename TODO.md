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
`Result`, then sets and more of the standard library, then packages, then arrays with SIMD, then f32, i32, broadcasting, slices and more fusion). `make check` passes (1375 tests, ruff, strict mypy).

| Area | State |
| --- | --- |
| Front end | Tokenizer and Pratt parser for everything in grammar.md; lenient parsing for editors |
| Type checker | Inference by unification (lambdas other than top-level functions can leave out their types), generics with built-in and user-defined constraints (met structurally), methods, `mut` rules and exclusivity, `nocopy` rules, generators, stages, exhaustive `match`, warnings (cases that never match, unused variables, parameters and functions, `mut` that isn't needed, code that never runs) |
| Interpreters | AST interpreter (the reference) and IR interpreter (with arena and memory checks); both agree on every test program |
| IR and optimizer | Typed SSA CFG with a verifier, and generator functions that `yield`; mem2reg, direct calls, inlining, constant folding, CFG simplification, DCE, copy-to-move |
| Native code | x64 assembly plus a C runtime with real arenas, and SSA register allocation; byte-for-byte the same output and errors as the interpreters |
| Memory model | Value semantics without a GC: arenas per call and per loop iteration, compiler-chosen placement, loops that carry values in two arenas taking turns (no region bloat), pools with generation-checked handles, arenas freed as a unit, `nocopy` and finalizers, generators that keep their frame and levels in the `Gen` |
| Staging | `quote` and `$` at any depth, compile-time splices, types as values, reflection, computed types (`new_struct`) |
| User types | Structs, enums, instance and type-namespace functions, receiver calls for compatible prelude functions, operators (`add`, `compare`, ...), protocols (`get`, `set`, `iter`, `next`, `contains`, `len`); generators (`yield`, `Gen[T]`, `next`) |
| Library | Conversions and parsing of numbers, math (`sqrt`, `floor`, `ceil`, `round`, `exp`, `ln`, `log10`, `log2`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `abs`, `min`, `max`), `ord`, `chr`, strings (`split`, `join`, `find`, `replace`, `trim`, `chars`, `upper`, `lower`, `starts_with`, `ends_with`, `pad_left`, `pad_right`, `format_f64`), `hash`, slices, `read_line`, `read_file`, `write_file`, `args`, `env`, `exit`; all but `exit` usable as values, as are `print`, `eprint`, `len` and constructors; `print` and `eprint` with any number of values; a standard library in Ella (modules `lists`, `maps`, `sets`, `strings`, `math`, whose names are the prelude) with `map`, `filter`, `sort`, `sort_by`, `reverse`, `sum`, `fold`, `flatten`, `flat_map`, `position`, `first`, `last`, `index_of`, `min_of`, `max_of`, `min_by`, `max_by`, `partition`, `chunks`, `unique`, `map_of`, `group_by`, `counts`, `any`, `all`, `zip`, `enumerate`, `range` with a step, `pi`, `tau`, `e`, `inf`, `nan`, `Map[K, V]` and `Set[T]`, and strings (`repeat`, `lines`, `words`, `trim_start`, `trim_end`, `strip_prefix`, `strip_suffix`, `split_once`, `count_of`, `is_digit`, `is_alpha`, `is_space`); lazy versions on generators in `iters` (`import iters`: `each`, `count`, `map`, `filter`, `take`, `skip`, `take_while`, `chain`, `enumerate`, `zip`, `scan`, `collect`) |
| Modules | A file is a module; `import geo.poly`, `as`, imports of one item (an enum's variants come with it); private names (`_x`) for the module and those below it, fields and methods too; other modules' variables are read-only; whole-program compilation, leaving out unused functions of modules; packages found on a package path (`--path`, `ELLA_PATH`) |
| Tooling | CLI with `--ir`, `--asm`, `--placement`, `--profile`, `--max-depth`, program arguments after `--`, exit status from `main` (or a `Result`), stack traces for run-time errors in every backend, warnings; language server (errors, warnings, hover, go to definition, semantic tokens, completion, all across imported modules, reading open documents); VS Code extension; Makefile; benchmarks with memory bounds (`make bench`) |

Historical speed before tail-call support, from `fib(35)` (about 30 million
calls, test/benchmarks):
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
`sort_by`, `sum`, `fold`, `zip`, ...), constants, `Map[K, V]` and
`Set[T]`, written in Ella.

- A `Map`'s `==` still compares its order too: an `eq` method comparing
  values would need `V: Eq`, which `Map[K: Eq, V]` doesn't ask for (maps
  of functions are fine), and a method can't add a constraint of its own.
- `hash` is structural, like `==`, and can't be customized: a type
  can't hash only the fields that matter, and since `==` can't be
  customized either, there is no use for a `Hash` constraint yet. A `Map`
  (or `Set`) also compares with `==` by its inner fields, its order too.
- `update` changes an item in place only for an assignment that is a line
  of a block; one whose value is used (`let y = (m[k] += 1)`) still gets
  and sets the item, and `f(mut m[k])` isn't allowed.
- `sum` takes a start value (`sum(xs, 0)`): a generic function has no way
  to make the zero of a `Num` type.
- `upper` and `lower` only change ASCII letters; the rest of Unicode would
  need case tables in the runtime.
- The `strings` module's tests (`is_digit`, `is_alpha`, `is_space`) are
  ASCII only, like `upper` and `lower`; there's no string formatting
  beyond interpolation and `format_f64` (no padding or alignment
  specifiers), and no hyperbolic functions.
- Library code is written in Ella on the built-ins, so string functions
  that look at characters one by one (`words`, `trim_start`) index the
  string per character, and `is_digit` builds a list of its characters.
- A run-time error always gives status 1; there's no way to choose
  another.

### Language

- Top-level functions and methods must give their whole type, even
  `let f: (i64) -> i64 = (x) => x + 1`, whose declared type could supply
  it. Lambdas with inferred types have one type, decided by their first
  use; generic functions bound with `let` can be used at several types.
- `print` and `eprint` as values take one argument, not any number.
- The numbers are `i64`, `i32`, `f64` and `f32` (and `Array[T]` of them);
  no `i8`, `i16` or fixed-size SIMD vector types, and no f32 literals.
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

- A type with `eq` must also have `hash`, and nothing checks that they
  agree (equal values must hash the same), nor that `eq` is an
  equivalence.
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
- Constraints can't require other constraints or operators
  (`constraint Ring: Add`), and `Num` is only met by `i64` and `f64`.
- Protocol method names (`get`, `set`, `iter`, `add`, ...) are reserved for
  their meaning, so they can't be ordinary methods.
- No generic computed types (`new_struct` makes non-generic structs only),
  and types can't be spliced into struct definitions.

### Memory model

From [docs/memory-model.md](docs/memory-model.md) §11, §12 and §15:

- Arenas hold at most 16 MiB and about a million values each (a slot
  arena's size). Slot arenas are retired before the 24-bit ref generation
  wraps; pool slots are retired before their 20-bit handle generation wraps.
- `Arena.set` retains replaced allocations until `free(mut arena)`; use a
  `Pool` for independently replaceable values. Pool slot arenas reserve 16
  MiB of virtual address space each, but the current Map benchmarks use under
  9 MiB resident memory, so chunked or size-class storage is not yet justified
  by measured resident usage.
- Owning (non-copyable) handles remain open.
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
- Consider persistent collections or arena-local transient updates followed
  by freezing/compacting the final value. If only one result is needed,
  building it with ordinary mutation and then giving it value semantics is
  simpler and likely sufficient. Structural sharing may help when several
  versions must coexist (for example, retained snapshots); updates could
  share unchanged parts, with a final copy of only the reachable graph when
  it escapes its arena. This needs lifetime rules for every reference into
  the arena, and may cost more than it saves when intermediate versions are
  discarded or mutation in place is already possible.
- Exclusivity and some global loop analysis still go by name. Borrow
  invalidation, local return placement, and local loop carrying use resolved
  binding identities.
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
- Consider compiler-checked `pure` and `infallible` function annotations.
  Verify them transitively against called functions and operations; use
  proven purity and infallibility to enable safe dead-call elimination and
  common-subexpression elimination without changing observable effects or
  runtime errors.
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

### In progress: arrays typed by shape

Plan (agreed 2026-11-03; modelled on Futhark's size types). Static
shapes only *add* checks: the runtime keeps every shape check it has, so
memory safety never depends on them, and where a size isn't known the
fallback is today's run-time error.

**Syntax.** Dimensions follow a `;` (a token nowhere else; grammar.md §2
says `;` isn't one yet):

```ella
Array[f64]              # any rank, any sizes: today's meaning, unchanged
Array[f64; _, _]        # rank 2, sizes not tracked
Array[f64; N, 3]        # rank 2: N rows (a size name), exactly 3 columns
let matmul[T: Num; N, K, M] = (a: Array[T; N, K], b: Array[T; K, M]) -> Array[T; N, M]
let mean[; N] = (xs: Array[f64; N]) -> f64 => total(xs) / f64(N)
```

`Array[T;]` (no dimensions) is an error. Size parameters go after `;` in
a generic's brackets, so their kind is declared, not inferred.

**Rules.**

- A dimension is a literal (`3`), a size name (`N`) or `_`.
- In a local annotation, a size name's first use binds it to the value's
  real size, and later uses check against it:
  `let a: Array[f64; N1, N2] = load()` then
  `let b: Array[f64; N2, N1] = transpose(a)` checks statically. (A typo
  only binds a new name, losing a check, so this was accepted.)
- A size name is also an `i64` value in its scope: `for i in 0..N1`.
- Size names unify like type variables. Inside a function a size
  parameter is rigid: `N` against `3` or `M` is a static error. Sizes
  never cause specialization (lowering erases them).
- `_` fits anything without binding. Where a `_` (or unknown-rank) value
  reaches a place naming its sizes or rank (annotated `let`, argument,
  return), a run-time `check_shape` goes there: "array of shape [2, 3]
  where [N, 4] was expected". It also defines the size values.
- Broadcasting: line up from the right; each pair must unify, or one be
  the literal 1. A `_` side gives `_` (the run-time check decides);
  unknown rank on either side gives unknown rank. `[N, M] + [K]` with
  different names is a static error (use `_` to defer).
- Indexing checks the index count statically when the rank is known. In
  a slice an `i64` drops its dimension, a range gives `_`, dimensions left
  out keep their sizes.
- Library: `transpose: Array[T; N, M] -> Array[T; M, N]`; `matmul` as
  above (inner `K` mismatch is a compile error); `dot` needs equal
  shapes; `array(xs) -> Array[T; _]`; `fill`, `reshape` take a list and
  give unknown rank (annotate to get a shape; a tuple form
  `fill((N, M), x)` can come later); `total -> T`.
- New `sums(a, axis)`: sums over one dimension; `axis` must be a literal
  for a typed result: `Array[T; N1, N2]`, `1` -> `Array[T; N1]`.
- `arr.transpose()` (library functions as methods) is a separate feature.

**Implementation.**

- checker.py: size terms (literal, rigid param, unification var, `_`);
  an array type holding its dims or "unknown rank" (keep it
  `BuiltinTy("Array", (item, *dims))` so `array_item` and its ~30 users
  barely change; `unify` must handle size terms); the rules above;
  library types; a table (keyed by `id(node)`, like `updates`) of where
  run-time shape checks go and which size names they bind.
- parser.py / syntax.py: `;` in type arguments and type parameter lists
  (`Binding`/`EnumDef` get size params beside `type_params`).
- lower.py: erase dims when making IR types (the IR compares types
  exactly, e.g. ir.py's `dest in (BuiltinTy("Array", (F64,)), ...)`);
  emit `check_shape` (new IR op) and bind size values.
- `check_shape` and `sums` in every backend with identical messages:
  interpreter.py, ir_interpreter.py, codegen.py, runtime.c (+ library.py
  for `sums`).
- docs/grammar.md (§2 tokens, §9 Arrays, Generic functions, §10
  built-ins), editors/vscode grammar, tests, test/programs.

**Steps.**

1. Lexer: `;` token (SEMICOLON). *(done)*
2. Parser + AST: `Array[T; dims]` types and `[T; N]` parameter lists,
   with parser tests; `show()` output. *(done: `NamedType.dims`, a tuple
   of `Dim` (int, name or "_") or None for no `;`; `size_params` on
   `Binding` and `EnumDef`; `Parser.parse_type_params` reads both. The
   checker rejects both as "not supported yet" (`annotation`,
   `declare_enum`, `declare_function`); step 3 replaces those errors.)*
3. Checker, static only: size terms, rank and sizes, unification,
   library signatures, index counts, broadcasting; lowering erases dims.
   No run-time checks yet: `_` to named coercions are trusted.
   *(done. In checker.py: `SizeTy` (3, "N", or "_" = `ANY_SIZE`); an
   array type is `BuiltinTy("Array", (item, *dims))`, `array_dims` gives
   them (None: rank not tracked). Sizes not known yet are `TyVar`s.
   `unify`: `_` fits any size; a rank not tracked fits any rank (and makes
   the other side's undecided sizes `_`). `finish` erases dims, so
   lowering, the IR (its verifier `finish`es `LIBRARY` types) and the
   backends never see them. Size names share `type_params` with type
   parameters (`is_size`: a SizeTy or TyVar there); `param_types` takes
   `sizes`. `let_type`: a `let`'s new size name is a fresh TyVar that
   unification decides, or, if the value's size is `_`, a rigid
   `SizeTy(name)` (step 4 must check it at run time). `fresh_sizes` gives
   each use of a top-level function or library function fresh sizes
   (`named_sizes`, `replace_sizes`). `array_shapes` (broadcasting and
   `dot`), `array_index` (index counts, slice shapes), and `LIBRARY`'s
   `array`, `transpose`, `matmul`. Tests: test_checker.py
   `test_array_shapes`, `test_broadcasting_shapes`,
   `test_size_parameters` (`shape_of` shows types with shapes).
   Loose ends: structs/enums can't have size parameters yet (rejected in
   `declare_enum`); a let's rigid `SizeTy("N")` equals another function's
   `N` (harmless unless an inferred lambda type carries it out); an
   undecided size prints as `?6` in errors.)*
4. Run-time `check_shape` at the boundaries, size names as values.
5. `sums(a, axis)`.
6. Docs, editor grammar, test programs.

### Next

Small steps that build on the above:

1. Take a top-level function's missing types from its `let`'s declared
   type: `let inc: (i64) -> i64 = (x) => x + 1`.
2. A way for generic code to make the zero of a `Num` type, so that
   `sum(xs)` needs no start value. The plan:
   - Put it in `Num`, not `Add`. `Add` is met by arrays, whose zero
     depends on a shape the type doesn't know, and by any type with an
     `add` method, which would all have to give a zero too (and those
     that meet `Add` today would stop). Only the four number types meet
     `Num`, so each has an obvious zero and no code changes.
   - A library function `zero[T: Num]() -> T`, with `T` from where the
     result goes (`let mut total: T = zero()`): the checker already infers
     a type argument from the result alone. Nothing deciding it
     (`print(zero())`) is an error. Each use is specialized, so the
     lowering and backends need nothing new.
   - `sum` can't be both: there is no overloading, and `sum(xs, start)` is
     used on strings, lists, arrays and structs with `add` (prelude,
     constraints and methods test programs). Either `sum(xs)` takes only
     numbers (strings use `join`, lists `flatten`, the rest `fold`), the
     cleaner way, or the version without a start value gets another name.
   - Not a new `Zero` constraint yet: for user types to meet it they'd need
     a method called on the type, not on a value (`zero: () -> Self`),
     which constraints can't have. With that (and constraints requiring
     others, an open question in docs/grammar.md), `Num` could require
     `Zero`.
   - One function for lists and arrays (and ranges, generators, `iter`
     types) needs a constraint with an item type:
     `let sum[C: Iter[T], T: Num] = (xs: C) -> T`. Only the checker would
     change (working out `T` from `C`); `for` already compiles for each
     concrete type. `map`, `any`, `all` and `fold` could then take any of
     them. Arrays keep `total`, which fuses and runs as AVX2 loops.
3. Warnings for unused methods, enum variants and top-level variables.

### Done (2026-11-03): arrays and SIMD

1. `Array[T]`, for `i64` and `f64` items, of up to 32 dimensions, kept
   flat in row-major order: `array(xs)`, `fill(shape, x)`,
   `reshape(a, shape)`, `shape(a)`, `flat(a)`, `transpose(a)`,
   `matmul(a, b)`, `total(a)` and `dot(a, b)`; `a[i, j]` (the parser
   reads `a[i, j]` as `a[(i, j)]`, which works for `get` keys too) and
   `a[i, j] = x`; `+ - * /` and unary `-` item by item, with an array or a
   number on either side; `len`, `for`, `==`, `hash` and printing as nested
   lists. Arrays meet `Add`, so `sum` adds lists of them.
2. Native code runs arithmetic on arrays, `total` and `dot` as AVX2 loops
   that codegen writes (four items at a time, i64 overflow gathered as
   sign bits); `matmul` uses AVX2 intrinsics in the runtime. `total` and
   `dot` add in four sums, an order the language defines, so results are
   bit for bit the same in every backend. Indexing is inline
   (`array_offset`), allocating nothing. A program with AVX2 code checks
   the CPU has it when it starts.
3. Arenas now start at staggered offsets: two arenas a power of two apart
   holding arrays at the same offsets made a loop carrying an array ten
   times slower. A scope arena also keeps the pages it used the last time
   it was freed, so a loop making big arrays doesn't fault them in again.
   On 1,000-item arrays, `c = a * 2.0 + b - c` with `dot` runs 26× faster
   than the same loop over lists; on 1,000,000 items, where memory is the
   limit, 5.7×.
4. A `fuse` pass (last in the pipeline) makes arithmetic on arrays in a row
   one `fused` instruction: `a * 2.0 + b - c` is one loop that reads a, b
   and c once and makes no arrays in between, with masked loads and stores
   for the last n % 4 items. Up to 6 steps, 4 arrays and 3 numbers (the
   ymm registers); an operation joins only if nothing but constants is
   between it and the next, so errors still come in order, and the runtime
   finds the error by doing the steps one by one. The 1,000,000-item loop
   went from 0.47 s to 0.20 s, the `arrays` benchmark from 18 ms to 7 ms.
5. DCE no longer removes an unused `div` of arrays, which can fail, and
   a top-level definition that fails still hides a library function of the
   same name from later lines (only the error collection for editors saw
   the difference).

### Done (2026-11-03, second round): faster, wider arrays

1. Float results of arrays may now differ in their last bits between
   backends (docs/grammar.md §9, "Arrays"): native code uses fused
   multiply-adds (FMA) in fused loops (`a * b + c` is one `vfmadd231pd`),
   `dot` and `matmul`, and adds `total` and `dot` in sixteen sums at once
   (four vectors, so additions don't wait for each other). The interpreters
   stay the reference; i64 results and every error are still exactly the
   same everywhere. Programs that need agreement in every bit avoid
   inexact values in tests; the native fuzz tests compare floats with a
   tolerance.
2. `matmul` keeps a 4×8 block of the result in registers (FMA, each row of
   b loaded once for four rows): 20 products of 300×300 matrices take 0.07 s.
3. Fusion goes further: `total(...)` and (on floats) `dot(...)` end a fused
   loop, which adds as it goes and makes no array; a value used twice
   inside a group (`dot(d, d)`) joins it; and a value that later code also
   needs is *kept*: the loop writes it too, as an extra result of the
   `fused` instruction (`outs`), in its own region. Groups are formed
   without keeping first, then take in operations that would be left on
   their own: keeping greedily split a simulation's loop in two, with an
   array in between, and made it 1.7× slower.
4. Broadcasting, as in NumPy: arrays whose shapes line up from the right
   with sizes equal or 1 combine (`m + row`, `m * column`); natively a
   strided loop in the runtime (`ella_array_done`), and fused groups fall
   back to doing their steps one by one.
5. Slices: `a[1..3]`, `m[0, 1..3]`, `m[1..3]` (the last dimensions left
   out are whole); a new array (`array_slice`).
6. `f32` and `i32`: numbers of 32 bits (`to_f32`, `to_i32`; `to_f64` and
   `to_i64` take any number), with arithmetic rounded to f32 or trapping at
   32 bits, and `Array[f32]` and `Array[i32]`, whose items take 4 bytes (an
   array records its item kind): eight to an AVX2 vector, so f32 array
   arithmetic runs about twice as fast as f64's. f32 prints with the fewest
   digits that give it back (`0.1`). i32 arrays use AVX2 for `+` and `-`
   (overflow checked in 32-bit lanes); their `*`, `total`, `dot` and
   `matmul` are checked scalar code, and they aren't fused.
7. The 1,000,000-item loop is now 5.5× faster than over lists (0.22 s);
   the 1,000-item one about 18× (0.06 s).

Still open: `i8` and `i16`; fusing `i32` arithmetic and i64 `*` (AVX2 has
no multiply that notices overflow); broadcasting inside the fused loop
itself (shapes that differ take the step-by-step path); slices that are
views rather than copies, and assigning to slices; f32 literals (`to_f32`
is the only way to write one).

### Done (2026-11-02, tenth round): a bigger standard library

1. A `sets` module in the prelude: `Set[T]`, a `Map[T, ()]` inside, made
   by `new_set()` or `set_of(xs)`, with `insert`, `remove`, `in`, `len`,
   `for`, `items`, `union`, `intersection`, `difference` and `is_subset`,
   keeping items in the order they were first inserted.
2. List functions: `fold`, `flatten`, `flat_map` and `position`.
3. Math: the library functions `tan`, `asin`, `acos`, `atan`, `atan2`,
   `log10` and `log2` (NaN outside their domains, as in C, in every
   backend), and the constants `tau`, `e`, `inf` and `nan` in `math`.
4. `hash` works on any `Eq` type, `[T: Eq] (T) -> i64`, structurally
   (the runtime walks the type descriptor, as `==` does) and the same in
   every backend, so that equal values hash the same. `Map` and `Set`
   find keys by `hash` and `==` instead of building each key's text form:
   a map with `(i64, i64)` keys runs 1.8× faster natively, and `0.0` and
   `-0.0` are now one key. A separate `Hash` constraint turned out
   unneeded, since every `Eq` type can be hashed.
5. A `strings` module in the prelude: `repeat`, `lines`, `words`,
   `trim_start`, `trim_end`, `strip_prefix`, `strip_suffix`,
   `split_once`, `count_of`, `is_digit`, `is_alpha` and `is_space`.
6. More list functions: `first`, `last`, `index_of`, `min_of`, `max_of`,
   `chunks`, and `unique` (in `sets`, so that `lists` needs no other
   module).
7. `min_by`, `max_by` and `partition` on lists; `m.get_or(k, default)`,
   and `map_of`, `group_by` and `counts`, which make `Map`s. `group_by`
   adds items to lists in place.
8. `m.is_empty()`, `m.merge(other)`, `s.is_empty()` and
   `mut s.insert_all(xs)`.
9. Map and set literals: `{"a": 1, "b": 2}`, `{}` (an empty `Map`) and
   `{1, 2, 3}`. The parser makes `MapLit` and `SetLit`, which linking
   turns into calls of the library's `map_of` and `set_of` (whatever the
   program defines), so nothing after it changes. Braces are brackets,
   so newlines inside are ignored; an interpolation ends at `}}` only
   when no brace is open in it.
10. An `update` protocol method, `(mut Self, K, (mut V) -> ()) -> ()`, which
    `Map` has: an assignment line through an item of such a type runs as
    `mut v.update(k, (mut item) => item op= x)` (the checker makes that
    code, `Checker.updates`, which the interpreter and the lowering run
    instead). So `m[k] += [x]` grows the list in place, and parts of items
    can be assigned to: `m[k].count += 1`, `m[a][b] = x`. Lists grown
    in a map went from quadratic to linear: 400,000 appends to one went
    from 41 s to 0.01 s (benchmark `map_lists`).
11. Lambdas capture only the local variables their code names, rather
    than every variable in scope; each capture is a copy, so a lambda made
    next to a big local (such as the map `update` changes) no longer
    copies it.
12. A `text` protocol method, `(Self) -> str`, gives a struct's or enum's
    text form for `print`, `eprint` and interpolation, inside other values
    too; `Map` and `Set` print as their literals (`{"a": 1}`, `{1, 2}`).
    The lowering makes the text methods of the types a printed value may
    hold (`Module.texts`, specialized per type arguments); the IR
    interpreter follows the value's type to call them, and native code
    finds each in its type descriptor, which the runtime calls through
    `ella_call1` with a level of its own for the result. Error messages
    keep the built-in form, and a call shows as `called to make a value's
    text` in stack traces.
13. `eq` and `hash` protocol methods, done like `text` (`Module.eqs`,
    `Module.hashes`, and closures in type descriptors, called through
    `ella_call2` and `ella_call1`): `==`, `!=`, `in` on lists and `hash`
    use them, inside other values too, so `Map` keys and `Set` items are
    told apart by them. A type with `eq` must have `hash`. `Set` has both:
    sets are equal with the same items in any order. An empty `Set` is
    written, and prints, as `{/}`.

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
has list functions, `Map[K, V]` and `Set[T]`, with C runtime primitives underneath
(I/O, math, `hash`) added as library functions (src/ella/library.py and
the runtime). Growing it tests the language on real code, finds rough
edges in the type system, and makes example programs possible. Writing
`Map` and `Set` showed some: `sum` can't make a zero, `==` and printing can't be
customized, and there is no `Map.new()` (so `new_map()`). Now that it is
made of modules, it grows by topic (`lists`, `maps`, `sets`, `strings`,
`math`, `iters`).

### 2. Packages

Modules are done (see above): whole-program compilation, with generics and
methods specialized across modules, and compile-time code calling imported
functions. Packages, code from outside the program, are found on a package path
(`--path DIR`, `ELLA_PATH`; docs/grammar.md §3). What's left: importing
several names at once, re-exports, a way for a project to name its
packages (a manifest, rather than the environment), and perhaps separate
compilation of modules that don't change.

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
codegen indexes lists inline, calling the runtime only to report an index
out of range, which wins back the extra load.

### 6. SIMD and narrower number types

`Array[T]`, `f32` and `i32` are done (see "arrays and SIMD" above). Next:
`i8` and `i16`, fixed-size vector types (`f32x4`, ...), and AVX-512 where
the CPU has it.

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

Generators are a useful starting point for fibers: they already keep a
stackless frame and its arenas alive across `yield`. They are not concurrent
tasks yet; `next`/`yield` hands control directly between caller and generator,
without a scheduler.

Keep the design choices distinct:

- **Stackless vs. stackful:** stackless fibers fit generator frames and need
  explicit suspension points, but suspension must be represented through the
  call chain. Stackful fibers can suspend inside ordinary calls, but require
  saved stacks and make stack lifetime, cleanup, and memory use more complex.
- **Cooperative vs. preemptive:** cooperative tasks suspend at explicit
  points, keeping mutation and arena lifetimes easier to reason about, but
  code that does not suspend can delay other tasks. Preemption improves
  responsiveness but adds runtime complexity and can interrupt code in the
  middle of an operation.
- **Thread migration:** pinning a fiber to one OS thread keeps thread-local
  state and arena ownership simple. Migration can improve load balancing, but
  requires task-owned transferable arenas (or equivalent rules), and careful
  handling of thread-affine resources.

A natural first step is stackless, cooperative fibers, initially pinned to an
OS thread. Consider migration at safe suspension points if work stealing is
needed. Treat OS threads as workers for parallel execution, not as one thread
per fiber. Before implementing this, decide whether arenas are owned by tasks
or workers; they must never be concurrently shared.

### 9. Developer experience

A REPL built on the AST interpreter, `ella fmt`, LSP rename, references and
signature help, and DWARF line info so gdb can step through native programs.
Each is independent and fairly small.

### 10. Self-hosting (stretch)

Once ideas 1 and 2 exist, rewriting the tokenizer or parser in Ella would be
the ultimate test of the language, and a fun milestone.
