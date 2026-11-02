# Ella

A Linux, x64 statically typed language for fun and learning, implemented in
pure Python, with a small C runtime for native executables.

Ella has a tokenizer, parser, type checker, tree-walking interpreter, typed
intermediate representation (IR), IR interpreter and optimizer, and an x64
native code generator.

## Goals

* Keep the implementation as simple as possible without hiding important ideas.
* Use a lightweight, extensible syntax and grammar.
* Learn how interpreters, IRs, optimization, and native code generation fit together.
* Explore memory safety without GC.
* Compile to x64 on Linux.
* Leverage modern Python in the design.
* All user-facing integer types are signed.
* Everything is an expression; only booleans are truthy.
* No implicit type conversions.
* Pass-by-value semantics; bindings are immutable unless marked `mut`.
* Future goals include built-in vector types and SIMD.

The language itself is described in [docs/grammar.md](docs/grammar.md), and
[example_syntax.ella](example_syntax.ella) is a tour of the syntax.

## Design

### Compiler pipeline

```text
source → tokenizer → parser → AST → type checker → IR ──→ IR interpreter
                                ↓                    └→ optimizer → IR → x64 assembly → native executable
                                AST interpreter                          (+ C runtime)
```

Start with one IR. Add another representation only when a later lowering needs
to make a real change in abstraction, such as replacing structured operations
with explicit memory and machine-level operations.

The IR is a typed control-flow graph (specified in
[src/ella/ir.py](src/ella/ir.py)):

* Functions contain basic blocks.
* Blocks contain simple, three-address instructions and end in a terminator
  (`branch`, `jump`, `return`, or `trap` for run-time errors).
* Values have explicit types and unique IDs, and never change.
* Control-flow joins use block parameters: `jump join(%4)` passes a value to
  `join(%5: i64)`. They do the job of SSA `phi` nodes.
* The lowering keeps variables in named, typed local slots (`load`/`store`).
  The mem2reg optimization pass then promotes them to values, giving full SSA
  form; that is easier as a pass than in the lowering.
* Lists and structs have value semantics, but their objects can be changed
  in place. The lowering inserts explicit copies where needed to preserve
  value semantics.
* Lambdas are closure converted: each becomes an IR function taking its
  captured variables as extra parameters.
* Side effects such as calls, printing and global writes are explicit operations.

The IR interpreter defines and exercises the meaning of these operations. The
IR verifier checks structural, dominance and type rules before interpretation,
optimization, or code generation. Optimization passes operate on the IR and
must preserve its behavior.

### Staged programming

Staging is separate from the number of compiler passes. Ella programs are
*multi-stage*: `quote e` is the code of `e` as a value, for a later stage,
and `$c` splices code into a quote, one stage earlier. Stages nest without
limit in both directions. A splice outside any quote runs while compiling,
and the code it gives becomes part of the program; code built at run time
runs with `run`. Compile-time code is evaluated by the AST interpreter, and
only the residual code it produces reaches the IR, so the boundary is
explicit in the language rather than tied to compiler passes. See
[docs/grammar.md](docs/grammar.md) §11 and
[src/ella/staging.py](src/ella/staging.py).

## Implementation history and remaining work

### 1. Language front end

Done: the tokenizer and Pratt parser handle the language documented in
[docs/grammar.md](docs/grammar.md). The syntax tour
[example_syntax.ella](example_syntax.ella) is tested to tokenize and parse.

### 2. Source interpreter

Done: the tree-walking interpreter is the reference for program behavior and
also evaluates compile-time splices.

### 3. IR and IR interpreter

Done: checked programs lower to a typed control-flow IR, which has a verifier
and printer and is run by the IR interpreter.

### 4. Optimization

Done: separate passes promote local slots to SSA values, fold constants,
simplify control flow, and remove dead code; the IR is verified after each
pass. Further optimizations remain future work.

### 5. Native code generation

Done: the x64 backend and C runtime build native executables. Register
allocation is future work; currently every IR value has a stack slot.

### 6. Memory model

Done: explicit copies, in-place mutation, `mut` parameters, scope arenas,
compiler-chosen placement, pools, non-copyable types, and finalization are
implemented. Sub-arenas are not implemented. See
[docs/memory-model.md](docs/memory-model.md) for the design and limitations.

### 7. Staged programming

Done: `quote`, `$`, compile-time splices, generated types, structs, and
generic functions are implemented. Runtime-built code works in the
interpreters; running it in native code (which would require a JIT) remains
future work.

## Current status

The syntax is specified in [docs/grammar.md](docs/grammar.md).

* Stage 1 is done: the tokenizer ([src/ella/lexer.py](src/ella/lexer.py)) and
  Pratt parser ([src/ella/parser.py](src/ella/parser.py)) turn source into the
  syntax tree in [src/ella/syntax.py](src/ella/syntax.py).
* Stage 2 is done: the tree-walking interpreter
  ([src/ella/interpreter.py](src/ella/interpreter.py), with values in
  [src/ella/values.py](src/ella/values.py)) runs programs.
  [test/programs/](test/programs/) holds programs with their expected
  output, for every interpreter and backend to share.
* Stage 3 is done: the type checker ([src/ella/checker.py](src/ella/checker.py))
  works out every expression's type, the lowering
  ([src/ella/lower.py](src/ella/lower.py)) turns the checked tree into IR
  ([src/ella/ir.py](src/ella/ir.py), which also has the printer and verifier),
  and the IR interpreter ([src/ella/ir_interpreter.py](src/ella/ir_interpreter.py))
  runs it. Every test program gives the same results through both interpreters.
* Stage 4 is done: optimization passes in [src/ella/passes/](src/ella/passes/)
  promote slots to SSA values (mem2reg), fold and propagate constants,
  simplify the control-flow graph, and remove dead code. The IR is verified
  after every pass, and every test program also runs through the optimized IR.
* Stage 5 is done: the code generator ([src/ella/codegen.py](src/ella/codegen.py))
  turns IR into x64 assembly, which gcc links with a small C runtime
  ([src/ella/runtime.c](src/ella/runtime.c)) into a native executable
  ([src/ella/native.py](src/ella/native.py)). Every value gets a stack slot,
  and Ella functions pass arguments on the stack. Every test program runs
  natively too, and must print the same output and errors byte for byte.
* Stage 6 is done: values are copied only where a program could tell, lists
  change in place, and functions can take `mut` parameters. Every object
  lives in an arena: one per function call and per loop iteration, freed
  when the call returns or the iteration ends, so there is no garbage
  collector. The compiler decides which arena each object goes in
  ([src/ella/lower.py](src/ella/lower.py)), the IR interpreter checks its
  decisions as programs run, and native code allocates in real arenas
  ([src/ella/runtime.c](src/ella/runtime.c)). See
  [docs/memory-model.md](docs/memory-model.md) §14.
* Stage 7 is done: programs are multi-stage. `quote` makes code, `$` splices
  code in, and splices outside quotes run while compiling
  ([src/ella/staging.py](src/ella/staging.py)), so a program can generate
  its own specialized code. Code made at run time runs with `run`, in the
  interpreters. Types are values too (`type Point`), so compile-time code
  can look at a struct's fields and generate code for it, such as an
  equality test, and functions can compute new types while compiling
  (`$(Stack(type i64))`). The language also has structs, generic functions, pools of
  long-lived values with handles that catch use after free, and `nocopy`
  structs with finalizers. Generic functions can constrain their type
  parameters (`let largest[T: Ord]`) to use operators on them. See [docs/memory-model.md](docs/memory-model.md)
  §15.

```sh
uv run ella example_syntax.ella           # check and run a program (through the IR)
uv run ella --tree example_syntax.ella    # check and run it with the AST interpreter
uv run ella -O example_syntax.ella        # optimize the IR, then run it
uv run ella --native example_syntax.ella  # compile to native code and run it (add -O to optimize)
uv run ella -o tour example_syntax.ella   # compile to a native executable named tour
uv run ella --asm example_syntax.ella     # print the x64 assembly
uv run ella --ir example_syntax.ella      # print the IR (add -O to optimize it,
                                          # or --passes mem2reg,fold to pick passes)
uv run ella --placement example_syntax.ella  # print which arena each object goes in
uv run ella --profile example_syntax.ella    # run it, then print how much memory each arena used
uv run ella --ast example_syntax.ella     # print the syntax tree
uv run ella --expanded example_syntax.ella  # print it after running the splices outside quotes
uv run ella --tokens example_syntax.ella  # print the tokens
```

Native programs need gcc, and can't run code built at run time.
