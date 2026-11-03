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
in patterns and more warnings). `make check` passes (933 tests, ruff,
strict mypy).

| Area | State |
| --- | --- |
| Front end | Tokenizer and Pratt parser for everything in grammar.md; lenient parsing for editors |
| Type checker | Inference by unification, generics with built-in and user-defined constraints (met structurally), methods, `mut` rules and exclusivity, `nocopy` rules, stages, exhaustive `match`, warnings (cases that never match, unused variables, code that never runs) |
| Interpreters | AST interpreter (the reference) and IR interpreter (with arena and memory checks); both agree on every test program |
| IR and optimizer | Typed SSA CFG with a verifier; mem2reg, constant folding, CFG simplification, DCE, copy-to-move |
| Native code | x64 assembly plus a C runtime with real arenas; byte-for-byte the same output and errors as the interpreters |
| Memory model | Value semantics without a GC: arenas per call and per loop iteration, compiler-chosen placement, pools with generation-checked handles, `nocopy` and finalizers |
| Staging | `quote` and `$` at any depth, compile-time splices, types as values, reflection, computed types (`new_struct`) |
| User types | Structs, enums, methods, operators (`add`, `compare`, ...), protocols (`get`, `set`, `iter`, `contains`, `len`) |
| Library | Conversions and parsing of numbers, math (`sqrt`, `floor`, `ceil`, `round`, `exp`, `ln`, `sin`, `cos`, `abs`, `min`, `max`), `ord`, `chr`, strings (`split`, `join`, `find`, `replace`, `trim`, `chars`, `upper`, `lower`, `starts_with`, `ends_with`, `pad_left`, `pad_right`, `format_f64`), slices, `read_line`, `read_file`, `write_file`, `args`, `env`, `exit`; all but `exit` usable as values; `print` and `eprint` with any number of values; a prelude in Ella with `map`, `filter`, `sort`, `reverse` and `pi` |
| Tooling | CLI with `--ir`, `--asm`, `--placement`, `--profile`, `--max-depth`, program arguments after `--`, exit status from `main`, warnings; language server (errors, warnings, hover, go to definition, semantic tokens, completion); VS Code extension; Makefile |

Rough speed, from `fib(32)` (about 3.5 million calls): the native executable
takes 0.05 s, about 4× slower than C at `gcc -O0`. A `fib(24)` run takes
1 s in the AST interpreter and 2 s in the IR interpreter; the IR one is
slower because it checks every memory access.

## Findings

Gaps found by writing small programs against the current build, plus
what the docs already list as open. ★ marks items that make ordinary
programs hard to write today.

### Standard library and I/O

The built-ins are `print`, `eprint`, `len`, `run`, the pool functions,
reflection, and the library (docs/grammar.md §10): number conversions and
parsing, math, code points, string functions, files, arguments, the
environment and `exit`. The prelude (src/ella/prelude.py) adds `map`,
`filter`, `sort`, `reverse` and `pi`, written in Ella.

- ★ No map or set type, and no map literal (`{` is an unexpected character).
  The protocols and the prelude mean a `Map[K, V]` could be written in Ella
  itself, given a hash function in the library.
- The prelude is small: no `sum`, `any`, `all`, `zip`, `enumerate`,
  `sort_by` with a key, or `range` with a step.
- `sort` piles up a copy of the list per merge pass in the caller's arena
  (42 MB for 200,000 numbers): the region bloat described under Memory
  model below.
- A run-time error inside prelude code (a stack overflow at a call it
  makes, say) reports a line and column of the prelude, as if they were
  the program's.
- `upper` and `lower` only change ASCII letters; the rest of Unicode would
  need case tables in the runtime.
- Missing math: `tan`, `atan2`, `log10`, `f64` powers other than `**`,
  and constants besides `pi`.
- A run-time error always gives status 1; there's no way to choose
  another.

### Language

- ★ Lambdas need full annotations (`x => x + 1` is rejected), which makes
  `map` and `filter` wordy: `map(xs, (n: i64) -> i64 => n * 2)`. Library
  and prelude functions are values, but `print`, `len` and constructors
  aren't (`let f = print`, `xs |> map(Some)`).
- Generic functions must be top-level; there are no generic lambdas.
- Only `i64`, `f64`, `bool` and `str`. Narrower integers, `f32` and SIMD
  vectors (a stated goal) aren't designed yet.
- No negative indexing (deliberate for now).
- The warning for cases that never match doesn't compare ranges with
  literals or each other (`case 1` after `case 0..5` isn't reported), and
  ranges never count as covering an `i64`. Unused parameters, top-level
  names and functions aren't reported, nor a `mut` variable that never
  changes.
- Native code has room for about two scope levels per nested call; a deep
  recursion through loops can run out of scope arenas before the depth
  limit, which the interpreters don't.
- Modules and `import`: one program is one file.
- `comptime` is still reserved, though `quote`/`$` may make it unnecessary.

### Types, methods and protocols

- `==` and the text form (`print`) can't be customized; both are structural
  and done by the runtime, so they would need callbacks into Ella code.
- `iter` builds a whole list; there is no lazy iterator protocol (`next`).
- No functions in a type's namespace that don't take a value
  (`Stack.new()`), and no methods on built-in types (`xs.len()`).
- Constraints can't require other constraints or operators
  (`constraint Ring: Add`), and `Num` is only met by `i64` and `f64`.
- Protocol method names (`get`, `set`, `iter`, `add`, ...) are reserved for
  their meaning, so they can't be ordinary methods.
- No generic computed types (`new_struct` makes non-generic structs only),
  and types can't be spliced into struct definitions.
- `Error` holds only a message; no location or cause.

### Memory model

From [docs/memory-model.md](docs/memory-model.md) §11, §12 and §15:

- Sub-arenas for groups of long-lived values that die together.
- Owning (non-copyable) handles, and handles that know their pool; a handle
  used with the wrong pool of the same type isn't caught.
- Region bloat: values rebuilt in a loop and kept outside it pile up in
  the outer arena.
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

- No register allocation: every IR value has a stack slot.
- No inlining, loop optimizations, common subexpression elimination or
  escape analysis; the optimizer is mostly cleanup.
- `iter` and copies allocate where a smarter lowering wouldn't.
- Only x64 Linux, through gcc.

### Tooling

- The language server has no rename, find references, formatting, signature
  help or code actions.
- No formatter, REPL or debugger support (no DWARF line info in native code).
- No benchmark suite to catch performance regressions; the timings above
  were measured by hand.
- Run-time errors show one location, with no stack trace.

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

### Next

Small steps that build on the above:

1. Lambda parameters without annotations where the type is known from
   the call: `map(xs, (n) => n * 2)`. The checker knows `map`'s parameter
   types before it checks the lambda, so this is checking with an
   expected type, not full inference.
2. More of the prelude: `sum`, `any`, `all`, `zip`, `enumerate`,
   `sort_by(xs, key)` and `range` with a step.
3. `print`, `len` and constructors as values (`map(xs, Some)`), made like
   the library functions' small IR functions.
4. Prelude spans: give prelude code its own file name in spans, so that an
   error inside it reads `<prelude>:12:5` rather than pointing into the
   program.
5. A `hash(str) -> i64` library function and a `Map[K, V]` in the
   prelude, the first real collection (★ above).
6. Warnings for unused functions and parameters, and for a `mut`
   variable that never changes.

## Roadmap ideas

Bigger directions, roughly in order of how much they would unlock. Each fits
Ella's goals: learning, simplicity first, no GC, x64.

### 1. A standard library written in Ella

The protocols, constraints and methods are now strong enough to write
collections in Ella itself: `Map[K, V]`, `Set[T]`, string utilities. The
prelude (src/ella/prelude.py) is the start: `map`, `filter`, `sort` and
`reverse` are Ella code that every program can use, with C runtime
primitives underneath (I/O, math) added as library functions
(src/ella/library.py and the runtime). Growing it tests the language on
real code, finds rough edges in the type system, and makes example
programs possible. Writing the prelude showed two: lambdas need full
annotations, and its code must avoid built-ins and name patterns, since a
program's own definitions would change them (modules would fix that). It
naturally leads to idea 2.

### 2. Modules and packages

`import` is reserved but undesigned. A simple design: one file is one
module, top-level names are exported unless private, and imports are
resolved relative to the file or a library path. Open questions are how
generics and methods are specialized across modules (probably still whole
program, since monomorphization needs all uses) and how staging sees
imported functions at compile time.

### 3. A real back end: register allocation and inlining

The IR is already SSA with a verifier, which is the right starting point.
Linear-scan register allocation would be a good learning project and should
give a several-fold speedup. Inlining small functions and methods (including
the protocol calls, which are tiny) would follow, then simple loop
optimizations. A benchmark suite in `test/` would keep this honest.

### 4. Error handling and diagnostics

Give `Error` a location and an optional cause, add stack traces for run-time
errors in all backends (the IR knows each call's span), and let `main`
return a `Result` or an exit code. Together with the exhaustiveness
checking that exists now, this makes failures much easier to understand.

### 5. Iterators and views without copying

memory-model §12 calls zero-copy views and iterators awkward. A lazy
iterator protocol (`next`), slices that borrow their list for a single
expression or loop, and second-class references in loops would remove many
copies and the list that `iter` builds. This touches the memory model's
core, so it is a design project first.

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
