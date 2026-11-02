# Ella memory model

**Status:** mostly implemented, in roadmap stages 6 and 7 of the
[README](../README.md). Stage 6 built steps 1 to 5 and 9 of the
[plan](#9-implementation-plan): explicit copies, lists that change in place,
`mut` parameters, scope arenas whose placement the compiler chooses and the
IR interpreter checks, and real arenas in native code. Stage 7 built steps
6 to 8: generics, pools with generation-checked handles, non-copyable types
and finalizers, together with structs and multi-stage programming.
Sub-arenas are not built yet. [What stage 6 built](#14-what-stage-6-built)
and [what stage 7 built](#15-what-stage-7-built) describe the
implementation, and where it settles or departs from this design.

The rest of this document is the design, recorded so an implementation can
start from it without repeating the discussion. Items marked
**(provisional)** are syntax or details that are expected to change.
[Open questions](#11-open-questions) lists what is still undecided.

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
  else can alias its storage. (Before stage 6, the IR's `set_index` made a
  new list instead.)
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
| --- | --- | --- |
| (default) | read-only input | copy or hidden reference, chosen by the compiler |
| `mut` | read and write the caller's variable (like Ada `in out`, Swift `inout`) | a reference (defined as value-result; see §14) |
| `out` | the callee must initialize it (maybe; see the open questions) | always a reference |

```ella
let push_twice = (mut xs: [i64], x: i64) -> () =>
    xs = xs + [x, x]

let mut nums = [1]
push_twice(nums, 5)       # nums is now [1, 5, 5]
```

The call site marks the argument too: `push_twice(mut nums, 5)`.

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
| --- | --- | --- | --- |
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
   return, capture). The program's behavior does not change. *(Done in
   stage 6.)*
2. **In-place mutation.** Change `set_index` (and list append) to mutate in
   place. This is correct because step 1 made every copy explicit. Then add
   a pass that removes copies at last uses (copy → move). The test programs
   in [test/programs/](../test/programs/) must still give the same output.
   *(Done in stage 6.)*
3. **Parameter modes.** Add `mut` parameters to the grammar, checker,
   lowering and IR (a reference-typed parameter), with the static
   exclusivity check. *(Done in stage 6, with `mut` results instead of
   reference types; see §14.)*
4. **Simulated arenas in the IR interpreter.** Model scope arenas as Python
   objects tagged with a depth. Have the interpreter implement the placement
   rules (§6), check them at run time (a reference or stored value must never
   point to a deeper depth than its location), and `trap` on violation.
   Report high-water marks per scope and per allocation site; this is the
   first version of the arena profiler. *(Done in stage 6.)*
5. **Placement inference in the compiler.** Annotate IR allocations with
   their chosen depth or result region. The interpreter's run-time checks
   from step 4 then verify the compiler's choices. *(Done in stage 6.)*
6. **Comptime and generics** (§8), including `needs_cleanup` and generated
   copy functions. *(Done in stage 7, with generics as type parameters and
   staging as quotes and splices; see §15.)*
7. **Pools, handles and slabs**, simulated in the interpreter with
   generation checks. *(Done in stage 7, with an arena per pool slot instead
   of size-class slabs; see §15.)*
8. **Non-copyable types and finalization lists** (§3, §7). *(Done in stage 7.)*
9. **Native runtime** for x64: real virtual-range reservations with
   `mmap`/`madvise`, bump allocation, pools, slabs. Compare results against
   the IR interpreter on the same test programs. *(Done in stages 6 and 7.)*

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

* Settled: parameter modes are `mut name: T`, call sites mark `mut`
  arguments (stage 6), and non-copyable types are `nocopy struct` (stage 7).
* Whether `out` parameters are worth having, or whether returning tuples is
  enough.
* Owning, non-copyable handles. Stage 7's handles are plain copyable
  values that don't own their slots (a freed value is caught by its
  generation, not prevented); whether owning handles are worth adding is
  open, as is a handle knowing its pool.
* Whether types need an `adjust` hook after copies.
* What exactly a "scope" is for placement: a function, any block, or a loop
  iteration. Stage 6 uses function bodies and loop iterations only (§14);
  whether other blocks are worth their own arenas is still open.
* How to report placement decisions. Stage 6 has an `--placement` flag next
  to `--ir`, and no warnings; whether warnings are worth adding is open.
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

## 14. What stage 6 built

Stage 6 implemented the core of this design. The code is in
[src/ella/lower.py](../src/ella/lower.py) (copies and placement),
[src/ella/ir_interpreter.py](../src/ella/ir_interpreter.py) (simulated
arenas and checks), [src/ella/passes/moves.py](../src/ella/passes/moves.py)
(copy → move), and [src/ella/runtime.c](../src/ella/runtime.c) with
[src/ella/codegen.py](../src/ella/codegen.py) (native arenas). The IR
module docstring ([src/ella/ir.py](../src/ella/ir.py)) specifies the new
operations.

### Scopes and regions

* A **scope** is a function body (level 0) or a loop's iterations (one level
  deeper per nested loop). Other blocks, such as `if` branches, share their
  function's or loop's level. A loop frees its level after each iteration
  and when it ends (`reset k` in the IR); returning frees all of a
  function's levels.
* Every instruction that makes an object names its **region**: a level
  (`@0`, `@1`, ...), `@result` (the arena the caller chose for the result,
  passed as a hidden argument), `@mut0`, ... (the arena of the variable
  passed to a `mut` parameter, also passed by the caller), or `@global`.
  String constants are static, and i64, f64, bool and () aren't objects.
* There is **no stack allocation** of objects yet: every list, string, tuple,
  range, enum value and closure lives in an arena. Numbers and bools are
  never boxed.
* Arenas are numbered by **depth**, not by call: each call takes the next
  `levels` depths for its scope levels, so a function with a loop uses two.
  Depth 0 is the global arena.

### Placement

The lowering chooses regions as it goes, rather than in a separate
inference pass. Each expression is lowered with a *destination* region,
where its value will end up: the variable's region for `let`, the result
region for a returned value, the level outside a loop for a `break` value.
New objects are made there directly, which is the doc's rule 2 (§6): a
returned list is built in the caller's arena, and a string assigned to a
variable outside a loop is built in that variable's level. A local
variable that the function returns (as its last value or in a `return`)
lives in the result region from the start, so `let mut out = []; for ...:
out += [x]; out` builds `out` in the caller's arena with no copy.

`--placement` prints each allocating instruction and its region, marking
allocations inside a loop that go to an arena outside it (region bloat).

### Copies and sharing

Values are references to objects, and reading a variable doesn't copy. The
lowering tracks, for each value, which variables it may share objects with
(*borrows*) and a region all its objects outlive (*lives*), and inserts
`copy v @r` (a deep copy into region r) when storing a value would either:

* share a list that can change: the value has lists in it and borrows
  from a variable that is changed in place somewhere (an item assignment,
  `+=` on a list, or a `mut` argument), or it is stored into a `mut`
  variable, which could change it; or
* outlive its objects: it doesn't live as long as the place it is stored in.

A returned value is copied only if it borrows from something other than
the function's own local variables, which die anyway. A value read from a
variable is also copied if a later part of the same expression changes
that variable in place (`xs + [(xs[0] = 5)]`), so that a read always gives
the value at the time of reading, as in the AST interpreter.

`set_index` changes a list in place. `xs += ys` and `xs = xs + ys` become
`append`, which adds copies of ys's items to xs in place: in native code
the list grows where it is if it is the last object in its arena, and
otherwise moves to a new home with room for twice as many items.

The `moves` pass removes a copy whose original has no other use, in the
same block, when the original is made entirely of new objects; the
original is then made in the copy's region. General last-use analysis
(liveness) is not done yet.

### `mut` parameters

`mut` parameters are defined as **value-result** (copy in, copy back), so
that the AST interpreter can implement them simply, and implemented by
reference wherever no program can tell the difference: the caller passes
its variable's object, the function changes it in place, and returns it as
an extra result (`return %r, mut %xs`) for the caller to store back. In
native code the function writes it over its own argument on the stack. A
global is the one case a function could tell apart, by reading the global
directly, so a global is copied in. The exclusivity check (§4) works by
name: a variable passed as `mut` can't appear in another argument of the
same call unless that argument's type has no lists.

### Checks and the profiler

The IR interpreter tags each value with the depth and generation of the
shortest-lived arena it may have objects in, and checks every use (the
arena must not have been freed since) and every store into a variable,
list, global or result (the value must live at least as long as the
place). A failure is a compiler bug, reported as "memory check failed".
`--profile` runs a program through the IR and prints the high-water mark
of each scope level and the bytes made at each allocation site, estimated
from native object sizes.

### Native arenas

Each depth's arena reserves 4 GiB of address space with `mmap` the first
time it is used, and commits pages 1 MiB at a time as its top pointer
grows. Freeing an arena resets the pointer and returns memory beyond
16 MiB to the OS with `madvise`. Objects are aligned to 16 bytes. With
`ELLA_POISON` set, freed memory is overwritten, so a dangling reference
shows up as nonsense; the tests set it. With `ELLA_ARENA_STATS` set, a
program reports its peak arena use when it finishes.

### Known limitations

* Strings and other values rebuilt in a loop and kept outside it pile up in
  the outer arena until it is freed (§6, region bloat). Lists appended to
  in place don't.
* Copies are deep: copying a list of strings copies the strings too, even
  when they would live long enough.
* The exclusivity check, and the analysis of which variables change in
  place, go by name, so shadowed names make them more cautious than needed.

## 15. What stage 7 built

Stage 7 added the rest of the memory model (steps 6 to 8 of the plan),
with the language features it needed: structs, generic functions, and
multi-stage programming. The syntax is in [grammar.md](grammar.md) §9 and
§11.

### Generics, and stages instead of comptime

The design (§8) makes generics comptime functions that return types. Stage
7 separates the two ideas:

* **Generics** are type parameters, as enums already had: `struct Pair[T]`
  and `let first[T] = (xs: [T]) -> T => ...`. Every generic function is
  specialized for each set of type arguments it is used with
  (monomorphization), so every type's size is still known when compiling.
  `needs_cleanup(T)` is worked out while compiling (`is_nocopy` and the
  `finalize` of structs, in ella.checker), and copying uses type
  descriptors in the runtime rather than generated copy functions.
* **Staging** is multi-stage rather than two-stage: `quote` and `$` build
  code at any stage, in both directions, so compile time is just stage -1
  ([src/ella/staging.py](../src/ella/staging.py)). Splices outside quotes
  run in the AST interpreter, before lowering, and only the code they give
  reaches the IR, as §8 says. Code built at run time runs in the
  interpreters, which check and lower it when it is `run`; native code
  can't do that yet.

The two meet in **types as values** ([grammar.md
§11](grammar.md#types-as-values-and-generated-code)): `type T` is a
`Type[T]`, quotes can splice in a type (`$t`) or a field name (`v.$f`), and
built-ins such as `fields(t)` look at a type. That is the compile-time
reflection §8 asks for: functions like `derive_eq(type Point)` generate
equality, printing or copying code for any struct while compiling. Because
a `Type[T]` says which type it holds, generated code still has a known type
(`Code[(T, T) -> bool]`). Only a spliced field's type is unknown until the
code is built (a *deferred* type, which the checker lets through, and checks
when the built code is checked).

Functions can also **compute types**: `$(Stack(type i64))` in a type runs
while the program is being checked, and `new_struct` makes struct types,
named after what they are made from, so that asking twice gives the same
type (as Zig caches comptime calls). That needs the checker and
compile-time code to take turns: when the checker meets a splice in a
type, it checks the functions the splice will call (and expands their own
splices), runs it in the AST interpreter, and goes on with the type it
gives. Splices of code still run after checking. After expansion, the
structs that compile-time code made become ordinary definitions in the
program, so the IR and native code never see a computed type.

Still apart, and the next step if it is wanted: compile-time type
parameters, `(comptime T: type, x: T)`, with each use checked separately as
in Zig or C++ templates, so that a generic body can use `+` on `T` if every
use is with a type that has it. Constraints on type parameters would be the
other way to get that.

### Structs

A struct is an enum with one variant named after it. Its fields can change
in place (`set_field`), so structs count as mutable for the copying rules
of §14, like lists. Assignments to fields and items, and `mut` arguments,
can go through any mix of indexes and fields (`w.players[i].score += 1`),
and `+=` on a list anywhere appends in place.

### Pools: an arena per slot

The design gives pooled values size-class slabs for their strings and
lists (§5). Stage 7 instead gives each slot of a pool an arena of its own,
a *slot arena*. A value put in a pool is copied into a new slot arena, and
everything later stored into it (`pool[h].names += [s]`) goes there too.
Freeing the value frees its whole arena at once, the way a scope arena is
freed, so there is no need to free a value's parts one by one, or to tell
static data from pooled data. Replacing a value (`pool[h] = v`) copies the
new value into a new arena and frees the old one.

The cost is memory: changing a pooled value without replacing it leaves the
old parts in its arena until the value is freed or replaced (region bloat,
per value). A handle is one word: the slot and the generation. Freeing a
value bumps its slot's generation, so a stale handle is always caught, even
after the slot is reused. Natively, all arenas, scope and slot, are parts of
one reserved range of address space, so the arena an object is in can be
worked out from its address: 16384 scope arenas of 1 GiB, and about a
million slot arenas of 16 MiB. A pool is itself a value that can't be
copied, and when it dies, it frees its values.

### Non-copyable values and finalizers

`nocopy struct` (§3) marks a type that can't be copied. The checker
(`check_copies` in ella.checker) allows its values only where they are
borrowed or moved, never copied, so the compiler never has to copy one.
Its one special case is a function returning a local variable: the IR
`move` instruction moves the value to the caller's arena.

Finalizers (§7) are registered in the arena a value is made in, on a
finalization list. Freeing an arena runs its list newest first, before the
memory is reused, as Ada's finalization masters do. Moving a value (`move`,
`alloc`) moves its registrations too. A value given to `return` or `break`
is made in the arena it goes to, so it is finalized there. The AST
interpreter, which has no arenas, keeps an owner per function call and
loop iteration instead, and moves values between owners the same way; the
tests check that all the interpreters and native code finalize in the same
order. Nothing is finalized when a program stops with an error.

### Not built

* Sub-arenas (§5) for groups of long-lived values that die together.
* Owning, non-copyable handles, and an `adjust` hook (§11).
* Running code built at run time in native code.
* In the IR, a quote inside a function that is defined inside a splice
  can't refer to the variables of the quote the splice is in (the AST
  interpreter can). The checker doesn't catch this yet.
