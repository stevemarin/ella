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
uv run pytest -q -x    # tests (test/), about 2.5 minutes; -q -x keeps the output short
uv run ruff check      # lint
uv run ruff format     # format (line length 119)
uv run mypy            # strict type checking of src/ and test/
uv run ella FILE       # run a program (`-- ARGS` passes it arguments); --help lists --ir, --asm, --native, -O, ...
uv run ella-lsp        # language server over stdio (editors/vscode starts it)
make bench             # time the benchmarks (test/benchmarks) natively, with their arena peaks
make                   # lists the Makefile's recipes: check, lsp, install-extension, package, ...
```

Narrow runs, cheapest first. Run the narrowest one that covers a change, and
the whole suite once at the end:

```sh
make program NAME=arithmetic                        # one test program, all six ways (< 1 s)
uv run pytest -q test/test_programs.py -k "[NAME- and not native"  # the same, interpreters only
uv run pytest -q test/test_checker.py -k WORDS   # tests of one file whose names contain WORDS
uv run pytest -q --lf                            # only what failed last time
uv run pytest -q -x --tb=short                   # the whole suite, stopping at the first failure
uv run ella --tree F.ella; uv run ella F.ella; uv run ella --native -O F.ella  # by hand, per backend
```

Program test ids are `NAME-RUNNER`, the runners being `tree`, `ir`,
`optimized`, `native`, `native-optimized` and `native-spilling`.

## Reading the code

Each module's docstring explains it and has a **Map**: what is in it, by
concern, with the names to search for, and what it **must match** elsewhere
(runtime.c and the Python twin of each of its functions, regalloc and
codegen, the VS Code grammar). Read that first, then only the parts you need:

```sh
make outline FILE=src/ella/checker.py            # every class and function, with lines and first docstring line
uv run python tools/outline.py src/ella/lower.py Lowerer   # one class's methods
uv run python tools/outline.py src/ella/runtime.c          # C functions, with their comments
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

The pipeline, in order: lexer → parser → modules (linking) → defaults →
checker → staging (expand splices, check again) → interpreter, or lower →
passes → ir_interpreter, or regalloc → codegen → native (gcc + runtime.c).
All in `src/ella/`:

* `lexer.py`: `Span`, `EllaError` (one error type, with a run-time stack
  trace), `Token`, `tokenize`, with layout and string interpolation.
* `syntax.py`: AST node dataclasses, and `show()` for S-expressions.
* `parser.py`: Pratt parser following docs/grammar.md §8 and §12.
* `modules.py`: modules and imports (docs/grammar.md §3): `link` makes one
  program of a program's files, giving top-level names their module's name.
* `defaults.py`: constraints' default methods, made into generic functions.
* `checker.py`: the static type checker (inference, generics, constraints,
  `mut` rules, stages, nocopy, generators, array shapes, warnings). It
  records each node's type and meaning in tables keyed by `id(node)`.
* `exhaustive.py`: whether patterns cover every value of a type.
* `staging.py`: multi-stage programming (quotes, splices, reflection), and
  `prepare` = check + expand + check again; use it to run a program.
* `values.py`: run-time values, scopes, copying, equality, text form.
* `library.py`: the library functions on run-time values, for both
  interpreters; runtime.c's `ella_lib_NAME` must match them exactly.
* `lib/`: the standard library in Ella (`lists`, `iters`, `maps`, `sets`,
  `strings`, `math`). Their public names are the prelude, except where
  `iters` (`checker.LAZY`) shares a name with `lists`.
* `operators.py`: operator semantics on run-time values (arrays too), shared
  by both interpreters and matched by runtime.c.
* `interpreter.py`: the tree-walking interpreter, the reference for the
  language's behavior, and the compile-time evaluator for splices.
* `ir.py`: the IR (typed CFG); its docstring specifies every operation.
* `lower.py`: checked AST to IR, deciding memory: regions (arenas), copies,
  carried loop variables, views (docs/memory-model.md §14, §16, §18).
* `ir_interpreter.py`: runs IR, simulating and checking the arenas
  ("memory check failed" means a compiler bug), and `--profile`.
* `passes/`: optimization passes, one module each, run by `optimize()`.
  Passes must keep behavior identical, including run-time errors.
* `regalloc.py`: register allocation for codegen.
* `codegen.py`: IR to x64 GNU assembly (Intel syntax).
* `runtime.c`: the C runtime linked into native programs; it must match
  `operators.py` and `values.py` exactly, error messages and float
  formatting (Python's `repr`) included.
* `native.py`: builds executables with gcc, and runs them.
* `analysis.py`: what editors need (every error, symbols, completions),
  going on after errors.
* `lsp.py`: the language server (`ella-lsp`, needs the `lsp` extra: pygls).

Elsewhere:

* `editors/vscode/`: VS Code extension: TextMate grammar (keywords must
  match `KEYWORDS`, and built-in functions the checker's: see
  `test/test_vscode.py`), language configuration, and a client that starts
  `ella-lsp`.
* `tools/outline.py`: the outline above.
* Tests: one `test/test_MODULE.py` per stage, and `test_ir.py`
  (differential tests of both interpreters, the optimized IR and native
  code), `test_lsp.py` (starts `ella-lsp` as a client would), `test_modules.py`
  (programs of several files). Native tests need gcc and are skipped
  without it. `test/programs/` holds whole programs run six ways by
  `test_programs.py` (its README gives the files: `.out`, `.err`, `.in`,
  `.args`, `.stderr`, `.status`). `test/benchmarks/` holds benchmarks
  with a bound on their arena peak (`.peak`), checked by `test_benchmarks.py`
  and timed by `test/bench.py`.

Stage 7 (staged programming, structs, generics, pools, nocopy and
finalizers) is done, and so are modules; see README-Claude.md,
TODO-Claude.md and docs/memory-model.md §15 for what is still open.
