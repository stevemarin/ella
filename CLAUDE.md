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

## Layout (stages 1 and 2 done)

* `src/ella/lexer.py`: `Span`, `EllaError`, `TokenKind`, `Token`,
  `tokenize(source)`. Handles comments, INDENT/DEDENT/NEWLINE layout, bracket
  depth (newlines ignored inside `()`/`[]`), number formats, escapes, and
  string interpolation (`STRING_START`, `STRING_TEXT`, `INTERP_START` …
  tokens … `INTERP_END`, `STRING_END`).
* `src/ella/syntax.py`: AST node dataclasses, and `show()` for S-expressions.
* `src/ella/parser.py`: Pratt parser following docs/grammar.md §8 and the
  parsing notes in §11.
* `src/ella/values.py`: run-time values (plain Python values where possible),
  scopes, copying, equality and text form. Shared by the interpreters.
* `src/ella/interpreter.py`: tree-walking interpreter over the AST (stage 2).
  It defines the language's behavior; later stages are checked against it.
  Types are checked at run time until there is a type checker.
* Tests: `test/test_lexer.py`, `test/test_parser.py`,
  `test/test_interpreter.py`, `test/test_cli.py`. `test/programs/` holds
  `NAME.ella` programs with `NAME.out` (expected output) and optionally
  `NAME.err` (expected error), run by `test/test_programs.py` and shared by
  every interpreter and backend.

Next is stage 3 (IR and IR interpreter); see README.md.
