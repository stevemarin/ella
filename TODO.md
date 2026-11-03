# Ella: status and TODO

A review of where Ella stands on 2026-11-02, what is missing, and ideas for
what to build next. It complements [README.md](README.md) (goals and stage
history), the open questions in [docs/grammar.md](docs/grammar.md#open-questions),
and [docs/memory-model.md](docs/memory-model.md) §11, §12 and §15.

## Where things stand

All seven planned stages are done, and work since then added methods,
protocols, user-defined constraints and editor support. `make check` passes
(751 tests, ruff, strict mypy).

| Area | State |
| --- | --- |
| Front end | Tokenizer and Pratt parser for everything in grammar.md; lenient parsing for editors |
| Type checker | Inference by unification, generics with built-in and user-defined constraints (met structurally), methods, `mut` rules and exclusivity, `nocopy` rules, stages |
| Interpreters | AST interpreter (the reference) and IR interpreter (with arena and memory checks); both agree on every test program |
| IR and optimizer | Typed SSA CFG with a verifier; mem2reg, constant folding, CFG simplification, DCE, copy-to-move |
| Native code | x64 assembly plus a C runtime with real arenas; byte-for-byte the same output and errors as the interpreters |
| Memory model | Value semantics without a GC: arenas per call and per loop iteration, compiler-chosen placement, pools with generation-checked handles, `nocopy` and finalizers |
| Staging | `quote` and `$` at any depth, compile-time splices, types as values, reflection, computed types (`new_struct`) |
| User types | Structs, enums, methods, operators (`add`, `compare`, ...), protocols (`get`, `set`, `iter`, `contains`, `len`) |
| Tooling | CLI with `--ir`, `--asm`, `--placement`, `--profile`, ...; language server (errors, hover, go to definition, semantic tokens, completion); VS Code extension; Makefile |

Rough speed, from `fib(32)` (about 3.5 million calls): the native executable
takes 0.05 s, about 4× slower than C at `gcc -O0`. A `fib(24)` run takes
1 s in the AST interpreter and 2 s in the IR interpreter; the IR one is
slower because it checks every memory access.

## Findings

Gaps found by writing small programs against the current build, plus
what the docs already list as open. ★ marks items that make ordinary
programs hard to write today.

### Standard library and I/O

The only built-ins are `print`, `len`, `run`, the pool functions and
reflection. Most real programs stop here first.

- ★ No numeric conversions: `f64(3)`, `i64(3.7)`, parsing `"3"` into an
  `i64`. The no-implicit-conversion rule needs explicit ones to exist.
- ★ No input of any kind: stdin, files, command-line arguments, environment.
- ★ No string functions: split, join, find, replace, trim, upper and lower,
  characters and code points (`ord`, `chr`), formatting numbers.
- ★ No map or set type, and no map literal (`{` is an unexpected character).
  The new protocols mean a `Map[K, V]` could be written in Ella itself.
- No list helpers (sort, reverse, slicing `xs[1..3]`, `range` with a step)
  and no math functions (sqrt, abs, min, max, floor).
- `print` takes exactly one argument; printing several values needs a tuple.
- No exit codes: `main` must be `() -> ()`, and an error always exits with
  the same status.

### Language

- ★ No destructuring `let`: `let (a, b) = pair` is a parse error, so tuples
  have to be taken apart with `match`. Tuples also have no `.0` access.
- ★ Calls nest at most 1000 deep in every backend, native code included, so
  that all backends agree. Recursive code over deep data overflows early.
- `match` isn't checked for exhaustiveness; a missing case is a run-time
  error (`no case matches None`).
- A one-line function body that is an assignment has the assigned type,
  not `()`: `(mut xs: [i64]) -> () => xs += [1]` is a type error, while the
  same assignment as a block works. This is easy to trip over; consider
  giving assignments the value `()` everywhere.
- Lambdas need full annotations (`x => x + 1` is rejected), and built-ins
  and constructors can't be used as values (`let f = print`).
- Generic functions must be top-level; there are no generic lambdas.
- Only `i64`, `f64`, `bool` and `str`. Narrower integers, `f32` and SIMD
  vectors (a stated goal) aren't designed yet.
- No negative indexing or slicing (deliberate for now, but slicing is
  needed).
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
- Known bug: in the IR, a quote inside a function defined inside a splice
  can't refer to the variables of the quote the splice is in (the AST
  interpreter can), and the checker doesn't catch it.

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

Small, mostly independent changes that would make Ella much easier to use:

1. Conversion built-ins: `to_f64`, `to_i64` (truncating, with a trap on
   overflow), `parse_i64` and `parse_f64` returning `Result`.
2. Destructuring `let` with the pattern grammar that `match` and `for`
   already use, and tuple field access.
3. Read input: `read_line() -> Optional[str]`, `read_file(path) ->
   Result[str, Error]` and `args() -> [str]`. These are a few functions in
   the C runtime and in `values.py`.
4. String built-ins: `split`, `join`, `find`, `replace`, `trim`, `chars`,
   slicing.
5. Exhaustiveness checking for `match` over enums, `Optional` and `Result`.
6. Make assignments always have the value `()`, or document the one-line
   body rule prominently.
7. Raise the call depth limit, or make it a command-line option, keeping the
   backends in agreement.
8. Add the staging bug above to the checker as an error, until the IR
   supports it.

## Roadmap ideas

Bigger directions, roughly in order of how much they would unlock. Each fits
Ella's goals: learning, simplicity first, no GC, x64.

### 1. A standard library written in Ella

The protocols, constraints and methods are now strong enough to write
collections in Ella itself: `Map[K, V]`, `Set[T]`, sorting, string
utilities. Ship them as a prelude that every program sees, with a small
set of C runtime primitives underneath (I/O, hashing, math). This tests
the language on real code, finds rough edges in the type system, and makes
example programs possible. It naturally leads to idea 2.

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
return a `Result` or an exit code. Together with exhaustiveness checking,
this makes failures much easier to understand.

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
