# Ella

A statically typed language for learning and fun, implemented in pure Python
3.15. See [README.md](README.md) for goals and roadmap.

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
uv run ella            # CLI entry point (src/ella/__init__.py:main)
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

## Layout (stages 1 to 4 done)

* `src/ella/lexer.py`: `Span`, `EllaError`, `TokenKind`, `Token`,
  `tokenize(source)`. Handles comments, INDENT/DEDENT/NEWLINE layout, bracket
  depth (newlines ignored inside `()`/`[]`), number formats, escapes, and
  string interpolation (`STRING_START`, `STRING_TEXT`, `INTERP_START` …
  tokens … `INTERP_END`, `STRING_END`).
* `src/ella/syntax.py`: AST node dataclasses, and `show()` for S-expressions.
* `src/ella/parser.py`: Pratt parser following docs/grammar.md §8 and the
  parsing notes in §11.
* `src/ella/checker.py`: static type checker with simple inference
  (unification of type variables). Records each node's type and what each
  name refers to, in tables keyed by `id(node)`, for the lowering.
* `src/ella/values.py`: run-time values (plain Python values where possible),
  scopes, copying, equality and text form. Shared by the interpreters.
* `src/ella/operators.py`: operator semantics on run-time values, shared by
  both interpreters so results and error messages match exactly.
* `src/ella/interpreter.py`: tree-walking interpreter over the AST (stage 2),
  the reference for the language's behavior.
* `src/ella/ir.py`: the IR (typed CFG: functions, blocks with parameters,
  three-address instructions, terminators), its printer and its verifier.
  The module docstring specifies every operation.
* `src/ella/lower.py`: checked AST to IR (`compile_program`). Variables are
  local slots; lambdas are closure converted.
* `src/ella/ir_interpreter.py`: runs IR.
* `src/ella/passes/`: optimization passes, one module each (`mem2reg`, `fold`,
  `simplify`, `dce`), each `pass(function, module) -> bool` (changed?).
  `optimize()` in `__init__.py` runs them, verifying the IR after every pass.
  Passes must keep behavior identical, including run-time errors: never fold
  or remove an operation that can fail.
* Tests: `test/test_lexer.py`, `test/test_parser.py`, `test/test_checker.py`,
  `test/test_interpreter.py`, `test/test_ir.py` (including differential tests
  of both interpreters and the optimized IR), `test/test_passes.py`,
  `test/test_cli.py`. `test/programs/` holds
  `NAME.ella` programs with `NAME.out` (expected output) and optionally
  `NAME.err` (expected error), run through both interpreters and the
  optimized IR by `test/test_programs.py`.

Next is stage 5 (x64 code generation); see README.md.
