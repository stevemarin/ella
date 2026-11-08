# Ella TODO

What's being worked on, what's next, and the known rough edges. The
detailed notes (implementation plans, design reasoning, and a log of
finished work) are in [TODO-Claude.md](TODO-Claude.md).

*Last reviewed 2026-11-08.*

## In progress

Nothing right now. The last pieces finished:

- Default methods in constraints: a constraint's method can have a body,
  which every type implementing it gets unless it defines its own, so
  types share behavior without inheritance.
- Fields in constraints: a constraint can require fields (`x: f64`),
  which each implementing struct has and its defaults can use.

- A built-in `Zero` constraint: generic code reads `T.ZERO` (`0`, `""`,
  `[]`, or a type's own `const ZERO`), so `sum(xs)` needs no start value
  and takes any iterable.
- Built-in constraints `Sub`, `Mul` and `Neg`, so generic arithmetic
  works on structs with those methods, and `One` (`T.ONE`, for
  `product(xs)`) and `Bounded` (`T.MIN`, `T.MAX`).

- Lambda parameters that are patterns (`((a, b)) => a + b`), also usable
  in `where` conditions.
- `Iter[T]` parameters, which take anything a `for` loop can go through.
- `map`, `filter`, `zip` and `enumerate` stay eager (giving lists) but
  take any iterable. `iters` is in the prelude for its other lazy
  functions (`take`, `count`, `collect`, ...); its lazy `map` and the like
  are `iters.map` and so on.
- Giving away a generator (or other non-copyable value) held in a
  variable at its last use: `let g = xs.map(f)` then `g.collect()`.

## Next up

Small steps that build on what's there:

- [ ] Let a top-level function take its parameter types from a declared
      type: `let inc: (i64) -> i64 = (x) => x + 1`.
- [ ] Warnings for unused methods, enum variants and top-level variables.
- [ ] More list functions taking `Iter[T]` (`any`, `all`, `fold`,
      ...), and a lazy `iters.flat_map`.

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
- A generator, or an `Iter[T]` parameter, takes a non-copyable argument
  (like another `Gen`) only as a new value or a variable at its last use,
  not inside a loop the variable was declared outside of. A lazy result
  can't be used twice (collect it first), and a generator can't yield
  non-copyable values.
- `Iter[T]` is only for parameters of `let`-bound functions (not methods
  or results).

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
12. **Iterators as values**: `Iter[T]` as a constraint usable anywhere
    (type parameters, struct fields), so generic code can hold "something
    giving `T`s", and lazy `iters.map` and the like as small copyable
    structs instead of generators, so a lazy result can be named, copied
    and used twice.
13. **A `Parse` protocol**, the inverse of `text`: `parse[T: Parse](s)`
    giving a `Result[T, Error]` for numbers, `bool`, `str` and types that
    implement it. Needs constraints that can require functions called on
    the type (`T.parse(s)`), not just methods and constants.

Other explorations are written up in [docs/ideas/](docs/ideas/):
compile-time value parameters, a JIT, and SQLite integration.
