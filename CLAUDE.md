# Ella

A statically typed language for learning and fun, implemented in pure Python
3.15. Native executables also link a small C runtime (`src/ella/runtime.c`),
built with gcc. See [README.md](README.md) for goals and roadmap.

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

* `src/ella/lexer.py`: `Span`, `EllaError`, `TokenKind`, `Token`,
  `tokenize(source)`. Handles comments, INDENT/DEDENT/NEWLINE layout, bracket
  depth (newlines ignored inside `()`/`[]`), number formats, escapes, and
  string interpolation (`STRING_START`, `STRING_TEXT`, `INTERP_START` …
  tokens … `INTERP_END`, `STRING_END`).
* `src/ella/syntax.py`: AST node dataclasses, and `show()` for S-expressions.
* `src/ella/parser.py`: Pratt parser following docs/grammar.md §8 and the
  parsing notes in §12.
* `src/ella/checker.py`: static type checker with simple inference
  (unification of type variables). Records each node's type and what each
  name refers to, in tables keyed by `id(node)`, for the lowering. Also the
  `mut` argument rules and their exclusivity check, stages (each variable's
  level), generic functions, methods and constraints (built-in and
  user-defined, met structurally), and the rules for `nocopy` values
  (`check_copies`). Splices in types run while checking, through
  `compile_time` (set by `staging.Expander`). `LIBRARY` has the types of
  the library functions.
* `src/ella/exhaustive.py`: whether patterns cover every value of a type
  (`match` cases, and `let`/`for` patterns), with an example of one they
  miss. The checker runs it once types are final.
* `src/ella/staging.py`: multi-stage programming: building code from quotes
  (fresh names, constants, and splices of code, types and field names),
  reflection on types (`fields(t)`, ...), and expanding splices outside quotes
  before lowering (`prepare` = check + expand + check again; use it rather
  than `check` to run a program).
* `src/ella/values.py`: run-time values (plain Python values where possible),
  scopes, copying, equality and text form. Shared by the interpreters, as
  are `MAX_CALL_DEPTH` (every backend's default call depth limit) and
  `run_deep` (runs an interpreter on a thread with a big stack).
* `src/ella/library.py`: the library functions (`split`, `read_line`,
  `parse_i64`, ...) on run-time values, shared by both interpreters; the
  runtime's `ella_lib_NAME` functions must match them exactly. `Host` has a
  program's standard input and arguments.
* `src/ella/operators.py`: operator semantics on run-time values, shared by
  both interpreters so results and error messages match exactly.
* `src/ella/interpreter.py`: tree-walking interpreter over the AST (stage 2),
  the reference for the language's behavior. It copies eagerly, passes
  `mut` arguments in and out (value-result), and finalizes values through
  owners (one per call and loop iteration). It is also the compile-time
  evaluator for splices.
* `src/ella/ir.py`: the IR (typed CFG: functions, blocks with parameters,
  three-address instructions, terminators), its printer and its verifier.
  The module docstring specifies every operation.
* `src/ella/lower.py`: checked AST to IR (`compile_program`). Variables are
  local slots; lambdas are closure converted. It also decides memory: which
  region (arena) each object is made in, and where `copy` is needed, by
  tracking what each value borrows and how long it lives (see its docstring
  and docs/memory-model.md §14). Generic functions are specialized per type
  arguments, and so are methods (functions named `Type.method`); operators
  on structs with methods like `add` become calls.
* `src/ella/ir_interpreter.py`: runs IR. Simulates the arenas (scope arenas,
  and an arena per pool slot), runs finalizers, checks every use and store
  ("memory check failed" means a compiler bug), and keeps the `--profile`
  numbers. It builds run-time code and lowers it into the module for `run`.
* `src/ella/passes/`: optimization passes, one module each (`mem2reg`,
  `moves`, `fold`, `simplify`, `dce`), each `pass(function, module) -> bool`
  (changed?).
  `optimize()` in `__init__.py` runs them, verifying the IR after every pass.
  Passes must keep behavior identical, including run-time errors: never fold
  or remove an operation that can fail.
* `src/ella/codegen.py`: IR to x64 GNU assembly (Intel syntax). A stack slot
  per value, Ella's own calling convention (arguments pushed; see the module
  docstring), C calling convention for runtime calls.
* `src/ella/runtime.c`: the C runtime linked into native programs. It must
  match `operators.py` and `values.py` exactly, including error messages and
  float formatting (Python's `repr`). Objects live in arenas, all in one
  reserved address range (so an object's arena follows from its address),
  bump allocated, freed by scope or pool slot, with finalization lists.
* `src/ella/native.py`: builds executables with gcc (the compiled runtime is
  cached in the temp directory) and runs them (`run_native`, which sets
  `ELLA_POISON` so freed arena memory is overwritten).
* `src/ella/analysis.py`: what editors need, as plain data: every error,
  symbols (hover text, definition, semantic token kind per name), and
  completions. It keeps going after errors: it patches lexer errors in the
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
  `test/test_native.py`, `test/test_cli.py`, `test/test_analysis.py`,
  `test/test_lsp.py` (starts `ella-lsp` as a client would),
  `test/test_vscode.py`. Native tests need
  gcc and are skipped without it. `test/programs/` holds
  `NAME.ella` programs with `NAME.out` (expected output) and optionally
  `NAME.err` (expected error), `NAME.in` (standard input) and `NAME.args`
  (arguments, one per line), run by `test/test_programs.py` through both
  interpreters, the optimized IR, and native code (plain and optimized).

Stage 7 (staged programming, structs, generics, pools, nocopy and
finalizers) is done; see README.md and docs/memory-model.md §15 for what is
still open.
