# Ella memory model (design draft)

**Status:** design only. Nothing here is implemented yet. This is roadmap
stage 6 in the [README](../README.md). It records decisions made while
exploring the design, so an implementation can start from them without
repeating the discussion. Items marked **(provisional)** are syntax or
details that are expected to change. [Open questions](#11-open-questions)
lists what is still undecided.

## 1. Goals and priorities

* Memory safety **without a garbage collector**.
* When trade-offs conflict: **simplicity > speed > memory**. Spending extra
  memory to keep the design or implementation simpler is acceptable.
* Ella is a learning project. Prefer designs that make the ideas visible over
  designs that are clever but opaque.
* Targets: Linux x64, **64-bit only**. Large virtual address reservations are
  always available.
* **Single-threaded** for now. Threading comes later (see §10).

Non-goals: a tracing GC, reference counting as the main strategy, 32-bit or
WASM targets, a Rust-style borrow checker with lifetime parameters.

## 2. Summary

```text
semantics     everything is a value; copies are semantic, moves are an optimization
non-copyable  explicitly marked resource types, built in place, never copied
references    second-class only: parameter modes, never stored or returned
placement     stack if size is known and small; otherwise a scope arena,
              chosen by the compiler (MLKit-style inference, simplified)
returns       built directly in the caller's arena (like Ada's secondary stack)
long-lived    Pool[T] with generational handles, size-class slabs,
              sub-arenas freed as a unit
cleanup       finalization lists, generated only for types that need cleanup
generics      comptime functions that return types (Zig-style staging)
dispatch      enums (tagged unions) first; vtables later, if ever
safety net    run-time checks trap (the IR's `trap` terminator)
tooling       arena profiler, and compiler reports of where values were placed
```

## 3. Value semantics

This extends [grammar.md §4 "Values"](grammar.md#values), which already says
that binding, passing, returning and capturing all copy.

* **Every copy is a semantic deep copy.** No two names can observe the same
  storage. There is no aliasing that a program can see.
* **Moves are an optimization, not a language rule.** When the compiler can
  prove a value is not used again (a last use), it may move it instead of
  copying it. Because copying is the semantics, there is no "use after move"
  error for users to learn.
* **Read-only parameters are passed by hidden reference** when that is cheaper.
  The callee cannot change them, and the caller is suspended, so the program
  cannot observe the difference. (Ada does this: parameter modes state intent,
  and the compiler chooses the mechanism.)
* **In-place mutation is always safe for a `mut` binding**, because nothing
  else can alias its storage. This replaces the current IR rule that
  `set_index` makes a new list (see §9, phase 2).
* Large values that would overflow the stack are placed in the scope arena
  automatically (see §5). The threshold is a compile-time constant, starting
  at 4 KiB **(provisional)**.

### Non-copyable types

Some values have identity and cannot be meaningfully copied: file handles,
sockets, pool handles that own a slot, sub-arenas. These are **non-copyable**
types (like Ada's `limited` types).

* They must be marked explicitly. Syntax is an open question.
* They are **built in place**: a function returning one constructs it directly
  in the caller's storage, so it is never moved or copied.
* Passing one to a function uses a parameter mode (§4), never a copy.
* A struct containing a non-copyable field is itself non-copyable.

## 4. References

References are **second-class**:

* They exist only as **function parameters**, using parameter modes.
* They are **never stored** in a struct, list, enum, closure or variable, and
  **never returned**.
* Because of this, a reference always points into a frame or arena that is
  older than the callee using it. No lifetime annotations are needed.

Parameter modes **(provisional syntax)**:

| Mode | Meaning | Mechanism |
|---|---|---|
| (default) | read-only input | copy or hidden reference, chosen by the compiler |
| `mut` | read and write the caller's variable (like Ada `in out`, Swift `inout`) | always a reference |
| `out` | the callee must initialize it (maybe; see the open questions) | always a reference |

```ella
let push_twice = (mut xs: [i64], x: i64) -> () =>
    xs = xs + [x, x]

let mut nums = [1]
push_twice(nums, 5)       # nums is now [1, 5, 5]
```

Whether the call site also has to mark the argument (`push_twice(mut nums, 5)`)
is an open question.

### Exclusivity

A `mut` argument must not overlap any other argument of the same call. This
is checked **statically**. Calls the checker cannot prove safe are rejected,
for example `swap(mut a[i], mut a[j])`. The library provides helpers for
these cases (`swap_at(mut a, i, j)`). Run-time exclusivity checks are not
planned.

### Closures

A closure captures **by value** (as in the grammar today). A closure cannot
capture a `mut` parameter by reference, so closures can always be stored and
returned.

### Known limitation

Zero-copy views (a parser holding a slice of its input, an iterator holding
its collection) cannot be expressed with second-class references. For now,
copy, or use index pairs such as `(start, len)`. Swift's experience suggests
a limited storable reference ("this value depends on parameter X") may be
needed later. Leave room in the syntax for it, but don't design it yet.

## 5. Storage classes

| Storage | Holds | Freed | Safety mechanism |
|---|---|---|---|
| Stack | fixed-size, small locals | function return | references are second-class |
| Scope arena | dynamically sized values, large values, return values | scope exit (reset to a mark) | placement rules (§6), depth checks |
| `Pool[T]` | long-lived objects of one type | explicit `free(handle)` | generational handles |
| Size-class slab | long-lived variable-size data (strings, list buffers) | when the owning pool slot or sub-arena is freed | owned by exactly one value |
| Sub-arena | a group of long-lived values with one lifetime | explicitly, as a unit | per-arena generation, address range never reused |

### Scope arenas

* There is **one reserved virtual address range per call depth**. Depth N
  allocates in range N. Ranges are reserved lazily (for example 4 GiB each
  **(provisional)**), and pages are committed as the bump pointer grows.
* Entering a scope records the bump pointer (a *mark*). Leaving it resets the
  pointer to the mark. Pages above a high-water threshold may be returned to
  the OS with `madvise(MADV_DONTNEED)`, done lazily.
* **Returns go straight into the caller's arena.** The caller (depth N−1) is
  suspended while the callee (depth N) runs, so the callee can bump-allocate
  its result directly into range N−1. No copy is needed on return. This is
  the same idea as GNAT Ada's secondary stack.
* **Growable values** (a list that is appended to) can only grow in place if
  they are the last allocation in their arena. Give each growable value that
  outlives a statement its own small reserved range. Address space is cheap,
  and this keeps growth from ever moving data.
* **Loop bodies are scopes.** Each iteration resets its own mark, so
  per-iteration temporaries do not pile up.

### Pools

* `Pool[T]` holds fixed-size slots for one type `T`, created per type at
  compile time (§8).
* A `Handle[T]` is `(slot index, generation)`. Every access checks the
  generation. A stale handle **traps**; it never reads a reused slot. The
  check is always on.
* A handle that owns its slot is non-copyable. A shared, non-owning handle
  type is an open question.
* Freed slots go on a free list. Shrinking pools (returning empty pages to
  the OS) is not planned at first.

**(provisional)** API: `pool.alloc(value) -> Handle[T]`, `pool[h]`,
`pool.free(h)`.

### Size-class slabs

Long-lived variable-size data (strings and list buffers owned by pooled
objects, map keys) goes in power-of-two size classes from 16 B to 4 KiB.
Anything larger gets its own mapped range. Up to 2× wasted space is
accepted, in exchange for simplicity. This is needed early: real programs
have long-lived strings almost immediately.

### Sub-arenas

A sub-arena is an arena with its own reserved range and its own generation,
used for a group of values that die together (a level, a request, a
document). This is the same idea as Ada 2012 storage subpools.

* Freeing a sub-arena decommits its pages and bumps its generation.
* **Address ranges are never reused** (or not until much later). Any stale
  raw pointer into a freed sub-arena then faults instead of reading reused
  memory. With 128 TiB of user address space this is affordable.
* Handles into a sub-arena are `(arena id, arena generation, offset)`, and
  there is one generation check per arena, not one per object.

## 6. Placement inference

Users do not name arenas. The compiler decides where each allocation goes,
using a simplified version of MLKit's region inference:

1. If the value's size is known at compile time and it is under the stack
   threshold, and it does not escape the function: **stack**.
2. Otherwise it goes into the arena of the **outermost scope it flows to**:
   * a returned value goes to the caller's arena,
   * a value stored into a variable declared in an outer scope goes to that
     scope's depth,
   * anything else stays in the current scope's arena.
3. Values explicitly placed in a pool or sub-arena go there.

Each function has one implicit **result region** parameter. The caller
chooses where the callee's result goes: its own arena, a pool slot, a
sub-arena, or a container. The callee never needs to know which one.

### Region bloat

Region bloat is the known failure mode of this approach. Example: an
accumulator declared outside a loop and rebuilt on every iteration puts a new
version in the outer arena each time, and the old versions are not freed
until the outer scope exits. Mitigations, in order:

1. **Prefer in-place mutation.** Value semantics makes this safe (§3), so
   `acc = acc + [x]` on a `mut acc` should append in place.
2. **Reset when the old contents are dead** (MLKit's storage-mode analysis):
   if a value is entirely replaced and the old version is dead, reuse its
   space.
3. **Report it.** The compiler warns when a loop allocates into an outer
   arena, for example "allocation in loop goes to outer arena (depth 1)".
4. Move long-lived, frequently changing state into a pool or sub-arena.

## 7. Cleanup

Resetting an arena does not run destructors, so resource-owning values in
arenas need explicit help.

* At compile time, `needs_cleanup(T)` is true if `T` contains any value that
  needs finalization (a non-copyable resource, an owning handle, a
  sub-arena).
* A scope arena that holds such values keeps a **finalization list**. On
  reset, it runs the finalizers in reverse allocation order and then resets
  the bump pointer. This is like Ada's finalization masters.
* Arenas holding only plain data have no list and cost nothing extra.
* Optional hooks, modelled on Ada controlled types: `finalize` (destructor)
  and possibly `adjust` (runs after a copy). Whether `adjust` is needed is an
  open question.

## 8. Staging and generics

`comptime` is already a reserved keyword ([grammar.md
§1](grammar.md#keywords)). The memory model depends on staging, so the two
should be designed together.

* **Generics are comptime functions that return types** **(provisional
  syntax)**:

  ```ella
  let List = (comptime T: type) -> type =>
      struct
          items: Buffer[T]
          len: i64
  ```

* Every generic is fully specialized (monomorphized), so every type's size is
  known at compile time. Value semantics, the stack threshold and `Pool[T]`
  all need this.
* Compile-time reflection over fields can generate `deep_copy`, `promote`,
  `==`, hashing, printing and `needs_cleanup` without a macro system.
* Comptime parameters are **marked explicitly**, never inferred, to avoid
  confusing "not known at compile time" errors.
* Errors in generic bodies appear at instantiation (as with C++ templates and
  Zig). Comptime assertions such as `comptime assert has_field(T, "x")`
  partly make up for this.
* The comptime evaluator can be built on the existing interpreters: they
  already run Ella. Compile-time code runs before lowering, and only residual
  run-time code reaches the IR.

### Dynamic dispatch

Use **enums** (tagged unions) first. Ella already has them ([grammar.md
§9](grammar.md#enums-provisional-syntax)). An enum's size is that of its
largest variant, so it is fixed-size, fits in pools and needs no boxing. The
wasted space is accepted. Comptime-generated vtables can come later if they
are needed.

## 9. Implementation plan

Build the semantics in the IR interpreter first (in Python), and generate x64
code only after the rules are settled and tested.

1. **Make copies explicit in the IR.** Add a `copy` operation, and have the
   lowering insert it at every semantic copy point (binding, argument,
   return, capture). The program's behavior does not change.
2. **In-place mutation.** Change `set_index` (and list append) to mutate in
   place. This is correct because step 1 made every copy explicit. Then add
   a pass that removes copies at last uses (copy → move). The test programs
   in [test/programs/](../test/programs/) must still give the same output.
3. **Parameter modes.** Add `mut` parameters to the grammar, checker,
   lowering and IR (a reference-typed parameter), with the static
   exclusivity check.
4. **Simulated arenas in the IR interpreter.** Model scope arenas as Python
   objects tagged with a depth. Have the interpreter implement the placement
   rules (§6), check them at run time (a reference or stored value must never
   point to a deeper depth than its location), and `trap` on violation.
   Report high-water marks per scope and per allocation site; this is the
   first version of the arena profiler.
5. **Placement inference in the compiler.** Annotate IR allocations with
   their chosen depth or result region. The interpreter's run-time checks
   from step 4 then verify the compiler's choices.
6. **Comptime and generics** (§8), including `needs_cleanup` and generated
   copy functions.
7. **Pools, handles and slabs**, simulated in the interpreter with
   generation checks.
8. **Non-copyable types and finalization lists** (§3, §7).
9. **Native runtime** for x64: real virtual-range reservations with
   `mmap`/`madvise`, bump allocation, pools, slabs. Compare results against
   the IR interpreter on the same test programs.

Each step should keep every test program producing the same output through
both interpreters, as the README requires today.

## 10. Later: threads

Out of scope for now. The intended direction:

* Arenas, pools and slabs are **per thread**, so bump allocation needs no
  atomics.
* Data moves between threads by **transferring a whole sub-arena** (or a
  non-copyable value), never by sharing.
* Async code and generators conflict with second-class references (a
  suspended coroutine holding a reference is a stored reference). Expect to
  revisit §4 then.

## 11. Open questions

* Syntax for non-copyable types, parameter modes, and whether call sites mark
  `mut` arguments.
* Whether `out` parameters are worth having, or whether returning tuples is
  enough.
* A shared, non-owning handle type for pools, and what copying an owning
  handle means (forbidden, or a deep copy of the target?).
* Whether types need an `adjust` hook after copies.
* What exactly a "scope" is for placement: a function, any block, or a loop
  iteration. Blocks and loop iterations are currently assumed.
* How to report placement decisions: compiler warnings, an `--placement`
  flag next to `--ir`, or both.
* Whether stored, dependent references (Swift-style lifetime dependence) are
  ever needed, once real programs are written.
* FFI: pinning memory that C holds a pointer to, and wrapping C libraries
  that keep pointers.

## 12. Known drawbacks (accepted for now)

* Zero-copy views and iterators are awkward (§4).
* Copy costs are not visible in the source. Mitigated by the move
  optimization, and later by placement and copy reports.
* Long-running programs (event loops, servers) keep much of their state in
  pools and sub-arenas that need **explicit** freeing. Generation checks catch
  stale handles, but nothing catches leaks.
* Region bloat (§6).
* Monomorphization increases code size and compile time.
* Exclusivity rejects some valid code (§4).

## 13. Prior art

* **Ada / GNAT**: parameter modes, secondary stack, `limited` types and
  build-in-place returns, controlled types, finalization masters, storage
  subpools (Ada 2012), accessibility levels. Lesson: keep the depth rule to
  one sentence; Ada's special cases made it notoriously confusing.
* **SPARK**: ownership and borrowing rules for access types.
* **MLKit** (Tofte–Talpin regions): region inference, region polymorphism,
  storage-mode analysis, the region profiler. Lesson: pure region inference
  leaked on some programs, and MLKit eventually added a GC.
* **Hylo** (formerly Val): mutable value semantics, second-class references,
  projections.
* **Swift**: `inout`, the law of exclusivity, and the later addition of
  non-escapable types and lifetime dependence.
* **Zig**: `comptime` generics, explicit allocators.
* **Cyclone**: safe regions. Lesson: an annotation burden hurts adoption.
* **Vale**: generational references.
