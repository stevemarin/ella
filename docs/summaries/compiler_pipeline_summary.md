# Compiler pipeline and IR

This is a developer guide to how an Ella source file reaches the tree
interpreter, IR interpreter, or native executable. Main sources are
[`src/ella/parser.py`](../../src/ella/parser.py),
[`src/ella/checker.py`](../../src/ella/checker.py),
[`src/ella/lower.py`](../../src/ella/lower.py),
[`src/ella/ir.py`](../../src/ella/ir.py),
[`src/ella/passes/`](../../src/ella/passes/),
[`src/ella/ir_interpreter.py`](../../src/ella/ir_interpreter.py),
[`src/ella/codegen.py`](../../src/ella/codegen.py), and
[`src/ella/native.py`](../../src/ella/native.py).

## Pipeline

At a high level, normal execution follows:

```text
source → tokenize/parse → link modules → check and expand stages → typed IR
                                                        ├→ IR interpreter
                                                        └→ optimize → x64 assembly + C runtime
```

The AST interpreter is a separate reference execution path and is also used
for compile-time evaluation. `compile_program` in `lower.py` orchestrates
preparation, lowering, and IR verification. The CLI selects an execution or
inspection path; native compilation adds optimization, assembly generation,
and linking with `runtime.c`.

## Typed control-flow IR

The IR is a control-flow graph. A function has basic blocks, each containing
three-address instructions and one terminator. Terminators transfer control
with `jump`, `branch`, `return`, `yield`, or `trap`. Values have unique IDs,
fixed types, and SSA-style dominance. Block parameters carry values across
edges and represent control-flow merges instead of explicit phi nodes.

Mutable source bindings initially lower to named local slots accessed by
`load` and `store`. This keeps lowering direct and understandable. The
`mem2reg` pass promotes eligible slots to SSA values later. Closures become
ordinary IR functions with captured values added as parameters; known function
calls may later become direct calls.

IR instructions make important effects explicit: calls, global access,
allocation regions, copies and moves, pool/arena operations, and generator
creation/resumption. `verify()` checks block structure, types, dominance,
regions, and operation-specific invariants. Compilation and optimization
verify the module at key boundaries so errors are localized to the stage
that introduced them.

## Optimization

The default optimizer runs `mem2reg` and direct-call conversion, iterates
copy/move cleanup, folding, CFG simplification, and dead-code elimination,
then attempts inlining and cleanup again, and finally fuses eligible array
operations. Each pass mutates IR in place and reports whether it changed
anything. The optimizer verifies after each pass. See
[`src/ella/passes/__init__.py`](../../src/ella/passes/__init__.py) for ordering and
individual pass modules for their rules.

Passes must preserve observable behavior, including runtime errors, source
locations, finalizer timing, and memory-region validity. A pass that removes
or reorders an instruction must account for effects; an optimization that
changes evaluation order can change which checked operation fails first.

## IR interpreter as a semantic and memory checker

The IR interpreter executes instructions using the same operator functions
as the AST interpreter and models native arenas with tags and generations.
It checks use-after-reset and stores that would outlive the referenced
arena. It also models loop-carried arenas and generator-owned arenas. Its
objects are Python values, not native structs; special bookkeeping
approximates native views and lifetime behavior. A `memory check failed`
diagnostic usually means lowering or an optimization violated a region
invariant, even if the native executable appears to work by accident.

Keep the three observable paths aligned: AST interpreter, IR interpreter,
and native runtime. Tests commonly compare stdout, stderr, exit status, and
runtime errors across them. Differences due to representation (for example,
the IR interpreter copying a native view) require explicit checking logic.

## Native backend

`codegen.py` maps IR values to machine registers or stack slots, emits x64
assembly, and calls into `runtime.c` for complex operations. The generated
program and runtime share an ABI for arguments, result destinations, mutable
arguments, call sites, scope depths, and object layouts. Changes to either
side must preserve that agreement. Native support currently targets Linux
x64 and uses GCC to assemble/link.

Register allocation assigns registers to SSA values; spills use stack slots.
Generator functions are a special case: their IR values and locals live in
the generator frame, addressed from its frame pointer register, so they
survive `yield`. Eligible direct self tail calls reuse the function frame
and reset its scope arenas; this is narrower than general tail-call
optimization.

## Debugging workflow

Use `--ir` to inspect lowering and optimized IR, `--asm` for generated
assembly, and `--passes` to isolate optimizer behavior. `--tree`, default IR
execution, and `--native` help localize backend differences. For memory
placement, inspect `--placement`; run native code with poisoning enabled in
tests to expose stale arena pointers. Keep source spans attached to emitted
instructions so errors and traces remain useful after optimization.

## Design constraints and gaps

* There is one main IR rather than separate high- and low-level IRs. Add a
  new representation only when a later phase needs a genuinely different
  abstraction.
* The AST interpreter is valuable as an independent implementation, but
  it is not the native memory representation. Do not infer native object
  layout from Python values.
* Optimization and code generation are intentionally explicit and
  inspectable. Avoid hiding semantics in a backend-only shortcut unless the
  interpreter and verifier can still validate it.
* Native support is platform-specific; a different target needs a backend,
  runtime ABI, and tests rather than only different assembly syntax.

