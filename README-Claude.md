# Ella: design and status (for Claude)

The detailed companion to [README.md](README.md), which is the short,
human-readable introduction: keep the two in step. This file has the
design of the compiler, the status of each stage and feature area, and
the CLI. [CLAUDE.md](CLAUDE.md) has the commands, code style and a map of
the source files; [TODO-Claude.md](TODO-Claude.md) has the open work and
the log of finished rounds.

Source-of-truth documents:

* [docs/grammar.md](docs/grammar.md): the language (syntax and semantics).
  Update it in the same change as any rule the implementation changes.
* [docs/memory-model.md](docs/memory-model.md): the memory design (arenas,
  placement, loops, pools, generators, views) and its limits.
* [example_syntax.ella](example_syntax.ella): the syntax tour; it must
  always tokenize and parse (stage 1's acceptance test).
* [docs/summaries/](docs/summaries/): short developer notes per subsystem
  (pipeline, types, staging, generators, arrays, modules, memory).
* [docs/ideas/](docs/ideas/): explorations not yet planned (const
  generics, JIT, SQLite).
* [GETTING_STARTED.md](GETTING_STARTED.md): the tutorial-style introduction
  for users. [TEXTBOOK.md](TEXTBOOK.md) is the user's own; leave it alone.

## Goals

* Keep the implementation as simple as possible without hiding important ideas.
* Use a lightweight, extensible syntax and grammar.
* Learn how interpreters, IRs, optimization, and native code generation fit together.
* Explore memory safety without GC.
* Compile to x64 on Linux (64-bit only).
* Leverage modern Python (3.15) in the design.
* All user-facing integer types are signed.
* Everything is an expression; only booleans are truthy.
* No implicit type conversions.
* Pass-by-value semantics; bindings are immutable unless marked `mut`.
* Built-in arrays of numbers, which native code works on with SIMD (AVX2).

When trading off, prefer simplicity, then speed, then memory.

## Design

### Compiler pipeline

```text
source → tokenizer → parser → AST → linker → checker → staging → checker → lowering → IR ──→ IR interpreter
                                                     (splices)       ↓                └→ optimizer → IR → x64 assembly
                                                                 AST interpreter                          → native executable
                                                                                                          (+ C runtime, gcc)
```

Start with one IR. Add another representation only when a later lowering needs
to make a real change in abstraction, such as replacing structured operations
with explicit memory and machine-level operations.

The IR is a typed control-flow graph (specified in
[src/ella/ir.py](src/ella/ir.py)):

* Functions contain basic blocks.
* Blocks contain simple, three-address instructions and end in a terminator
  (`branch`, `jump`, `return`, or `trap` for run-time errors).
* Values have explicit types and unique IDs, and never change.
* Control-flow joins use block parameters: `jump join(%4)` passes a value to
  `join(%5: i64)`. They do the job of SSA `phi` nodes.
* The lowering keeps variables in named, typed local slots (`load`/`store`).
  The mem2reg optimization pass then promotes them to values, giving full SSA
  form; that is easier as a pass than in the lowering.
* Lists and structs have value semantics, but their objects can be changed
  in place. The lowering inserts explicit copies where needed to preserve
  value semantics.
* Lambdas are closure converted: each becomes an IR function taking its
  captured variables as extra parameters.
* Side effects such as calls, printing and global writes are explicit operations.

The IR interpreter defines and exercises the meaning of these operations. The
IR verifier checks structural, dominance and type rules before interpretation,
optimization, or code generation. Optimization passes operate on the IR and
must preserve its behavior.

### Staged programming

Staging is separate from the number of compiler passes. Ella programs are
*multi-stage*: `quote e` is the code of `e` as a value, for a later stage,
and `$c` splices code into a quote, one stage earlier. Stages nest without
limit in both directions. A splice outside any quote runs while compiling,
and the code it gives becomes part of the program; code built at run time
runs with `run`. Compile-time code is evaluated by the AST interpreter, and
only the residual code it produces reaches the IR, so the boundary is
explicit in the language rather than tied to compiler passes. See
[docs/grammar.md](docs/grammar.md) §11 and
[src/ella/staging.py](src/ella/staging.py).

Quoted binders and references carry fresh identities through staging, so
spliced code cannot accidentally capture a generated variable. The existing
checker and runtime receive collision-free internal names after expansion;
source names remain available for display and diagnostics. Examples include
[generated struct helpers](examples/example_staged_derive.ella) and
[JSON shape validation using reflection and staging](examples/example_staged_json_validation.ella),
which also exercises hygienic identity by generating a local that shares a
spelling with caller code. The JSON validation is implemented in Ella rather
than as compiler or built-in schema support. Runtime `Symbol` values remain
future work.

## Stages

All seven planned stages are done.

1. **Front end.** The tokenizer ([src/ella/lexer.py](src/ella/lexer.py))
   and Pratt parser ([src/ella/parser.py](src/ella/parser.py)) turn source
   into the syntax tree in [src/ella/syntax.py](src/ella/syntax.py), for
   everything in docs/grammar.md. Editors get lenient versions
   (`tokenize_leniently`, `parse_partial`).
2. **Source interpreter.** The tree-walking interpreter
   ([src/ella/interpreter.py](src/ella/interpreter.py), values in
   [src/ella/values.py](src/ella/values.py)) is the reference for program
   behavior and also evaluates compile-time splices.
3. **Checker, IR and IR interpreter.** The type checker
   ([src/ella/checker.py](src/ella/checker.py)) works out every
   expression's type; the lowering ([src/ella/lower.py](src/ella/lower.py))
   turns the checked tree into IR ([src/ella/ir.py](src/ella/ir.py), with
   the printer and verifier), which the IR interpreter
   ([src/ella/ir_interpreter.py](src/ella/ir_interpreter.py)) runs, checking
   every memory access.
4. **Optimization.** Passes in [src/ella/passes/](src/ella/passes/):
   `mem2reg`, `direct` (calls of known functions), `moves` (copy to move),
   `fold`, `simplify`, `dce`, `inline` and `fuse` (array arithmetic as one
   loop). The IR is verified after every pass.
5. **Native code.** The code generator
   ([src/ella/codegen.py](src/ella/codegen.py)) turns IR into x64 assembly,
   with SSA register allocation ([src/ella/regalloc.py](src/ella/regalloc.py):
   greedy coloring in dominator-tree order), which gcc links with the C
   runtime ([src/ella/runtime.c](src/ella/runtime.c)) through
   [src/ella/native.py](src/ella/native.py). Ella functions take their
   arguments on the stack. Output and errors match the interpreters byte
   for byte (float results of arrays may differ in their last bits; see
   grammar.md §9).
6. **Memory model.** Value semantics without a GC: copies only where a
   program could tell, in-place mutation, `mut` parameters, an arena per
   call and per loop iteration, compiler-chosen placement, loops that carry
   values in two arenas taking turns, views for slices, pools with
   generation-checked handles, explicit arenas freed as a unit, `nocopy`
   types and finalizers. See [docs/memory-model.md](docs/memory-model.md)
   §14 to §18.
7. **Staged programming.** `quote` and `$` at any depth, compile-time
   splices ([src/ella/staging.py](src/ella/staging.py)), types as values,
   reflection, computed types (`new_struct`), hygienic generated names, and
   `run` for code built at run time (interpreters only). Structs, generic
   functions, pools and `nocopy` came with this stage.

Every program in [test/programs/](test/programs/) runs through both
interpreters, the optimized IR, and native code (plain, optimized, and
optimized with one register of each kind so values spill), and must give
the same output, errors, standard error and exit status.

## Features since the stages

Grouped by area; [TODO-Claude.md](TODO-Claude.md) has the dated log.

* **Types and methods.** Structs and enums have methods (`p.norm()`,
  `mut s.push(3)`), which give them operators (`add`, `compare`, ...),
  protocols (`len`, `get`, `set`, `update`, `iter`, `next`, `contains`,
  `text`, `eq`, `hash`) and type-namespace functions (`Stack.new()`).
  Compatible prelude functions can be called on their first argument
  (`xs.map(f)`, `" text ".trim()`). A type defined inside a function is
  moved to the top level by linking (`f#S`).
* **Generics and constraints.** Generic functions with built-in
  constraints (`Eq`, `Ord`, `Add`, `Num`, ...) and user-defined ones
  (`constraint Shape`), which a type meets only by declaring
  `implements Shape`. Constraints can require others
  (`constraint Ranked: Named + Ord`) and constants (`const EMPTY: Self`,
  read in generic code as `T.EMPTY`, computed once at compile time), also
  through a struct's own type parameters (`struct Acc[T: Monoid]`). The
  built-in `Zero` (implied by `Num`) has one, `T.ZERO`: built in for
  numbers, `str` and lists (`checker.BUILTIN_CONSTANTS`, made where read by
  `lower_builtin_constant`), and given by a struct or enum that implements
  it; `sum(xs)` starts from it.
  Generic code is specialized per use: no dynamic dispatch. Local
  `let`-bound generic functions can capture local values. A parameter of
  type `Iter[T]` takes anything a `for` loop can go through (list, str,
  range, array, `Gen`, iterator, a type with `iter`): the parser gives each
  one a hidden type parameter (`Iter#0`, `parser.with_iter_params`; the
  checker's `ParamTy.item`, `expect_iterables`/`settle_iterable`), so the
  function is specialized per source type, and the lowering picks how each
  loop over it goes from the concrete type (`Checker.iteration`). Such a
  value may be a generator, so it can't be copied; arguments for it are
  taken (`CopyChecker.walk_iterable`). `lists`' `map`, `filter`, `zip`
  and `enumerate`, and every function of `iters`, take one.
* **Inference.** Lambdas other than top-level functions and methods can
  leave out their types (`xs |> map(n => n * 2)`); `print`, `len` and
  constructors are values (`map(xs, Some)`).
* **Pattern parameters.** A lambda parameter can be a pattern
  (`((a, b)) => a + b`, `(Point(x, y): Point) -> f64 => ...`): the parser
  makes it a hidden parameter (`#0`) and a `let` at the start of the body
  (`parse_param`, after a look ahead for `) =>`: `closes_before_arrow`).
  Each `where` condition that uses its names gets its own copy of the
  `let`, keeping only those names (`take_apart`), so warnings stay right.
* **Arrays.** `Array[T]` of `i64`, `i32`, `f64` or `f32`, any rank,
  broadcasting, slices (copies), `matmul`, `transpose`, `total`, `dot`,
  `sums` (along one axis); AVX2 loops natively, fused with `-O`. Shapes in
  types (`Array[f64; N, 3]`, size parameters `[; N]`) are checked by the
  checker, and at run time where it can't be sure (parameters on entry,
  results, `let`s: the checker's `shape_checks`, run through the hidden
  library function `match_shape`); size names are `i64` variables
  (`for i in 0..N`). Arrays inside other values are trusted
  (grammar.md §9, "Static shapes").
* **Generators.** A function with `yield` gives a `Gen[T]`; `for` and
  `next(mut g)` drive it; its frame and arenas live in the `Gen`
  (memory-model.md §17). Eager by default: `lists`' `map`, `filter`, `zip`
  and `enumerate` give lists. `iters` has the lazy ones (`iters.map`, ...:
  their names give way to `lists`' in the prelude and for method calls,
  `modules.prelude`, `checker.LAZY`) and `each`, `count`, `take`, `skip`,
  `take_while`, `chain`, `scan`, `collect`, which are in the prelude.
* **Giving away.** A local variable that can't be copied (a `Gen`, ...)
  can be given away at its last use, to a parameter that takes it (a
  generator's or an `Iter[T]` one) or a `for` without `mut`: the copy
  checker's `give_away` (not across a loop it is declared outside of;
  then any use is an error, `check_not_given`; branches are merged). A
  generator's own parameters, `Iter[T]` parameters (which take their
  arguments), and top-level variables no function uses, can be too. The
  lowering already handled such values as moves.
* **Modules and packages.** A file is a module (`import geo.poly`, `as`,
  imports of one item); `_names` are private; other modules' variables are
  read-only; packages are found on `--path` and `ELLA_PATH`. Errors name
  each module's file (`geo/poly.ella:3:5`, `<std>/maps.ella:12:5`).
* **Errors.** Stack traces for run-time errors in every backend; `Error`
  with a `cause` and location; `main` may return an exit status or a
  `Result`; `?` propagates errors.
* **Calls.** 10,000-deep calls by default (`--max-depth`); direct self
  tail calls reuse the frame (grammar.md §5, "Tail recursion").
* **Library.** Built-ins in [src/ella/library.py](src/ella/library.py)
  (matched by `ella_lib_*` in the runtime): number conversion and parsing,
  math, code points, strings, `hash`, files, arguments, environment,
  `exit`. The standard library in Ella ([src/ella/lib/](src/ella/lib/)):
  `lists`, `maps` (`Map[K, V]`), `sets` (`Set[T]`), `strings`, `math`
  and `iters` (all in the prelude). Map and set literals
  (`{"a": 1}`, `{1, 2}`, `{/}` for an empty set).
* **Warnings.** Cases that never match, unused variables, parameters,
  functions and imports, `mut` that isn't needed, code that never runs,
  printing a library function's generator (`<gen>`), and an eager `map`
  (or `filter`, `zip`, `enumerate`, `collect`) of a generator without end.
* **Tooling.** The language server ([src/ella/lsp.py](src/ella/lsp.py)):
  errors and warnings as you type, hover, go to definition, semantic
  tokens, completion, across imported modules and open documents. The VS
  Code extension in [editors/vscode/](editors/vscode/). Benchmarks with
  arena-peak bounds in [test/benchmarks/](test/benchmarks/) (`make bench`).

## Command line

```sh
uv run ella example_syntax.ella           # check and run a program (through the IR)
uv run ella --tree example_syntax.ella    # check and run it with the AST interpreter
uv run ella -O example_syntax.ella        # optimize the IR, then run it
uv run ella --native example_syntax.ella  # compile to native code and run it (add -O to optimize)
uv run ella -o tour example_syntax.ella   # compile to a native executable named tour
uv run ella --asm example_syntax.ella     # print the x64 assembly
uv run ella --ir example_syntax.ella      # print the IR (add -O to optimize it,
                                          # or --passes mem2reg,fold to pick passes)
uv run ella --placement example_syntax.ella  # print which arena each object goes in
uv run ella --profile example_syntax.ella    # run it, then print how much memory each arena used
uv run ella --ast example_syntax.ella     # print the syntax tree
uv run ella --expanded example_syntax.ella  # print it after running the splices outside quotes
uv run ella --tokens example_syntax.ella  # print the tokens
uv run ella prog.ella -- a b              # run a program with the arguments a and b
uv run ella --max-depth 100000 prog.ella  # let functions call each other deeper than 10,000
uv run ella --path ../libs prog.ella      # also find packages in ../libs (or set ELLA_PATH)
```

Native programs need gcc, and can't run code built at run time.
`uv run ella --help` lists every option; `make` lists the Makefile's
recipes (`check`, `bench`, `lsp`, `install-extension`, `package`, ...).
