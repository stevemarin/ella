# Ella memory model: a developer summary

This document describes the memory model as implemented in the compiler and
native runtime. The fuller design and implementation history is in
[`docs/memory-model.md`](docs/memory-model.md). The most relevant code is
[`src/ella/lower.py`](src/ella/lower.py), [`src/ella/ir.py`](src/ella/ir.py),
[`src/ella/ir_interpreter.py`](src/ella/ir_interpreter.py),
[`src/ella/codegen.py`](src/ella/codegen.py), and
[`src/ella/runtime.c`](src/ella/runtime.c).

## The contract

Ella has value semantics without a tracing garbage collector. A value can be
assigned, passed, or returned as though it were independent of its source.
Mutating one list, array, or mutable struct must not change another value that
was copied from it. Strings and immutable aggregates may share storage when
that sharing cannot affect observable behavior.

The compiler is responsible for making this contract true. It analyzes where
values come from and how long they must live, then emits explicit IR operations
such as `copy`, `move`, and `carry`. Storage is reclaimed in bulk when its
arena ends. Pool handles and arena references are the explicit mechanisms for
keeping or addressing values managed outside ordinary lexical lifetimes.

## Values and object representation

The IR represents a value as an SSA-like ID or a mutable local slot. At
runtime, primitive values are unboxed machine words; strings, lists, arrays,
tuples, ranges, enum values, closures, pools, arenas, and generators are
boxed objects. `i64`, `i32`, `f64`, `f32`, `bool`, unit, handles, and refs are
unboxed in the native representation.

Native boxed objects are allocated in arenas. An object does not move when an
arena is reset. The current runtime reserves a large virtual address range and
bumps an arena's `top` pointer for each allocation; physical pages are made
available on demand. Native code does not currently place language objects on
the machine stack. Machine stack slots hold IR values, locals, and call state.

Copies are deep for the copyable aggregate types. Copying a list recursively
copies boxed elements; copying a tuple, enum, or closure recursively copies
its boxed contents. Strings and arrays get their own storage. This is
conservative: nested objects are copied even where lifetime analysis could
prove sharing safe. Unboxed values, handles, and refs are copied as words.
Pools and arenas themselves cannot be copied.

`move` is a lifetime transfer for non-copyable values. It relocates the
container/header into the destination arena and transfers finalizer ownership;
the values held in a pool's slots, an arena's slot arena, or a generator's
frame remain in their separately managed storage. A move must consume the old
value so that it is finalized exactly once.

## Placement analysis in lowering

`Lowerer` associates boxed intermediate values with `Info(borrows, lives)`:

* `borrows` records variable slots (or globals) whose objects may be shared by
  this value. Reading a variable, taking an item, or making a view propagates
  this relationship. A freshly built object starts without borrowed sources,
  then inherits the borrows of boxed values placed inside it.
* `lives` is the shortest-lived region among the objects the value may
  contain. A value can only be stored in a region that does not outlive this
  region unless it is copied or moved there.

The analysis is deliberately conservative and slot/name based. `unstable`
tracks variables that may be changed in place. `needs_copy` asks whether the
type can contain mutable data, or borrows from a pool/arena whose contents may
be freed. `place` copies when sharing could be observed or the source does not
outlive the destination. `protect` copies an earlier read if later evaluation
in the same expression could mutate its source. For example, a read used
after a mutation must retain the value from the time it was read.

Returns use destination passing: an IR function receives the result region
from its caller, so objects can be built directly where the caller needs
them. Returning a local that borrows only from locals that are dying can
transfer it without preserving visible sharing. Borrowed data that outlives
the callee must either already live long enough or be copied. The `moves`
optimization pass can eliminate eligible copies when the old owner has no
later use.

`mut` parameters have value-result semantics at the language level. Lowering
passes the relevant place and region through the IR, then writes the final
value back to the caller's place. This preserves the value semantics and
copying rules while allowing in-place updates when aliasing cannot be
observed.

## Regions and reclamation

IR object-producing instructions name their destination region with `@`:

| Region | Meaning |
| --- | --- |
| `@0`, `@1`, … | Function scope and nested loop iteration levels. |
| `@carryN` | Values carried by a loop between iterations. |
| `@result` | The caller-provided result destination. |
| `@mutN` | Storage associated with a mutable argument. |
| `@global` | Program-lifetime storage. |
| `@slot` | The slot arena of a pool value or arena value being updated. |

Each native call reserves enough consecutive scope depths for its body and
nested loops. Returning resets those depths, running finalizers before
reclaiming their storage. A loop resets its iteration levels on each
iteration. Depth ordering makes nested scopes free from deepest to shallowest.
The IR interpreter simulates this scheme with arena tags and generations. It
checks that a value is not used after its arena is reset and that it is not
stored somewhere that would outlive its arena; violations report a
`memory check failed` error. Native tests can set `ELLA_POISON` to overwrite
freed bytes and expose stale references.

The lowering recognizes some loops that repeatedly replace values declared
outside them and gives each such loop two arenas that alternate roles. The
loop carries at most the current and previous iteration's storage; `carry`
copies a still-needed value out of the arena about to be freed, and `swap`
recycles the other arena. Not every loop shape is recognized, so some
patterns can still accumulate allocations until an enclosing arena ends.

Eligible direct self tail calls in native code reuse the current function
frame and reset its scope arenas before continuing with the new scalar
arguments. This is a stack/frame optimization, separate from arena
placement; it does not change the ownership rules. The IR interpreter has a
corresponding direct-self-call loop.

## Long-lived storage: pools and arenas

These facilities let programs retain data beyond ordinary scope lifetimes.
They use slot arenas rather than the ordered scope depths, because slots may
be freed in any order.

### Pools and handles

A `Pool[T]` owns independently allocated values. Each live slot has a slot
arena. `alloc` moves a non-copyable value into its slot or places a copy of a
copyable boxed value there. `pool_set` copies a replacement into a fresh slot
arena before freeing the previous one, so the replacement may safely derive
from the old value. `free` finalizes and reclaims the slot's value. A handle
encodes its pool id, slot and generation; wrong-pool use and stale handles
raise errors. Slots are retired before generation wrap. When a pool dies, its
live slots are freed in reverse slot order.

### Arenas and refs

An Ella `Arena` owns one slot arena for all values allocated into it. `alloc`
moves non-copyable values or copies copyable boxed values into that storage
and returns a generation-checked `Ref[T]`. `free(mut arena)` clears the
storage as a unit and invalidates its refs while keeping the slot arena for
reuse. Setting a ref copies the new value; the old allocation remains until
the whole arena is cleared. When an `Arena` value itself dies, its slot arena
is returned for reuse.

The native implementation currently reserves 16 MiB of virtual address
space per slot arena, with a fixed maximum number of slot arenas. These are
reservation/limit details, not a promise that all that memory is resident.
They are implementation constraints contributors should keep in view when
changing runtime allocation behavior.

## Finalizers and non-copyable values

A `nocopy` struct may define a `finalize` method. Its finalizer is registered
in the arena holding that value and runs when the arena is reclaimed. Pool,
arena, and generator objects also register runtime cleanup actions. Finalizers
run newest-first within an arena, before its bytes are reused; nested arenas
are reclaimed deepest-first. Moving a nocopy value transfers its finalizer
registration rather than running and recreating it.

This ordering is part of the runtime contract. Ordinary Ella runtime errors
attempt to unwind active scopes and globals, running their finalizers. An
explicit `exit` and out-of-memory termination do not unwind. A failure inside
a finalizer during native error unwinding can stop the remaining cleanup, so
finalizers should not be treated as process-exit handlers.

## Views and slices

Compiled list and string slices are usually small headers pointing into the
source object's item/character storage. `trim` and the strings returned by
`split` also use views in compiled code. A view shares its source's borrows and
cannot outlive its backing storage. Lowering copies it if its source could be
mutated while the view is observable, if it must outlive the source, or if it
is stored into a pool/arena/generator context that requires an owned value.
Appending to a list view allocates its own list storage. Array slices are
materialized copies, not these list/string views.

The AST interpreter copies slices. The IR interpreter also uses copied host
values, but tags them with the backing arena and checks that list views have
not observed a mutation. These checks make the interpreter useful for finding
placement bugs even though its object representation differs from native.

## Generators

A generator's frame and its scope-level storage must survive between calls to
`resume`. Native code keeps the frame and one slot arena per scope level in
the `Gen` object. `yield` suspends with the next block recorded; resuming
continues there. When the generator finishes or dies, its levels are reset
and their finalizers run. The IR interpreter models the same lifetime with
persistent generator state and owned slot arenas.

## Invariants for compiler and runtime changes

When changing lowering, IR, or runtime allocation, preserve these invariants:

1. No mutable object may be observably shared across independent values.
2. Every object a value can reach must remain alive for every use of that
   value; stores and returns cannot extend a borrow accidentally.
3. Every non-copyable value is moved at most once, and its finalizer is
   registered in exactly the arena that owns it.
4. Reset order is deepest-first; finalizers run before arena bytes are
   reclaimed or overwritten.
5. A reused pool slot or arena storage invalidates old handles/refs through
   generations.
6. Region annotations on IR instructions and the assumptions in
   `verify()`/the IR interpreter must agree with native code generation.

For a memory bug, inspect `--ir` and `--placement`, then compare the
interpreter's memory-check failure with the native behavior under
`ELLA_POISON=1`. `--profile` and `ELLA_ARENA_STATS=1` help identify allocation
volume and arena high-water marks.

## Current limitations and contributor watch-outs

* There is no garbage collector or general leak detector. Long-lived pools
  and arenas need explicit freeing.
* Some loop-carried patterns are not recognized and may retain per-iteration
  allocations until an outer scope ends.
* Deep copies can do more work than necessary; nested values are copied
  conservatively.
* Borrow and mutation tracking is based substantially on variable names,
  which can reject safe programs or copy more than required.
* Kept views are restricted by their source lifetime. A slice retained in a
  long-lived value is copied; iterator/generator view behavior is also
  constrained by ownership and lifetime requirements.
* Slot arenas have fixed runtime limits. Repeated `Arena.set` operations
  retain replaced allocations until `free`.
* Native code generation targets x64 on Linux and uses runtime layout and
  calling-convention details that must remain synchronized with `runtime.c`.
* The memory-model design document contains historical plans as well as
  implemented behavior. In particular, its original stack-placement plan is
  not implemented: current native language objects are arena allocated.
