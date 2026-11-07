# Ella TODO

What's being worked on, what's next, and the known rough edges. The
detailed notes (implementation plans, design reasoning, and a log of
finished work) are in [TODO-Claude.md](TODO-Claude.md).

*Last reviewed 2026-11-06.*

## In progress

Nothing right now. The last piece finished was arrays typed by shape:
`Array[f64; N, 3]` is checked while compiling and, where that can't be
sure, when the program runs; size names are numbers (`for i in 0..N`);
and `sums(a, axis)` adds up along one dimension.

## Next up

Small steps that build on what's there:

- [ ] Let a top-level function take its parameter types from a declared
      type: `let inc: (i64) -> i64 = (x) => x + 1`.
- [ ] A way for generic code to make the zero of a number type, so
      `sum(xs)` doesn't need a start value.
- [ ] Warnings for unused methods, enum variants and top-level variables.

## Known gaps

### Language and types

- No `i8` or `i16`, no `f32` literals (write `to_f32(1.5)`), no
  negative indexing.
- Array shapes are only checked on arrays themselves: arrays inside
  lists, tuples or structs (`[Array[f64; 3]]`), and values assigned to a
  `mut` variable later, are trusted. Structs and enums can't have size
  parameters.
- Constraints can't require operators beyond the built-in ones, or
  associated types, and their constants can't be used in types
  (`Array[f64; T.SIZE]`). Built-in types can't implement user-defined
  constraints.
- Protocol method names (`get`, `set`, `iter`, `add`, ...) are reserved,
  so they can't be ordinary methods.
- `print` and `eprint` used as values take only one argument.
- The "case never matches" warning doesn't reason about ranges
  (`case 1` after `case 0..5`).
- Imports: one name per `import` line, no re-exports, no cycles.
- A generator can only take a non-copyable argument (like another `Gen`)
  as a brand-new value, not from a variable, and can't yield non-copyable
  values.

### Library

- `Map`'s `==` also compares insertion order (`Set`'s doesn't).
- `upper`, `lower`, `is_digit`, `is_alpha` and `is_space` only know ASCII.
- No string formatting beyond interpolation and `format_f64`.
- No hyperbolic math functions.
- A run-time error always exits with status 1.

### Memory

- Some loops still pile up memory: a variable that a loop both replaces
  and changes in place, globals replaced in loops, labeled `break` or
  `continue` out of an outer loop, and values rebuilt by recursion.
- Copies are always deep, and a struct, iterator or generator that holds
  a slice gets a copy rather than a view. Array slices are always copies.
- An explicit `Arena` holds at most 16 MiB and about a million values.

### Back end and performance

- Register allocation is basic (no live-range splitting, arguments go on
  the stack, `f64` values sit in general registers).
- No loop optimizations, common subexpression elimination or strength
  reduction.
- Only x64 Linux, through gcc. Native programs can't `run` code built at
  run time.

### Tooling

- The language server re-checks everything after each edit, and has no
  rename, find references, formatting or signature help.
- No formatter, REPL, or debugger support (no DWARF line info).

## Bigger ideas

Roughly in order of how much they'd unlock:

1. **Grow the standard library**, written in Ella, to find rough edges in
   the language.
2. **Packages**: importing several names at once, re-exports, a project
   manifest.
3. **A better back end**: arguments in registers, smarter spilling, loop
   optimizations.
4. **Better diagnostics**: source lines in stack traces, DWARF so `gdb`
   can step through native programs.
5. **Views without copying**, kept in structs and generators too.
6. **SIMD and narrow numbers**: `i8`, `i16`, vector types like `f32x4`,
   AVX-512.
7. **Native staging**: a JIT, so native programs can run code built at
   run time ([docs/ideas/jit.md](docs/ideas/jit.md)).
8. **Concurrency**: fibers built on generators, then threads with arenas
   per thread.
9. **Developer experience**: REPL, `ella fmt`, more language-server
   features.
10. **Self-hosting** (stretch): the tokenizer or parser rewritten in Ella.
11. **Symbols and hygienic macros** built on `quote` and `$`.

Other explorations are written up in [docs/ideas/](docs/ideas/):
compile-time value parameters, a JIT, and SQLite integration.
