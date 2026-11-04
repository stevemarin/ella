# Generators and iterators

Generators make lazy sequences that suspend and resume a function body. They
are implemented through IR terminators and persistent frames rather than by
rewriting the body into a separate source-level state machine. User syntax is
documented in [`docs/grammar.md`](docs/grammar.md) §9; memory lifetime details
are in [`docs/memory-model.md`](docs/memory-model.md) §17.

## Language behavior

A function whose body contains `yield` has a `Gen[T]` result, where `T` is
the yielded item type. Calling it creates a generator without running its
body. `next(mut g)` resumes it and returns `Some(item)` for each yield, then
`None` forever after completion. `for x in g` repeatedly calls `next`; a
mutable generator is consumed through a mutable place. Generator values are
non-copyable. A generator cannot have `mut` parameters, and yielded values
cannot be nocopy.

The standard `iters` module builds lazy operators such as map, filter, take,
zip, and scan on top of generators. This keeps those algorithms in Ella and
makes their execution incremental. Import `iters` explicitly; it is not a
prelude module because of name overlap.

## IR representation

Lowering turns a generator into two functions. A constructor function emits
`gen_new`, storing the body entry point, captures, parameters, frame size,
and level count in a Gen. The body is an IR function marked as a generator.
Its `yield value, target(args)` terminator returns the yielded value and
records the continuation block; the next resume starts there. Completion
resets the generator's levels and repeatedly yields `None`.

The IR interpreter stores the frame and continuation in its generator value.
Native code stores all generator IR values and local slots in the Gen frame,
addressed from a dedicated register. Ordinary calls made by generator code
still use normal call frames; they finish before the generator yields.

## Lifetime and ownership

The generator's parameters, captures, locals, and in-progress expression
values must survive between calls to `next`. Therefore the Gen owns one slot
arena for each scope level, including loop levels that may be suspended
mid-iteration. A yielded value that could borrow from the persistent frame
must be copied to the caller's result region. The generator itself is
non-copyable and registered for cleanup in the region where it was created.

When a generator finishes, it frees its levels. If it is dropped while
suspended, its cleanup frees the levels deepest-first and runs finalizers
before returning slot arenas. The AST interpreter uses a separate thread per
generator to preserve suspension state; its cleanup finalizes the suspended
owner stack. Thus the interpreter implementation differs, while language
semantics should match.

## Contributor invariants

* Every yield edge must record a valid continuation and compatible block
  arguments. The verifier and both interpreters must agree on yielded and
  completion behavior.
* Frame layout in codegen must reserve all locals and SSA values that remain
  live across suspension. Do not rely on machine stack storage for these.
* Generator-owned arenas must outlive every resume and be reclaimed exactly
  once on completion or drop.
* Arguments and captures follow nocopy/move rules. A generator constructor
  cannot silently duplicate ownership.
* Errors during resume need the call site and source span from the resuming
  operation, so stack traces identify the `next` that executed the body.

The implementation touches `lower.py`, `ir.py`, `ir_interpreter.py`,
`codegen.py`, `runtime.c`, and the AST interpreter. Test creation, first
yield, multiple yields, empty/completed generators, early drop, nested loops,
captured values, finalizers, and errors in a resumed body across all
execution paths.

## Current restrictions

Generators cannot be copied, cannot use `mut` parameters, and cannot yield
nocopy values. A generator that takes a nocopy argument must receive a fresh
value it can move into its frame. Runtime-built generator code inherits the
native staging limitation: native executables do not have a run-time compiler.

