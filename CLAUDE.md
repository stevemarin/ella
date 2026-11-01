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

## Current stage: 1 (tokenizer and parser)

Suggested layout:

* `src/ella/lexer.py`: `Span`, `TokenKind`, `Token`, `tokenize(source)`.
  Handles comments, INDENT/DEDENT/NEWLINE layout, bracket depth (newlines
  ignored inside `()`/`[]`), labels (`'outer`), number formats, escapes, and
  string interpolation (`STRING_START`, `STRING_TEXT`, `INTERP_START` …
  tokens … `INTERP_END`, `STRING_END`).
* `src/ella/syntax.py`: AST node dataclasses.
* `src/ella/parser.py`: Pratt parser following docs/grammar.md §8 and the
  parsing notes in §11.
* Tests in `test/test_lexer.py` and `test/test_parser.py`. `test/programs/` is
  reserved for `.ella` programs with expected output, shared by every
  interpreter and backend later.

No type checker or interpreter yet; those are later stages.
