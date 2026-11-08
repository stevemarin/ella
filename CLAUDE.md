# Ella

A statically typed language for learning and fun, implemented in pure Python
3.15. Native executables also link a small C runtime (`src/ella/runtime.c`),
built with gcc. See [README-Claude.md](README-Claude.md) for goals, design
and the status of each feature, and [TODO-Claude.md](TODO-Claude.md) for
open work, plans and the log of finished work.

* **Docs come in pairs.** [README.md](README.md) and [TODO.md](TODO.md)
  are short and written for people; README-Claude.md and TODO-Claude.md
  are the detailed versions, for you. When a change affects one, update
  its partner too (the human one only where a reader would notice).
  Ignore TEXTBOOK.md.

* **Language reference:** [docs/grammar.md](docs/grammar.md). It is the source
  of truth for syntax and semantics. If the implementation needs a rule to
  change, update the doc in the same change.
* **Syntax tour:** [example_syntax.ella](example_syntax.ella). It must always
  tokenize and parse; it is the stage 1 acceptance test.

## Commands

```sh
uv run pytest          # tests (test/)
uv run ruff check      # lint
uv run ruff format     # format (line length 119)
uv run mypy            # strict type checking of src/ and test/
uv run ella FILE       # run a program (`-- ARGS` passes it arguments); --help lists --ir, --asm, --native, -O, ...
uv run ella-lsp        # language server over stdio (editors/vscode starts it)
make bench             # time the benchmarks (test/benchmarks) natively, with their arena peaks
make                   # lists the Makefile's recipes: check, lsp, install-extension, package, ...
```

## Code style

The first attempt was restarted because tokens had too much structure (a class
per token type, a class per operator, `__init_subclass__` registries). Keep it
plain:

* One `Token` dataclass with a `TokenKind` enum, the token text/value, and a
  source span. Not a class hierarchy.
* AST nodes as `@dataclass(frozen=True, slots=True)`, grouped with
  `type Expr = ...` unions. Later stages use `match` over them.
* Operator precedence lives in data (a table on the parser instance), not in
  global class registries.
* Every token and AST node carries a source span (line, column) for error
  messages. Errors are one exception type carrying a message and span.
* Use modern Python freely (3.15: `match`, `type` aliases, PEP 695 generics,
  deferred annotations, so no `from __future__ import annotations`).
* Avoid module names that shadow the standard library at top level (e.g.
  prefer `lexer.py`, `syntax.py` over `token.py`, `ast.py`).
* Favor the simplest workable design, even if it uses more memory.

## Layout (stages 1 to 7 done)

* `src/ella/lexer.py`: `Span` (line, column, and a file: empty for the
  entry file, else the module's path from it, or `<std>/NAME.ella`),
  `EllaError` (a run-time one also has its stack trace of `CallSite`s, set
  by `traced`, and `format` prints it), `TokenKind`, `Token`,
  `tokenize(source, file)`. Handles comments, INDENT/DEDENT/NEWLINE layout, bracket
  depth (newlines ignored inside `()`/`[]`), number formats, escapes, and
  string interpolation (`STRING_START`, `STRING_TEXT`, `INTERP_START` …
  tokens … `INTERP_END`, `STRING_END`).
* `src/ella/syntax.py`: AST node dataclasses, and `show()` for S-expressions.
  `MapLit` and `SetLit` (`{k: v}`, `{x}`) only live until linking, which
  makes them calls of the library's `map_of` and `set_of`.
* `src/ella/parser.py`: Pratt parser following docs/grammar.md §8 and the
  parsing notes in §12. A file's `import` lines come first
  (`Program.imports`); paths in types and patterns are dotted names. A
  lambda's pattern parameter becomes a hidden parameter `#i` and a `let`
  first in its body, and each `Iter[T]` parameter of a `let`'s function a
  hidden type parameter `Iter#i` (`with_iter_params`; `syntax.is_hidden`).
* `src/ella/checker.py`: static type checker with simple inference
  (unification of type variables). Records each node's type and what each
  name refers to, in tables keyed by `id(node)`, for the lowering. Lambdas
  other than top-level functions and methods may leave out types: they get
  type variables, and their bodies (checked later, like every function's)
  are checked early when `known` needs a type they may decide. `print`,
  `len` and constructors used as values get a small lambda calling them
  (`wrappers`), which the lowering makes into a function. Also the
  `mut` argument rules and their exclusivity check, stages (each variable's
  level), generic functions, methods and constraints (built-in, and
  user-defined ones, met by `implements`, which can require others,
  `constraint_closure`, and constants, read as `T.NAME`:
  `associated_constants`, whose value each type's implementation gives,
  `implementation_constants`, compile-time code computed once by
  `staging.Expander.write_constant` and written in as a literal; the
  lowering inlines it in each specialization, and the interpreter binds a
  generic function's type arguments, `with_type_args`, to find it), the
  `update` protocol (an assignment
  line through an item of such a type becomes code that calls it,
  `updates`, which the interpreters and lowering run instead), the `text`, `eq` and `hash` protocols (a type's text form
  for `print`, its `==`, and its hash; `eq` needs `hash`),
  generators (a lambda with `yield`,
  `syntax.is_generator`; how each `for` loops is in `iterations`, "any"
  over an `Iter[T]` parameter, a `ParamTy` with an `item`, whose uses
  `settle_iterable` checks and whose loops the lowering decides per
  specialization, `iteration`), and the
  rules for `nocopy` values (`check_copies`; a call giving a `Gen`, an
  `Iter[T]` parameter and a `for` without `mut` take their nocopy
  arguments: new values, or variables given away at their last use,
  `give_away`), and array shapes
  (`Array[f64; N, 3]`: `SizeTy`, `array_dims`; `finish` erases them):
  where it can't be sure of one, a `ShapeCheck` (`shape_checks` by
  Lambda for its parameters and by Let, `result_shapes`) that the
  backends run through the hidden library function `match_shape`
  (`HIDDEN_LIBRARY`), defining size names as `i64` variables. Splices in types run while checking, through
  `compile_time` (set by `staging.Expander`). `LIBRARY` has the types of
  the library functions. `Checker.warnings` has the problems that don't
  stop a program: unused local variables, parameters and top-level
  functions, and `mut` variables that never change (`warn_unused`, skipped
  after an error), code after a line of type never, and cases that never
  match.
* `src/ella/exhaustive.py`: whether patterns cover every value of a type
  (`match` cases, and `let`/`for` patterns), with an example of one they
  miss, and which `match` cases can never match. The checker runs it once
  types are final; never-matching cases become warnings, which the CLI
  prints and editors show.
* `src/ella/staging.py`: multi-stage programming: building code from quotes
  (fresh names, constants, and splices of code, types and field names),
  reflection on types (`fields(t)`, ...), and expanding splices outside quotes
  and computing types' constants (`T.NAME`: a literal, or a global made
  first, `constant_globals`; editors compute them too) before lowering (`prepare` =
  check + expand + check again; use it rather than `check` to run a
  program).
* `src/ella/values.py`: run-time values (plain Python values where possible),
  scopes, copying, equality and text form. Shared by the interpreters, as
  are `MAX_CALL_DEPTH` (every backend's default call depth limit),
  `run_deep` (runs an interpreter on a thread with a big stack), `to_text`
  (whose `custom` calls `text` methods, for `print`; error messages leave it out),
  `equal` (likewise for `eq` methods; patterns leave it out),
  `make_error` (a built-in `Error`, whose `error` flag makes `==` and the
  text form leave out its `at`) and `main_error` (the error of an `Err`
  that `main` returns).
* `src/ella/library.py`: the library functions (`split`, `read_line`,
  `parse_i64`, ...) on run-time values, shared by both interpreters; the
  runtime's `ella_lib_NAME` functions must match them exactly (`abs`,
  `min` and `max` have one per number type: `ella_lib_abs_i64`, as do the
  array functions, `sums` and `match_shape`). `Host` has
  a program's standard input, arguments and standard error. `exit` raises
  `Exit`, which the interpreters catch at the top (finalizers don't run).
  A library function used as a value becomes a small IR function
  (`trim(str)`) made by the lowering.
* `src/ella/modules.py`: modules and imports (docs/grammar.md §3).
  `link` loads the modules a program's entry file imports (found from its
  directory, then from the package path: `--path` and `ELLA_PATH`,
  `package_path`; a package's module's file in spans is its path from the
  root, `../libs/json.ella`) and the library modules whose prelude names it uses, and
  makes one program of them, in the order their code runs: each module's
  top-level names get its name in front (`geo.area`; the entry file's stay
  bare), and every use of a top-level name becomes that name, tracking
  local scopes. A type defined inside a function gets a name of its own
  (`f#S`; `bare_name` prints `S`) and is moved to the top level in front of
  it (`Resolver.local_name`). It also checks private names (`_x`), imports and
  read-only variables of other modules; the checker checks private fields
  and methods. `prepare` and the interpreter's `run` call it; `Linker`
  takes more options, for editors. It also turns map and set literals into
  calls. Code of a module other than the entry
  file only has built-ins left as bare top-level names, so the checker and
  the interpreters give it those even if the entry file defines the names
  (`visible`, by the span's file). Values print with bare type names
  (`bare_name`).
* `src/ella/lib/`: the standard library, in Ella: `lists.ella` (`map`,
  `filter`, `zip`, `enumerate`, eager on any `Iter[T]`, and `sort_by`,
  `sum`, `fold`, ...), `iters.ella` (lazy: each takes an `Iter[T]` and
  gives a `Gen`: `iters.map`, ..., `each`, `count`, `take`, ...,
  `collect`), `maps.ella` (`Map[K, V]`, `new_map`), `sets.ella` (`Set[T]`
  on a `Map`, `new_set`, `set_of`, `unique`), `strings.ella` (`lines`,
  `words`, `split_once`, ...) and `math.ella` (`pi`, `e`, `inf`, `nan`,
  ...). Their public names are the prelude, which every module sees
  without importing them, except where `iters` (`checker.LAZY`) shares a
  name with `lists`; they can be imported too.
* `src/ella/operators.py`: operator semantics on run-time values, shared by
  both interpreters so results and error messages match exactly. Arrays
  (`ArrayValue` in values.py; `Array[T]` in the checker, `array_item`) work
  item by item here (broadcasting shapes, and slices), and their library
  functions are in library.py; `total` and `dot` add in four sums
  (`lane_sum`). Float results of arrays may differ in their last bits
  natively (FMA, other orders: docs/grammar.md §9); i64 and i32 ones and
  errors may not. `f32` and `i32` values are `F32` and `I32` (values.py).
* `src/ella/interpreter.py`: tree-walking interpreter over the AST (stage 2),
  the reference for the language's behavior. It copies eagerly, passes
  `mut` arguments in and out (value-result), and finalizes values through
  owners (one per call and loop iteration). A generator's body runs on a
  thread of its own, which `next` and `yield` hand control to and back.
  It is also the compile-time evaluator for splices.
* `src/ella/ir.py`: the IR (typed CFG: functions, blocks with parameters,
  three-address instructions, terminators), its printer and its verifier.
  The module docstring specifies every operation. A generator is a
  function that makes its `Gen` (`gen_new`) and a generator function, its
  body, which `resume` runs until a `yield` terminator; its frame and
  levels live in the Gen (docs/memory-model.md §17). A call's `within` is
  the function it is in, for stack traces, kept when inlining moves it.
* `src/ella/lower.py`: checked AST to IR (`compile_program`). Variables are
  local slots; lambdas are closure converted, capturing (copying) the
  variables their code names. It also decides memory: which
  region (arena) each object is made in, and where `copy` is needed, by
  tracking what each value borrows and how long it lives (see its docstring
  and docs/memory-model.md §14). A loop that gives outer variables new
  values carries them in two arenas that take turns (`swap`, `carry`;
  memory-model.md §16), and `x = y` moves y at its last use in its block.
  A slice (and `trim`, `split`: `VIEWS` in ella.ir) is a view sharing its
  list's items, borrowing what the list borrows (memory-model.md §18).
  Generic functions are specialized per type arguments, and so are methods
  (functions named `Type.method`); operators on structs with methods like
  `add` become calls. Printing a value makes the `text` methods of the
  types it may hold (`Module.texts`), and `==`, `in` and `hash` their `eq`
  and `hash` methods (`Module.eqs`, `hashes`), which the IR interpreter and
  the runtime (through type descriptors, `ella_call1` and `ella_call2`) call.
  `a[i, j]` on an array becomes `array_offset` (checking the indexes) and
  `index`/`set_index` at that offset.
* `src/ella/ir_interpreter.py`: runs IR. Simulates the arenas (scope arenas,
  and an arena per pool slot), runs finalizers, checks every use and store
  ("memory check failed" means a compiler bug; a list's view, `View`, checks
  its list's items haven't changed), and keeps the `--profile` numbers. It builds run-time code and lowers it into the module for `run`.
* `src/ella/passes/`: optimization passes, one module each (`mem2reg`,
  `direct`, `moves`, `fold`, `simplify`, `dce`, `inline`, `fuse`), each
  `pass(function, module) -> bool` (changed?).
  `optimize()` in `__init__.py` runs them, verifying the IR after every pass,
  then drops functions inlining left unused (unless the program has quotes).
  Passes must keep behavior identical, including run-time errors: never fold
  or remove an operation that can fail. `direct` turns calls of known
  functions into `call_direct`; `inline` copies small non-recursive
  functions into their callers (bottom up), their levels going above the
  caller's, with `enter`/`leave` so the call still counts toward the depth
  limit. `fuse` (last) makes arithmetic on arrays in a row (maybe ending in
  `total` or `dot`) one `fused` instruction, one AVX2 loop natively (with
  FMA), whose errors the runtime finds by doing its steps one by one; a
  step whose array later code needs is kept (an extra result, `outs`).
* `src/ella/regalloc.py`: register allocation for codegen: liveness, then
  greedy coloring in dominator-tree order (the IR is SSA); a value with no
  free register spills to a stack slot. Values live across a call (`calls`,
  `calls_partway`, which must match codegen) get callee-saved registers
  (rbx, r12-r15), others caller-saved ones (rsi, rdi, r8-r10); rax, rcx,
  rdx, r11 are codegen's scratch. Generator functions get none. Arithmetic
  on arrays, `total` and `dot` call partway, and their AVX2 loops use the
  caller-saved registers (and ymm registers, which no value is ever in).
* `src/ella/codegen.py`: IR to x64 GNU assembly (Intel syntax). Each value
  in its register or a stack slot, Ella's own calling convention (arguments
  pushed; see the module docstring), C calling convention for runtime calls
  (arguments moved all at once, `parallel_move`). A generator
  function's frame is in its Gen, addressed from `r12`. A source line passed
  to the runtime has its file's number (in the table `ella_files`) above
  its low 32 bits, for errors in modules.
* `src/ella/runtime.c`: the C runtime linked into native programs. It must
  match `operators.py` and `values.py` exactly, including error messages and
  float formatting (Python's `repr`). A list's or string's header points
  at its items (right after it, or another's in a view); a tuple's are in
  it. Objects live in arenas, all in one reserved address range (so an
  object's arena follows from its address), bump allocated, freed by scope
  or pool slot, with finalization lists. Each call puts its site in
  `ella_trace` at its call depth, which `fail` prints as the stack trace.
* `src/ella/native.py`: builds executables with gcc (the compiled runtime is
  cached in the temp directory) and runs them (`run_native`, which sets
  `ELLA_POISON` so freed arena memory is overwritten).
* `src/ella/analysis.py`: what editors need, as plain data: every error,
  symbols (hover text, definition, semantic token kind per name), and
  completions. A file is linked with its imports (`Place`: its path, the
  workspace root, open documents' text); errors in an imported module are
  shown at the import line, and definitions can be in other files. It keeps going after errors: it patches lexer errors in the
  text (`tokenize_leniently`), and uses `parse_partial` (a line with an error
  becomes `()`) and `Checker.errors` (each top-level step and block line
  records its error and goes on; `scopes` gives each name's scope). Both are
  opt-in: `parse` and `check` still stop at the first error.
* `src/ella/lsp.py`: the language server (`ella-lsp`, needs the `lsp` extra:
  pygls): diagnostics, hover, definition, semantic tokens, completion. The
  analysis runs in a worker process that's killed after a timeout, since
  compile-time code may not finish; between analyses, symbols follow edits
  (`follow_edits`).
* `editors/vscode/`: VS Code extension: TextMate grammar (keywords must match
  `KEYWORDS`, and built-in functions the checker's, see
  `test/test_vscode.py`), language configuration, and a
  client that starts `ella-lsp`.
* Tests: `test/test_lexer.py`, `test/test_parser.py`, `test/test_checker.py`,
  `test/test_interpreter.py`, `test/test_ir.py` (including differential tests
  of both interpreters, the optimized IR and native code), `test/test_passes.py`,
  `test/test_native.py`, `test/test_regalloc.py`, `test/test_cli.py`, `test/test_analysis.py`,
  `test/test_staging.py`,
  `test/test_lsp.py` (starts `ella-lsp` as a client would),
  `test/test_vscode.py`, `test/test_modules.py` (programs of several
  files). Native tests need
  gcc and are skipped without it. `test/programs/` holds
  `NAME.ella` programs with `NAME.out` (expected output) and optionally
  `NAME.err` (expected error, with its stack trace), `NAME.in` (standard input), `NAME.args`
  (arguments, one per line), `NAME.stderr` (expected standard error) and
  `NAME.status` (expected exit status), run by `test/test_programs.py` through both
  interpreters, the optimized IR, and native code (plain, optimized, and
  optimized with one register of each kind, so values spill). A
  program of several modules is a directory `NAME/` with `main.ella`
  (and `main.out`, ...). `test/benchmarks/` holds `NAME.ella` with
  `NAME.out` and `NAME.peak` (a bound on its arena peak, in bytes), checked
  natively by `test/test_benchmarks.py`; `test/bench.py` times them.

Stage 7 (staged programming, structs, generics, pools, nocopy and
finalizers) is done, and so are modules; see README-Claude.md,
TODO-Claude.md and docs/memory-model.md §15 for what is still open.
